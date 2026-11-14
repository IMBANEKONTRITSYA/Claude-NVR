"""Выбор бэкенда апскейла и цена недоступной модели.

Почему этого набора не было до цикла 65. Все существовавшие тесты
апскейла подменяют `enhance()` целиком (см. докстринг
`test_process_event.py`) — то есть настоящий выбор бэкенда и настоящий
`_load_gfpgan()` не исполнялись ни разу. Дефект, который ловит этот
файл, сидел ровно в непокрытом месте и по результату был невиден:
снимок улучшался OpenCV-fallback'ом, `enhanced` вставал в `true`,
оператор получал картинку. Расходился только журнал.

Состояние, которое проверяется, — штатное для поддерживаемого
развёртывания, а не авария: профиль пакета `core`
(`packaging/build-deb.sh`) ставится без torch/GFPGAN осознанно, при
`UPSCALE_BACKEND=gfpgan` из `packaging/deb/conf/facewatch.env.template`.
"""
import builtins
import logging
import os

import pytest

# Та же защита, что в соседних файлах: upscaler.py читает DATABASE_URL на
# уровне модуля, и без переменной это KeyError, а не ImportError.
if not os.environ.get("DATABASE_URL"):
    pytest.skip("нет DATABASE_URL — апскейл требует БД", allow_module_level=True)

upscaler = pytest.importorskip(
    "upscaler",
    reason="нужны зависимости апскейла (cv2/numpy/sqlalchemy/redis)",
)

import numpy as np  # noqa: E402


@pytest.fixture()
def fresh_loader(monkeypatch):
    """Сбрасывает кэш загрузчика: он живёт в модуле на весь процесс."""
    monkeypatch.setattr(upscaler, "_gfpgan", None)
    monkeypatch.setattr(upscaler, "_gfpgan_failed", False)


@pytest.fixture()
def import_counter(monkeypatch):
    """Считает попытки импорта gfpgan.

    Считается именно импорт, а не вызов `_load_gfpgan`: провалившийся
    импорт Питон не кэширует (модуль снимается из sys.modules), поэтому
    каждая попытка — это заново обойдённый sys.path, и именно её число
    отличает исправленный код от прежнего.
    """
    calls = []
    real_import = builtins.__import__

    def counting_import(name, *a, **kw):
        if name == "gfpgan" or name.startswith("gfpgan."):
            calls.append(name)
            raise ModuleNotFoundError("No module named 'gfpgan'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", counting_import)
    return calls


def _face(h=40, w=32):
    rng = np.random.default_rng(65)
    return rng.integers(0, 256, (h, w, 3), dtype=np.uint8)


def test_failed_load_is_attempted_once_per_process(fresh_loader, import_counter, monkeypatch):
    """Провал загрузки запоминается: импорт пробуется один раз, не на событие."""
    monkeypatch.setattr(upscaler, "UPSCALE_BACKEND", "gfpgan")
    for _ in range(5):
        out, backend = upscaler.enhance(_face())
        assert backend == "opencv"
        assert out is not None
    assert len(import_counter) == 1, (
        f"импорт gfpgan пробовался {len(import_counter)} раз на 5 событий — "
        "провал загрузки не кэшируется"
    )


def test_missing_model_does_not_log_per_event(fresh_loader, import_counter, monkeypatch, caplog):
    """О недоступной модели сообщается один раз, а не на каждое лицо.

    Проверяется число записей в журнале, а не их текст: ровно это и
    заливало systemd journal — одинаковый traceback на каждое событие.
    """
    monkeypatch.setattr(upscaler, "UPSCALE_BACKEND", "gfpgan")
    with caplog.at_level(logging.WARNING, logger="facewatch.upscaler"):
        for _ in range(5):
            upscaler.enhance(_face())
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, (
        f"{len(warnings)} предупреждений на 5 событий — журнал заливается "
        "повторным сообщением о недоступной модели"
    )
    assert warnings[0].exc_info is not None, "причина недоступности должна быть видна хотя бы раз"


def test_preload_failure_silences_later_events(fresh_loader, import_counter, monkeypatch, caplog):
    """Предзагрузка в main() и есть то единственное сообщение.

    Прежний код предзагрузку делал, но её провал не запоминал: смысл
    предзагрузки («заплатить один раз») терялся молча.
    """
    monkeypatch.setattr(upscaler, "UPSCALE_BACKEND", "gfpgan")
    with caplog.at_level(logging.WARNING, logger="facewatch.upscaler"):
        assert upscaler._load_gfpgan() is None
        before = len([r for r in caplog.records if r.levelno >= logging.WARNING])
        for _ in range(4):
            upscaler.enhance(_face())
        after = len([r for r in caplog.records if r.levelno >= logging.WARNING])
    assert before == 1
    assert after == before, "после предзагрузки события не должны ничего добавлять в журнал"
    assert len(import_counter) == 1


def test_per_frame_failure_is_still_reported(fresh_loader, monkeypatch, caplog):
    """Загруженная модель, упавшая на кадре, — разовый случай, его видно.

    Обратная сторона правки: заглушив «модели нет», нельзя заглушить
    «модель есть и упала». Без этого теста фикс мог бы спрятать второе.
    """
    monkeypatch.setattr(upscaler, "UPSCALE_BACKEND", "gfpgan")

    class _Exploding:
        def enhance(self, *a, **kw):
            raise RuntimeError("кадр не по зубам модели")

    monkeypatch.setattr(upscaler, "_gfpgan", _Exploding())
    with caplog.at_level(logging.WARNING, logger="facewatch.upscaler"):
        for _ in range(3):
            out, backend = upscaler.enhance(_face())
            assert backend == "opencv"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 3, (
        "падение модели на конкретном кадре зависит от картинки и должно "
        f"быть видно поштучно, получено {len(warnings)} на 3 кадра"
    )


def test_opencv_backend_really_upscales(monkeypatch):
    """Настоящий OpenCV-fallback: ни один тест не исполнял его до цикла 65.

    Утверждение ТЗ §15 — «нейросетевой апскейл… улучшает качество
    скриншотов». Проверяется то, что можно проверить без модели: кадр
    действительно увеличивается вдвое, а не отдаётся как есть.
    """
    monkeypatch.setattr(upscaler, "UPSCALE_BACKEND", "opencv")
    src = _face(h=40, w=32)
    out, backend = upscaler.enhance(src)
    assert backend == "opencv"
    assert out.shape[0] == src.shape[0] * 2
    assert out.shape[1] == src.shape[1] * 2
    assert out.dtype == src.dtype
