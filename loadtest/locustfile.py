"""Нагрузочный тест backend/API FaceWatch.

SPEC §16 ("CI/CD и DevOps"): "нагрузочный сценарий пересмотрен под новый
масштаб (см. §26)". §26 задаёт масштаб буквально: **120 камер в режиме
записи (remux) + N камер аналитики (по умолчанию 2)**.

До цикла 27 файл реализовывал сценарий **удалённой редакции ТЗ** — 16
камер, все в аналитике, — и ссылался на "SPEC.md §14", которым в
действующей редакции называется совсем другой раздел
(отказоустойчивость и мониторинг). То есть раздел §16 числился закрытым
с цикла 13, но проверял масштаб, которого в ТЗ уже не было.

Область теста и её честные границы
-----------------------------------
Этот сценарий нагружает HTTP/WS **API-слой backend'а** запросами,
которые реально шлёт браузер на объекте из 120 камер: опрос снепшотов
своей страницы живой сетки (§4 — грид до 16, для 120 камер выбор
страницы), список камер целиком, статус слоя записи по всем 120 потокам,
дашборд KPI, прогноз места, просмотр архива, экспорт отчётов, журнал
аудита. Это ближайший testable-прокси для "стабильности и задержек" без
физических IP-камер.

Он **не** нагружает и не измеряет:
  - реальную RTSP-декодировку 120 потоков воркером/MediaMTX (нужны
    настоящие камеры или RTSP-генератор, которых нет в CI/песочнице);
  - инференс детекции лиц / FPS воркера (см. carryover-пункт "аппаратное
    декодирование/инференс на реальном GPU" в REVIEW_LOG.md — тот же
    физический ограничитель: тестовая песочница без камер и GPU);
  - HLS-видеопоток через nginx/MediaMTX (тоже требует реального
    видеопотока на входе).

Для полноценной проверки §26 (120 потоков записи + 2 аналитики, 24 ч)
нужен цикл на реальном железе с эмулированными RTSP-источниками
(например, `ffmpeg -re -stream_loop -1 -i sample.mp4 -f rtsp ...` на 120
портов) — вне возможностей этой песочницы, см. `docs/DEPLOY_CHECKLIST.md`,
раздел 6.

Число виртуальных клиентов — это операторы, а не камеры. На 120 камерах
одновременно смотрящих рабочих мест по-прежнему единицы: §4 даёт гриду
не больше 16 камер на экран.

Запуск
------
    pip install locust
    locust -f loadtest/locustfile.py --headless -u 16 -r 4 -t 5m \
        --host http://localhost:8000 \
        --csv loadtest/results/run1

Веб-UI (интерактивно, с графиками):
    locust -f loadtest/locustfile.py --host http://localhost:8000
    # затем открыть http://localhost:8089

Подробности и целевые метрики — loadtest/README.md.
"""
import os
import random

import requests
from locust import HttpUser, between, events, task

USERNAME = os.environ.get("LOCUST_FW_USERNAME", "admin")
PASSWORD = os.environ.get("LOCUST_FW_PASSWORD", "admin")
# SPEC §16 требует, чтобы нагрузочный сценарий был «пересмотрен под новый
# масштаб (см. §26)», а §26 задаёт этот масштаб буквально: «120 камер в
# режиме записи (remux) + N камер аналитики (по умолчанию 2)». До цикла 27
# здесь стояло 16 — число из удалённой редакции ТЗ, где вся система была на
# 16 камер и все они шли в аналитику.
TARGET_CAMERAS = int(os.environ.get("LOCUST_FW_CAMERAS", "120"))
# SPEC §1: аналитика «только на N выбранных камерах (по умолчанию 2)».
# Остальные заводятся в record_only — это режим по умолчанию по §3, и
# именно такое соотношение (2 из 120) нагружает API так, как боевой объект.
ANALYTICS_CAMERAS = int(os.environ.get("LOCUST_FW_ANALYTICS_CAMERAS", "2"))
# SPEC §4: «Мозаичный режим (грид до 16 камер)... для 120 камер — выбор
# группы/страницы». Один оператор смотрит страницу, а не все 120 сразу.
GRID_PAGE_SIZE = 16

_camera_ids: list[int] = []


def _login(session: requests.Session, base_url: str, username: str, password: str) -> str:
    resp = session.post(
        f"{base_url}/api/auth/login",
        data={"username": username, "password": password},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


@events.test_start.add_listener
def setup_cameras(environment, **kwargs):
    """Один раз перед стартом досеивает камеры до TARGET_CAMERAS —
    большинство целевых окружений (свежая БД в CI/песочнице) начинают с
    нуля, а сценарий §26 про 120 камер, а не про "сколько уже есть".

    Режимы расставляются по ТЗ: первые ANALYTICS_CAMERAS — `analytics`,
    остальные — `record_only` (§1, §3). Это не косметика: аналитические
    камеры единственные, у которых есть ROI, лента распознавания и
    события лиц, и сценарий на 120 камерах `record_only` нагружал бы
    совсем другие запросы, чем боевой объект.

    RTSP-адреса — заведомо недостижимые TEST-NET-1 (RFC 5737), чтобы не
    провоцировать воркер на реальные попытки подключения к посторонним
    хостам."""
    host = environment.host
    if not host or getattr(environment.parsed_options, "worker", False):
        # На worker-узлах распределённого запуска (--worker) сетап уже
        # выполнил master — не дублировать создание камер.
        return
    session = requests.Session()
    try:
        token = _login(session, host, USERNAME, PASSWORD)
    except Exception as exc:  # noqa: BLE001 — цель теста не поднялась, явно сообщаем и не валим import
        print(f"[loadtest] не удалось войти как {USERNAME!r} на {host}: {exc} — "
              f"камеры не досеяны, тест будет работать с тем, что уже есть в БД")
        return
    headers = {"Authorization": f"Bearer {token}"}
    try:
        existing = session.get(f"{host}/api/cameras", headers=headers, timeout=10).json()
    except Exception as exc:  # noqa: BLE001
        print(f"[loadtest] не удалось прочитать список камер: {exc}")
        return
    _camera_ids.extend(c["id"] for c in existing)
    # Уже заведённые камеры в режиме analytics считаем — иначе досев
    # упрётся в лимит `analytics_cameras_max` (по умолчанию 2) и половина
    # запросов на создание вернёт 400.
    analytics_have = sum(1 for c in existing if c.get("mode") == "analytics")

    to_create = TARGET_CAMERAS - len(existing)
    for i in range(max(to_create, 0)):
        idx = len(existing) + i + 1
        # TEST-NET-1 — /24, а камер 120: второй октет двигается вместе с
        # третьим, иначе адреса начали бы повторяться после 254-й.
        octet3, octet4 = divmod(idx, 254)
        mode = "analytics" if analytics_have < ANALYTICS_CAMERAS else "record_only"
        payload = {
            "name": f"LoadTest-Cam-{idx}",
            "rtsp_url": f"rtsp://192.0.{octet3}.{octet4 + 1}:554/main",
            "sub_rtsp_url": f"rtsp://192.0.{octet3}.{octet4 + 1}:554/sub",
            "location": f"loadtest-{idx // 20}",  # группы по 20 — §3 требует группировку
            "mode": mode,
            "enabled": True,
        }
        resp = session.post(f"{host}/api/cameras", json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            _camera_ids.append(resp.json()["id"])
            if mode == "analytics":
                analytics_have += 1
        else:
            print(f"[loadtest] создание камеры {idx} вернуло {resp.status_code}: {resp.text[:200]}")
    print(f"[loadtest] готово: {len(_camera_ids)} камер для сценария "
          f"(цель — {TARGET_CAMERAS}, из них analytics {analytics_have} "
          f"из {ANALYTICS_CAMERAS})")


class CameraViewerUser(HttpUser):
    """Основная роль сценария: оператор, держащий открытой мозаичную
    сетку и дашборд.

    По §4 сетка — «грид до 16 камер», а «для 120 камер — выбор
    группы/страницы». Поэтому оператор опрашивает снепшоты **своей
    страницы из 16**, а не всех 120 подряд: последнее не только не
    соответствует интерфейсу, но и размазывало бы нагрузку ровным слоем
    по всему парку, тогда как в реальности горячих камер — ровно столько,
    сколько открыто на экране."""

    weight = 5
    wait_time = between(1, 3)  # ТЗ: задержка живого просмотра ≤3 сек — опрос в этом диапазоне

    def on_start(self):
        resp = self.client.post(
            "/api/auth/login",
            data={"username": USERNAME, "password": PASSWORD},
            name="/api/auth/login",
        )
        resp.raise_for_status()
        self.token = resp.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}
        # У каждого оператора своя страница сетки — как разные рабочие
        # места смотрят разные группы камер объекта.
        pool = _camera_ids or list(range(1, TARGET_CAMERAS + 1))
        start = random.randrange(0, max(1, len(pool)))
        self.page = (pool + pool)[start:start + GRID_PAGE_SIZE]

    def _camera_id(self):
        return random.choice(self.page) if self.page else 1

    @task(6)
    def snapshot_poll(self):
        """Опрос снепшота камеры — то же, чем живая сетка на фронтенде
        обновляет превью. 404 ожидаем и не считается сбоем сценария: в
        песочнице без реального RTSP-воркера кадров ещё нет (см. область
        теста в docstring файла) — сюда важна задержка ответа API, а не
        код."""
        cam_id = self._camera_id()
        with self.client.get(
            f"/api/cameras/{cam_id}/snapshot",
            params={"token": self.token},
            name="/api/cameras/[id]/snapshot",
            catch_response=True,
        ) as resp:
            if resp.status_code in (200, 404):
                resp.success()

    @task(3)
    def dashboard_kpi(self):
        self.client.get("/api/stats/kpi", headers=self.headers, name="/api/stats/kpi")

    @task(2)
    def cameras_list(self):
        # На 120 камерах этот запрос отдаёт весь парк одним ответом —
        # именно так его зовёт интерфейс, и именно поэтому он должен быть
        # в сценарии: его стоимость растёт с числом камер, в отличие от
        # опроса снепшотов одной страницы.
        self.client.get("/api/cameras", headers=self.headers, name="/api/cameras")

    @task(2)
    def record_layer_status(self):
        """Карточка «Слой записи» — статус всех 120 потоков (§14, §9).

        На боевом объекте это самый тяжёлый из регулярно опрашиваемых
        запросов страницы «Мониторинг»: два агрегата по video_segments за
        сутки (на 120 камерах — ~34 560 строк) плюс чтение Redis. В
        сценарии удалённой редакции его не было вовсе.
        """
        self.client.get("/api/system/record-layer", headers=self.headers,
                        name="/api/system/record-layer")

    @task(1)
    def storage_report(self):
        """Прогноз места (§5, §21): SUM(size_bytes) по всему архиву."""
        self.client.get("/api/system/storage", headers=self.headers,
                        name="/api/system/storage")

    @task(1)
    def dashboard_heatmap(self):
        self.client.get("/api/stats/heatmap", headers=self.headers, name="/api/stats/heatmap")


class OperatorBackofficeUser(HttpUser):
    """Меньшая доля клиентов: работа с архивом/отчётами/аудитом —
    менее частые, но более тяжёлые для БД запросы (ТЗ: поиск по архиву
    ≤5 сек)."""

    weight = 1
    wait_time = between(2, 5)

    def on_start(self):
        resp = self.client.post(
            "/api/auth/login",
            data={"username": USERNAME, "password": PASSWORD},
            name="/api/auth/login",
        )
        resp.raise_for_status()
        self.token = resp.json()["access_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    @task(3)
    def archive_segments(self):
        self.client.get(
            "/api/archive/segments",
            params={"limit": 50},
            headers=self.headers,
            name="/api/archive/segments",
        )

    @task(2)
    def audit_log(self):
        self.client.get("/api/audit", headers=self.headers, name="/api/audit")

    @task(1)
    def report_appearances_csv(self):
        # /api/reports/* — намеренно другой контракт авторизации, чем
        # остальной REST: токен в query, не в заголовке (прямые
        # download-ссылки из браузера, см. reports.py::_check_token). Тот
        # же паттерн, что /api/cameras/[id]/snapshot и /hls/.
        self.client.get(
            "/api/reports/appearances.csv",
            params={"token": self.token},
            name="/api/reports/appearances.csv",
        )
