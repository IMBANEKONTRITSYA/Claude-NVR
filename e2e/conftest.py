"""Фикстуры браузерного опроса живой системы (см. e2e/README.md).

Сьют не поднимает стенд сам: его поднимает `e2e/run-stack.sh` (локально) и
джоба `ui-live` в CI. Адрес и пароль администратора приезжают
переменными окружения — так один и тот же сьют ходит и в песочницу, и на
временный стенд CI, и (при желании) на настоящий объект.
"""
import os
import re
import urllib.parse

import pytest

BASE_URL = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8080")
ADMIN_USER = os.environ.get("E2E_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "E2eProbeAdmin!2026")
CHROMIUM_PATH = os.environ.get("E2E_CHROMIUM_PATH") or None

# Локально отсутствующий браузер или psycopg2 — повод пропустить сьют, а не
# завалить прогон. В CI ровно наоборот: молчаливый skip там означал бы, что
# опрос «проведён», ни разу не выполнившись, — тот самый класс ложного
# зелёного, из-за которого джоба backend отдельно ставит ffmpeg и pg_dump.
# Джоба ui-live выставляет E2E_STRICT=1, и тогда нехватка зависимости
# роняет прогон с внятным сообщением.
STRICT = os.environ.get("E2E_STRICT") == "1"


def _need(module: str, reason: str):
    if STRICT:
        return __import__(module, fromlist=["__name__"])
    return pytest.importorskip(module, reason=reason)


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL.rstrip("/")


@pytest.fixture(scope="session")
def admin_credentials():
    return ADMIN_USER, ADMIN_PASSWORD


@pytest.fixture(scope="session")
def browser():
    playwright = _need(
        "playwright.sync_api",
        "нужен playwright: pip install playwright && playwright install chromium",
    )
    with playwright.sync_playwright() as p:
        kwargs = {"args": ["--no-sandbox"]}
        if CHROMIUM_PATH:
            kwargs["executable_path"] = CHROMIUM_PATH
        br = p.chromium.launch(**kwargs)
        yield br
        br.close()


class PageProbe:
    """Страница вместе со всем, что она нажаловала за переход.

    Ошибки консоли и ответы ≥ 400 собираются на каждой навигации: раздел,
    который «открылся», но уронил запрос или исключение React, для
    оператора сломан ровно так же, как не открывшийся.
    """

    def __init__(self, page, base_url):
        self.page = page
        self.base_url = base_url
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.failed_requests: list[tuple[str, str]] = []
        self.bad_responses: list[tuple[int, str, str]] = []
        page.on("pageerror", lambda e: self.page_errors.append(str(e)[:400]))
        # «Failed to load resource: … status of NNN» — это не отдельная
        # жалоба, а дубль того же самого ответа, который уже посчитан в
        # bad_responses, только без URL. Держать его в канале консоли
        # значило бы, что один отказ приходит дважды и второй раз — без
        # адреса, то есть его нельзя ни соотнести с разделом, ни осознанно
        # разрешить. Настоящие ошибки JS (исключения, отвергнутые промисы)
        # остаются здесь.
        page.on("console", lambda m: self.console_errors.append(m.text[:400])
                if m.type == "error" and not m.text.startswith("Failed to load resource")
                else None)
        # net::ERR_ABORTED — не отказ, а нормальный исход перехода: чанк
        # code-splitting'а, запрошенный предыдущим разделом, отменяется
        # браузером при уходе с него. Всё остальное (отказ соединения,
        # обрыв, CORS) — жалоба.
        page.on("requestfailed", lambda r: self.failed_requests.append((r.url, str(r.failure)))
                if "ERR_ABORTED" not in str(r.failure) else None)
        page.on("response", lambda r: self.bad_responses.append(
            (r.status, r.request.method, r.url)) if r.status >= 400 else None)

    def reset(self):
        self.console_errors.clear()
        self.page_errors.clear()
        self.failed_requests.clear()
        self.bad_responses.clear()

    def visit(self, path: str):
        self.reset()
        self.page.goto(self.base_url + path, wait_until="networkidle")
        # Данные разделов приезжают вторым запросом после монтирования —
        # networkidle его застаёт не всегда.
        self.page.wait_for_timeout(1200)

    def _path_of(self, url: str) -> str:
        return url.replace(self.base_url, "").split("?", 1)[0]

    def complaints(self, allowed: tuple[str, ...] = ()) -> str:
        """Что раздел нажаловал за переход.

        `allowed` — регулярные выражения **на путь целиком** (без query),
        для которых отказ ожидаем и о состоянии приложения ничего не
        говорит: в песочнице и в CI нет ни MediaMTX, ни снятых снимков
        камер. Именно полное совпадение, а не префикс: `/api/cameras/`
        префиксом накрыл бы и список парка, и CRUD камеры, то есть
        разрешение молча съело бы настоящие дефекты §3.
        """
        rules = [re.compile(r) for r in allowed]
        allow = lambda url: any(p.fullmatch(self._path_of(url)) for p in rules)  # noqa: E731
        bad = [(s, m, self._path_of(u) + ("?…" if "?" in u else ""))
               for s, m, u in self.bad_responses if not allow(u)]
        failed = [f for f in self.failed_requests if not allow(f[0])]
        parts = []
        if self.page_errors:
            parts.append(f"исключения: {self.page_errors}")
        if self.console_errors:
            parts.append(f"консоль: {self.console_errors}")
        if bad:
            parts.append(f"ответы>=400: {bad}")
        if failed:
            parts.append(f"неотправленные запросы: {failed}")
        return "; ".join(parts)


@pytest.fixture()
def probe(browser, base_url):
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    p = PageProbe(ctx.new_page(), base_url)
    yield p
    ctx.close()


@pytest.fixture()
def logged_in(probe, admin_credentials):
    """Вход ровно так, как его делает оператор: через форму на /login.

    Не через API и не подсовыванием токена в localStorage: транспорт формы
    — часть проверяемого контракта (цикл 63 нашёл дефект журнала аудита
    именно в нём).
    """
    user, password = admin_credentials
    probe.visit("/login")
    probe.page.fill("form.card input >> nth=0", user)
    probe.page.fill("form.card input[type=password]", password)
    probe.page.click("form.card button[type=submit]")
    probe.page.wait_for_url("**/dashboard", timeout=15000)
    return probe


@pytest.fixture(scope="session")
def pg_conn():
    """Прямое подключение к той же базе — для проверок следов в БД."""
    psycopg2 = _need("psycopg2", "нужен psycopg2-binary")
    dsn = os.environ.get(
        "DATABASE_URL", "postgresql+asyncpg://facewatch:facewatch@127.0.0.1:5432/facewatch")
    parsed = urllib.parse.urlsplit(dsn.replace("+asyncpg", ""))
    conn = psycopg2.connect(
        host=parsed.hostname, port=parsed.port or 5432,
        user=parsed.username, password=parsed.password,
        dbname=parsed.path.lstrip("/"), connect_timeout=5,
    )
    conn.autocommit = True
    yield conn
    conn.close()


@pytest.fixture(scope="session")
def seeded_camera(base_url, admin_credentials):
    """Одна камера в парке на всё время прогона.

    Без неё раздел «Живой просмотр» открывался на пустом парке и не
    доходил ни до одной плитки — то есть проверка §4 держалась на том,
    что база CI пуста, и перестала бы что-либо значить на первой же
    заведённой камере. С камерой раздел проходит свой настоящий путь:
    запрашивает плейлист у MediaMTX и снимок у бэкенда. Ни того, ни
    другого в песочнице нет, и отказы на этих двух путях разрешены
    предметно (см. `ALLOWED_WITHOUT_MEDIAMTX` в test_ui_live.py).
    """
    httpx = _need("httpx", "нужен httpx")
    user, password = admin_credentials
    with httpx.Client(base_url=base_url, timeout=30.0) as c:
        token = c.post("/api/auth/login",
                       data={"username": user, "password": password}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        r = c.post("/api/cameras", headers=headers, json={
            "name": "e2e-probe-cam",
            "rtsp_url": "rtsp://127.0.0.1:554/e2e-probe",
            "location": "e2e",
            "enabled": True,
            "mode": "record_only",
        })
        r.raise_for_status()
        cam_id = r.json()["id"]
        yield cam_id
        c.delete(f"/api/cameras/{cam_id}", headers=headers)


@pytest.fixture(scope="session")
def redis_client():
    redis = _need("redis", "нужен redis")
    url = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
    client = redis.from_url(url)
    yield client
    client.close()
