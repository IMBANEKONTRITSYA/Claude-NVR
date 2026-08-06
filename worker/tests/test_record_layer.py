"""Слой записи на MediaMTX (SPEC §2, §20, §24) — цикл 24.

До цикла 24 запись жила внутри нити камеры: `ffmpeg -c copy` на камеру
ради републикации плюс `cv2.VideoWriter` в mp4v с последующим
перекодированием в H.264/H.265. Новое ТЗ запрещает и то, и другое (§20 —
«не использовать 120 отдельных FFmpeg-процессов», §24 — «Перекодирование
архива (только remux)») и требует независимости слоёв (§2).

Тесты гоняются против **настоящего HTTP-сервера** (`http.server` из stdlib,
как в `test_onvif_client.py`/`test_snapshot_http.py`) и настоящих файлов на
диске, а не против моков: проверяется, что клиент шлёт те методы и пути,
которые описаны в OpenAPI MediaMTX v1.20, и что решение «дописан ли
сегмент» принимается по реальным mtime/размеру.

Модуль на одном stdlib, поэтому прогоняется в CI-джобе воркера, где нет ни
cv2, ни insightface.
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from record_layer import (
    MediaMTXClient,
    MediaMTXError,
    camera_id_from_path,
    collect_complete_segments,
    diff_paths,
    parse_segment_name,
    path_conf,
    path_name,
    sync_paths,
)


# --------------------------------------------------------------------------
# Настоящий HTTP-сервер, повторяющий контракт Control API MediaMTX


class _FakeMediaMTX(BaseHTTPRequestHandler):
    paths: dict = {}
    calls: list = []
    fail_on: set = set()

    def log_message(self, *args):
        pass

    def _send(self, code: int, body=None):
        raw = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/v3/config/paths/list":
            items = [dict(v, name=k) for k, v in sorted(type(self).paths.items())]
            self._send(200, {"itemCount": len(items), "items": items, "pageCount": 1})
        else:
            self._send(404, {"error": "not found"})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else {}

    def do_POST(self):
        name = self.path.rsplit("/", 1)[-1]
        type(self).calls.append(("POST", name))
        if name in type(self).fail_on:
            self._send(400, {"error": "rejected"})
            return
        type(self).paths[name] = self._body()
        self._send(200, {})

    def do_PATCH(self):
        name = self.path.rsplit("/", 1)[-1]
        type(self).calls.append(("PATCH", name))
        type(self).paths.setdefault(name, {}).update(self._body())
        self._send(200, {})

    def do_DELETE(self):
        name = self.path.rsplit("/", 1)[-1]
        type(self).calls.append(("DELETE", name))
        type(self).paths.pop(name, None)
        self._send(200, {})


@pytest.fixture()
def mediamtx():
    _FakeMediaMTX.paths = {}
    _FakeMediaMTX.calls = []
    _FakeMediaMTX.fail_on = set()
    srv = HTTPServer(("127.0.0.1", 0), _FakeMediaMTX)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield srv, _FakeMediaMTX
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)


def _client(srv) -> MediaMTXClient:
    host, port = srv.server_address
    return MediaMTXClient(f"http://{host}:{port}", timeout=5)


# --------------------------------------------------------------------------
# Конфигурация пути: то, что прямо предписано ТЗ


def test_path_conf_records_remux_without_transcode():
    """SPEC §24 запрещает перекодирование архива. Конфигурация пути не
    должна содержать ничего, что заставило бы MediaMTX декодировать."""
    conf = path_conf("rtsp://cam/main", segment_duration_min=7)
    assert conf["record"] is True
    assert conf["recordFormat"] == "fmp4"
    assert conf["recordSegmentDuration"] == "7m"
    assert conf["source"] == "rtsp://cam/main"
    forbidden = {"runOnPublish", "encode", "recordCodec", "videoCodec", "bitrate"}
    assert not (forbidden & set(conf)), "в конфигурации пути появилось перекодирование"


def test_path_conf_pulls_continuously_not_on_demand():
    """SPEC §5: запись непрерывная. sourceOnDemand=True означал бы дыры в
    архиве всякий раз, когда никто не смотрит live."""
    assert path_conf("rtsp://cam/main")["sourceOnDemand"] is False


def test_record_path_yields_spec_segment_naming():
    """SPEC §20: `cam{camera_id}_{unix_ts}.mp4`. %path -> cam3, %s -> unix
    epoch, расширение MediaMTX добавляет сам — то есть шаблон обязан быть
    именно таким, иначе ротация (fileage.py) и индексация не разберут имя."""
    template = path_conf("rtsp://cam/main")["recordPath"]
    rendered = template.replace("%path", path_name(3)).replace("%s", "1754460000") + ".mp4"
    assert os.path.basename(rendered) == "cam3_1754460000.mp4"
    assert parse_segment_name(os.path.basename(rendered)) == (3, 1754460000)


def test_retention_left_to_worker_not_mediamtx():
    """recordDeleteAfter=0s: удалять сегменты должен воркер, потому что он
    удаляет файл вместе со строкой video_segments. MediaMTX о БД не знает и
    оставил бы в архиве строки на несуществующие файлы."""
    assert path_conf("rtsp://cam/main")["recordDeleteAfter"] == "0s"


# --------------------------------------------------------------------------
# Синхронизация путей


def test_sync_creates_paths_for_every_camera(mediamtx):
    srv, fake = mediamtx
    desired = {path_name(i): path_conf(f"rtsp://cam{i}/main") for i in (1, 2, 7)}

    stats = sync_paths(_client(srv), desired)

    assert stats["added"] == 3 and stats["failed"] == 0
    assert set(fake.paths) == {"cam1", "cam2", "cam7"}
    assert fake.paths["cam7"]["source"] == "rtsp://cam7/main"


def test_sync_is_idempotent(mediamtx):
    """Синхронизация стоит в цикле менеджера (каждые ~10 с). На
    неизменившемся списке камер она не должна делать ни одного пишущего
    запроса — иначе 120 камер дают 12 запросов в секунду на пустом месте."""
    srv, fake = mediamtx
    desired = {path_name(i): path_conf(f"rtsp://cam{i}/main") for i in (1, 2)}
    sync_paths(_client(srv), desired)
    fake.calls.clear()

    stats = sync_paths(_client(srv), desired)

    assert fake.calls == [], f"повторная синхронизация сделала запросы: {fake.calls}"
    assert stats == {"added": 0, "updated": 0, "deleted": 0, "failed": 0}


def test_sync_updates_changed_rtsp_url(mediamtx):
    """Смена RTSP-адреса камеры в админке должна доезжать до записи без
    перезапуска слоёв (SPEC §2)."""
    srv, fake = mediamtx
    sync_paths(_client(srv), {"cam1": path_conf("rtsp://old/main")})
    fake.calls.clear()

    stats = sync_paths(_client(srv), {"cam1": path_conf("rtsp://new/main")})

    assert stats["updated"] == 1
    assert fake.calls == [("PATCH", "cam1")]
    assert fake.paths["cam1"]["source"] == "rtsp://new/main"


def test_sync_removes_path_of_deleted_camera(mediamtx):
    srv, fake = mediamtx
    sync_paths(_client(srv), {"cam1": path_conf("rtsp://a/main"),
                              "cam2": path_conf("rtsp://b/main")})
    fake.calls.clear()

    stats = sync_paths(_client(srv), {"cam1": path_conf("rtsp://a/main")})

    assert stats["deleted"] == 1
    assert set(fake.paths) == {"cam1"}


def test_sync_leaves_foreign_paths_alone(mediamtx):
    """MediaMTX может обслуживать и пути, заведённые не слоем записи
    (ручная публикация при диагностике). Сносить их синхронизация не
    вправе — удаляются только `cam{N}`."""
    srv, fake = mediamtx
    fake.paths["debug_stream"] = {"source": "publisher"}

    sync_paths(_client(srv), {"cam1": path_conf("rtsp://a/main")})

    assert "debug_stream" in fake.paths


def test_one_rejected_path_does_not_block_the_rest(mediamtx):
    """119 камер не должны оставаться без записи из-за одной, чей URL
    MediaMTX отверг."""
    srv, fake = mediamtx
    fake.fail_on = {"cam2"}
    desired = {path_name(i): path_conf(f"rtsp://cam{i}/main") for i in (1, 2, 3)}

    stats = sync_paths(_client(srv), desired)

    assert stats["failed"] == 1 and stats["added"] == 2
    assert set(fake.paths) == {"cam1", "cam3"}


def test_client_reports_unreachable_mediamtx():
    """Порт закрыт — ошибка должна быть типизированной, чтобы менеджер
    воркера залогировал её и пошёл дальше, а не упал."""
    client = MediaMTXClient("http://127.0.0.1:1", timeout=1)
    with pytest.raises(MediaMTXError):
        client.list_path_configs()


def test_paginated_listing_does_not_look_like_extra_paths():
    """На 120 камерах один запрос с itemsPerPage=100 вернул бы 100 путей,
    и diff по такому списку решил бы, что оставшихся 20 нет, — то есть
    завёл бы их повторно. Проверяется на самой чистой функции."""
    current = {path_name(i): path_conf(f"rtsp://cam{i}/main") for i in range(1, 121)}
    desired = dict(current)
    to_add, to_update, to_delete = diff_paths(current, desired)
    assert (to_add, to_update, to_delete) == ({}, {}, [])


def test_camera_id_from_path():
    assert camera_id_from_path("cam42") == 42
    assert camera_id_from_path("debug_stream") is None
    assert camera_id_from_path("") is None


# --------------------------------------------------------------------------
# Разбор каталога сегментов


def _write(path: str, size: int, age_sec: float):
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)
    when = time.time() - age_sec
    os.utime(path, (when, when))


def test_finished_segment_is_collected_when_successor_exists(tmp_path):
    d = str(tmp_path)
    _write(os.path.join(d, "cam1_1000.mp4"), 2048, age_sec=1)
    _write(os.path.join(d, "cam1_1300.mp4"), 2048, age_sec=1)

    found = collect_complete_segments(d, now=time.time())

    assert [s["file_path"] for s in found] == [os.path.join(d, "cam1_1000.mp4")]
    seg = found[0]
    assert seg["camera_id"] == 1
    # Граница берётся у следующего сегмента: MediaMTX режет встык, и mtime
    # дал бы длительность с точностью до сброса последней части на диск.
    assert seg["started_ts"] == 1000 and seg["ended_ts"] == 1300


def test_last_segment_is_collected_once_it_stops_growing(tmp_path):
    """Камера пропала — следующего сегмента не будет часами. Без второго
    признака («файл не менялся») последняя минута записи не попадала бы в
    архив всё это время."""
    d = str(tmp_path)
    _write(os.path.join(d, "cam1_1000.mp4"), 2048, age_sec=120)

    found = collect_complete_segments(d, now=time.time(), settle_sec=30)

    assert len(found) == 1 and found[0]["camera_id"] == 1


def test_segment_still_being_written_is_not_collected(tmp_path):
    """Файл, в который MediaMTX пишет прямо сейчас, не должен попасть в
    архив: длительность у него ещё неизвестна, а строка появилась бы уже
    сейчас и показывала бы обрезанную запись."""
    d = str(tmp_path)
    _write(os.path.join(d, "cam1_1000.mp4"), 2048, age_sec=2)

    assert collect_complete_segments(d, now=time.time(), settle_sec=30) == []


def test_foreign_files_are_ignored(tmp_path):
    d = str(tmp_path)
    _write(os.path.join(d, "cam1_1000.mp4"), 2048, age_sec=120)
    _write(os.path.join(d, "notes.txt"), 10, age_sec=120)
    _write(os.path.join(d, "cam1_legacy_tmp.mp4"), 2048, age_sec=120)

    found = collect_complete_segments(d, now=time.time(), settle_sec=30)

    assert [os.path.basename(s["file_path"]) for s in found] == ["cam1_1000.mp4"]


def test_missing_directory_is_not_an_error(tmp_path):
    assert collect_complete_segments(str(tmp_path / "nope"), now=time.time()) == []


def test_segments_of_different_cameras_do_not_bound_each_other(tmp_path):
    """Сегмент камеры 2 не должен закрывать сегмент камеры 1: границы
    считаются внутри одной камеры."""
    d = str(tmp_path)
    _write(os.path.join(d, "cam1_1000.mp4"), 2048, age_sec=120)
    _write(os.path.join(d, "cam2_1100.mp4"), 2048, age_sec=120)

    found = {s["camera_id"]: s for s in collect_complete_segments(d, now=time.time(), settle_sec=30)}

    assert set(found) == {1, 2}
    assert found[1]["ended_ts"] != 1100
