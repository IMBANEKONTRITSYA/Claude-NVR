#!/usr/bin/env python3
"""Замер §19 «Запись: восстановление потока ≤ 5 секунд после обрыва».

Норматив стоял в §19 рядом с «0 пропущенных сегментов при штатной работе»
и до этого замера **никогда не измерялся**: RTO циклов 38–39 мерил
восстановление *сервиса* (падение процесса — 6.4 с, зависание — 126.5 с),
а это другой отказ. Обрыв потока — самый частый отказ на объекте: камера
ушла в перезагрузку, коммутатор моргнул, PoE просел. Запись при этом не
падает — она **перестаёт писать эту камеру**, и вопрос ровно один:
сколько секунд архива теряется сверх самого обрыва.

**Что именно меряется.** Не длительность обрыва (её задаёт отказ, а не
система), а *добавка* системы: время от момента, когда камера снова
отдаёт поток, до момента, когда запись этой камеры снова растёт на диске.
Это и есть величина, которую §19 ограничивает пятью секундами, и её нельзя
получить из логов: MediaMTX пишет «reconnecting», но не пишет, когда
файл снова начал расти.

**Почему нужен настоящий MediaMTX.** Реконнект — его поведение, не наше.
Воркер заводит путь с `source: rtsp://…` и `sourceOnDemand: no`
(`worker/record_layer.py:path_conf()`), после чего соединением управляет
сервер: он же решает, через сколько повторять. Двойник Control API об этом
не знает ничего — он умеет отвечать на запросы конфигурации, а не терять
и восстанавливать RTSP-сессию. Конфигурация пути берётся из
production-функции, а не переписывается здесь: копия разошлась бы, и замер
мерил бы не то, что поедет на объект.

**Обрыв меряется несколькими длительностями** (по умолчанию 2, 5, 15 и
30 секунд) — и это главное в схеме. §2 и §13 описывают
«автопереподключение с экспоненциальной задержкой»: если задержка растёт,
то короткий обрыв укладывается в норматив, а получасовой — нет, и одно
число про «обрыв» ничего не значило бы. Растёт ли она у MediaMTX на самом
деле — вопрос замера, а не документации.

**Чего замер не покрывает** (перепроверить на сервере, см.
`docs/DEPLOY_CHECKLIST.md`):

* обрыв здесь — исчезновение публикатора на localhost; на объекте это
  пропажа сети, и TCP-таймаут к недоступному хосту добавляет своё;
* камера возвращается мгновенно и с той же точки; настоящая после
  перезагрузки поднимается десятки секунд и начинает новый GOP;
* одна камера, а не полный объект: на 120 камерах реконнекты после
  моргнувшего коммутатора идут пачкой.

**Супервизор восстановления (цикл 43).** Замер цикла 40 показал, что
бюджет §19 недостижим одной логикой медиасервера: его собственная пауза
повторного подключения равна всему бюджету. Поэтому воркер с цикла 43
опрашивает камеру сам и пересоздаёт путь, как только она ответила
(`worker/stream_recovery.py`). Бенчмарк по умолчанию меряет **боевую**
конфигурацию — с супервизором; `--no-supervisor` оставлен, чтобы каждый
цикл видел обе величины рядом и мог отличить «стало быстрее» от «раннер
сегодня быстрее».

Супервизор здесь — не копия его логики, а он сам (`recover_once()` с тем
же планировщиком, что в воркере): копия разошлась бы с боевой и мерила бы
не то, что поедет на объект. Разница только в приводе — здесь его крутит
цикл замера, в воркере отдельная нить.

Запуск (MediaMTX должен лежать рядом либо быть в PATH):

    python perf/bench_recovery.py                # 2, 5, 15, 30 с обрыва
    python perf/bench_recovery.py --no-supervisor # как было до цикла 43
    python perf/bench_recovery.py --outages 2 30 # свои длительности
    python perf/bench_recovery.py --json         # машиночитаемо, для CI
    MEDIAMTX_BIN=/usr/local/bin/mediamtx python perf/bench_recovery.py
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib import error, request

# §19: «Восстановление потока ≤ 5 секунд после обрыва».
RECOVERY_BUDGET_SEC = 5.0

# Длительности обрыва. Смысл набора — увидеть, растёт ли задержка
# повторного подключения (§2, §13 «экспоненциальная задержка»): если да,
# то норматив выполняется не «вообще», а до какой-то длительности обрыва.
DEFAULT_OUTAGES = (2, 5, 15, 30)

WORK = os.environ.get("BENCH_RECOVERY_DIR") or os.path.join(
    tempfile.gettempdir(), "facewatch_recovery_bench")
MEDIAMTX_BIN = (os.environ.get("MEDIAMTX_BIN")
                or shutil.which("mediamtx")
                or os.path.join(WORK, "mediamtx"))
SEGDIR = os.path.join(WORK, "segments")

# Сегмент боевой длительности — 5 минут (SPEC §20); ждать его нельзя.
# Берётся минута: этого хватает, чтобы после обрыва сервер открыл НОВЫЙ
# файл, и замер не зависел от того, дописывается ли старый.
SEGMENT_MIN = 1
# Опрос диска. Мельче нет смысла: сегмент растёт порциями
# `recordPartDuration`, и разрешение замера ограничено ею, а не опросом.
POLL_SEC = 0.05
PART_SEC = 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _conf(api_port: int, rtsp_port: int) -> str:
    return (
        "logLevel: error\n"
        "api: yes\n"
        f"apiAddress: 127.0.0.1:{api_port}\n"
        f"rtspAddress: :{rtsp_port}\n"
        "rtmp: no\nhls: no\nwebrtc: no\nsrt: no\n"
        "pathDefaults:\n"
        f"  recordPartDuration: {PART_SEC}s\n"
        "paths: {}\n"
    )


def _api_post(api: str, path: str, payload: dict) -> None:
    req = request.Request(f"{api}{path}", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"},
                          method="POST")
    with request.urlopen(req, timeout=10):
        pass


def _api_get(api: str, path: str):
    with request.urlopen(f"{api}{path}", timeout=10) as r:
        return json.loads(r.read().decode())


def _worker_path() -> None:
    """`worker/` в `sys.path` — бенчмарк берёт боевые модули, а не копии."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    worker = os.path.join(root, "worker")
    if worker not in sys.path:
        sys.path.insert(0, worker)


class _Supervisor:
    """Привод боевого супервизора для замера.

    Нить `RecoverySupervisor` здесь не поднимается намеренно: замер и так
    крутит цикл с шагом `POLL_SEC`, и второй, независимый от него, сделал
    бы момент пинка невоспроизводимым от прогона к прогону. Логика —
    боевая целиком: и планировщик задержек, и проба, и пересоздание пути.
    """

    def __init__(self, api: str, desired: dict):
        _worker_path()
        from record_layer import MediaMTXClient  # noqa: PLC0415
        from stream_recovery import RecoveryPlanner, recover_once  # noqa: PLC0415

        self.client = MediaMTXClient(api)
        self.desired = desired
        self.planner = RecoveryPlanner()
        self._recover_once = recover_once
        self.kicks = 0

    def poll(self) -> None:
        try:
            runtime = self.client.runtime_paths()
        except Exception:
            return
        stats = self._recover_once(self.client, self.desired, runtime,
                                   self.planner)
        self.kicks += stats["kicked"]


def _production_path_conf(source: str) -> dict:
    """Конфигурация пути **из воркера**, а не её копия.

    Реконнект зависит от того, тянет ли MediaMTX источник сам
    (`sourceOnDemand: no`) — то есть ровно от того, что задаёт эта
    функция. Повторить её здесь значило бы получить право разойтись с
    боевой и не заметить.
    """
    _worker_path()
    from record_layer import path_conf  # noqa: PLC0415

    conf = path_conf(source, segment_duration_min=SEGMENT_MIN,
                     media_root=os.path.dirname(SEGDIR))
    conf["recordPath"] = f"{SEGDIR}/%path_%s"
    return conf


def _publisher(rtsp_port: int) -> subprocess.Popen:
    """ffmpeg в роли камеры: публикует 720p 15 fps в путь `src`."""
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=15",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
         "-g", "15", "-pix_fmt", "yuv420p",
         "-f", "rtsp", "-rtsp_transport", "tcp",
         f"rtsp://127.0.0.1:{rtsp_port}/src"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _written_bytes() -> int:
    """Сколько всего записано в каталог сегментов.

    Именно суммарный размер, а не «появился новый файл»: после короткого
    обрыва MediaMTX может дописывать тот же сегмент, и ожидание нового
    файла засчитало бы восстановлением ротацию, которая случилась бы и
    без обрыва.
    """
    total = 0
    for name in os.listdir(SEGDIR):
        try:
            total += os.path.getsize(os.path.join(SEGDIR, name))
        except OSError:
            pass
    return total


def _wait(pred, timeout: float, message: str, interval: float = POLL_SEC,
          supervisor=None):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if pred():
                return time.monotonic()
        except Exception as exc:
            last = exc
        # Супервизор крутится вместе с ожиданием, а не вместо него: на
        # объекте он работает всё время, включая обрыв, и опрашивать
        # камеру только после её возвращения значило бы мерить схему,
        # которой нет.
        if supervisor is not None:
            supervisor.poll()
        time.sleep(interval)
    raise RuntimeError(f"{message} (последняя ошибка: {last})")


def _wait_growth(timeout: float, message: str, supervisor=None) -> float:
    """Момент, когда запись СНОВА выросла. Возвращает monotonic-время."""
    base = _written_bytes()
    return _wait(lambda: _written_bytes() > base, timeout, message,
                 supervisor=supervisor)


def measure_one(api: str, rtsp_port: int, outage_sec: float,
                settle_sec: float = 6.0, supervisor=None) -> dict:
    """Один цикл: пишем → обрываем на `outage_sec` → возвращаем → ждём рост.

    `settle_sec` — сколько дать записи установиться перед обрывом, чтобы
    мерить восстановление, а не первый выход на режим.
    """
    pub = _publisher(rtsp_port)
    _wait(lambda: _api_get(api, "/v3/paths/get/src").get("ready") is True,
          30, "источник не начал публиковаться")
    _wait_growth(30, "запись не пошла перед обрывом")
    time.sleep(settle_sec)
    _wait_growth(20, "запись не росла перед обрывом")

    # --- обрыв
    pub.terminate()
    try:
        pub.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pub.kill()
    kicks_before = supervisor.kicks if supervisor is not None else 0
    outage_end = time.monotonic() + outage_sec
    while time.monotonic() < outage_end:
        # Во время обрыва супервизор обязан молчать: пинок по молчащей
        # камере ничего не чинит, а конфигурацию рвёт. Число пинков за
        # обрыв выписывается в результат ровно затем, чтобы это было
        # видно числом, а не подразумевалось.
        if supervisor is not None:
            supervisor.poll()
        time.sleep(POLL_SEC)

    # --- камера вернулась. Точка отсчёта — не запуск ffmpeg, а момент,
    # когда сервер снова видит источник опубликованным: время старта
    # процесса и кодирования первого кадра к системе не относится и
    # завысило бы результат.
    kicks_during_outage = ((supervisor.kicks - kicks_before)
                           if supervisor is not None else 0)
    back = _publisher(rtsp_port)
    try:
        t_source = _wait(
            lambda: _api_get(api, "/v3/paths/get/src").get("ready") is True,
            30, "источник не вернулся", supervisor=supervisor)
        t_record = _wait_growth(
            120, "запись не возобновилась за 120 с после возвращения источника",
            supervisor=supervisor)
    finally:
        back.terminate()
        try:
            back.wait(timeout=10)
        except subprocess.TimeoutExpired:
            back.kill()

    recovery = t_record - t_source
    out = {
        "outage_sec": outage_sec,
        "recovery_sec": round(recovery, 2),
        "within_budget": recovery <= RECOVERY_BUDGET_SEC,
    }
    if supervisor is not None:
        out["kicks_during_outage"] = kicks_during_outage
        out["kicks_total"] = supervisor.kicks
    return out


def summarize(runs: list[dict]) -> dict:
    """Сводка по замерам.

    Вердикт выносится по **худшему** из прогонов, а не по среднему и не по
    медиане. Здесь это не вкус: результат зависит от того, на какую фазу
    цикла повторных подключений пришлось возвращение камеры, то есть
    равномерно размазан по интервалу. Медиана такого набора отвечала бы на
    вопрос «сколько обычно», а §19 ограничивает «сколько в худшем случае»
    — и именно худший определяет, сколько архива теряется на обрыве.

    Пустой набор — не «pass»: замера не было, и отсутствие данных не
    является выполнением норматива.
    """
    values = [r["recovery_sec"] for r in runs]
    worst = max(values) if values else None
    return {
        "budget_sec": RECOVERY_BUDGET_SEC,
        "runs": runs,
        "worst_recovery_sec": worst,
        "best_recovery_sec": min(values) if values else None,
        "verdict": ("pass" if worst is not None and worst <= RECOVERY_BUDGET_SEC
                    else "fail"),
    }


def run(outages, supervisor_on: bool = True) -> dict:
    if not os.path.exists(MEDIAMTX_BIN):
        return {"skipped": f"нет бинарника MediaMTX ({MEDIAMTX_BIN})"}
    if not shutil.which("ffmpeg"):
        return {"skipped": "нет ffmpeg"}

    shutil.rmtree(SEGDIR, ignore_errors=True)
    os.makedirs(SEGDIR, exist_ok=True)
    api_port, rtsp_port = _free_port(), _free_port()
    conf_path = os.path.join(WORK, "mediamtx_recovery.yml")
    with open(conf_path, "w") as fh:
        fh.write(_conf(api_port, rtsp_port))
    api = f"http://127.0.0.1:{api_port}"

    # cwd — рабочий каталог замера, а не корень репозитория: MediaMTX
    # генерирует рядом с собой самоподписанные `auto.crt`/`auto.key`, и
    # запуск из корня оставлял их в дереве проекта после каждого прогона.
    mtx = subprocess.Popen([MEDIAMTX_BIN, conf_path], cwd=WORK,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    results = []
    try:
        _wait(lambda: _api_get(api, "/v3/config/paths/list") is not None,
              20, "Control API не поднялся", interval=0.3)
        # Путь-источник (камера) и записывающий путь, который его ТЯНЕТ, —
        # как заводит настоящую камеру воркер.
        _api_post(api, "/v3/config/paths/add/src",
                  {"source": "publisher", "record": False})
        cam_conf = _production_path_conf(f"rtsp://127.0.0.1:{rtsp_port}/src")
        _api_post(api, "/v3/config/paths/add/cam1", cam_conf)

        supervisor = _Supervisor(api, {"cam1": cam_conf}) if supervisor_on else None
        for outage in outages:
            results.append(measure_one(api, rtsp_port, float(outage),
                                       supervisor=supervisor))
    finally:
        mtx.terminate()
        try:
            mtx.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mtx.kill()

    out = summarize(results)
    out["supervisor"] = supervisor_on
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--outages", type=float, nargs="+", default=list(DEFAULT_OUTAGES),
                    help="длительности обрыва, с")
    ap.add_argument("--no-supervisor", dest="supervisor", action="store_false",
                    help="без супервизора восстановления — поведение до цикла 43")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    out = run(args.outages, supervisor_on=args.supervisor)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if "skipped" in out:
        print(f"пропущено: {out['skipped']}")
        return 0
    print(f"§19 восстановление потока после обрыва (бюджет {out['budget_sec']} с), "
          f"супервизор: {'включён' if out['supervisor'] else 'выключен'}")
    print(f"{'обрыв, с':>10} {'восстановление, с':>20} {'в бюджете':>12} {'пинков':>8}")
    for r in out["runs"]:
        print(f"{r['outage_sec']:>10.0f} {r['recovery_sec']:>20.2f} "
              f"{'да' if r['within_budget'] else 'НЕТ':>12} "
              f"{r.get('kicks_during_outage', '—'):>8}")
    print(f"лучшее: {out['best_recovery_sec']} с, "
          f"худшее: {out['worst_recovery_sec']} с — {out['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
