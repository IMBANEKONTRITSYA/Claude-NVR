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
  выход), а при **зависании** это слагаемое доминирует.

Поэтому вывод скрипта — не одно число, а разложение по классам отказа.

**Что изменилось в цикле 38.** Замер различает два класса отказа, и
раньше второй из них он показывал неверно:

* **падение процесса** — супервизор видит выход, обнаружение ~0;
* **зависание** («сервис жив и не отвечает») — до цикла 38 слагаемое
  брали из интервала healthcheck. Это было ошибкой по существу: Docker
  **не перезапускает контейнер по проваленному healthcheck**, он лишь
  метит его `unhealthy`. Механизма восстановления не существовало, то
  есть настоящий RTO этого класса был **бесконечным**, а скрипт печатал
  «уложились в 5 минут». Теперь механизм есть (сторож живости,
  `backend/app/liveness.py` и `worker/liveness.py`), и слагаемое
  **измеряется**: поднимается настоящий сторож над настоящим
  заблокированным циклом событий, берётся время от блокировки до выхода
  процесса и проверяется код выхода.

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

# Перезапуск контейнера супервизором — единицы секунд; берётся из
# конфигурации, а не меряется (`restart: unless-stopped` в режиме 1 §26,
# `Restart=always` с `RestartSec` в режиме 2).
RESTART_SEC = 5

# --- почему слагаемое «обнаружение зависания» больше не константа ---------
#
# До цикла 38 здесь стояло `DETECT_HANG_SEC = 30 * 3` с комментарием
# «healthcheck бэкенда interval: 30s, retries: 3, зависший сервис
# признаётся мёртвым через 90 с». В этом были неверны **оба** утверждения,
# и вместе они давали замеру ложное дно:
#
# 1. Числа не совпадали с docker-compose.yml: у бэкенда `interval: 15s`,
#    `retries: 5`.
# 2. Куда важнее: **Docker не перезапускает контейнер по проваленному
#    healthcheck.** Он лишь метит его `unhealthy`; на `restart:` это не
#    влияет. То есть «признаётся мёртвым» не приводило ни к чему, и
#    настоящий RTO класса «зависание» был не 90 секунд, а **бесконечность**:
#    сервис не восстанавливался никогда. Замер при этом печатал «уложились
#    в 5 минут» — то есть успокаивал ровно там, где механизма не было
#    вовсе.
#
# С цикла 38 механизм есть — сторож живости (`backend/app/liveness.py`,
# `worker/liveness.py`), — и слагаемое **измеряется**, а не постулируется:
# ниже поднимается настоящий сторож над настоящим заблокированным циклом
# событий, и берётся время от блокировки до выхода процесса.
WATCHDOG_EXIT_CODE = 17


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


# Драйвер для замера обнаружения зависания. Отдельным процессом, потому что
# сторож по срабатывании делает `os._exit()` — внутри процесса замера он
# унёс бы и сам замер.
#
# Блокировка настоящая: `time.sleep` **внутри** корутины держит цикл
# событий намертво, как его держал бы синхронный вызов в обработчике или
# дедлок в C-расширении. Подменять её на «перестать отмечаться» было бы
# замером арифметики порогов, а не механизма.
_HANG_DRIVER = r"""
import asyncio, sys, time
sys.path.insert(0, {backend!r})
from app.liveness import LoopHeartbeat, LoopWatchdog, beat_loop

async def main():
    hb = LoopHeartbeat()
    stop = asyncio.Event()
    asyncio.create_task(beat_loop(hb, stop, interval=0.2))
    await asyncio.sleep(1.0)                  # цикл заведомо жив
    wd = LoopWatchdog(hb, lag_budget_sec={budget}, kill_grace_sec={grace},
                      interval_sec=0.2)
    wd.start()
    # Отметка ровно перед блокировкой: иначе отсчёт отставания начался бы с
    # предыдущей отметки (до 0.2 с раньше), и замер показывал бы
    # обнаружение «быстрее суммы порогов» — артефакт, а не свойство.
    hb.beat()
    print("BLOCK", time.time(), flush=True)
    time.sleep({block})                       # цикл событий встал НАМЕРТВО
    print("SURVIVED", flush=True)             # сюда попадать не должны

asyncio.run(main())
"""


def measure_hang_detection(budget: float, grace: float) -> tuple[float, int]:
    """Секунды от блокировки цикла событий до выхода процесса, и код выхода.

    Меряется настоящий сторож над настоящим заблокированным циклом. Бюджет
    и отсрочка занижены против боевых умолчаний (60 + 60 с) — иначе замер
    шёл бы две минуты; масштабируется линейно, боевое значение считается
    подстановкой умолчаний.
    """
    driver = _HANG_DRIVER.format(backend=BACKEND, budget=budget, grace=grace,
                                 block=budget + grace + 30)
    proc = subprocess.Popen([sys.executable, "-c", driver],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            text=True)
    blocked_at = None
    for line in proc.stdout:
        if line.startswith("BLOCK"):
            blocked_at = time.monotonic()
            break
        if line.startswith("SURVIVED"):
            raise RuntimeError("сторож не сработал: процесс пережил блокировку")
    if blocked_at is None:
        raise RuntimeError("драйвер замера не дошёл до блокировки")
    code = proc.wait(timeout=budget + grace + 60)
    return time.monotonic() - blocked_at, code


def main() -> int:
    # --hang-only: замер одного слагаемого — обнаружения зависания. Без
    # Postgres и Redis, поэтому годится для джобы `perf` в CI, где их нет:
    # так у механизма появляется отслеживание числа от цикла к циклу, а не
    # только разовый замер в песочнице.
    if "--hang-only" in sys.argv[1:]:
        budget, grace = 2.0, 2.0
        detect, code = measure_hang_detection(budget, grace)
        overhead = detect - (budget + grace)
        print(f"обнаружение зависания: {detect:.2f} с при порогах "
              f"{budget + grace:.2f} с (накладной расход {overhead:.2f} с), "
              f"код выхода {code}")
        if code != WATCHDOG_EXIT_CODE:
            print(f"ОШИБКА: ожидался код выхода {WATCHDOG_EXIT_CODE} — сторож не сработал")
            return 1
        return 0

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

    # Обнаружение зависания — замер на заниженных порогах, чтобы прогон шёл
    # секунды, а не две минуты. Проверяется и накладной расход механизма:
    # насколько фактическое обнаружение превышает сумму порогов.
    bench_budget, bench_grace = 2.0, 2.0
    detect_bench, exit_code = measure_hang_detection(bench_budget, bench_grace)
    overhead = detect_bench - (bench_budget + bench_grace)
    from_defaults = (
        liveness_defaults()[0] + liveness_defaults()[1] + max(overhead, 0.0)
    )

    print()
    print("§19 RTO ≤ 5 минут — разложение")
    print(f"  прогрев (замерено, медиана из {runs})           : {warm:6.2f} с   (худший {worst:.2f} с)")
    print(f"  рестарт супервизором (конфигурация)         : {RESTART_SEC:6.2f} с")
    print(f"  обнаружение падения процесса                : {0.0:6.2f} с   (супервизор видит выход)")
    print()
    print("  обнаружение ЗАВИСАНИЯ — сторож живости (замерено)")
    print(f"    пороги замера (бюджет + отсрочка)         : {bench_budget + bench_grace:6.2f} с")
    print(f"    фактически от блокировки до выхода        : {detect_bench:6.2f} с")
    print(f"    накладной расход механизма                : {overhead:6.2f} с")
    print(f"    код выхода                                : {exit_code:6d}   "
          f"({'сторож' if exit_code == WATCHDOG_EXIT_CODE else 'НЕ сторож — проверьте'})")
    print(f"    то же на боевых порогах "
          f"({liveness_defaults()[0]:.0f}+{liveness_defaults()[1]:.0f} с) : {from_defaults:6.2f} с")
    print()
    crash = worst + RESTART_SEC
    hang = worst + RESTART_SEC + from_defaults
    print(f"  RTO при падении процесса : {crash:6.1f} с  "
          f"({'уложились' if crash <= RTO_LIMIT_SEC else 'НЕ уложились'} в {RTO_LIMIT_SEC} с, "
          f"запас {RTO_LIMIT_SEC - crash:.0f} с)")
    print(f"  RTO при зависании        : {hang:6.1f} с  "
          f"({'уложились' if hang <= RTO_LIMIT_SEC else 'НЕ уложились'} в {RTO_LIMIT_SEC} с, "
          f"запас {RTO_LIMIT_SEC - hang:.0f} с)")
    print()
    print("  До цикла 38 строки «RTO при зависании» не существовало по существу:")
    print("  механизма восстановления не было вовсе, а Docker по проваленному")
    print("  healthcheck контейнер не перезапускает — настоящее значение было ∞.")

    if exit_code != WATCHDOG_EXIT_CODE:
        return 1
    return 0 if hang <= RTO_LIMIT_SEC else 1


def liveness_defaults() -> tuple[float, float]:
    """Боевые пороги сторожа — из самого модуля, а не переписанные сюда.

    Копия констант разъехалась бы с кодом ровно так, как разъехались
    константы healthcheck до цикла 38, — и замер снова врал бы.
    """
    sys.path.insert(0, BACKEND)
    from app.liveness import DEFAULT_LAG_BUDGET_SEC, DEFAULT_KILL_GRACE_SEC
    return DEFAULT_LAG_BUDGET_SEC, DEFAULT_KILL_GRACE_SEC


if __name__ == "__main__":
    raise SystemExit(main())
