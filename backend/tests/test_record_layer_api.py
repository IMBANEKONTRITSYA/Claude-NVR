"""Состояние слоя записи в API (SPEC §14, §9) — `/api/system/record-layer`.

Production path: настоящий Redis (тот же, что использует приложение) и
настоящий Postgres. Мок Redis здесь проверял бы только мок — а именно на
границе «воркер положил / бэкенд прочитал» и живут ошибки формата.
"""
import json

import pytest

from app.config import settings

KEY = "record:layer"


@pytest.fixture()
def redis_key():
    """Синхронный клиент Redis для засева ключа — как `pg_conn` для Postgres.

    Асинхронный `get_redis()` приложения использовать нельзя: он один на
    процесс и привязан к циклу событий, в котором создан, а `TestClient`
    крутит свой цикл на каждый запрос — попытка писать им из теста даёт
    «Future attached to a different loop».

    Ключ снимается после теста: `record:layer` живёт 120 секунд и иначе
    пережил бы прогон и достался следующему (тот же класс, что
    `changepw_fail` в цикле 24).
    """
    import redis as sync_redis

    client = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)

    def _set(value):
        client.set(KEY, value, ex=120)

    _set.delete = lambda: client.delete(KEY)
    _set.delete()
    yield _set
    client.delete(KEY)
    client.close()


@pytest.fixture()
def publish_state(redis_key):
    """Кладёт состояние слоя записи в Redis так, как это делает воркер."""
    return lambda payload: redis_key(json.dumps(payload))


def _payload(streams, gaps=()):
    online = sum(1 for s in streams if s["status"] == "online")
    return {
        "streams": streams,
        "summary": {
            "streams_total": len(streams), "streams_online": online,
            "streams_offline": len(streams) - online, "streams_unknown": 0,
            "inbound_bytes": sum(s.get("inbound_bytes", 0) for s in streams),
            "frames_in_error": 0,
        },
        "segment_gaps": list(gaps),
        "updated_at": 1_800_000_000.0,
    }


def test_reports_no_data_when_worker_silent(client, admin_headers, redis_key):
    """Воркер не публиковал состояние — честное «нет данных».

    Нули вместо этого выглядели бы как «все потоки потеряны», то есть
    отказ воркера читался бы как отказ всех 120 камер.
    """
    redis_key.delete()

    d = client.get("/api/system/record-layer", headers=admin_headers).json()
    assert d["available"] is False
    assert d["streams"] == [] and d["summary"] is None
    assert "reason" in d
    # Счётчики архива при этом обязаны считаться: они из БД, а не от воркера.
    assert "segments_last_day" in d and "gb_last_day" in d


def test_streams_from_worker_are_exposed(client, admin_headers, publish_state):
    """SPEC §14: «статус каждого RTSP-потока слоя записи»."""
    publish_state(_payload([
        {"camera_id": 1, "name": "Проходная", "status": "online",
         "inbound_bytes": 1024, "online_since": "2026-08-06T07:00:00Z",
         "frames_in_error": 0},
        {"camera_id": 2, "name": "Склад", "status": "offline",
         "inbound_bytes": 0, "online_since": None, "frames_in_error": 3},
    ]))

    d = client.get("/api/system/record-layer", headers=admin_headers).json()
    assert d["available"] is True
    assert [s["camera_id"] for s in d["streams"]] == [1, 2]
    assert d["summary"]["streams_online"] == 1
    assert d["summary"]["streams_total"] == 2
    by_id = {s["camera_id"]: s for s in d["streams"]}
    assert by_id[2]["frames_in_error"] == 3


def test_segment_gaps_are_exposed(client, admin_headers, publish_state):
    """SPEC §14 отдельной строкой требует алерт «пропуск записи сегмента»,
    и интерфейс должен уметь его показать, а не только лог воркера."""
    publish_state(_payload(
        [{"camera_id": 5, "name": "Ворота", "status": "online",
          "inbound_bytes": 10, "online_since": None, "frames_in_error": 0}],
        gaps=[5]))

    d = client.get("/api/system/record-layer", headers=admin_headers).json()
    assert d["segment_gaps"] == [5]


def test_record_root_warning_reaches_monitoring(client, admin_headers, publish_state):
    """SPEC §5 «путь архива конфигурируется под отдельный диск»: если корень
    записи MediaMTX разведён с корнем архива, сегменты пишутся в один
    каталог, а индексируются из другого. Наружу это выходит как «пропуск
    записи» сразу на всех камерах — по такому симптому причина не читается,
    поэтому воркер передаёт её текстом, а мониторинг обязан её показать."""
    payload = _payload([{"camera_id": 1, "name": "Вход", "status": "online",
                         "inbound_bytes": 10, "online_since": None,
                         "frames_in_error": 0}])
    payload["record_root_warning"] = "MediaMTX пишет в /recordings/segments, архив сканирует /media/segments"
    publish_state(payload)

    d = client.get("/api/system/record-layer", headers=admin_headers).json()
    assert d["record_root_warning"] == payload["record_root_warning"]


def test_record_root_warning_absent_when_roots_agree(client, admin_headers, publish_state):
    """Позитивный контроль: на согласованных каталогах поля нет — иначе
    предупреждение висело бы на каждом штатном развёртывании и его
    перестали бы читать."""
    publish_state(_payload([{"camera_id": 1, "name": "Вход", "status": "online",
                             "inbound_bytes": 10, "online_since": None,
                             "frames_in_error": 0}]))

    d = client.get("/api/system/record-layer", headers=admin_headers).json()
    assert d["record_root_warning"] is None


def test_corrupted_redis_payload_does_not_break_endpoint(client, admin_headers, redis_key):
    """Мусор в ключе (чужой писатель, оборванная запись) не должен ронять
    мониторинг — он деградирует до «нет данных»."""
    redis_key("не json")
    r = client.get("/api/system/record-layer", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["available"] is False


def test_record_layer_denied_to_viewer(client, make_user_headers):
    """SPEC §25: «Системный мониторинг» — админ, оператор ограниченно,
    наблюдатель нет."""
    r = client.get("/api/system/record-layer",
                   headers=make_user_headers("recl-viewer", "viewer"))
    assert r.status_code == 403


def test_record_layer_allowed_to_operator(client, make_user_headers):
    """Позитивный контроль к предыдущему: оператору мониторинг доступен."""
    r = client.get("/api/system/record-layer",
                   headers=make_user_headers("recl-operator", "operator"))
    assert r.status_code == 200


def test_recovery_state_is_attached_to_its_stream(client, admin_headers, publish_state):
    """SPEC §19: воркер восстанавливает поток сам и сообщает, что видит.

    Оператору на стене камер нужно различать «камера выключена» и «камера
    отвечает по RTSP, а запись всё равно не идёт»: первое чинят на
    объекте, второе — в настройках потока. Воркер отдаёт это картой по
    camera_id (после JSON — строковые ключи), а бэкенд обязан приклеить её
    к своему потоку, иначе интерфейсу пришлось бы держать вторую карту и
    сшивать её руками.
    """
    payload = _payload([{"camera_id": 7, "name": "Склад", "status": "offline",
                         "inbound_bytes": 0, "online_since": None,
                         "frames_in_error": 0}])
    payload["recovery"] = {"7": {"down_for_sec": 12.5, "probes_failed": 0,
                                 "kicks": 1, "camera_answering": True}}
    publish_state(payload)

    d = client.get("/api/system/record-layer", headers=admin_headers).json()

    assert d["streams"][0]["recovery"]["camera_answering"] is True
    assert d["streams"][0]["recovery"]["kicks"] == 1


def test_stream_without_recovery_state_has_no_such_field(client, admin_headers, publish_state):
    """Позитивный контроль: на здоровом потоке поля нет вовсе.

    Пустой объект вместо отсутствия поля означал бы, что интерфейс рисует
    строку «камера не отвечает по RTSP» под каждой исправной камерой.
    """
    publish_state(_payload([{"camera_id": 7, "name": "Склад", "status": "online",
                             "inbound_bytes": 10, "online_since": None,
                             "frames_in_error": 0}]))

    d = client.get("/api/system/record-layer", headers=admin_headers).json()

    assert "recovery" not in d["streams"][0]


def test_quota_state_reaches_monitoring(client, admin_headers, publish_state):
    """Состояние циклической перезаписи доезжает от воркера до §9.

    С этого цикла проход идёт в своей нити воркера и сторожем живости не
    проверяется (SPEC §2): его 900-секундный бюджет означал четверть часа
    без синхронизации путей, без индексации сегментов и без публикации
    статусов, причём на КАЖДОМ проходе менеджера, а не раз в час, как у
    уборки. Плата за вынос — проход стал невидимым снаружи, и это поле —
    то, чем он снова виден.

    Проверка откатом: снимите `"quota": payload.get("quota")` в
    `routers/system.py` — тест упадёт.
    """
    payload = _payload([])
    payload["quota"] = {"state": "failed", "seconds": 3.0,
                        "last_pass_sec": 41.2, "error": "enforce_disk_quota",
                        "skipped": 7}
    publish_state(payload)

    d = client.get("/api/system/record-layer", headers=admin_headers).json()

    assert d["quota"]["state"] == "failed"
    assert d["quota"]["error"] == "enforce_disk_quota"
    assert d["quota"]["skipped"] == 7


def test_quota_field_is_present_and_null_when_the_worker_is_silent(
        client, admin_headers, redis_key):
    """Молчащий воркер даёт `null`, а не отсутствие ключа.

    Тот же довод, что у `cleanup`: иначе фронтенд отличал бы «перезапись
    здорова» от «данных нет» по отсутствию поля, то есть по совпадению.
    """
    redis_key.delete()

    d = client.get("/api/system/record-layer", headers=admin_headers).json()

    assert "quota" in d and d["quota"] is None


def test_quota_and_cleanup_are_separate_fields(client, admin_headers, publish_state):
    """Два прохода — два поля: у них разные правила показа.

    Пропуск у уборки означает «архив чистится медленнее, чем растёт» —
    тревога; пропуск у перезаписи штатен, она запрашивается каждые ~10 с.
    Свести их в одно поле значило бы либо сыпать тревогой на каждом
    переполненном томе, либо потерять единственный ранний признак у уборки.
    """
    payload = _payload([])
    payload["cleanup"] = {"state": "done", "seconds": 12.0,
                          "last_pass_sec": 31.4, "error": None, "skipped": 0}
    payload["quota"] = {"state": "running", "seconds": 14.0,
                        "last_pass_sec": 13.9, "error": None, "skipped": 42}
    publish_state(payload)

    d = client.get("/api/system/record-layer", headers=admin_headers).json()

    assert d["cleanup"]["skipped"] == 0
    assert d["quota"]["skipped"] == 42
