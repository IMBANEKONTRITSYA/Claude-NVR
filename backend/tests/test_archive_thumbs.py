"""Миниатюры кадров сегментов архива (ТЗ §7) — уровень сервиса.

Здесь проверяется всё, что видно без БД и роутера: раскладка файлов,
выбор кадра, атомарность записи и поведение на сегментах, из которых кадр
не достаётся. Production path целиком — в
`test_integration_archive_thumbs.py`.
"""
import asyncio
import os
import shutil
import subprocess

import pytest

from app.services import thumbs

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")


def _make_clip(path: str, seconds: float, size: str = "320x240") -> str:
    """Короткий тестовый MP4 из синтетического источника ffmpeg."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=size={size}:rate=10:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "10", path],
        check=True,
    )
    return path


# ---------------------------------------------------------------- раскладка

def test_thumb_rel_path_shards_by_thousand():
    """500 000 сегментов (§7) не должны лечь в один каталог."""
    assert thumbs.thumb_rel_path(0) == os.path.join("thumbs", "0", "0.jpg")
    assert thumbs.thumb_rel_path(999) == os.path.join("thumbs", "0", "999.jpg")
    assert thumbs.thumb_rel_path(1000) == os.path.join("thumbs", "1", "1000.jpg")
    assert thumbs.thumb_rel_path(499_999) == os.path.join("thumbs", "499", "499999.jpg")


def test_thumb_path_is_inside_media_root():
    p = thumbs.thumb_path("/media", 42)
    assert p == os.path.join("/media", "thumbs", "0", "42.jpg")


# ------------------------------------------------------------- выбор кадра

@pytest.mark.parametrize("duration,expected", [
    (None, 0.0),      # длительность неизвестна (сегмент ещё пишется)
    (0, 0.0),
    (1.0, 0.0),       # короче 2×offset — кадр с начала
    (2.0, 0.0),
    (2.5, 1.0),
    (300, 1.0),
])
def test_seek_offset(duration, expected):
    assert thumbs.seek_offset(duration) == expected


def test_ffmpeg_args_seek_before_input():
    """`-ss` обязан стоять до `-i`.

    Перестановка не роняет ничего и не меняет картинку — она заставляет
    ffmpeg декодировать сегмент от начала до точки реза. Регрессия
    невидимая: миниатюра просто станет считаться секунды вместо
    миллисекунд, и только на пятиминутных сегментах объекта.
    """
    args = thumbs.ffmpeg_args("/media/segments/a.mp4", "/media/thumbs/0/1.jpg", 1.0)
    assert args.index("-ss") < args.index("-i")
    assert args[args.index("-i") + 1] == "/media/segments/a.mp4"
    assert args[-1] == "/media/thumbs/0/1.jpg"
    assert "-frames:v" in args and args[args.index("-frames:v") + 1] == "1"


# --------------------------------------------------------------- генерация

def test_generate_writes_jpeg(tmp_path):
    src = _make_clip(str(tmp_path / "seg.mp4"), 3)
    dst = str(tmp_path / "thumbs" / "0" / "1.jpg")

    asyncio.run(thumbs.generate(src, dst, 1.0))

    assert os.path.getsize(dst) > 0
    with open(dst, "rb") as fh:
        assert fh.read(2) == b"\xff\xd8", "не JPEG"


def test_generate_scales_to_thumb_width(tmp_path):
    """Ширина кадра — THUMB_WIDTH, а не исходное разрешение камеры.

    Без масштабирования миниатюра 2K-камеры (§1) весила бы сотни
    килобайт, и выдача из 200 строк тянула бы десятки мегабайт.
    """
    if shutil.which("ffprobe") is None:
        pytest.skip("нужен ffprobe")
    src = _make_clip(str(tmp_path / "seg.mp4"), 2, size="1280x720")
    dst = str(tmp_path / "t.jpg")

    asyncio.run(thumbs.generate(src, dst, 0.0))

    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", dst],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    width, height = (int(x) for x in out.split(","))
    assert width == thumbs.THUMB_WIDTH
    assert height % 2 == 0, "нечётная высота не кодируется в yuv420p"


def test_generate_rejects_empty_output(tmp_path):
    """ffmpeg вышел с кодом 0, не записав кадра — это не успех.

    Ровно так он ведёт себя, когда смещение вышло за длительность. Пустой
    файл, попавший в кэш, не самоисправился бы никогда: следующий запрос
    увидел бы существующий путь и отдал те же ноль байт.
    """
    src = _make_clip(str(tmp_path / "short.mp4"), 1)
    dst = str(tmp_path / "t.jpg")

    with pytest.raises(thumbs.ThumbError):
        asyncio.run(thumbs.generate(src, dst, 999.0))

    assert not os.path.exists(dst)


def test_generate_leaves_no_temp_file_on_failure(tmp_path):
    """После неудачи в каталоге не остаётся ни .jpg, ни .tmp.

    Мусор здесь копился бы по файлу на каждый битый сегмент, а чистить
    `thumbs/` некому: ротация (worker/thumbs.py) удаляет строго по id
    сегмента.
    """
    src = str(tmp_path / "broken.mp4")
    with open(src, "wb") as fh:
        fh.write(b"not a video at all")
    dst = str(tmp_path / "thumbs" / "0" / "7.jpg")

    with pytest.raises(thumbs.ThumbError):
        asyncio.run(thumbs.generate(src, dst, 0.0))

    assert os.listdir(os.path.dirname(dst)) == []


def test_generate_is_atomic_no_partial_file(tmp_path):
    """Конечный путь появляется уже готовым, а не наполняется постепенно.

    Проверяется через подмену `_run_ffmpeg`: пока «ffmpeg» пишет во
    временный файл, конечного пути ещё не существует. Без os.replace
    параллельный запрос успевал бы прочитать обрезанный JPEG и положить
    его в кэш браузера.
    """
    dst = tmp_path / "thumbs" / "0" / "1.jpg"
    seen: dict[str, bool] = {}

    async def fake_run(args):
        out = args[-1]
        seen["dst_exists_during_write"] = dst.exists()
        seen["tmp_is_hidden"] = os.path.basename(out).startswith(".")
        with open(out, "wb") as fh:
            fh.write(b"\xff\xd8partial")

    original = thumbs._run_ffmpeg
    thumbs._run_ffmpeg = fake_run
    try:
        asyncio.run(thumbs.generate("/src.mp4", str(dst), 0.0))
    finally:
        thumbs._run_ffmpeg = original

    assert seen["dst_exists_during_write"] is False
    assert seen["tmp_is_hidden"] is True
    assert dst.exists()


# ------------------------------------------------------------------- кэш

def test_ensure_generates_once(tmp_path):
    """Второй запрос той же миниатюры не запускает ffmpeg.

    Это и есть смысл кэша: выдача архива перезапрашивается на каждое
    уточнение фильтра, и без кэша каждое стоило бы 200 вызовов ffmpeg.
    """
    src = _make_clip(str(tmp_path / "seg.mp4"), 3)
    dst = str(tmp_path / "t.jpg")
    calls = {"n": 0}
    original = thumbs.generate

    async def counting(src_, dst_, offset):
        calls["n"] += 1
        await original(src_, dst_, offset)

    thumbs.generate = counting
    try:
        async def scenario():
            await thumbs.ensure(src, dst, 3)
            await thumbs.ensure(src, dst, 3)
        asyncio.run(scenario())
    finally:
        thumbs.generate = original

    assert calls["n"] == 1


def test_ensure_regenerates_empty_cached_file(tmp_path):
    """Нулевой файл в кэше — не кэш.

    Такой мог остаться от прежней (неатомарной) версии или от заполнения
    диска. Считать его готовой миниатюрой значит навсегда показать
    оператору битую картинку на этом сегменте.
    """
    src = _make_clip(str(tmp_path / "seg.mp4"), 3)
    dst = tmp_path / "t.jpg"
    dst.write_bytes(b"")

    asyncio.run(thumbs.ensure(src, str(dst), 3))

    assert dst.stat().st_size > 0


def test_ensure_bounded_by_semaphore(tmp_path):
    """Одновременных ffmpeg не больше THUMB_CONCURRENCY.

    Браузер тянет всю выдачу пачкой; без потолка один поиск породил бы до
    200 процессов ffmpeg и отнял бы у слоя аналитики ядра, которые §2
    обязывает считать независимыми.
    """
    peak = {"now": 0, "max": 0}
    original = thumbs.generate

    async def slow(src_, dst_, offset):
        peak["now"] += 1
        peak["max"] = max(peak["max"], peak["now"])
        await asyncio.sleep(0.05)
        with open(dst_, "wb") as fh:
            fh.write(b"\xff\xd8jpeg")
        peak["now"] -= 1

    thumbs.generate = slow
    try:
        async def scenario():
            await asyncio.gather(*[
                thumbs.ensure("/src.mp4", str(tmp_path / f"{i}.jpg"), 5)
                for i in range(20)
            ])
        asyncio.run(scenario())
    finally:
        thumbs.generate = original

    assert peak["max"] <= thumbs.THUMB_CONCURRENCY


def test_semaphore_survives_new_event_loop(tmp_path):
    """Семафор не привязывается намертво к первому event loop.

    Модуль импортируется один раз, а циклов за жизнь процесса бывает
    несколько (TestClient поднимает свой на каждый контекст, uvicorn — при
    перезапуске воркера). Один общий `asyncio.Semaphore` на модуль падал
    бы во втором цикле с RuntimeError «is bound to a different event
    loop» — то есть миниатюры отказывали бы после первого же перезапуска.

    **Нагрузка обязана быть выше потолка.** `asyncio.Semaphore.acquire()`
    трогает цикл только когда ему приходится ждать: на свободном семафоре
    он просто уменьшает счётчик, привязки не происходит, и проверка
    проходит даже на общем семафоре. Первая версия этого теста запускала
    по одной задаче на цикл и была ложно-зелёной — поймано верификацией
    откатом, а не прогоном.
    """
    original = thumbs.generate

    async def slow(src_, dst_, offset):
        # Удержание permit'а на время сна гарантирует очередь: задач вдвое
        # больше, чем permit'ов.
        await asyncio.sleep(0.02)
        with open(dst_, "wb") as fh:
            fh.write(b"\xff\xd8jpeg")

    async def burst(tag: str):
        await asyncio.gather(*[
            thumbs.ensure("/src.mp4", str(tmp_path / f"{tag}{i}.jpg"), 5)
            for i in range(thumbs.THUMB_CONCURRENCY * 2)
        ])

    thumbs.generate = slow
    try:
        asyncio.run(burst("a"))
        asyncio.run(burst("b"))
    finally:
        thumbs.generate = original

    assert (tmp_path / f"b{thumbs.THUMB_CONCURRENCY}.jpg").stat().st_size > 0
