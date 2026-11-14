"""Экспорт фрагмента архива (ТЗ §5) — на настоящих MP4 и настоящем ffmpeg.

Мок на `_run_ffmpeg` проверял бы только то, что модуль складывает строку
аргументов так, как её задумал автор теста, — то есть ровно ту часть, где
ошибиться невозможно. Существенное здесь другое: что на выходе получается
воспроизводимый файл нужной длительности, склеенный из нескольких
сегментов и не перекодированный. Это видно только по настоящему ffprobe.

Фикстуры генерируются `ffmpeg -f lavfi -i testsrc` — без бинарника тесты
пропускаются, чтобы прогон без ffmpeg не выглядел падением кода.
"""
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="нужны ffmpeg и ffprobe",
)

from app.services import export as export_svc  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeSegment:
    """Строка `video_segments` в объёме, который нужен планировщику кусков."""

    def __init__(self, path, started_at, ended_at):
        self.file_path = str(path)
        self.started_at = started_at
        self.ended_at = ended_at


def _make_mp4(path, seconds: int) -> None:
    """Сегмент архива: H.264, 10 fps, ключевой кадр раз в секунду.

    GOP в секунду (а не дефолтные 250 кадров) — потому что рез без
    перекодирования идёт по ключевым кадрам, и на дефолтном GOP фрагмент
    в 6 секунд из 10-секундного файла округлялся бы до всего файла, то
    есть тест не отличал бы работающий рез от его отсутствия.
    """
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=10",
         "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


def _duration(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def _codec(path) -> str:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


BASE = datetime(2026, 8, 16, 12, 0, 0)


@pytest.fixture(scope="module")
def archive(tmp_path_factory):
    """Три подряд идущих сегмента по 10 секунд, как их пишет слой записи."""
    d = tmp_path_factory.mktemp("segments")
    segs = []
    for i in range(3):
        p = d / f"cam1_{i}.mp4"
        _make_mp4(p, 10)
        segs.append(FakeSegment(p, BASE + timedelta(seconds=10 * i),
                                BASE + timedelta(seconds=10 * (i + 1))))
    return segs


@pytest.mark.anyio
async def test_fragment_inside_one_segment(archive, tmp_path):
    """Окно внутри одного сегмента режется до запрошенной длительности."""
    pieces = export_svc.plan_pieces(archive, BASE + timedelta(seconds=2),
                                    BASE + timedelta(seconds=8))
    assert len(pieces) == 1
    assert not pieces[0].whole

    out = await export_svc.build_fragment(pieces, tmp_path, "out.mp4")
    # Вход ищется по ключевому кадру НЕ ПОЗЖЕ запрошенного, поэтому фрагмент
    # может быть длиннее запрошенных 6 с — но не короче (иначе экспорт терял
    # бы конец события) и не длиннее целого сегмента.
    assert 5.5 <= _duration(out) <= 10.0
    assert _codec(out) == "h264", "remux не должен менять кодек (§24)"


@pytest.mark.anyio
async def test_fragment_across_segment_boundary(archive, tmp_path):
    """Окно поверх трёх сегментов отдаётся ОДНИМ файлом.

    Это и есть требование §5, которого не было: раньше оператор получал три
    отдельных файла и склеивал их сам.
    """
    pieces = export_svc.plan_pieces(archive, BASE + timedelta(seconds=5),
                                    BASE + timedelta(seconds=25))
    assert len(pieces) == 3
    # Средний сегмент целиком внутри окна — он не режется, а идёт в склейку
    # как есть.
    assert [p.whole for p in pieces] == [False, True, False]

    out = await export_svc.build_fragment(pieces, tmp_path, "out.mp4")
    assert out.exists()
    # 20 секунд запрошено; допуск — на округление до ключевых кадров.
    assert 19.0 <= _duration(out) <= 26.0
    assert _codec(out) == "h264"


@pytest.mark.anyio
async def test_gap_between_segments_does_not_shift_boundaries(tmp_path):
    """Дыра в записи не сдвигает границы фрагмента.

    Ровно тот случай, ради которого смещение считается внутри каждого
    сегмента, а не по склеенной шкале: между сегментами минута без записи
    (реконнект RTSP). На общей склейке смещение уехало бы на длину дыры, и
    второй кусок начался бы не с того места — а на 10-секундном файле
    смещение 65 с не нашлось бы вовсе, и кусок вышел бы пустым.
    """
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    _make_mp4(a, 10)
    _make_mp4(b, 10)
    segs = [
        FakeSegment(a, BASE, BASE + timedelta(seconds=10)),
        # Дыра 60 секунд.
        FakeSegment(b, BASE + timedelta(seconds=70), BASE + timedelta(seconds=80)),
    ]
    pieces = export_svc.plan_pieces(segs, BASE + timedelta(seconds=5),
                                    BASE + timedelta(seconds=75))
    assert len(pieces) == 2
    # Второй кусок начинается с начала своего файла (окно накрыло его
    # начало), а не со смещения 65 с, которого в 10-секундном файле нет.
    assert pieces[1].start_offset == 0.0
    assert pieces[1].duration == pytest.approx(5.0)

    workdir = tmp_path / "w"
    workdir.mkdir()
    out = await export_svc.build_fragment(pieces, workdir, "out.mp4")
    assert out.exists()
    # Дыра в записи в файл не попадает: 5 с из первого + 5 с из второго.
    assert 9.0 <= _duration(out) <= 16.0


def test_segments_outside_window_are_dropped(archive):
    """Сегменты без пересечения с окном в план не попадают."""
    pieces = export_svc.plan_pieces(archive, BASE + timedelta(seconds=21),
                                    BASE + timedelta(seconds=29))
    assert len(pieces) == 1
    assert pieces[0].path == archive[2].file_path


def test_aware_datetime_does_not_break_planner(archive):
    """Границы с таймзоной не роняют планировщик.

    Браузер шлёт `2026-08-16T12:00:00Z`, столбцы БД — naive-UTC. Без
    нормализации вычитание aware из naive даёт TypeError, то есть 500 на
    любом клиенте, который присылает суффикс таймзоны.
    """
    pieces = export_svc.plan_pieces(
        archive,
        datetime(2026, 8, 16, 12, 0, 5, tzinfo=timezone.utc),
        datetime(2026, 8, 16, 12, 0, 15, tzinfo=timezone.utc),
    )
    assert len(pieces) == 2


def test_still_recording_segment_is_skipped(tmp_path):
    """Сегмент без корректного `ended_at` пропускается, а не роняет план."""
    p = tmp_path / "x.mp4"
    p.write_bytes(b"")
    segs = [
        FakeSegment(p, BASE, None),
        FakeSegment(p, BASE, BASE),  # нулевая длительность
    ]
    assert export_svc.plan_pieces(segs, BASE, BASE + timedelta(seconds=10)) == []


def test_within_media_root_blocks_escape(tmp_path):
    """Путь за пределами медиа-каталога отбраковывается.

    Список concat идёт в ffmpeg с `-safe 0`, то есть проверку путей самим
    ffmpeg он не делает — эта функция остаётся единственной.
    """
    media = tmp_path / "media"
    (media / "segments").mkdir(parents=True)
    good = media / "segments" / "a.mp4"
    good.write_bytes(b"")
    assert export_svc.within_media_root(str(good), str(media))
    assert not export_svc.within_media_root("/etc/passwd", str(media))
    assert not export_svc.within_media_root(str(media / ".." / "etc" / "x"), str(media))


def test_concat_line_escapes_quote():
    """Одинарная кавычка в имени файла не должна закрывать строку concat."""
    line = export_svc._concat_line("/media/seg's.mp4")
    assert line == "file '/media/seg'\\''s.mp4'\n"


@pytest.mark.anyio
async def test_ffmpeg_failure_becomes_export_error(tmp_path):
    """Битый сегмент даёт ExportError, а не необработанное исключение."""
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not an mp4")
    pieces = [export_svc.Piece(path=str(bad), start_offset=0.0, duration=1.0, whole=False)]
    with pytest.raises(export_svc.ExportError):
        await export_svc.build_fragment(pieces, tmp_path, "out.mp4")


@pytest.mark.anyio
async def test_empty_plan_is_rejected(tmp_path):
    with pytest.raises(export_svc.ExportError):
        await export_svc.build_fragment([], tmp_path, "out.mp4")


def test_cleanup_removes_workdir():
    d = export_svc.make_workdir()
    (d / "f").write_bytes(b"x")
    export_svc.cleanup_workdir(d)
    assert not os.path.exists(d)
