"""§12 «REST API для внешних систем», §9/§21 (Prometheus-экспортёр):
эндпоинты с токеном в query string обязаны принимать и штатный заголовок
`Authorization: Bearer`.

Зачем набор. До цикла 62 `get_user_from_query_token` объявлял
`token: str = Query(...)` — **обязательным**. Внешний клиент, отправляющий
токен так, как предписывают §12 и `docs/API_DOCS.md` для всех остальных
эндпоинтов (то есть заголовком), получал **422 от валидатора FastAPI** —
до всякой проверки подписи. Гэп нашёлся живым опросом через настоящий
nginx, а не чтением кода: юнит-тесты всегда ходили сюда с `?token=`,
поэтому обязательность параметра шесть циклов подряд читалась как
намеренная.

Больнее всего это било по `/api/system/prometheus`: `docs/ADMIN_GUIDE.md`
описывает его как «требует Bearer-токен» и «готов к подключению
scrape_config без дополнительного экспортёра», а Prometheus в своём
`authorization:`-блоке отправляет ровно заголовок — то есть документированная
интеграция мониторинга отвечала 422 на каждый scrape.

Что здесь проверяется:
  1. заголовочный транспорт работает на всех 16 эндпоинтах (по одному
     представителю на раздел ТЗ);
  2. query-транспорт не сломан — им ходит фронтенд по прямым ссылкам;
  3. отсутствие обоих — 401, а не 422 (нехватка учётных данных — состояние
     авторизации, а не синтаксис запроса);
  4. **матрица прав §18 одинакова на обоих транспортах** — заголовок не
     стал дырой в обход `require_role_query`;
  5. роль читается из БД, а не из claim'а, и на заголовочном пути тоже.
"""
import pytest

# По представителю на раздел ТЗ. Полный список даёт openapi.json:
# 16 эндпоинтов зависят от get_user_from_query_token.
ADMIN_ONLY = [
    ("/api/system/prometheus", "§9/§21 Prometheus-экспортёр"),
    ("/api/audit/export.csv", "§10 экспорт журнала аудита"),
    ("/api/audit/export.xlsx", "§10 экспорт журнала аудита"),
    ("/api/cameras/export?fmt=csv", "§3 выгрузка конфигурации камер"),
]
ADMIN_OR_OPERATOR = [
    ("/api/reports/cameras.csv", "§8 отчёты Excel/CSV"),
    ("/api/reports/cameras.xlsx", "§8 отчёты Excel/CSV"),
    ("/api/reports/persons.csv", "§8 отчёты Excel/CSV"),
    ("/api/reports/appearances.csv", "§8 отчёты Excel/CSV"),
]
ALL_ENDPOINTS = ADMIN_ONLY + ADMIN_OR_OPERATOR


@pytest.mark.parametrize("url,section", ALL_ENDPOINTS)
def test_bearer_header_is_accepted(client, admin_token, url, section):
    """Штатный заголовок §12 принимается — раньше был 422."""
    r = client.get(url, headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200, f"{section}: {url} -> {r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("url,section", ALL_ENDPOINTS)
def test_query_token_still_works(client, admin_token, url, section):
    """Прямые ссылки браузера (фронтенд) не сломаны переходом на два транспорта."""
    sep = "&" if "?" in url else "?"
    r = client.get(f"{url}{sep}token={admin_token}")
    assert r.status_code == 200, f"{section}: {url} -> {r.status_code} {r.text[:200]}"


@pytest.mark.parametrize("url,section", ALL_ENDPOINTS)
def test_no_credentials_is_401_not_422(client, url, section):
    """Ни заголовка, ни query — это 401, а не ошибка валидации схемы.

    По 422 клиент не знает, что делать; по 401 (и `WWW-Authenticate: Bearer`)
    знает — получить или обновить токен.
    """
    r = client.get(url)
    assert r.status_code == 401, f"{section}: {url} -> {r.status_code}"


@pytest.mark.parametrize("url,section", ALL_ENDPOINTS)
def test_bad_token_in_header_is_401(client, url, section):
    r = client.get(url, headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401, f"{section}: {url} -> {r.status_code}"


@pytest.mark.parametrize("header", ["Basic dXNlcjpwYXNz", "Bearer", "Bearer   ", "Digest x"])
def test_foreign_or_empty_scheme_is_401(client, header):
    """Чужая схема и пустой Bearer — 401, а не 500 и не пропуск внутрь.

    Разбор заголовка не должен падать на `partition`-е и не должен
    трактовать пустые учётные данные как валидные.
    """
    r = client.get("/api/system/prometheus", headers={"Authorization": header})
    assert r.status_code == 401, f"{header!r} -> {r.status_code}"


def test_header_scheme_is_case_insensitive(client, admin_token):
    """RFC 7235 §2.1: имя схемы регистронезависимо."""
    for scheme in ("bearer", "Bearer", "BEARER", "BeArEr"):
        r = client.get("/api/system/prometheus",
                       headers={"Authorization": f"{scheme} {admin_token}"})
        assert r.status_code == 200, f"{scheme} -> {r.status_code}"


@pytest.mark.parametrize("url,section", ADMIN_ONLY)
def test_viewer_forbidden_on_both_transports(client, make_user, request, url, section):
    """§18: заголовочный транспорт не обходит матрицу прав.

    Главное свойство правки с точки зрения безопасности: расширился только
    способ доставки токена, а не круг тех, кого пускают.
    """
    _, viewer_token = make_user(f"vw_{request.node.name}"[:60], "viewer")
    sep = "&" if "?" in url else "?"
    assert client.get(url, headers={"Authorization": f"Bearer {viewer_token}"}
                      ).status_code == 403, f"{section} header"
    assert client.get(f"{url}{sep}token={viewer_token}").status_code == 403, f"{section} query"


@pytest.mark.parametrize("url,section", ADMIN_OR_OPERATOR)
def test_viewer_forbidden_on_reports_both_transports(client, make_user, request, url, section):
    """§18: отчёты — admin/operator, наблюдателю закрыто на обоих транспортах."""
    _, viewer_token = make_user(f"vwr_{request.node.name}"[:60], "viewer")
    assert client.get(url, headers={"Authorization": f"Bearer {viewer_token}"}
                      ).status_code == 403, f"{section} header"
    assert client.get(f"{url}?token={viewer_token}").status_code == 403, f"{section} query"


def test_role_comes_from_db_not_claim_on_header_path(client, make_user, admin_headers,
                                                     pg_conn, request):
    """Разжалование действует немедленно и на заголовочном пути.

    Ровно то свойство, ради которого `get_user_from_query_token` в своё
    время перестал читать роль из claim'а: токен живёт 30 минут, а права
    меняются раньше. Проверяем, что второй транспорт не вернул старое
    поведение окольным путём.
    """
    name = f"demote_{request.node.name}"[:60]
    _, token = make_user(name, "admin")
    hdr = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/system/prometheus", headers=hdr).status_code == 200

    with pg_conn.cursor() as cur:
        cur.execute("UPDATE users SET role='viewer' WHERE username=%s", (name,))

    # Тот же самый, ещё не истёкший токен — но роль перечитывается из БД.
    assert client.get("/api/system/prometheus", headers=hdr).status_code == 403


def test_prometheus_scrape_config_shape(client, admin_token):
    """Сквозная проверка ровно того вызова, который делает Prometheus.

    `scrape_config` с блоком `authorization:` шлёт `Authorization: Bearer
    <credentials>` и ждёт `text/plain` с метриками. До правки здесь был 422
    с JSON-телом ошибки валидации.
    """
    r = client.get("/api/system/prometheus",
                   headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "# HELP" in body and "# TYPE" in body, body[:300]
