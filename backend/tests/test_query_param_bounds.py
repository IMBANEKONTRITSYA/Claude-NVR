"""Границы числовых параметров: класс находок цикла 21.

Двенадцать эндпоинтов принимали `days`/`limit`/`threshold` как голый
`int`/`float` без `ge`/`le` — значение пользователя уходило прямо в
`timedelta(days=...)` и в SQL `LIMIT`. Шесть входов давали необработанный
500 вместо 422, три из них — с ролью «наблюдатель», самой низкой в матрице
прав ТЗ. Полное описание класса и замеры — в `app/params.py`.

**Эти тесты идут production path, а не только edge cases**: запросы идут
через настоящий ASGI-стек (TestClient + lifespan приложения) к настоящему
Postgres+pgvector и настоящему Redis, с настоящим логином и настоящим
JWT — то есть ровно тем путём, которым запрос приходит в проде. Мокается
только сервис распознавания (его нет в CI), и только там, где без него
нельзя.

Позитивная половина (`test_default_and_in_range_values_still_work`) —
обязательная: сузить границы так, чтобы 500 ушли вместе с рабочими
сценариями, было бы регрессией, а не исправлением. Значения в ней взяты
из фронтенда: слайдер порога на странице поиска ходит 0.1..0.9, поле
«дней» в отчётах — 1..365 (`frontend/src/pages/Reports.tsx`).
"""
import pytest

# Входы, которые до фикса давали необработанный 500 на живом приложении.
# Каждая строка — (метод, url-шаблон, что именно ломалось).
CRASHING_INPUTS = [
    ("GET", "/api/stats/by-day?days=999999999", "OverflowError: date value out of range"),
    ("GET", "/api/stats/heatmap?days=99999999999999", "OverflowError: int too large to convert to C int"),
    ("GET", "/api/stats/top-persons?limit=-1", "LIMIT must not be negative"),
    ("GET", "/api/stats/top-persons?days=999999999", "OverflowError: date value out of range"),
    ("GET", "/api/archive/segments?limit=-1", "LIMIT must not be negative"),
    ("GET", "/api/persons/1/gallery?limit=-1", "LIMIT must not be negative"),
    ("POST", "/api/persons/1/enhance?limit=-1", "LIMIT must not be negative"),
]

# Входы без отказа, но с неограниченным расходом: до фикса отвечали 200 и
# делали ровно то, о чём просили.
UNBOUNDED_INPUTS = [
    ("GET", "/api/archive/segments?limit=100000000", "выборка архива без потолка"),
    ("GET", "/api/persons/1/gallery?limit=100000000", "галерея персоны без потолка"),
    ("GET", "/api/stats/by-day?days=-5", "отрицательное окно — дата в будущем"),
]


@pytest.mark.parametrize("method,url,failure", CRASHING_INPUTS,
                         ids=[u for _, u, _ in CRASHING_INPUTS])
def test_out_of_range_values_are_rejected_not_crashed(client, admin_headers, method, url, failure):
    """422, а не 500: параметр отбраковывается валидацией до обработчика."""
    r = client.request(method, url, headers=admin_headers)
    assert r.status_code == 422, (
        f"{method} {url} → {r.status_code}; ожидался 422. До фикса здесь был "
        f"необработанный 500 ({failure})"
    )


@pytest.mark.parametrize("method,url,why", UNBOUNDED_INPUTS,
                         ids=[u for _, u, _ in UNBOUNDED_INPUTS])
def test_unbounded_values_are_rejected(client, admin_headers, method, url, why):
    r = client.request(method, url, headers=admin_headers)
    assert r.status_code == 422, f"{method} {url} → {r.status_code}; ожидался 422 ({why})"


@pytest.mark.parametrize("url", [
    "/api/reports/appearances.csv?days=999999999",
    "/api/reports/persons.csv?days=999999999",
    "/api/reports/cameras.csv?days=-1",
])
def test_report_export_day_window_is_bounded(client, admin_token, url):
    """Выгрузки берут токен из query (открываются ссылкой), но окно —
    такой же параметр, и до фикса `days=999999999` роняло их в 500."""
    r = client.get(f"{url}&token={admin_token}", headers={})
    assert r.status_code == 422, f"{url} → {r.status_code}; ожидался 422"


def test_search_threshold_below_zero_is_rejected(client, admin_headers):
    """Отрицательный порог превращал `similarity >= :threshold` в
    тавтологию: вместо совпадений эндпоинт отдавал всю таблицу событий.

    Валидация формы срабатывает до чтения файла и до похода в сервис
    распознавания, поэтому живой worker для проверки не нужен.
    """
    r = client.post(
        "/api/search/face",
        headers=admin_headers,
        files={"file": ("photo.jpg", b"not-a-real-jpeg", "image/jpeg")},
        data={"threshold": "-1", "limit": "100"},
    )
    assert r.status_code == 422, f"threshold=-1 → {r.status_code}; ожидался 422"


def test_search_limit_above_ceiling_is_rejected(client, admin_headers):
    r = client.post(
        "/api/search/face",
        headers=admin_headers,
        files={"file": ("photo.jpg", b"not-a-real-jpeg", "image/jpeg")},
        data={"threshold": "0.4", "limit": "100000000"},
    )
    assert r.status_code == 422, f"limit=1e8 → {r.status_code}; ожидался 422"


def test_enhance_cannot_flood_the_upscale_queue(client, admin_headers, pg_conn):
    """`POST /api/persons/{id}/enhance?limit=<много>` не может переполнить
    очередь апскейла.

    Штатная постановка задач в воркере ограничена 500 элементами
    (`worker.py`: `if want and r.llen("upscale:queue") < 500`), а этот
    эндпоинт ставил столько задач, сколько у персоны событий, минуя
    ограничение. На архиве за месяц это десятки тысяч задач с `force=True`
    из одного HTTP-запроса — очередь Redis растёт, апскейл занят ей часами.

    Production path целиком: реальные строки в Postgres, реальный ответ
    эндпоинта, реальная длина очереди в Redis (не мок Redis — очередь
    и есть то, что проверяется).
    """
    from app.config import settings
    import redis as sync_redis

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO persons (name, status, created_at) "
            "VALUES ('bounds-test-person', 'unknown', NOW()) RETURNING id"
        )
        pid = cur.fetchone()[0]
        for _ in range(5):
            cur.execute(
                "INSERT INTO face_events (camera_id, person_id, ts, snapshot_path, is_known) "
                "VALUES (1, %s, NOW(), 'snapshots/bounds.jpg', false)", (pid,)
            )

    r_client = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        before = r_client.llen("upscale:queue")

        over = client.post(f"/api/persons/{pid}/enhance?limit=1000000", headers=admin_headers)
        assert over.status_code == 422, (
            f"limit=1e6 → {over.status_code}; ожидался 422. До фикса эндпоинт "
            "отвечал 200 и ставил в очередь всё, что нашёл"
        )
        assert r_client.llen("upscale:queue") == before, (
            "отбракованный запрос не должен ничего ставить в очередь"
        )

        # А в границах — по-прежнему работает и ставит ровно столько, сколько просили.
        ok = client.post(f"/api/persons/{pid}/enhance?limit=3", headers=admin_headers)
        assert ok.status_code == 200, ok.text
        assert ok.json()["queued"] == 3
        assert r_client.llen("upscale:queue") == before + 3
    finally:
        # Изоляция: тест убирает и строки, и свои задачи из очереди —
        # повторный локальный прогон по той же БД не должен разъезжаться
        # (известный пробел «изоляция тестов бэкенда», циклы 19-20).
        for _ in range(3):
            r_client.lpop("upscale:queue")
        r_client.close()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM face_events WHERE person_id = %s", (pid,))
            cur.execute("DELETE FROM persons WHERE id = %s", (pid,))


def test_default_and_in_range_values_still_work(client, admin_headers, admin_token):
    """Контроль на пережатие границ: рабочие сценарии фронтенда живы.

    Без этой половины «исправление» могло бы состоять в том, чтобы
    отбраковывать всё подряд, и тесты выше всё равно бы прошли.
    """
    in_range = [
        ("GET", "/api/stats/by-day", None),
        ("GET", "/api/stats/by-day?days=14", None),
        ("GET", "/api/stats/by-day?days=365", None),
        ("GET", "/api/stats/heatmap?days=30", None),
        ("GET", "/api/stats/top-persons?days=30&limit=10", None),
        ("GET", "/api/archive/segments?limit=200", None),
        ("GET", "/api/events?limit=100", None),
        ("GET", "/api/reports/appearances.csv?days=365", admin_token),
        ("GET", "/api/reports/persons.csv?days=1", admin_token),
    ]
    for method, url, token in in_range:
        full = f"{url}{'&' if '?' in url else '?'}token={token}" if token else url
        r = client.request(method, full, headers={} if token else admin_headers)
        assert r.status_code == 200, f"{method} {url} → {r.status_code}: {r.text[:200]}"
