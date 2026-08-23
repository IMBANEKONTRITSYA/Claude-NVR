"""Поток аналитики каждой камеры в `/api/system/metrics` (SPEC §9, §2, §15).

§2 и §15 называют допустимым источником кадров аналитики основной поток
либо субпоток «с разрешением не ниже 640×480». Какой из них реально
достался камере, решает воркер по измеренному кадру
(`worker/analytics_source.py`) и кладёт решение в хеш
`worker:analytics_source`. §9 требует показывать статус потоков — без этой
строки дежурный видел бы FPS детекции, не зная, по какому потоку он
получен, то есть не отличил бы штатную работу по субпотоку от аварийного
переезда на основной поток с кратно большим декодом.

Production path: настоящий Redis, как в `test_record_layer_api.py`. Мок
проверял бы только мок, а ошибки живут ровно на границе «воркер положил /
бэкенд прочитал».
"""
import json

import pytest

from app.config import settings

KEY = "worker:analytics_source"


@pytest.fixture()
def publish_source():
    """Кладёт решение о потоке так, как это делает воркер."""
    import redis as sync_redis

    client = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)
    client.delete(KEY)

    def _set(cam_id, payload):
        client.hset(KEY, str(cam_id), json.dumps(payload, ensure_ascii=False))

    _set.delete = lambda: client.delete(KEY)
    _set.raw = lambda cam_id, raw: client.hset(KEY, str(cam_id), raw)
    yield _set
    client.delete(KEY)
    client.close()


def _metrics(client, headers):
    r = client.get("/api/system/metrics", headers=headers)
    assert r.status_code == 200
    return r.json()


def test_absent_when_worker_silent(client, admin_headers, publish_source):
    """Воркер молчит — пустая карта, а не выдуманные значения."""
    publish_source.delete()

    d = _metrics(client, admin_headers)
    assert d["camera_analytics_source"] == {}


def test_sub_stream_within_floor_is_reported(client, admin_headers, publish_source):
    publish_source(3, {"stream": "sub", "reason": "sub_meets_floor",
                       "width": 704, "height": 576,
                       "note": "субпоток 704×576 — не ниже порога 640×480"})

    d = _metrics(client, admin_headers)
    row = d["camera_analytics_source"]["3"]
    assert row["stream"] == "sub"
    assert row["reason"] == "sub_meets_floor"
    assert (row["width"], row["height"]) == (704, 576)


def test_fallback_to_main_carries_the_numbers(client, admin_headers, publish_source):
    """Главное, ради чего строка существует: видно и что переехали, и почему.

    Без ширины и высоты субпотока дежурный видит «аналитика по основному
    потоку» и не знает, что чинить на камере, — а чинить надо профиль
    субпотока, поднять его до 640×480.
    """
    publish_source(5, {"stream": "main", "reason": "sub_below_floor",
                       "width": 352, "height": 288,
                       "note": "субпоток 352×288 ниже порога 640×480 — "
                               "аналитика переведена на основной поток"})

    row = _metrics(client, admin_headers)["camera_analytics_source"]["5"]
    assert row["stream"] == "main"
    assert row["reason"] == "sub_below_floor"
    assert (row["width"], row["height"]) == (352, 288)
    assert "352" in row["note"] and "640" in row["note"]


def test_garbage_entry_does_not_break_metrics(client, admin_headers, publish_source):
    """Мусор в хеше стоит одной камеры, а не всей страницы мониторинга.

    /metrics отдаёт CPU, RAM, диск и алерты §9. Уронить их разбором чужой
    строки значило бы обменять весь системный мониторинг на одну кривую
    запись.
    """
    publish_source.raw(9, "не-JSON")
    # Валидный JSON, но не объект: разбирается без ошибки, а полей у него
    # нет — фронтенд полез бы в число за `stream`.
    publish_source.raw(11, "5")
    publish_source(10, {"stream": "sub", "reason": "sub_meets_floor",
                        "width": 640, "height": 480, "note": ""})

    d = _metrics(client, admin_headers)
    assert "9" not in d["camera_analytics_source"]
    assert "11" not in d["camera_analytics_source"]
    assert d["camera_analytics_source"]["10"]["stream"] == "sub"
    # Остальные метрики §9 на месте.
    assert "cpu_percent" in d and "disk_percent" in d
