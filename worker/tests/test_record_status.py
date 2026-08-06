"""Статус потоков слоя записи и алерты по нему (SPEC §14, §9).

Чистая логика на stdlib — без `importorskip` и без БД, чтобы проверка шла
и в CI-джобе воркера, которая не ставит cv2/insightface (урок цикла 16).

Форма рантайм-ответа взята из `api/openapi.yaml` MediaMTX v1.20.0, а не из
памяти: актуальные поля — `online`, `onlineTime`, `inboundBytes`,
`inboundFramesInError`; `ready`/`readyTime`/`bytesReceived` в апстриме
помечены deprecated. Тест `test_deprecated_upstream_fields_are_not_used`
стережёт, чтобы код не съехал обратно на них.
"""
from record_status import (OFFLINE, ONLINE, UNKNOWN, newly_lost,
                           newly_restored, segment_gaps, stream_states,
                           summarize)

CAMS = [(1, "Проходная"), (2, "Склад"), (3, "Парковка")]


def _rt(**paths):
    """Ответ `/v3/paths/list` в форме, которую отдаёт `runtime_paths()`."""
    return {name: dict(name=name, **fields) for name, fields in paths.items()}


# --- состояние потоков ----------------------------------------------------

def test_online_and_offline_streams_are_distinguished():
    runtime = _rt(
        cam1={"online": True, "onlineTime": "2026-08-06T07:00:00Z",
              "inboundBytes": 12345, "inboundFramesInError": 0},
        cam2={"online": False, "onlineTime": None,
              "inboundBytes": 0, "inboundFramesInError": 7},
    )
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
    st = stream_states(CAMS, _rt(cam1={"online": True, "inboundBytes": 1}))
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
    runtime = _rt(
        cam1={"online": True, "inboundBytes": 100, "inboundFramesInError": 1},
        cam2={"online": False, "inboundBytes": 0, "inboundFramesInError": 0},
    )
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


def test_deprecated_upstream_fields_are_not_used():
    """Код обязан читать `online`, а не устаревший `ready`.

    В MediaMTX v1.20.0 `ready`/`readyTime`/`bytesReceived` помечены
    deprecated. Путь, где стоит только `ready`, обязан читаться как
    offline — иначе после их удаления в апстриме все 120 потоков молча
    станут «онлайн» и мониторинг перестанет что-либо значить.
    """
    runtime = _rt(cam1={"ready": True, "readyTime": "2026-08-06T07:00:00Z",
                        "bytesReceived": 999})
    assert stream_states([(1, "Проходная")], runtime)[1]["status"] == OFFLINE


# --- переходы: потеря и восстановление (SPEC §14) -------------------------

def test_loss_is_reported_once_on_transition():
    """Алерт — на переход, а не на факт «сейчас offline».

    Иначе физически выключенная камера слала бы алерт каждые десять секунд
    до починки, и алертинг перестали бы читать.
    """
    online = stream_states(CAMS, _rt(cam1={"online": True, "inboundBytes": 1}))
    offline = stream_states(CAMS, _rt(cam1={"online": False, "inboundBytes": 1}))

    prev = {cid: s["status"] for cid, s in online.items()}
    assert newly_lost(prev, offline) == [1]

    # Второй проход в том же состоянии — молчание.
    prev = {cid: s["status"] for cid, s in offline.items()}
    assert newly_lost(prev, offline) == []


def test_first_pass_reports_nothing():
    """На первом проходе предыдущего состояния нет — алертить не о чем."""
    st = stream_states(CAMS, _rt(cam1={"online": False, "inboundBytes": 0}))
    assert newly_lost(None, st) == []
    assert newly_restored(None, st) == []


def test_transition_from_unknown_is_not_a_loss():
    """Control API молчал — что было с потоком, неизвестно.

    Считать выход из `unknown` потерей значило бы слать пачку из 120
    алертов на каждый рестарт MediaMTX.
    """
    offline = stream_states(CAMS, _rt(cam1={"online": False, "inboundBytes": 0}))
    assert newly_lost({1: UNKNOWN, 2: UNKNOWN, 3: UNKNOWN}, offline) == []


def test_restore_is_reported_on_transition():
    online = stream_states(CAMS, _rt(cam1={"online": True, "inboundBytes": 5}))
    assert newly_restored({1: OFFLINE, 2: OFFLINE, 3: OFFLINE}, online) == [1]


# --- пропуск записи сегмента (SPEC §14) -----------------------------------

def test_segment_gap_detected_when_recording_stalls():
    """Поток онлайн, а сегменты не пишутся — отдельная авария.

    Ровно тот случай, ради которого SPEC §14 отдельной строкой требует
    алерт «пропуск записи сегмента»: нет места, нет прав, сбой записи в
    MediaMTX — поток при этом продолжает считаться живым.
    """
    st = stream_states([(1, "Проходная")], _rt(cam1={"online": True, "inboundBytes": 1}))
    now = 10_000.0
    # Сегмент 5 минут → порог 5 × 60 × 2.5 = 750 с.
    assert segment_gaps({1: now - 800}, st, now, segment_minutes=5) == [1]
    assert segment_gaps({1: now - 700}, st, now, segment_minutes=5) == []


def test_no_segment_gap_alert_for_offline_stream():
    """На потерянном потоке отсутствие сегментов — следствие уже
    сообщённой потери; второй алерт о том же только зашумляет."""
    st = stream_states([(1, "Проходная")], _rt(cam1={"online": False, "inboundBytes": 0}))
    assert segment_gaps({1: 0.0}, st, 10_000.0, segment_minutes=5) == []


def test_new_camera_without_segments_is_not_a_gap():
    """У только что добавленной камеры первый сегмент ещё пишется."""
    st = stream_states([(1, "Проходная")], _rt(cam1={"online": True, "inboundBytes": 1}))
    assert segment_gaps({}, st, 10_000.0, segment_minutes=5) == []


def test_gap_threshold_scales_with_segment_length():
    """Порог считается от настроенной длины сегмента, а не зашит числом:
    на 10-минутных сегментах 800 секунд молчания — норма."""
    st = stream_states([(1, "Проходная")], _rt(cam1={"online": True, "inboundBytes": 1}))
    now = 10_000.0
    assert segment_gaps({1: now - 800}, st, now, segment_minutes=10) == []
    assert segment_gaps({1: now - 1600}, st, now, segment_minutes=10) == [1]
