"""Состояние потоков слоя записи (SPEC §14, §20).

SPEC §14 требует показывать «статус каждого RTSP-потока слоя записи
(120 шт.)» и слать алерты на потерю потока и на **пропуск записи
сегмента**. Данные для этого отдаёт Control API MediaMTX
(`/v3/paths/list`), но до этого модуля они никуда не доезжали:
`MediaMTXClient.runtime_paths()` был написан и не использовался.

Модуль — чистая логика над уже полученными словарями: ни сети, ни БД, ни
Redis. Причина та же, что у `storage.py`: решение «этот поток потерян» и
решение «у этой камеры пропущен сегмент» должны быть проверяемы без
поднятия MediaMTX, которого в песочнице нет.

Имена полей рантайма сверены с `api/openapi.yaml` MediaMTX v1.20.0:
актуальные — `online`, `onlineTime`, `inboundBytes`,
`inboundFramesInError`; `ready`, `readyTime`, `bytesReceived`,
`bytesSent` помечены в апстриме **deprecated** и здесь не используются
(цикл 24 ссылался на `readyTime` — это устаревшее поле).
"""
from __future__ import annotations

# Сколько секунд отсутствия новых сегментов считать пропуском записи.
# Сегмент — 5–10 минут (SPEC §20), поэтому берётся длительность сегмента
# плюс запас: MediaMTX закрывает файл по границе, и мгновенная проверка
# «прошло ровно 5 минут» давала бы ложный алерт на каждом обороте.
SEGMENT_GAP_FACTOR = 2.5

# Статусы потока записи. `unknown` — не «offline»: MediaMTX может быть
# недоступен целиком (рестарт, сеть), и показывать в интерфейсе 120
# «потерянных» камер вместо «состояние неизвестно» значило бы
# спровоцировать реакцию на аварию, которой нет.
ONLINE, OFFLINE, UNKNOWN = "online", "offline", "unknown"


def path_name(camera_id: int) -> str:
    """Дублирует `record_layer.path_name` намеренно: этот модуль не должен
    зависеть от клиента API, чтобы оставаться проверяемым в одиночку."""
    return f"cam{camera_id}"


def stream_states(cameras, runtime: dict[str, dict] | None) -> dict[int, dict]:
    """Состояние потока записи по каждой камере.

    `cameras` — последовательность `(camera_id, name)`; `runtime` — ответ
    `runtime_paths()` либо `None`, если Control API недоступен.

    Возвращает словарь по `camera_id`, а не список: потребители (алерты,
    сравнение с предыдущим проходом, отдача в API) ищут по камере.
    """
    out: dict[int, dict] = {}
    for cam_id, name in cameras:
        if runtime is None:
            out[cam_id] = {"camera_id": cam_id, "name": name, "status": UNKNOWN,
                           "inbound_bytes": 0, "online_since": None,
                           "frames_in_error": 0}
            continue
        p = runtime.get(path_name(cam_id))
        if not p:
            # Путь не заведён вовсе: камера включена, а слой записи её ещё
            # не подхватил (или синхронизация упала). Это именно offline —
            # запись по ней не идёт, и знать об этом надо.
            out[cam_id] = {"camera_id": cam_id, "name": name, "status": OFFLINE,
                           "inbound_bytes": 0, "online_since": None,
                           "frames_in_error": 0}
            continue
        out[cam_id] = {
            "camera_id": cam_id,
            "name": name,
            "status": ONLINE if p.get("online") else OFFLINE,
            "inbound_bytes": int(p.get("inboundBytes") or 0),
            "online_since": p.get("onlineTime"),
            "frames_in_error": int(p.get("inboundFramesInError") or 0),
        }
    return out


def summarize(states: dict[int, dict]) -> dict:
    """Сводка слоя записи для дашборда (SPEC §9: «активные потоки (из 120)»)."""
    total = len(states)
    online = sum(1 for s in states.values() if s["status"] == ONLINE)
    unknown = sum(1 for s in states.values() if s["status"] == UNKNOWN)
    return {
        "streams_total": total,
        "streams_online": online,
        "streams_offline": total - online - unknown,
        "streams_unknown": unknown,
        "inbound_bytes": sum(s["inbound_bytes"] for s in states.values()),
        "frames_in_error": sum(s["frames_in_error"] for s in states.values()),
    }


def newly_lost(previous: dict[int, str] | None, current: dict[int, dict]) -> list[int]:
    """Камеры, чей поток записи только что потерян (SPEC §14: алерт и аудит).

    Именно **переход** online → offline, а не «сейчас offline»: иначе
    камера, выключенная физически, слала бы алерт каждые десять секунд
    до починки, и алертинг перестали бы читать.

    Переход из `unknown` потерей не считается: `unknown` означает, что
    Control API не ответил, и что было с потоком в это время — неизвестно.
    """
    if not previous:
        return []
    lost = [cam_id for cam_id, st in current.items()
            if st["status"] == OFFLINE and previous.get(cam_id) == ONLINE]
    return sorted(lost)


def newly_restored(previous: dict[int, str] | None, current: dict[int, dict]) -> list[int]:
    """Камеры, чей поток восстановился, — для парной записи в аудит."""
    if not previous:
        return []
    return sorted(cam_id for cam_id, st in current.items()
                  if st["status"] == ONLINE and previous.get(cam_id) == OFFLINE)


def segment_gaps(last_segment_ts: dict[int, float], states: dict[int, dict],
                 now: float, segment_minutes: int) -> list[int]:
    """Камеры, у которых пропущена запись сегмента (SPEC §14).

    Проверяются только те камеры, чей поток **онлайн**: на потерянном
    потоке отсутствие сегментов — следствие уже сообщённой потери, и
    второй алерт о том же событии только зашумляет.

    Камера без единого сегмента в `last_segment_ts` пропускается: она
    только что добавлена, и первый сегмент ещё пишется. Отличить это от
    настоящего пропуска здесь нечем — время добавления камеры в этот
    модуль не передаётся сознательно, чтобы он оставался чистой функцией
    от состояния записи.
    """
    deadline = segment_minutes * 60 * SEGMENT_GAP_FACTOR
    out = []
    for cam_id, st in states.items():
        if st["status"] != ONLINE:
            continue
        last = last_segment_ts.get(cam_id)
        if last is None:
            continue
        if now - last > deadline:
            out.append(cam_id)
    return sorted(out)
