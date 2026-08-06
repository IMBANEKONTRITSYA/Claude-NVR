"""Конфигурация слоя записи (SPEC §2, §20, §24) — цикл 24.

Репозиторные проверки, как `test_docker_compose_security.py` и
`test_nginx_hardening.py`: они стерегут решения, которые живут в yml, а не в
коде, и которые в песочнице без Docker-демона иначе не проверяются ничем.

Стерегут ровно те требования ТЗ, ради которых слой записи и переписан:

* §20 «MediaMTX — единственный процесс, пишущий все камеры; не использовать
  120 отдельных FFmpeg-процессов»;
* §24 «Перекодирование архива (только remux)» — в списке запрещённого;
* §2 «Отказ аналитики НЕ влияет на запись».
"""
import ast
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
MEDIAMTX = yaml.safe_load((ROOT / "mediamtx" / "mediamtx.yml").read_text(encoding="utf-8"))
WORKER_SRC = (ROOT / "worker" / "worker.py").read_text(encoding="utf-8")


def test_mediamtx_control_api_enabled():
    """Пути камер заводит воркер через Control API; без `api: yes` слой
    записи не поднимется вообще, а MediaMTX стартует молча и выглядит
    здоровым."""
    assert MEDIAMTX.get("api") is True


def test_mediamtx_writes_to_the_shared_media_volume():
    """Сегменты обязаны лечь в тот же том, который читает архив бэкенда и
    чистит ротация воркера, — иначе запись идёт «в никуда» внутрь
    контейнера и пропадает при его пересоздании."""
    volumes = COMPOSE["services"]["mediamtx"]["volumes"]
    assert any(v.split(":")[0] == "media" for v in volumes), volumes


def test_mediamtx_control_api_is_not_published_outside():
    """Доступ к Control API равен доступу к конфигурации всех камер
    (включая RTSP-адреса с учётными данными в `source`), а
    аутентификации у MediaMTX по умолчанию нет."""
    for mapping in COMPOSE["services"]["mediamtx"].get("ports", []):
        assert not str(mapping).endswith(":9997"), mapping


def test_mediamtx_does_not_accept_arbitrary_publishers():
    """`all_others: source: publisher` (было до цикла 24) разрешало любому,
    кто дотянулся до внутренней сети, опубликовать поток в путь `camN` и
    подменить картинку камеры — аутентификации у MediaMTX нет."""
    paths = MEDIAMTX.get("paths") or {}
    assert "all_others" not in paths, paths


def test_record_defaults_are_remux_and_continuous():
    defaults = MEDIAMTX["pathDefaults"]
    assert defaults["recordFormat"] == "fmp4"
    assert defaults["sourceOnDemand"] is False, (
        "sourceOnDemand: yes оставляет в архиве дыры между сеансами просмотра"
    )
    duration = str(defaults["recordSegmentDuration"])
    assert re.fullmatch(r"(5|6|7|8|9|10)m", duration), (
        f"SPEC §20 требует сегменты 5-10 минут, в конфиге {duration}"
    )


def test_record_path_matches_spec_naming():
    """SPEC §20: `cam{camera_id}_{unix_ts}.mp4`. Ротация (`worker/fileage.py`)
    и индексация разбирают именно это имя."""
    assert MEDIAMTX["pathDefaults"]["recordPath"] == "/media/segments/%path_%s"


def _executable_tokens(source: str) -> set[str]:
    """Имена, атрибуты и строковые литералы модуля — без комментариев и
    докстрингов.

    Искать подстроку в исходнике целиком нельзя: шапка `worker.py` и
    комментарии по коду намеренно упоминают снятый механизм («до цикла 24
    запись шла через cv2.VideoWriter»), и такая проверка падала бы на
    объяснении, ради которого её и писали.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docstrings.add(id(body[0].value))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            out.add(node.id)
        elif isinstance(node, ast.Attribute):
            out.add(node.attr)
        elif isinstance(node, ast.alias):
            out.add(node.name.split(".")[0])
            if node.asname:
                out.add(node.asname)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and id(node) not in docstrings:
            out.add(node.value)
    return out


def test_worker_no_longer_spawns_ffmpeg_per_camera():
    """SPEC §20 запрещает 120 отдельных FFmpeg-процессов, §24 — транскод
    архива. Проверка по исходнику, а не по поведению: единственный способ
    убедиться, что процесс не появится, — убедиться, что его никто не
    запускает."""
    tokens = _executable_tokens(WORKER_SRC)
    for forbidden in ("start_republish", "VideoWriter", "VideoWriter_fourcc",
                      "libx264", "libx265", "build_encode_args"):
        assert forbidden not in tokens, (
            f"worker.py снова использует {forbidden!r} — слой записи вернулся "
            "в нить камеры вопреки SPEC §20/§24"
        )


def test_forbidden_token_check_is_not_vacuous():
    """Страховка от того, что разбор сломается и вернёт пустое множество:
    тогда проверка выше проходила бы на любом коде."""
    tokens = _executable_tokens(WORKER_SRC)
    assert {"camera_worker", "manager", "sync_paths"} <= tokens


def test_worker_reaches_mediamtx_api_by_internal_network():
    env = COMPOSE["services"]["worker"]["environment"]
    assert env["MEDIAMTX_API_URL"] == "http://mediamtx:9997"


def test_worker_waits_for_mediamtx():
    assert "mediamtx" in COMPOSE["services"]["worker"]["depends_on"]
