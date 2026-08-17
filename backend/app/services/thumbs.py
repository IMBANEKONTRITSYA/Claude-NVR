"""Миниатюры кадров сегментов архива (SPEC §7).

§7 требует «миниатюры кадров для быстрого просмотра» в выдаче поиска по
архиву. До этого выдача была таблицей из времени, номера камеры и
длительности: чтобы понять, тот ли это сегмент, оператор открывал каждый
по очереди в плеере — то есть качал 5–10 минут видео ради одного взгляда.

**Кадр берётся из самого сегмента, а не из снимков.** В `snapshots/`
лежат кадры событий аналитики, и они есть только у камер в режиме
`analytics` (§2) — у остальных ста с лишним камер выдача осталась бы без
миниатюр вовсе.

**Генерация ленивая и кэшируется файлом.** Сегментов на объекте — сотни
тысяч (§7 нормирует поиск на 500 000), и генерировать миниатюру всем при
записи означало бы держать лишний вызов ffmpeg на каждый закрытый файл в
слое записи, который §2 обязывает быть только remux'ом. Поэтому кадр
режется при первом запросе и остаётся на диске: повторный просмотр той же
выдачи ffmpeg уже не запускает.

**Один кадр — один ключевой кадр.** `-ss` до `-i` (input seek) и
`-frames:v 1`: декодируется ровно один GOP, а не сегмент целиком.
"""
from __future__ import annotations

import asyncio
import logging
import os
import weakref
from pathlib import Path

logger = logging.getLogger("facewatch.backend.thumbs")

# Каталог миниатюр внутри MEDIA_PATH. Отдельный от `snapshots/`: у них
# разное время жизни (снимок события переживает сегмент) и разные права
# доступа — миниатюра сегмента архива по матрице прав §18 доступна только
# admin/operator, снимок события виден и наблюдателю.
THUMB_DIR = "thumbs"

# Ширина миниатюры. 320 px хватает, чтобы отличить пустой коридор от
# человека в кадре, и даёт JPEG на 10–20 КБ: выдача из 200 строк — это
# ~3 МБ, а не 200 запросов по мегабайту.
THUMB_WIDTH = 320

# Качество JPEG в шкале ffmpeg (2 — лучшее, 31 — худшее). 5 — визуально
# без артефактов на таком размере.
THUMB_QUALITY = 5

# Смещение кадра от начала сегмента. Первый кадр записи часто снят в
# момент переключения ИК-подсветки или автоэкспозиции камеры и выходит
# засвеченным/чёрным; секунда спустя картинка уже установившаяся.
THUMB_OFFSET_SEC = 1.0

# Потолок одновременных вызовов ffmpeg. Ограничение здесь не про диск, а
# про §19 «CPU ≤ 80 %»: выдача архива — это до 200 строк, браузер тянет
# миниатюры пачкой, и без потолка один поиск порождал бы двести процессов
# ffmpeg. На целевом сервере (§20, 2× Xeon без GPU) это отняло бы ядра у
# слоя аналитики, который §2 обязывает не зависеть от чужой нагрузки.
THUMB_CONCURRENCY = 4

# Таймаут одного вызова. Вырезание одного кадра — это десятки миллисекунд;
# 20 с означает «ffmpeg завис на битом файле или отвалившемся томе».
FFMPEG_TIMEOUT_SEC = 20


class ThumbError(Exception):
    """Миниатюру получить не удалось — ответ будет 404, а не 500."""


# Семафор на event loop, а не один на модуль. `asyncio.Semaphore` при
# первом ожидании привязывается к текущему циклу и на другом падает с
# RuntimeError «is bound to a different event loop»: модуль импортируется
# один раз, а циклов за жизнь процесса бывает несколько (TestClient
# поднимает свой на каждый контекст, uvicorn — свой при перезапуске
# воркера). Слабые ссылки на ключи: закрытый цикл не должен удерживаться
# этим словарём.
_semaphores: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]" = (
    weakref.WeakKeyDictionary()
)


def _gen_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(THUMB_CONCURRENCY)
        _semaphores[loop] = sem
    return sem


def thumb_rel_path(seg_id: int) -> str:
    """Путь миниатюры относительно MEDIA_PATH.

    Разложено по подкаталогам на тысячу файлов: §7 нормирует архив на
    500 000 сегментов, и столько же записей в одном каталоге — это
    медленный `readdir` при любой ручной проверке и просто неудобно
    администратору, который пришёл смотреть, что занимает диск.

    Функция продублирована в `worker/thumbs.py` (у воркера нет
    зависимости от пакета `app`) — расхождение копий стережёт
    `backend/tests/test_thumbs_parity.py`. Расходятся такие копии молча:
    ротация начала бы удалять «не те» миниатюры, и заметно это стало бы
    только по растущему диску на объекте.
    """
    return os.path.join(THUMB_DIR, str(seg_id // 1000), f"{seg_id}.jpg")


def thumb_path(media_root: str, seg_id: int) -> str:
    """Абсолютный путь миниатюры сегмента."""
    return os.path.join(media_root, thumb_rel_path(seg_id))


def seek_offset(duration_sec: float | None) -> float:
    """Смещение кадра внутри сегмента, с.

    Ноль для коротких сегментов. `-ss` за пределами длительности — это не
    ошибка ffmpeg: он завершается кодом 0, просто не записав ни одного
    кадра. Без этой границы сегмент короче двух секунд (последний перед
    реконнектом RTSP или перед остановкой сервиса — таких на объекте
    столько же, сколько обрывов) давал бы пустой файл, который дальше
    пришлось бы отличать от валидного JPEG по размеру.
    """
    if not duration_sec or duration_sec <= THUMB_OFFSET_SEC * 2:
        return 0.0
    return THUMB_OFFSET_SEC


def ffmpeg_args(src: str, dst: str, offset: float) -> list[str]:
    """Аргументы вырезания одного кадра.

    Вынесено отдельной функцией, чтобы порядок `-ss` до `-i` проверялся
    тестом: перестановка их местами не меняет картинку и не роняет ничего,
    но заставляет ffmpeg декодировать сегмент от начала до точки реза —
    вместо десятков миллисекунд миниатюра пятиминутного сегмента считалась
    бы секунды, и заметно это было бы только на объекте.
    """
    return [
        "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{offset:.3f}",
        "-i", src,
        "-frames:v", "1",
        # -2 вместо -1: высота округляется до чётной, иначе JPEG-энкодер
        # ffmpeg отказывается кодировать кадр с нечётной высотой при
        # субдискретизации 4:2:0.
        "-vf", f"scale={THUMB_WIDTH}:-2",
        "-q:v", str(THUMB_QUALITY),
        "-f", "image2",
        dst,
    ]


async def _run_ffmpeg(args: list[str]) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ThumbError("ffmpeg не уложился в отведённое время") from None
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
        logger.warning("ffmpeg не смог вырезать кадр (код %s)", proc.returncode,
                       extra={"ffmpeg_stderr": tail[-3:] if tail else []})
        raise ThumbError("Не удалось вырезать кадр из сегмента")


async def generate(src: str, dst: str, offset: float) -> None:
    """Вырезать кадр из `src` в `dst` атомарно.

    Пишем во временный файл рядом и переименовываем. `os.replace` в
    пределах одного каталога атомарен, а прямая запись в конечный путь
    отдавала бы параллельному запросу **обрезанный** JPEG: браузер
    показал бы битую картинку, файл остался бы в кэше, и повторный запрос
    вернул бы ту же битую картинку уже без всякого ffmpeg.
    """
    dst_path = Path(dst)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    # PID в имени: два процесса uvicorn (§26 поднимает несколько воркеров)
    # пишут в один каталог, и общее имя .tmp они бы затирали друг у друга.
    tmp = dst_path.with_name(f".{dst_path.name}.{os.getpid()}.tmp")
    try:
        await _run_ffmpeg(ffmpeg_args(src, str(tmp), offset))
        # ffmpeg завершается кодом 0 и не создав файла — например когда
        # смещение вышло за длительность (см. seek_offset) или в сегменте
        # нет видеопотока. Пустой JPEG, попавший в кэш, не самоисправится
        # никогда: следующий запрос увидит существующий файл и отдаст те же
        # ноль байт.
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise ThumbError("ffmpeg не записал кадр")
        os.replace(tmp, dst_path)
    finally:
        # Временный файл переживает только неудачу; иначе каталог миниатюр
        # копил бы по .tmp на каждый битый сегмент.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


async def ensure(src: str, dst: str, duration_sec: float | None) -> str:
    """Вернуть путь к миниатюре, вырезав её при первом обращении.

    Проверка кэша идёт дважды — до семафора и под ним. Вторая нужна не для
    красоты: браузер запрашивает всю выдачу разом, семафор пропускает по
    четыре, и без повторной проверки остальные, дождавшись очереди,
    заново гоняли бы ffmpeg по уже готовому файлу.
    """
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        return dst
    async with _gen_semaphore():
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            return dst
        await generate(src, dst, seek_offset(duration_sec))
    return dst
