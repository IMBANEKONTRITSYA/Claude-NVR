"""§18: «Системный мониторинг — Оператор: Ограниченно».

Строка матрицы прав, у которой не было реализации. `/api/system/metrics`
отдавал оператору всё, а докстринг эндпоинта при этом утверждал обратное:
«Оператору доступно ограниченно (см. матрицу прав)». Ограничение было
записано в ТЗ, в матрице и в собственном докстринге — и не существовало
нигде.

Два уровня, как и у соседних наборов прав (`test_face_feed_acl_unit.py`
цикла 51): чистое решение без Postgres и FastAPI — и production path,
настоящий оператор с настоящим токеном против настоящего эндпоинта.
Одного второго мало: на живом ответе не видно, какие поля решение
считает «железом», а какие — дашбордом, и разница между «поле убрали» и
«поля в этом ответе просто не было» неразличима.
"""
import pytest

from app.services.system_metrics_acl import (
    HARDWARE_FIELDS, filter_system_metrics,
)

# Ответ `_collect()` в миниатюре: поля железа §9 и поля дашборда рядом.
SAMPLE = {
    "cpu_percent": 12.5,
    "ram_percent": 41.0,
    "ram_used_mb": 6553,
    "ram_total_mb": 16000,
    "temperature_c": 47.0,
    "temperature_source": "coretemp",
    "disk_percent": 63.2,
    "disk_free_gb": 1400.5,
    "disk_total_gb": 4000.0,
    "disk_used_gb": 2599.5,
    "cameras_total": 120,
    "cameras_online": 118,
    "cameras_enabled": 120,
    "events_today": 431,
    "segments_total": 500123,
    "upscale_queue": 3,
    "redis_ok": True,
    "camera_fps": {"1": 8.4},
    "camera_analytics_source": {"1": {"stream": "main"}},
}


# --- решение --------------------------------------------------------------

def test_admin_gets_the_dict_unchanged():
    """Админу — байт в байт, без пересборки.

    Админ-дашборд читает поля, которых в списках этого модуля может быть
    не перечислено, и молчаливая потеря поля сломала бы его не сразу.
    """
    assert filter_system_metrics(SAMPLE, "admin") is SAMPLE


@pytest.mark.parametrize("field", HARDWARE_FIELDS)
def test_operator_does_not_receive_server_hardware_telemetry(field):
    """CPU, RAM и температура — телеметрия сервера, у оператора её нет."""
    assert field not in filter_system_metrics(SAMPLE, "operator")


def test_operator_keeps_disk_usage():
    """Место на диске остаётся, и это не послабление, а §5.

    §5 требует «индикация заполнения диска, прогноз на сколько дней хватит
    места», а §18 запрещает оператору не видеть хранилище, а **настраивать**
    его («Настройка retention и хранения» — Нет). Дежурный, который не
    видит, что архив кончается, не может даже позвать администратора.
    """
    limited = filter_system_metrics(SAMPLE, "operator")
    assert limited["disk_percent"] == 63.2
    assert limited["disk_free_gb"] == 1400.5
    assert limited["disk_total_gb"] == 4000.0


def test_operator_keeps_the_dashboard_row_intact():
    """«Дашборд и мониторинг» у оператора — «Да», и урезать его нельзя.

    Камеры, потоки, события, FPS и поток аналитики относятся к этой
    строке матрицы, а не к системному мониторингу.
    """
    limited = filter_system_metrics(SAMPLE, "operator")
    for field in ("cameras_total", "cameras_online", "cameras_enabled",
                  "events_today", "segments_total", "camera_fps",
                  "camera_analytics_source"):
        assert field in limited, field


def test_fields_absent_from_the_spec_enumeration_are_not_restricted():
    """`redis_ok` и `upscale_queue` §9 в системном мониторинге не называет.

    Убрать их значило бы ввести ограничение, которого в ТЗ нет, — ровно
    та ошибка, от которой предостерегает приём «отличать „строка ТЗ
    разрешает“ от „строки ТЗ нет“».
    """
    limited = filter_system_metrics(SAMPLE, "operator")
    assert limited["redis_ok"] is True
    assert limited["upscale_queue"] == 3


def test_limited_response_says_it_is_limited():
    """Признак нужен интерфейсу, чтобы объяснить пустоту.

    Без него страница мониторинга у оператора просто теряла бы три плитки,
    и это читалось бы как поломка мониторинга, а не как право.
    """
    assert filter_system_metrics(SAMPLE, "operator")["system_metrics_limited"] is True
    assert "system_metrics_limited" not in filter_system_metrics(SAMPLE, "admin")


def test_fields_are_removed_not_nulled():
    """Удаление, а не `None`.

    `null` фронтенд читает как «датчика на этом сервере нет» — то есть
    запрет выдавался бы за отсутствие железа, и оператор видел бы
    «температура недоступна» на сервере, где она есть.
    """
    limited = filter_system_metrics(SAMPLE, "operator")
    assert "temperature_c" not in limited
    assert limited.get("temperature_c", "отсутствует") == "отсутствует"


def test_the_source_dict_is_not_mutated():
    """Ответ собирается один раз на запрос и фильтруется на выдаче —
    порча исходного словаря отняла бы поля и у админа."""
    before = dict(SAMPLE)
    filter_system_metrics(SAMPLE, "operator")
    assert SAMPLE == before


def test_an_unknown_role_gets_the_limited_view():
    """Умолчание — в сторону запрета.

    Эндпоинт пускает только админа и оператора, но если роль когда-нибудь
    добавят, поведение по умолчанию не должно быть «показать всё».
    """
    assert "cpu_percent" not in filter_system_metrics(SAMPLE, "viewer")


# --- production path ------------------------------------------------------

def test_operator_gets_the_limited_payload_from_the_real_endpoint(
        client, make_user_headers, request):
    """Настоящий оператор, настоящий токен, настоящий эндпоинт."""
    headers = make_user_headers(f"op-{request.node.name}"[:40], "operator")
    r = client.get("/api/system/metrics", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["system_metrics_limited"] is True
    for field in HARDWARE_FIELDS:
        assert field not in body, field
    # Дашборд на месте — иначе правка отняла бы у оператора строку «Да».
    assert "cameras_total" in body
    assert "disk_percent" in body


def test_admin_still_gets_hardware_from_the_real_endpoint(client, admin_headers):
    """Позитивный контроль: без него правку можно было бы «пройти», убрав
    поля у всех."""
    r = client.get("/api/system/metrics", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "cpu_percent" in body and "ram_percent" in body
    assert "system_metrics_limited" not in body


def test_prometheus_export_is_still_admin_only(client, make_user_headers, request):
    """Оператор не должен обходить ограничение через Prometheus-экспорт.

    Тот же `_collect()` отдаётся там текстом, и без этой проверки
    ограничение снималось бы одним запросом на соседний путь.
    """
    headers = make_user_headers(f"op2-{request.node.name}"[:40], "operator")
    # Токен из заголовка — в query: экспорт читает его оттуда (скрейперы
    # не умеют слать Bearer).
    token = headers["Authorization"].split(" ", 1)[1]
    r = client.get(f"/api/system/prometheus?token={token}")
    assert r.status_code == 403, r.text
