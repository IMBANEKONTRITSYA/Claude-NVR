"""Статус потоков слоя записи и алерты по нему (SPEC §14, §9).

Чистая логика на stdlib — без `importorskip` и без БД, чтобы проверка шла
и в CI-джобе воркера, которая не ставит cv2/insightface (урок цикла 16).

**Форма ответа здесь — снятая с работающего сервера, а не выписанная из
схемы.** До цикла 43 фикстуры этого набора собирались из `openapi.yaml`
v1.20.0 (`online`, `inboundBytes`) — оба поля в схеме есть, оба непусты,
и набор был зелёным. На настоящем MediaMTX при этом `online` у пути со
статическим источником **не падал на обрыве никогда**, то есть потеря
потока не показывалась и не алертилась вообще, а `inboundBytes` в
закреплённой v1.16.0 отсутствовал и «Принято» всегда равнялось нулю.
Схема не различает «поле есть» и «поле означает то, что мы думаем», —
поэтому `LIVE`/`DOWN` ниже воспроизводят ответы обеих версий целиком, а
проверка семантики на живом сервере вынесена в
`test_record_status_live.py`.
"""
from record_status import (OFFLINE, ONLINE, UNKNOWN, newly_lost,
                           newly_restored, path_live, segment_gaps,
                           stream_states, summarize)

CAMS = [(1, "Проходная"), (2, "Склад"), (3, "Парковка")]


def _rt(**paths):
    """Ответ `/v3/paths/list` в форме, которую отдаёт `runtime_paths()`."""
    return {name: dict(name=name, **fields) for name, fields in paths.items()}


def LIVE(bytes_received: int = 1, frames_in_error: int = 0) -> dict:
    """Путь со статическим источником, по которому ИДЁТ поток — v1.20.0.

    `online: True` здесь не признак жизни, а фон: он такой же и в `DOWN`.
    """
    return {"ready": True, "readyTime": "2026-08-06T07:00:00Z",
            "available": True, "availableTime": "2026-08-06T07:00:00Z",
            "online": True, "onlineTime": "2026-08-06T07:00:00Z",
            "inboundBytes": bytes_received, "bytesReceived": bytes_received,
            "inboundFramesInError": frames_in_error}


def DOWN(bytes_received: int = 0) -> dict:
    """Тот же путь во время обрыва — снято с v1.16.0 и v1.20.0.

    Главное здесь `online: True`: сервер отдаёт его и через 2, и через 6,
    и через 12 секунд после исчезновения камеры, потому что путь заведён и
    сервер продолжает пытаться. Живость видна только по
    `available`/`ready`.
    """
    return {"ready": False, "readyTime": None,
            "available": False, "availableTime": None,
            "online": True, "onlineTime": "2026-08-06T07:00:00Z",
            "inboundBytes": bytes_received, "bytesReceived": bytes_received,
            "inboundFramesInError": 0}


def V1_16(live: bool, bytes_received: int = 0) -> dict:
    """Ответ **закреплённой в docker-compose.yml** v1.16.0: в ней нет ни
    `inboundBytes`, ни `inboundFramesInError` — они появились позже."""
    return {"ready": live, "readyTime": "2026-08-06T07:00:00Z" if live else None,
            "available": live,
            "availableTime": "2026-08-06T07:00:00Z" if live else None,
            "online": True, "onlineTime": "2026-08-06T07:00:00Z",
            "bytesReceived": bytes_received, "bytesSent": 0}


# --- состояние потоков ----------------------------------------------------

def test_online_and_offline_streams_are_distinguished():
    runtime = _rt(cam1=LIVE(12345), cam2=DOWN())
    runtime["cam2"]["inboundFramesInError"] = 7
    st = stream_states(CAMS, runtime)

    assert st[1]["status"] == ONLINE
    assert st[1]["inbound_bytes"] == 12345
    assert st[1]["online_since"] == "2026-08-06T07:00:00Z"
    assert st[2]["status"] == OFFLINE
    assert st[2]["frames_in_error"] == 7


def test_camera_without_path_is_offline_not_missing():
    """Камера включена, а пути в MediaMTX нет — запись по ней не идёт.

    Пропустить её в отчёте значило бы показать администратору 119 потоков
    из 120 и ни одного признака, что со сто двадцатым что-то не так.
    """
    st = stream_states(CAMS, _rt(cam1=LIVE(1)))
    assert st[3]["status"] == OFFLINE
    assert set(st) == {1, 2, 3}


def test_unreachable_control_api_is_unknown_not_offline():
    """MediaMTX недоступен целиком — это «неизвестно», а не «120 потерь».

    Показать 120 потерянных камер вместо «состояние неизвестно» значило бы
    спровоцировать реакцию на аварию, которой нет.
    """
    st = stream_states(CAMS, None)
    assert {s["status"] for s in st.values()} == {UNKNOWN}


def test_summary_counts_streams_for_dashboard():
    """SPEC §9: «активные потоки (из 120)»."""
    runtime = _rt(cam1=LIVE(100, frames_in_error=1), cam2=DOWN())
    s = summarize(stream_states(CAMS, runtime))
    assert s["streams_total"] == 3
    assert s["streams_online"] == 1
    assert s["streams_offline"] == 2       # cam2 offline + cam3 без пути
    assert s["streams_unknown"] == 0
    assert s["inbound_bytes"] == 100
    assert s["frames_in_error"] == 1


def test_summary_separates_unknown_from_offline():
    """Позитивный контроль к предыдущему: `unknown` не считается потерей."""
    s = summarize(stream_states(CAMS, None))
    assert (s["streams_unknown"], s["streams_offline"], s["streams_online"]) == (3, 0, 0)


def test_lost_stream_is_offline_even_though_the_server_calls_it_online():
    """Ровно тот дефект, который этот набор раньше **закреплял**.

    Прежний тест требовал читать `online` и считать путь с одним `ready`
    потерянным. На настоящем сервере всё наоборот: у пути со статическим
    источником (а других слой записи не заводит) `online` остаётся `true`
    и через 12 секунд после пропажи камеры. Пока код читал его, потеря
    потока не показывалась на мониторинге, не меняла статус камеры в БД и
    не давала алерта §14 — ни разу, ни на одной камере.
    """
    st = stream_states([(1, "Проходная")], _rt(cam1=DOWN()))
    assert st[1]["status"] == OFFLINE
    assert st[1]["online_since"] is None, (
        "«в сети с» обязано браться из availableTime/readyTime: onlineTime "
        "у статического источника проставляется один раз и переживает обрыв")


def test_pinned_version_without_the_new_field_names_still_reports_bytes():
    """v1.16.0 — та версия, что закреплена в docker-compose.yml и поедет на
    объект, — не отдаёт `inboundBytes` вовсе. Пока код читал только его,
    «Принято» на мониторинге показывало 0 МБ при любом трафике."""
    st = stream_states([(1, "Проходная")], _rt(cam1=V1_16(True, 777)))
    assert st[1]["status"] == ONLINE
    assert st[1]["inbound_bytes"] == 777
    # `inboundFramesInError` в v1.16.0 нет — это ноль, а не отказ разбора.
    assert st[1]["frames_in_error"] == 0

    down = stream_states([(1, "Проходная")], _rt(cam1=V1_16(False)))
    assert down[1]["status"] == OFFLINE


def test_path_live_prefers_the_field_that_survives_both_versions():
    """`available` не помечен deprecated ни в v1.16.0, ни в v1.20.0 и в
    обеих падает на обрыве; `ready` — тот же смысл, но в v1.20.0 уже
    deprecated, поэтому он только откат."""
    assert path_live({"available": True, "ready": False}) is True
    assert path_live({"available": False, "ready": True}) is False
    assert path_live({"ready": True}) is True          # сборка без `available`
    assert path_live({"online": True}) is False        # одного `online` мало
    assert path_live(None) is False


# --- переходы: потеря и восстановление (SPEC §14) -------------------------

def test_loss_is_reported_once_on_transition():
    """Алерт — на переход, а не на факт «сейчас offline».

    Иначе физически выключенная камера слала бы алерт каждые десять секунд
    до починки, и алертинг перестали бы читать.
    """
    online = stream_states(CAMS, _rt(cam1=LIVE(1)))
    offline = stream_states(CAMS, _rt(cam1=DOWN(1)))

    prev = {cid: s["status"] for cid, s in online.items()}
    assert newly_lost(prev, offline) == [1]

    # Второй проход в том же состоянии — молчание.
    prev = {cid: s["status"] for cid, s in offline.items()}
    assert newly_lost(prev, offline) == []


def test_first_pass_reports_nothing():
    """На первом проходе предыдущего состояния нет — алертить не о чем."""
    st = stream_states(CAMS, _rt(cam1=DOWN(0)))
    assert newly_lost(None, st) == []
    assert newly_restored(None, st) == []


def test_transition_from_unknown_is_not_a_loss():
    """Control API молчал — что было с потоком, неизвестно.

    Считать выход из `unknown` потерей значило бы слать пачку из 120
    алертов на каждый рестарт MediaMTX.
    """
    offline = stream_states(CAMS, _rt(cam1=DOWN(0)))
    assert newly_lost({1: UNKNOWN, 2: UNKNOWN, 3: UNKNOWN}, offline) == []


def test_restore_is_reported_on_transition():
    online = stream_states(CAMS, _rt(cam1=LIVE(5)))
    assert newly_restored({1: OFFLINE, 2: OFFLINE, 3: OFFLINE}, online) == [1]


# --- пропуск записи сегмента (SPEC §14) -----------------------------------

def test_segment_gap_detected_when_recording_stalls():
    """Поток онлайн, а сегменты не пишутся — отдельная авария.

    Ровно тот случай, ради которого SPEC §14 отдельной строкой требует
    алерт «пропуск записи сегмента»: нет места, нет прав, сбой записи в
    MediaMTX — поток при этом продолжает считаться живым.
    """
    st = stream_states([(1, "Проходная")], _rt(cam1=LIVE(1)))
    now = 10_000.0
    # Сегмент 5 минут → порог 5 × 60 × 2.5 = 750 с.
    assert segment_gaps({1: now - 800}, st, now, segment_minutes=5) == [1]
    assert segment_gaps({1: now - 700}, st, now, segment_minutes=5) == []


def test_no_segment_gap_alert_for_offline_stream():
    """На потерянном потоке отсутствие сегментов — следствие уже
    сообщённой потери; второй алерт о том же только зашумляет."""
    st = stream_states([(1, "Проходная")], _rt(cam1=DOWN(0)))
    assert segment_gaps({1: 0.0}, st, 10_000.0, segment_minutes=5) == []


def test_new_camera_without_segments_is_not_a_gap():
    """У только что добавленной камеры первый сегмент ещё пишется."""
    st = stream_states([(1, "Проходная")], _rt(cam1=LIVE(1)))
    assert segment_gaps({}, st, 10_000.0, segment_minutes=5) == []


def test_gap_threshold_scales_with_segment_length():
    """Порог считается от настроенной длины сегмента, а не зашит числом:
    на 10-минутных сегментах 800 секунд молчания — норма."""
    st = stream_states([(1, "Проходная")], _rt(cam1=LIVE(1)))
    now = 10_000.0
    assert segment_gaps({1: now - 800}, st, now, segment_minutes=10) == []
    assert segment_gaps({1: now - 1600}, st, now, segment_minutes=10) == [1]
