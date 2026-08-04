"""Нагрузочный тест backend/API FaceWatch.

SPEC.md §14 ("CI/CD и DevOps"): "Нагрузочное тестирование: сценарий на
16 камер, проверка стабильности FPS и задержек". До этого файла раздел
не был закрыт ни в одном из 12 предыдущих циклов аудита (см.
docs/reviews/REVIEW_LOG.md) — не было ни locustfile, ни какого-либо
иного нагрузочного сценария в репозитории.

Область теста и её честные границы
-----------------------------------
Этот сценарий нагружает HTTP/WS **API-слой backend'а** запросами,
которые реально шлёт браузер при работе с 16 камерами одновременно:
опрос снепшотов живой сетки, дашборд KPI, просмотр архива, экспорт
отчётов, журнал аудита. Это ближайший testable-прокси для "стабильности
и задержек" без физических IP-камер.

Он **не** нагружает и не измеряет:
  - реальную RTSP-декодировку 16 потоков воркером (нужны настоящие
    камеры или RTSP-генератор, которых нет в CI/песочнице);
  - инференс детекции лиц / FPS воркера (см. carryover-пункт "аппаратное
    декодирование/инференс на реальном GPU" в REVIEW_LOG.md — тот же
    физический ограничитель: тестовая песочница без камер и GPU);
  - HLS-видеопоток через nginx/MediaMTX (тоже требует реального
    видеопотока на входе).

Для полноценной проверки FPS/задержки живого видео нужен цикл на
реальном железе с 16 подключёнными или эмулированными RTSP-источниками
(например, `ffmpeg -re -stream_loop -1 -i sample.mp4 -f rtsp ...`
на 16 портов) — вне возможностей этой песочницы, аналогично
GPU-carryover пункту.

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
# SPEC.md §14 буквально требует "сценарий на 16 камер" — по умолчанию
# ровно столько, независимо от того, сколько камер уже есть в БД цели.
TARGET_CAMERAS = int(os.environ.get("LOCUST_FW_CAMERAS", "16"))

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
    """Один раз перед стартом теста досеивает камеры до TARGET_CAMERAS —
    большинство целевых окружений (свежая БД в CI/песочнице) начинают с
    нуля, а сценарий SPEC явно про 16 камер, а не про "сколько уже есть".
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
    to_create = TARGET_CAMERAS - len(existing)
    for i in range(max(to_create, 0)):
        idx = len(existing) + i + 1
        payload = {
            "name": f"LoadTest-Cam-{idx}",
            "rtsp_url": f"rtsp://192.0.2.{idx}:554/main",
            "sub_rtsp_url": f"rtsp://192.0.2.{idx}:554/sub",
            "location": "loadtest",
            "enabled": True,
        }
        resp = session.post(f"{host}/api/cameras", json=payload, headers=headers, timeout=10)
        if resp.status_code == 200:
            _camera_ids.append(resp.json()["id"])
        else:
            print(f"[loadtest] создание камеры {idx} вернуло {resp.status_code}: {resp.text[:200]}")
    print(f"[loadtest] готово: {len(_camera_ids)} камер для сценария (цель — {TARGET_CAMERAS})")


class CameraViewerUser(HttpUser):
    """Основная роль сценария: оператор, держащий открытой мозаичную
    сетку (до 16 камер) + дашборд. Большая часть из 16 симулированных
    клиентов при `-u 16` — этого типа."""

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

    def _camera_id(self):
        if _camera_ids:
            return random.choice(_camera_ids)
        return random.randint(1, TARGET_CAMERAS)

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
        self.client.get("/api/cameras", headers=self.headers, name="/api/cameras")

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
