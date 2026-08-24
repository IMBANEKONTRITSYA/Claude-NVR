"""Опрос живой системы браузером через настоящий nginx (см. e2e/README.md).

Проверяется цепочка целиком: браузер → nginx (боевые
`frontend/nginx*.conf`) → бэкенд → Postgres/Redis. Ни одно звено здесь не
подменено, и именно в них два цикла подряд находились дефекты, невидимые
юнит-тестам.
"""
import json
import time

import pytest

# Все разделы интерфейса (frontend/src/App.tsx: Routes). Список — не
# «несколько показательных страниц»: раздел, которого здесь нет, снова
# оказывается проверенным только юнит-тестом своего модуля.
SECTIONS = [
    ("/dashboard", "Дашборд", "§9"),
    ("/live", "Камеры в реальном времени", "§4"),
    ("/wall", "Стена распознавания", "§15"),
    ("/persons", "Карточки персон", "§15"),
    ("/archive", "Архив", "§5"),
    ("/roi", "Зоны детекции", "§15"),
    ("/reports", "Отчёты", "§8"),
    ("/search", "Поиск похожих лиц", "§15"),
    ("/cameras", "Управление камерами", "§3"),
    ("/users", "Пользователи", "§10"),
    ("/monitoring", "Мониторинг системы", "§9"),
    ("/settings", "Системные настройки", "§11"),
    ("/audit", "Журнал действий", "§10"),
    ("/profile", "Профиль", "§10"),
]


@pytest.mark.parametrize("path,heading,spec", SECTIONS, ids=[s[0] for s in SECTIONS])
def test_section_opens_clean_in_a_browser(logged_in, path, heading, spec):
    """Раздел открывается, показывает свой заголовок и ни на что не жалуется.

    «Открылся» здесь — не «вернулся 200 на HTML»: SPA отдаёт один и тот же
    index.html на любой путь, поэтому проверяется отрисованный заголовок
    раздела, а вместе с ним — отсутствие исключений React, ошибок консоли
    и ответов ≥ 400 на запросах, которые страница сделала сама.
    """
    logged_in.visit(path)
    body = logged_in.page.inner_text("body")
    assert heading in body, (
        f"{spec}: раздел {path} не показал заголовок {heading!r}. "
        f"{logged_in.complaints()}"
    )
    assert not logged_in.complaints(), f"{spec}: раздел {path} — {logged_in.complaints()}"


def test_login_from_the_browser_names_the_account_in_audit(logged_in, pg_conn):
    """§10, §14: журнал аудита обязан назвать учётку входа.

    Регрессия цикла 63. Форма входа уходит из браузера как
    `multipart/form-data` (`FormData` в `frontend/src/api.ts`), а
    middleware аудита разбирала тело `parse_qs`, понимающим только
    urlencoded, — и писала заглушку `?` вместо имени на **каждом** входе
    через веб-интерфейс. Юнит-тесты этого не видели: все они зовут
    `/login` через `data=`, то есть транспортом, который работал.

    Проверка стоит именно здесь, а не только в `backend/tests`: там тело
    формы собирает тест, а тут — сам браузер.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT username FROM audit_log WHERE action = 'Вход в систему' "
            "ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
    assert row is not None, "вход из браузера не попал в журнал аудита вовсе"
    assert row[0] and row[0] != "?", (
        f"журнал аудита не назвал учётку входа: записано {row[0]!r}"
    )


def test_live_face_event_reaches_the_open_wall(logged_in, redis_client):
    """§15: событие из Redis доезжает до открытой «Стены» через /ws/ за nginx.

    Payload — ровно тот, что публикует воркер (`worker/worker.py`:
    `faces:new`), включая `event_id`, по которому лента дедуплицирует
    карточки. Проверка держит вместе три вещи, которые ломаются
    независимо: WebSocket-локацию nginx (`proxy_set_header Upgrade`),
    подписку бэкенда на канал и разбор сообщения самой лентой.
    """
    logged_in.visit("/wall")
    before = _shown_count(logged_in.page.inner_text("body"))

    marker = int(time.time())
    payload = {
        "type": "face", "event_id": marker, "camera_id": 1, "person_id": None,
        "name": f"e2e-{marker}", "is_known": False, "alert": False, "tags": [],
        "snapshot": "faces/e2e-none.jpg", "ts": "2026-01-01T00:00:00",
        "bbox": [10, 20, 100, 120], "frame_w": 1280, "frame_h": 720,
    }
    redis_client.publish("faces:new", json.dumps(payload, ensure_ascii=False))

    logged_in.page.wait_for_selector(f"text=e2e-{marker}", timeout=15000)
    after = _shown_count(logged_in.page.inner_text("body"))
    assert after == before + 1, (
        f"счётчик ленты не вырос: было {before}, стало {after}"
    )


def _shown_count(body: str) -> int:
    import re
    m = re.search(r"Показано:\s*(\d+)", body)
    assert m, "на «Стене» нет счётчика «Показано:» — разметка изменилась"
    return int(m.group(1))


def test_hls_is_closed_without_the_auth_cookie(base_url):
    """§4/§18: nginx закрывает /hls/ раньше, чем запрос дойдёт до MediaMTX.

    Проверка исполняет `auth_request` из `nginx-locations.conf` целиком:
    сабреквест уходит на бэкенд и его отказ превращается в 401. Здесь это
    важнее, чем в статическом тесте текста конфига, — до цикла 62 конфиги
    не исполнялись вовсе, а `nginx -t` зелен и при неработающей директиве.

    Запрос идёт голым HTTP-клиентом, а не из контекста браузера: там уже
    лежит кука `hls_auth`, выставленная при входе, и «без куки» получилось
    бы только на словах.

    MediaMTX в CI не поднят, и это ничего не портит: проверяется как раз
    то, что до апстрима дело не доходит. Дошло бы — вместо 401 пришла бы
    502 от несуществующего апстрима, и тест это увидит.
    """
    httpx = pytest.importorskip("httpx")
    r = httpx.get(base_url + "/hls/cam1/index.m3u8", timeout=15.0)
    assert r.status_code == 401, (
        f"/hls/ без куки hls_auth ответил {r.status_code}, ожидался 401 от auth_request"
    )
