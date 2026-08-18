"""Согласованность каталогов слоя записи (SPEC §5, §26) — цикл 39.

До цикла 39 `recordPath` был захардкожен `/media/segments/%path_%s`, а
воркер сканировал `$MEDIA_PATH/segments`. Пока обе величины совпадали
(docker-compose монтирует том в `/media`), этого не было видно. Стоило
выполнить §5 «настраиваемая глубина хранения… путь архива конфигурируется
под отдельный диск» либо перейти на раскладку §26
(`/var/lib/facewatch/`) — и слой записи начинал писать в один каталог, а
архив индексировать другой:

* `video_segments` пуст → §5 (архив, плеер, экспорт) и §7 (поиск) не
  работают ни на одной камере;
* retention удаляет файлы по своему пути и настоящих не видит → диск
  заполняется до отказа, «прогноз на сколько дней хватит места» врёт;
* через два интервала сегмента загорается алерт «пропуск записи» **сразу
  на всех** камерах — симптом, по которому причина не читается.

Здесь проверяется вторая половина правки: воркер обязан сказать вслух,
куда он пишет и где ищет, и отдельно предупредить, если это разные корни.
"""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

# CI-джоба воркера намеренно не ставит cv2/insightface (см. шапку
# record_layer.py), а `worker.py` импортирует cv2 на верхнем уровне. Без
# этой обёртки модуль ронял бы сбор тестов, а не пропускался. Сам разбор
# путей проверяется без `worker` — в test_record_layer.py, который живёт на
# одном stdlib и в этой джобе прогоняется целиком.
worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)


@pytest.fixture()
def roots(monkeypatch):
    """Задаёт оба корня так, как они были бы разобраны на старте процесса."""
    def _set(media_path: str, record_root: str):
        monkeypatch.setattr(worker, "MEDIA_PATH", media_path)
        monkeypatch.setattr(worker, "RECORD_MEDIA_ROOT", record_root)
    return _set


def test_no_warning_when_roots_agree(roots, caplog):
    """Штатное развёртывание (и режим 1, и режим 2 §26): предупреждения
    нет, но факт всё равно записан в журнал — на него ссылается
    DEPLOY_CHECKLIST при первом запуске."""
    roots("/var/lib/facewatch/media", "/var/lib/facewatch/media")
    with caplog.at_level("INFO", logger="facewatch.worker"):
        assert worker.log_record_root() is None
    msg = [r for r in caplog.records if r.message == "слой записи: каталоги сегментов"]
    assert msg, "воркер не сказал, куда пишет и где ищет сегменты"
    rec = msg[0]
    assert rec.mediamtx_record_path == "/var/lib/facewatch/media/segments/%path_%s"
    assert rec.indexer_scan_dir == "/var/lib/facewatch/media/segments"


def test_trailing_slash_is_not_a_divergence(roots):
    """`MEDIA_PATH=/media/` против `/media` — одно и то же место, и путать
    оператора предупреждением здесь нельзя."""
    roots("/media/", "/media")
    assert worker.log_record_root() is None


def test_warning_names_both_directories(roots, caplog):
    """Корни разведены: текст обязан содержать оба пути — иначе он не
    помогает, а только сообщает, что что-то не так."""
    roots("/media", "/recordings")
    with caplog.at_level("WARNING", logger="facewatch.worker"):
        warning = worker.log_record_root()
    assert warning is not None
    assert "/recordings/segments/%path_%s" in warning
    assert "/media/segments" in warning
    assert any(r.levelname == "WARNING" for r in caplog.records)


def _publish(monkeypatch) -> dict:
    """Состояние слоя записи так, как его собирает воркер, без Redis и
    MediaMTX — как в test_model_failure_isolation.py."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        def runtime_paths(self):
            return {}

    monkeypatch.setattr(worker, "MediaMTXClient", _Client)
    monkeypatch.setattr(worker, "_last_segment_ts", lambda ids: {})
    monkeypatch.setattr(worker, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(worker.r, "set", lambda *a, **k: True)
    return worker.publish_record_layer_status([])


def test_warning_is_published_to_monitoring(roots, monkeypatch):
    """Предупреждение обязано доехать до страницы мониторинга, а не остаться
    в журнале: журнал на объекте открывают после отказа, а не до."""
    roots("/media", "/recordings")
    monkeypatch.setattr(worker, "_record_root_warning", worker.log_record_root())

    assert "/recordings/segments" in _publish(monkeypatch)["record_root_warning"]


def test_published_state_has_no_warning_on_agreeing_roots(roots, monkeypatch):
    """Позитивный контроль к предыдущему: на штатном развёртывании поле
    пустое, иначе баннер висел бы всегда и его перестали бы читать."""
    roots("/media", "/media")
    monkeypatch.setattr(worker, "_record_root_warning", worker.log_record_root())

    assert _publish(monkeypatch)["record_root_warning"] is None
