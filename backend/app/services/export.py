"""Экспорт фрагмента архива по границам времени (SPEC §5).

До этого архив умел отдавать только **файл сегмента целиком**
(`/api/archive/file/{id}`): оператору, которому нужны две минуты события,
приходилось скачивать сегмент на 5–10 минут, а событие на границе двух
сегментов — два файла, которые он потом склеивал вручную. ТЗ §5 требует
именно «экспорт фрагментов».

**Только remux.** §24 «Явно вне рамок» перечисляет «перекодирование архива
(только remux)» — поэтому весь модуль работает через `-c copy`. Это же
делает экспорт дешёвым: фрагмент на 10 минут копируется за доли секунды и
не занимает те 2–3 ядра, которые §23 отводит аналитике.

**Точность реза ограничена ключевыми кадрами.** Без перекодирования начать
файл с произвольного кадра нельзя: декодеру нужен предшествующий ключевой
кадр. Поэтому вход всегда ищется по ближайшему ключевому кадру **не позже**
запрошенного времени (`-ss` до `-i` — input seek). Фрагмент может начаться
на несколько секунд раньше запрошенного, но никогда не позже — для
видеонаблюдения потерять начало события хуже, чем получить лишние секунды
до него. При GOP камер из §1 (H.265, 15 fps) это обычно 1–2 с.

**Резать каждый сегмент отдельно, а не общий склеенный поток.** Соблазн
сделать один `concat` всех файлов и один рез по нему разбивается о дыры в
записи: склеенная шкала времени непрерывна, а настенная — нет, и после
первой же дыры (реконнект RTSP, перезапуск MediaMTX) смещение уезжает
ровно на её длину. Поэтому смещение считается **внутри каждого сегмента**
от его собственного `started_at`, и дыры не влияют на границы.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("facewatch.backend.export")

# Потолок длительности одного экспорта. Экспорт идёт remux'ом, то есть
# упирается не в процессор, а в диск и размер ответа: час записи одной
# камеры при 2 Mbps (§1) — это ~0.9 ГБ. Больше часа одним файлом оператору
# не нужно (для этого есть архив целиком), а без потолка один запрос мог бы
# попросить все 14 дней retention и занять диск под временный файл.
MAX_EXPORT_SECONDS = 3600

# Потолок числа сегментов во фрагменте. При сегментах по 5 минут (§20) час
# — это 12 файлов; 64 оставляет запас на архив с более короткими сегментами
# и одновременно ограничивает число вызовов ffmpeg на один запрос.
MAX_EXPORT_SEGMENTS = 64

# Таймаут одного вызова ffmpeg. Remux часового фрагмента укладывается в
# единицы секунд; 300 с — это «процесс завис», а не «медленно копирует».
# Без таймаута повисший ffmpeg (битый файл, отвалившийся NFS-том) держал бы
# и временный каталог, и обработчик запроса до перезапуска приложения.
FFMPEG_TIMEOUT_SEC = 300


class ExportError(Exception):
    """Экспорт не удался по причине, которую нужно показать оператору."""


@dataclass(frozen=True)
class Piece:
    """Кусок одного сегмента, попадающий в запрошенное окно."""

    path: str
    # Смещение от начала файла сегмента, секунды.
    start_offset: float
    # Длительность куска, секунды.
    duration: float
    # Кусок покрывает сегмент целиком — резать его нечем, файл идёт в
    # склейку как есть (экономит один проход ffmpeg на каждый средний
    # сегмент длинного фрагмента).
    whole: bool


def as_naive_utc(value: datetime) -> datetime:
    """Привести время к naive-UTC — виду, в котором лежат столбцы БД.

    `video_segments.started_at/ended_at` объявлены как `DateTime` без
    таймзоны (TIMESTAMP WITHOUT TIME ZONE) и хранят UTC. Браузер же
    отправляет границы окна с суффиксом `Z`/`+03:00`, и FastAPI отдаёт их
    как **aware**-datetime. Вычитание aware из naive — не расхождение на
    смещение пояса, а `TypeError` прямо в обработчике: без этой нормализации
    экспорт падал бы 500-й на любом клиенте, который присылает таймзону.
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def plan_pieces(segments, date_from: datetime, date_to: datetime) -> list[Piece]:
    """Разложить окно [date_from, date_to] на куски сегментов.

    Сегменты ожидаются отсортированными по `started_at`. Сегменты без
    пересечения с окном отбрасываются — попасть сюда они могут из-за того,
    что выборка идёт по `started_at`, а конец сегмента лежит в другом
    столбце.
    """
    date_from = as_naive_utc(date_from)
    date_to = as_naive_utc(date_to)
    pieces: list[Piece] = []
    for seg in segments:
        seg_start, seg_end = seg.started_at, seg.ended_at
        if seg_end is None or seg_end <= seg_start:
            # Сегмент, который ещё пишется (или битая строка): длительности
            # у него нет, резать нечего.
            continue
        overlap_start = max(seg_start, date_from)
        overlap_end = min(seg_end, date_to)
        duration = (overlap_end - overlap_start).total_seconds()
        if duration <= 0:
            continue
        start_offset = (overlap_start - seg_start).total_seconds()
        seg_duration = (seg_end - seg_start).total_seconds()
        # Допуск в 100 мс: границы окна приходят с точностью до секунды, а
        # `ended_at` пишется по факту закрытия файла — без допуска почти
        # каждый «целый» сегмент считался бы обрезанным и гонялся бы через
        # лишний remux.
        whole = start_offset <= 0.1 and duration >= seg_duration - 0.1
        pieces.append(Piece(path=seg.file_path, start_offset=max(start_offset, 0.0),
                            duration=duration, whole=whole))
    return pieces


def _concat_line(path: str) -> str:
    """Строка для demuxer'а concat с экранированием по его правилам.

    Формат concat различает кавычки: одинарная кавычка внутри имени файла
    закрывает строку, и остаток пути ffmpeg разберёт как директиву. Пути
    приходят из БД (их пишет слой записи, не пользователь), но экранирование
    здесь стоит потому, что `-safe 0` снимает проверку путей самим ffmpeg, —
    единственной защитой остаётся эта строка.
    """
    return "file '%s'\n" % path.replace("'", "'\\''")


async def _run_ffmpeg(args: list[str]) -> None:
    """Запустить ffmpeg и дождаться его, не блокируя event loop."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        # kill, а не terminate: зависший на вводе-выводе ffmpeg может не
        # ответить на SIGTERM, а осиротевший процесс продолжит держать
        # временный каталог, который вызывающий вот-вот удалит.
        proc.kill()
        await proc.wait()
        raise ExportError("Экспорт не уложился в отведённое время") from None
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
        logger.error("ffmpeg завершился с кодом %s", proc.returncode,
                     extra={"ffmpeg_stderr": tail[-5:] if tail else []})
        raise ExportError("Не удалось собрать фрагмент из сегментов архива")


async def build_fragment(pieces: list[Piece], workdir: Path, out_name: str) -> Path:
    """Собрать фрагмент из кусков в `workdir` и вернуть путь к файлу.

    Один кусок — один вызов ffmpeg. Несколько — сначала нарезка обрезанных
    краёв, потом склейка demuxer'ом `concat`, тоже без перекодирования:
    сегменты пишет одна камера с одними настройками кодека, поэтому
    требование concat «одинаковые параметры потока» выполняется.
    """
    if not pieces:
        raise ExportError("За выбранный период записей не найдено")

    out_path = workdir / out_name

    if len(pieces) == 1:
        piece = pieces[0]
        await _run_ffmpeg([
            "-y", "-hide_banner", "-loglevel", "error",
            # -ss ДО -i: быстрый поиск по ключевым кадрам. После -i ffmpeg
            # декодировал бы всё до точки реза — то самое перекодирование,
            # которого §24 не допускает.
            "-ss", f"{piece.start_offset:.3f}",
            "-i", piece.path,
            "-t", f"{piece.duration:.3f}",
            "-c", "copy",
            # Вход по ключевому кадру раньше запрошенного даёт отрицательные
            # временные метки; без сдвига к нулю плееры показывают чёрный
            # экран в начале фрагмента.
            "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart",
            str(out_path),
        ])
        return out_path

    parts_dir = workdir / "parts"
    parts_dir.mkdir(exist_ok=True)
    part_paths: list[str] = []
    for idx, piece in enumerate(pieces):
        if piece.whole:
            # Сегмент целиком внутри окна — резать нечего, идёт в склейку
            # как есть.
            part_paths.append(piece.path)
            continue
        part = parts_dir / f"part{idx:04d}.mp4"
        await _run_ffmpeg([
            "-y", "-hide_banner", "-loglevel", "error",
            "-ss", f"{piece.start_offset:.3f}",
            "-i", piece.path,
            "-t", f"{piece.duration:.3f}",
            "-c", "copy",
            "-avoid_negative_ts", "make_zero",
            str(part),
        ])
        part_paths.append(str(part))

    list_file = workdir / "concat.txt"
    list_file.write_text("".join(_concat_line(p) for p in part_paths), encoding="utf-8")

    await _run_ffmpeg([
        "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat",
        # -safe 0 нужен для абсолютных путей; экранирование — в _concat_line.
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ])
    return out_path


def make_workdir() -> Path:
    """Временный каталог под сборку фрагмента.

    Отдельный каталог на запрос, а не общий: удаление после отдачи файла
    тогда не может задеть параллельный экспорт.
    """
    return Path(tempfile.mkdtemp(prefix="fw-export-"))


def cleanup_workdir(workdir: Path) -> None:
    """Удалить временный каталог после отдачи файла клиенту.

    Вызывается как BackgroundTask ответа — то есть уже после того, как тело
    ответа ушло. Ошибка удаления не должна ломать ответ, но и молчать о ней
    нельзя: невидимая утечка /tmp на 120 камерах кончается заполненным
    диском.
    """
    try:
        shutil.rmtree(workdir, ignore_errors=False)
    except OSError:
        logger.warning("не удалось удалить временный каталог экспорта %s", workdir,
                       exc_info=True)


def within_media_root(path: str, media_root: str) -> bool:
    """Лежит ли файл сегмента внутри каталога медиа.

    Пути берутся из БД, а не из запроса, — но именно они уходят в список
    concat с `-safe 0`, где ffmpeg открывает всё, что там написано. Строка в
    `video_segments.file_path`, указывающая за пределы медиа-каталога,
    означала бы либо повреждение данных, либо запись, сделанную не слоем
    записи; отдавать по ней файл наружу не нужно ни в одном из случаев.
    """
    try:
        root = os.path.realpath(media_root)
        target = os.path.realpath(path)
    except OSError:
        return False
    return os.path.commonpath([root, target]) == root
