"""Снимок камеры по запросу — для камер без нити аналитики (D04, SPEC §4).

До этого модуля `/api/cameras/{id}/snapshot` отдавал только
`cam{id}_latest.jpg`, а его пишет нить аналитики. У камер `record_only`
(на объекте — 47 из 49) файла не было никогда, и запасной путь живого
просмотра, обещанный README на случай, когда HLS не играет, у них
отсутствовал: плитка оставалась чёрной (DEPLOY_FINDINGS D04). Ровно там он и
нужнее всего — HLS не играет чаще всего на H.265, а H.265 на объекте у 91 из
98 потоков.

Кадр снимается с RTSP-пути MediaMTX, который и так тянет камеру: повторного
подключения к камере нет. ffmpeg декодирует H.265 на CPU и отдаёт JPEG —
его показывает любой браузер, в том числе тот, что не умеет H.265 в HLS.

Почему именно так (всё замерено на MediaMTX 1.16.1, 720p):

* `-skip_frame nokey` — декодируются только ключевые кадры. Без него первый
  кадр после подключения — P-кадр без опорного, и JPEG получался серым
  месивом (ffmpeg: «Could not find ref with POC …»). Наивный `-frames:v 1`
  выдавал бы оператору негодную картинку с видом исправной.
* Время до кадра — около двух длительностей GOP (1.3–4 с при GOP 2 с: один
  ключевой кадр уходит на анализ потока), CPU — 0.09–0.16 с на снимок,
  RSS ffmpeg ~92 МБ. Отсюда кэш на `PREVIEW_TTL_SEC` (плитка обновляет
  снимок раз в 10 с), один ffmpeg на камеру за раз и не больше
  `MAX_PARALLEL` одновременно на весь сервер.
* Неудача кэшируется на `FAILURE_TTL_SEC`: камера без потока иначе
  запускала бы ffmpeg с таймаутом на каждый запрос каждой открытой вкладки.
"""
from __future__ import annotations

import asyncio
import os
import time

from ..config import settings

PREVIEW_TTL_SEC = 8.0
FAILURE_TTL_SEC = 15.0
GRAB_TIMEOUT_SEC = 12.0
MAX_PARALLEL = 4
# Кадр аналитики свежее этого — отдаётся он, а не снимок с RTSP: нить
# аналитики пишет его на каждом обработанном кадре, и он не стоит ничего.
ANALYTICS_FRESH_SEC = 15.0

_locks: dict[int, asyncio.Lock] = {}
_failed_at: dict[int, float] = {}
_parallel: asyncio.Semaphore | None = None


def _semaphore() -> asyncio.Semaphore:
    # Создаётся лениво — в цикле событий, который его будет использовать.
    global _parallel
    if _parallel is None:
        _parallel = asyncio.Semaphore(MAX_PARALLEL)
    return _parallel


def snapshots_dir() -> str:
    return os.path.join(settings.MEDIA_PATH, "snapshots")


def analytics_frame(cam_id: int) -> str:
    return os.path.join(snapshots_dir(), f"cam{cam_id}_latest.jpg")


def preview_frame(cam_id: int) -> str:
    # Отдельный файл, а не cam{id}_latest.jpg: тот пишет воркер, и две
    # стороны, переписывающие один файл, гонялись бы друг с другом.
    return os.path.join(snapshots_dir(), f"cam{cam_id}_preview.jpg")


def rtsp_url(cam_id: int) -> str:
    return f"rtsp://{settings.MEDIAMTX_HOST}:{settings.MEDIAMTX_PORT}/cam{int(cam_id)}"


def _age(path: str, now: float) -> float | None:
    try:
        return now - os.path.getmtime(path)
    except OSError:
        return None


def grab_command(cam_id: int, out_path: str) -> list[str]:
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-rtsp_transport", "tcp",
        # Анализ потока короче умолчания (5 с): параметры кодека MediaMTX
        # отдаёт в SDP, долго слушать поток ради них незачем.
        "-analyzeduration", "500000", "-probesize", "500000",
        "-skip_frame", "nokey",
        "-i", rtsp_url(cam_id),
        "-frames:v", "1", "-q:v", "5", "-update", "1",
        out_path,
    ]


async def _grab(cam_id: int) -> bool:
    os.makedirs(snapshots_dir(), exist_ok=True)
    final = preview_frame(cam_id)
    tmp = final + ".tmp.jpg"
    try:
        proc = await asyncio.create_subprocess_exec(
            *grab_command(cam_id, tmp),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        await asyncio.wait_for(proc.wait(), timeout=GRAB_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    if proc.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    # Замена атомарная: читатель видит либо прежний кадр, либо новый целиком.
    os.replace(tmp, final)
    return True


async def snapshot_for(cam_id: int, now: float | None = None) -> str | None:
    """Путь к кадру для плитки либо None, если кадра нет и снять его не вышло.

    Порядок: свежий кадр аналитики → снимок с RTSP (из кэша или новый) →
    устаревший кадр аналитики, если он есть, — лучше старый кадр с подписью
    причины на плитке, чем пустая плитка.
    """
    now = time.time() if now is None else now
    latest = analytics_frame(cam_id)
    latest_age = _age(latest, now)
    if latest_age is not None and latest_age < ANALYTICS_FRESH_SEC:
        return latest

    preview = preview_frame(cam_id)
    age = _age(preview, now)
    if age is not None and age < PREVIEW_TTL_SEC:
        return preview

    failed = _failed_at.get(cam_id)
    if failed is None or now - failed >= FAILURE_TTL_SEC:
        lock = _locks.setdefault(cam_id, asyncio.Lock())
        async with lock:
            # Пока ждали замок, кадр мог снять соседний запрос.
            age = _age(preview, time.time())
            if age is None or age >= PREVIEW_TTL_SEC:
                async with _semaphore():
                    ok = await _grab(cam_id)
                if ok:
                    _failed_at.pop(cam_id, None)
                else:
                    _failed_at[cam_id] = time.time()
        if _age(preview, time.time()) is not None and cam_id not in _failed_at:
            return preview

    if latest_age is not None:
        return latest
    # Снять не вышло, но предыдущий снимок есть — он лучше пустоты.
    return preview if os.path.exists(preview) else None
