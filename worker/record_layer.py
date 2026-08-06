"""Слой записи (SPEC §2, §20): MediaMTX сам тянет основные потоки всех камер
и пишет их remux'ом на диск, без декодирования и без перекодирования.

Почему отдельный слой, а не запись из нити камеры (как было до цикла 24):

* SPEC §20 — «MediaMTX — единственный процесс, пишущий все камеры; **не
  использовать 120 отдельных FFmpeg-процессов**». До этого воркер поднимал
  по `ffmpeg -c copy` на камеру только ради републикации в MediaMTX и
  дополнительно писал архив сам через `cv2.VideoWriter`;
* SPEC §24 — «Перекодирование архива (только remux)» в списке того, что
  реализовывать запрещено. Прежняя запись шла в `mp4v` и потом
  перекодировалась в H.264/H.265 через ffmpeg на каждый сегмент;
* SPEC §2 — «Отказ аналитики НЕ влияет на запись». Пока запись жила внутри
  `camera_worker()`, любая её остановка (падение нити, смена модели,
  рестарт воркера) означала дыру в архиве. Теперь запись не зависит от
  процесса воркера вообще: MediaMTX держит её сам, воркер только
  сверяет конфигурацию путей и заносит готовые сегменты в БД.

Модуль намеренно на одном stdlib (как `onvif_client.py`, `snapshot_http.py`,
`backoff.py`): CI-джоба воркера не ставит `cv2`/`insightface`, поэтому вся
логика здесь проверяется в CI против настоящего HTTP-сервера и настоящих
файлов, а не моков (см. `.github/workflows/ci.yml`).
"""
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger("facewatch.worker")

# Имя пути в MediaMTX и, через `recordPath`, префикс имени файла сегмента.
# SPEC §20 фиксирует именование `cam{camera_id}_{unix_ts}.mp4` — оно же
# используется ротацией (fileage.py) и уже разобрано в архиве.
_PATH_RE = re.compile(r"^cam(\d+)$")
_SEGMENT_RE = re.compile(r"^cam(\d+)_(\d+)\.mp4$")


def path_name(camera_id: int) -> str:
    return f"cam{camera_id}"


def camera_id_from_path(name: str) -> int | None:
    m = _PATH_RE.match(name or "")
    return int(m.group(1)) if m else None


def path_conf(rtsp_url: str, *, segment_duration_min: int = 5) -> dict:
    """Конфигурация одного пути MediaMTX для камеры слоя записи.

    `sourceOnDemand: False` — принципиально: с `True` MediaMTX тянет камеру
    только пока есть читатель, то есть запись бы прерывалась каждый раз,
    когда никто не смотрит live. SPEC §5 требует непрерывную запись.

    `record: True` + `recordFormat: fmp4` — remux как есть, без
    декодирования (SPEC §19, §24). Ни кодек, ни битрейт здесь не задаются
    осознанно: это ровно то перекодирование, которое §24 запрещает.

    `recordDeleteAfter: 0s` — автоудаление MediaMTX выключено: retention
    ведёт воркер (`cleanup_old()`), потому что он удаляет файл вместе со
    строкой `video_segments`, а MediaMTX о БД не знает и оставил бы
    висячие строки архива, указывающие на несуществующие файлы.
    """
    return {
        "source": rtsp_url,
        "sourceOnDemand": False,
        "record": True,
        "recordFormat": "fmp4",
        # %s — unix epoch, %path — имя пути (cam{id}); расширение MediaMTX
        # добавляет сам. Даёт ровно `cam{camera_id}_{unix_ts}.mp4` (SPEC §20).
        "recordPath": "/media/segments/%path_%s",
        "recordSegmentDuration": f"{int(segment_duration_min)}m",
        "recordDeleteAfter": "0s",
    }


# Ключи, по которым сверяется уже заведённый в MediaMTX путь. Сравнивать
# весь ответ `/v3/config/paths/list` бессмысленно: MediaMTX возвращает все
# ~80 полей PathConf с дефолтами, которых мы не задаём.
_MANAGED_KEYS = tuple(path_conf("rtsp://x/y").keys())


class MediaMTXError(RuntimeError):
    pass


class MediaMTXClient:
    """Тонкий клиент Control API MediaMTX (`api: yes` в mediamtx.yml).

    Ровно четыре нужных вызова из `/v3/config/paths/*` плюс рантайм-список
    `/v3/paths/list` для мониторинга состояния потоков записи (SPEC §14:
    «статус каждого RTSP-потока слоя записи»).
    """

    def __init__(self, base_url: str, timeout: float = 10.0, opener=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = opener or urllib.request.build_opener()

    def _request(self, method: str, path: str, body: dict | None = None):
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = e.read()[:200].decode("utf-8", "replace")
            raise MediaMTXError(f"{method} {path} -> HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise MediaMTXError(f"{method} {path} -> {e}") from e
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _list(self, endpoint: str) -> list[dict]:
        """Постранично забирает список: на 120 камерах один запрос с
        дефолтным itemsPerPage=100 вернул бы неполный список, а по нему
        sync_paths() решил бы, что 20 путей лишние, и удалил бы их."""
        items: list[dict] = []
        page = 0
        while True:
            sep = "&" if "?" in endpoint else "?"
            data = self._request("GET", f"{endpoint}{sep}page={page}&itemsPerPage=100") or {}
            items.extend(data.get("items") or [])
            page += 1
            if page >= int(data.get("pageCount") or 1):
                break
        return items

    def list_path_configs(self) -> dict[str, dict]:
        return {i["name"]: i for i in self._list("/v3/config/paths/list") if i.get("name")}

    def runtime_paths(self) -> dict[str, dict]:
        return {i["name"]: i for i in self._list("/v3/paths/list") if i.get("name")}

    def add_path(self, name: str, conf: dict) -> None:
        self._request("POST", f"/v3/config/paths/add/{urllib.parse.quote(name)}", conf)

    def patch_path(self, name: str, conf: dict) -> None:
        self._request("PATCH", f"/v3/config/paths/patch/{urllib.parse.quote(name)}", conf)

    def delete_path(self, name: str) -> None:
        self._request("DELETE", f"/v3/config/paths/delete/{urllib.parse.quote(name)}")


def diff_paths(current: dict[str, dict], desired: dict[str, dict]) -> tuple[dict, dict, list[str]]:
    """(добавить, обновить, удалить) — чистая функция, чтобы решение о том,
    что именно делать с чужими путями, было проверяемо отдельно от сети.

    Удаляются только пути вида `cam{N}`: MediaMTX может обслуживать и другие
    (ручная публикация при диагностике), и слой записи не имеет права их
    сносить.
    """
    to_add = {n: c for n, c in desired.items() if n not in current}
    to_update = {}
    for name, conf in desired.items():
        if name not in current:
            continue
        have = current[name]
        if any(have.get(k) != v for k, v in conf.items()):
            to_update[name] = conf
    to_delete = [
        n for n in current
        if n not in desired and camera_id_from_path(n) is not None
    ]
    return to_add, to_update, sorted(to_delete)


def sync_paths(client: MediaMTXClient, desired: dict[str, dict]) -> dict[str, int]:
    """Приводит конфигурацию путей MediaMTX к desired. Идемпотентна:
    вызывается в цикле менеджера воркера каждые ~10 с, и на неизменившемся
    списке камер не делает ни одного пишущего запроса.

    Отказ на одном пути не отменяет остальные: недоступная камера или
    отвергнутый MediaMTX URL не должны блокировать запись 119 остальных.
    """
    current = client.list_path_configs()
    to_add, to_update, to_delete = diff_paths(current, desired)
    stats = {"added": 0, "updated": 0, "deleted": 0, "failed": 0}
    for name, conf in to_add.items():
        try:
            client.add_path(name, conf)
            stats["added"] += 1
        except MediaMTXError:
            stats["failed"] += 1
            logger.error("не удалось завести путь записи в MediaMTX",
                         exc_info=True, extra={"path": name})
    for name, conf in to_update.items():
        try:
            client.patch_path(name, conf)
            stats["updated"] += 1
        except MediaMTXError:
            stats["failed"] += 1
            logger.error("не удалось обновить путь записи в MediaMTX",
                         exc_info=True, extra={"path": name})
    for name in to_delete:
        try:
            client.delete_path(name)
            stats["deleted"] += 1
        except MediaMTXError:
            stats["failed"] += 1
            logger.error("не удалось удалить путь записи из MediaMTX",
                         exc_info=True, extra={"path": name})
    return stats


def parse_segment_name(filename: str) -> tuple[int, int] | None:
    """`cam3_1754460000.mp4` -> (3, 1754460000); иначе None."""
    m = _SEGMENT_RE.match(filename)
    return (int(m.group(1)), int(m.group(2))) if m else None


# Сколько сегмент должен пролежать без изменений, чтобы считаться дописанным.
# fMP4 пишется частями по recordPartDuration (1 с), поэтому «файл не менялся
# 30 с» — надёжный признак того, что MediaMTX перешёл на следующий сегмент,
# даже если следующего файла ещё нет (камера отвалилась ровно на границе).
SEGMENT_SETTLE_SEC = 30.0


def collect_complete_segments(segments_dir: str, now: float,
                              settle_sec: float = SEGMENT_SETTLE_SEC) -> list[dict]:
    """Дописанные сегменты слоя записи, готовые к занесению в архив.

    Признак «дописан» двойной, и оба нужны:

    * есть более поздний сегмент той же камеры — значит MediaMTX уже
      переключился, и этот файл больше не растёт;
    * файл не менялся `settle_sec`. Без этого условия сегмент, оставшийся
      последним из-за пропажи камеры, никогда не попал бы в архив: следующего
      файла не будет, пока камера не вернётся, а это может быть часами.

    Возвращает описания, а не строки БД: модуль не знает ни про SQLAlchemy,
    ни про модели (см. шапку файла), запись делает `segment_index.py`.
    """
    try:
        names = os.listdir(segments_dir)
    except OSError:
        return []

    by_cam: dict[int, list[tuple[int, str]]] = {}
    for name in names:
        parsed = parse_segment_name(name)
        if parsed is None:
            continue
        cam_id, ts = parsed
        by_cam.setdefault(cam_id, []).append((ts, name))

    out: list[dict] = []
    for cam_id, entries in by_cam.items():
        entries.sort()
        for idx, (ts, name) in enumerate(entries):
            full = os.path.join(segments_dir, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            has_successor = idx + 1 < len(entries)
            if not has_successor and (now - st.st_mtime) < settle_sec:
                continue
            # Конец сегмента: начало следующего, если он есть (точная
            # граница, MediaMTX режет встык), иначе время последней записи
            # в файл.
            ended = float(entries[idx + 1][0]) if has_successor else st.st_mtime
            if ended < ts:
                ended = float(ts)
            out.append({
                "camera_id": cam_id,
                "file_path": full,
                "started_ts": float(ts),
                "ended_ts": ended,
                "size_bytes": st.st_size,
            })
    out.sort(key=lambda s: (s["camera_id"], s["started_ts"]))
    return out
