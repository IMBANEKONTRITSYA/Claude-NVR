"""«Запись только при движении» со стороны API (SPEC §6).

Уборку сегментов проверяет воркер (`worker/tests/test_motion_recording.py`);
здесь — то, что администратор вообще может включить режим и что он
переживает обычную жизнь камеры: правку формы, выгрузку и загрузку
конфигурации, смену режима.

Отдельная тема файла — отказ включать режим на камере, которую никто не
декодирует. Флаг на `record_only` не ломает данные, он просто ничего не
делает, и это худший вид неисправности: администратор считает, что диск
экономится, а узнаёт обратное по заполнению тома через неделю.
"""
import io
import json

import pytest

from app.services import camera_config


@pytest.fixture()
def analytics_slots(client, admin_headers):
    """Поднимает предел камер аналитики на время теста.

    По умолчанию их две (SPEC §1), а тестам ниже нужна своя: без этого
    файл падал бы или не падал в зависимости от того, сколько камер
    аналитики оставила предыдущая работа с этой БД.
    """
    before = client.get("/api/settings", headers=admin_headers).json()
    client.put("/api/settings", json={"analytics_cameras_max": 16},
               headers=admin_headers)
    yield
    client.put("/api/settings",
               json={"analytics_cameras_max": int(before.get("analytics_cameras_max", 2))},
               headers=admin_headers)


def test_analytics_camera_accepts_the_option(make_camera, analytics_slots):
    cam = make_camera("движение-1", mode="analytics", record_on_motion=True)
    assert cam["record_on_motion"] is True


def test_record_only_camera_rejects_the_option(client, admin_headers, make_camera):
    r = client.post("/api/cameras", headers=admin_headers, json={
        "name": "движение-отказ", "rtsp_url": "rtsp://cam/x",
        "mode": "record_only", "record_on_motion": True,
    })
    # Уборка объявляется ДО проверки: если отказа не случилось, камера
    # создана, и `assert` ниже унесёт тест мимо любой уборки, записанной
    # после него. Ровно так этот файл при проверке мутациями оставил в БД
    # три камеры и уронил четыре чужих теста следующим прогоном.
    if r.status_code == 200:
        make_camera.adopt(r.json()["id"])
    assert r.status_code == 400
    # Сообщение обязано объяснять причину: «400 Bad Request» без слова
    # «аналитика» отправляет администратора искать опечатку в URL.
    assert "аналитик" in r.json()["detail"].lower()


def test_option_defaults_to_off(make_camera, analytics_slots):
    """Опция — значит выключена, пока её не включили явно."""
    cam = make_camera("движение-дефолт", mode="analytics")
    assert cam["record_on_motion"] is False


def test_editing_other_field_keeps_the_option(client, admin_headers, make_camera,
                                              analytics_slots):
    """Правка локации не должна выключать режим.

    Ровно та мина, которая уже срабатывала с полями ONVIF и расписанием
    детекции: форма не вернула поле — сервер счёл это «убрать».
    """
    cam = make_camera("движение-правка", mode="analytics", record_on_motion=True)
    r = client.put(f"/api/cameras/{cam['id']}", headers=admin_headers, json={
        "name": cam["name"], "rtsp_url": "rtsp://cam/движение-правка",
        "mode": "analytics", "location": "2 этаж", "record_on_motion": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["record_on_motion"] is True
    assert r.json()["location"] == "2 этаж"


def test_switching_to_record_only_with_option_is_refused(client, admin_headers,
                                                         make_camera, analytics_slots):
    """Режим нельзя «забыть» включённым на камере без аналитики."""
    cam = make_camera("движение-смена", mode="analytics", record_on_motion=True)
    r = client.put(f"/api/cameras/{cam['id']}", headers=admin_headers, json={
        "name": cam["name"], "rtsp_url": "rtsp://cam/движение-смена",
        "mode": "record_only", "record_on_motion": True,
    })
    assert r.status_code == 400
    # Камера осталась прежней, а не наполовину обновлённой.
    got = client.get("/api/cameras", headers=admin_headers).json()
    got = [c for c in got if c["id"] == cam["id"]][0]
    assert got["mode"] == "analytics" and got["record_on_motion"] is True


def test_export_carries_the_option(client, admin_token, make_camera, analytics_slots):
    """Выгрузка конфигурации несёт режим (SPEC §3).

    Без этого перенос парка на боевой сервер молча возвращает все камеры
    к непрерывному хранению — та же потеря, что цикл 35 нашёл у
    расписания детекции и зон.
    """
    make_camera("движение-выгрузка", mode="analytics", record_on_motion=True)
    # Ссылку открывает браузер, поэтому токен идёт query-параметром, а не
    # заголовком (см. require_role_query в routers/cameras.py).
    body = client.get(f"/api/cameras/export?format=json&token={admin_token}").text
    rows = json.loads(body)["cameras"]
    row = [r for r in rows if r["name"] == "движение-выгрузка"][0]
    assert row["record_on_motion"] is True
    assert "record_on_motion" in camera_config.FIELDS


def _upload(client, headers, rows, dry_run=False):
    body = camera_config.rows_to_csv(rows).encode("utf-8")
    return client.post(
        f"/api/cameras/import?dry_run={'true' if dry_run else 'false'}",
        headers=headers,
        files={"file": ("cameras.csv", io.BytesIO(body), "text/csv")},
    )


def test_import_sets_and_clears_the_option(client, admin_headers, make_camera,
                                           analytics_slots):
    cam = make_camera("движение-импорт", mode="analytics", record_on_motion=False)
    rows = [{"name": "движение-импорт", "rtsp_url": "rtsp://cam/движение-импорт",
             "mode": "analytics", "record_on_motion": True}]
    assert _upload(client, admin_headers, rows).json()["ok"] is True

    def current():
        got = client.get("/api/cameras", headers=admin_headers).json()
        return [c for c in got if c["id"] == cam["id"]][0]

    assert current()["record_on_motion"] is True

    rows[0]["record_on_motion"] = False
    assert _upload(client, admin_headers, rows).json()["ok"] is True
    assert current()["record_on_motion"] is False


def test_import_without_the_column_keeps_saved_value(client, admin_headers,
                                                     make_camera, analytics_slots):
    """Файл прежней версии не выключает режим на всём парке.

    Колонки в нём нет вовсе, и трактовка «нет колонки — снять» превратила
    бы правку одной локации в отключение функции на всех камерах.
    """
    cam = make_camera("движение-старыйфайл", mode="analytics", record_on_motion=True)
    body = "name,rtsp_url,mode\nдвижение-старыйфайл,rtsp://cam/движение-старыйфайл,analytics\n"
    r = client.post("/api/cameras/import", headers=admin_headers,
                    files={"file": ("old.csv", io.BytesIO(body.encode("utf-8")), "text/csv")})
    assert r.json()["ok"] is True, r.text
    got = client.get("/api/cameras", headers=admin_headers).json()
    assert [c for c in got if c["id"] == cam["id"]][0]["record_on_motion"] is True


def test_import_refuses_the_option_on_record_only_row(client, admin_headers, cam_residue):
    """Проверка режима действует и на импорте, не только на форме.

    `cam_residue` — по той же причине, что и `adopt` выше: id камеры,
    заведённой импортом, тест заранее не знает, и если проверка режима
    когда-нибудь отвалится, строка останется в БД и уронит чужой тест.
    """
    rows = [{"name": "движение-плохаястрока", "rtsp_url": "rtsp://cam/x",
             "mode": "record_only", "record_on_motion": True}]
    r = _upload(client, admin_headers, rows)
    body = r.json()
    assert body["ok"] is False and body["created"] == 0
    assert any("analytics" in e["error"] for e in body["errors"])
    # Камера не заведена: файл применяется целиком или никак.
    names = {c["name"] for c in client.get("/api/cameras", headers=admin_headers).json()}
    assert "движение-плохаястрока" not in names
