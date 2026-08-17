#!/usr/bin/env python3
"""Замер §19 «RTO ≤ 5 минут» и §13 «авторестарт сервисов при падении».

**Зачем.** §19 задаёт RTO как норматив, и до цикла 34 это был единственный
числовой норматив ТЗ, к которому ни разу не подступались: остальные либо
измерены (детекция 8.4 FPS/канал, remux 0.0174 ядра/камера, поиск по
архиву), либо явно отложены на сервер. Норматив, ни разу не измеренный,
не отличим от невыполненного.

**Что именно меряется.** RTO — «время от падения до восстановления
обслуживания», то есть от смерти процесса до первого успешного
`GET /api/health`. Раскладывается на три слагаемых, и мерить их надо
порознь: у них разные причины роста.

    RTO = обнаружение + рестарт + прогрев
          └ политика     └ старт    └ миграции, пул БД, сид настроек

* **прогрев** (замеряется здесь) — старт процесса до готовности
  обслуживать: `lifespan` создаёт таблицы, гоняет идемпотентные миграции,
  сидит admin и настройки, поднимает пул asyncpg;
* **рестарт** — время, которое тратит супервизор на повторный запуск. В
  режиме 1 (§26) это `restart: unless-stopped` docker-compose, в режиме 2 —
  `Restart=always` systemd с `RestartSec` (по умолчанию 100 мс);
* **обнаружение** — сколько супервизор считает сервис живым после
  фактической смерти. При падении процесса это ~0 (супервизор видит
  выход), а вот при **зависании** — это интервал healthcheck и число
  повторов, и именно это слагаемое доминирует.

Поэтому вывод скрипта — не одно число, а разложение: прогрев измеряется,
остальное берётся из конфигурации того режима, который проверяется.

**Чего замер не покрывает** (перепроверить на сервере, см.
`docs/DEPLOY_CHECKLIST.md`):

* база песочницы пуста, а на объекте `video_segments` — сотни тысяч
  строк; миграции идемпотентны и не переписывают данные, но проверка
  наличия колонок идёт по непустой схеме;
* не меряется восстановление MediaMTX и время, за которое он заново
  дотягивается до 120 камер (§19 отдельно требует ≤ 5 с на поток);
* Postgres и Redis считаются живыми: их собственный рестарт в RTO здесь
  не входит.

Запуск:

    python perf/bench_rto.py            # 5 прогонов
    python perf/bench_rto.py 10
"""
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
PORT = int(os.environ.get("BENCH_RTO_PORT", "8931"))
HEALTH = f"http://127.0.0.1:{PORT}/api/health"

# Норматив §19.
RTO_LIMIT_SEC = 5 * 60

# Слагаемые, которые задаёт конфигурация, а не код. Значения — из
# docker-compose.yml (режим 1 §26): healthcheck бэкенда `interval: 30s`,
# `retries: 3`, то есть зависший (не упавший) сервис признаётся мёртвым
# через 30 × 3 = 90 с; перезапуск контейнера — единицы секунд.
DETECT_HANG_SEC = 30 * 3
RESTART_SEC = 5


def health_ok() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def one_run() -> float:
    """Секунды от запуска процесса до первого успешного /api/health."""
    env = dict(os.environ)
    env.setdefault("ALLOW_INSECURE_DEFAULT_SECRETS", "true")
    env.setdefault("FACEWATCH_DISABLE_REPORT_SCHEDULER", "true")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(PORT), "--log-level", "warning"],
        cwd=BACKEND, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    started = time.monotonic()
    try:
        while time.monotonic() - started < 120:
            if health_ok():
                return time.monotonic() - started
            if proc.poll() is not None:
                raise RuntimeError(f"процесс бэкенда умер с кодом {proc.returncode}")
            time.sleep(0.05)
        raise RuntimeError("бэкенд не стал здоровым за 120 с")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    if health_ok():
        print(f"порт {PORT} уже занят здоровым сервисом — задайте BENCH_RTO_PORT")
        return 2

    warmups = []
    for i in range(runs):
        t = one_run()
        warmups.append(t)
        print(f"  прогон {i + 1}/{runs}: прогрев {t:.2f} с")
        # Пауза, чтобы порт освободился и пул asyncpg закрылся до следующего.
        time.sleep(1.0)

    warm = statistics.median(warmups)
    worst = max(warmups)
    print()
    print("§19 RTO ≤ 5 минут — разложение")
    print(f"  прогрев (замерено, медиана из {runs})   : {warm:6.2f} с   (худший {worst:.2f} с)")
    print(f"  рестарт супервизором (конфигурация)     : {RESTART_SEC:6.2f} с")
    print(f"  обнаружение падения процесса            : {0.0:6.2f} с")
    print(f"  обнаружение ЗАВИСАНИЯ (healthcheck)     : {DETECT_HANG_SEC:6.2f} с")
    print()
    crash = worst + RESTART_SEC
    hang = worst + RESTART_SEC + DETECT_HANG_SEC
    print(f"  RTO при падении процесса : {crash:6.1f} с  "
          f"({'уложились' if crash <= RTO_LIMIT_SEC else 'НЕ уложились'} в {RTO_LIMIT_SEC} с, "
          f"запас {RTO_LIMIT_SEC - crash:.0f} с)")
    print(f"  RTO при зависании        : {hang:6.1f} с  "
          f"({'уложились' if hang <= RTO_LIMIT_SEC else 'НЕ уложились'} в {RTO_LIMIT_SEC} с, "
          f"запас {RTO_LIMIT_SEC - hang:.0f} с)")
    return 0 if hang <= RTO_LIMIT_SEC else 1


if __name__ == "__main__":
    raise SystemExit(main())
