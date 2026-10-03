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
from urllib.parse import urlsplit

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


def test_hls_segment_count_satisfies_low_latency_minimum():
    """§4 «просмотр видео в реальном времени» держится на HLS из MediaMTX.

    `hlsVariant` не задан, то есть действует значение по умолчанию —
    `lowLatency`, а Low-Latency HLS требует **минимум 7 сегментов**. При
    меньшем числе муксер не поднимается вовсе: в лог идёт «Low-Latency HLS
    requires at least 7 segments», а `GET /cam{N}/index.m3u8` отвечает 404,
    то есть живого просмотра нет ни на одной камере.

    Найдено в цикле 30 запуском настоящего MediaMTX v1.9.3 с этим самым
    файлом (бинарник статический, Docker для него не нужен — 13 циклов
    подряд пробел числился как «нет Docker в песочнице»). При 5 сегментах —
    404 и ошибка в логе, при 7 — 200 и валидный плейлист.

    Проверка на минимум, а не на равенство: увеличить число сегментов
    можно, уменьшить ниже семи — нельзя.
    """
    variant = MEDIAMTX.get("hlsVariant", "lowLatency")
    if variant != "lowLatency":
        pytest.skip(f"hlsVariant={variant}: ограничение LL-HLS не применяется")
    assert MEDIAMTX.get("hlsSegmentCount", 0) >= 7, (
        "с hlsVariant=lowLatency и менее чем 7 сегментами HLS-муксер не "
        "стартует, и живой просмотр (§4) не работает ни на одной камере"
    )


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


def _compose_media_path(service: str) -> str:
    return str(COMPOSE["services"][service]["environment"]["MEDIA_PATH"])


def test_record_path_matches_spec_naming():
    """SPEC §20: `cam{camera_id}_{unix_ts}.mp4`. Ротация (`worker/fileage.py`)
    и индексация разбирают именно это имя."""
    assert MEDIAMTX["pathDefaults"]["recordPath"] == "/media/segments/%path_%s"


def test_mediamtx_default_record_path_matches_compose_media_path():
    """Значение из `mediamtx.yml` — это режим 1 §26 (docker-compose), и оно
    обязано указывать в тот же каталог, который воркер сканирует как
    `MEDIA_PATH`. Разъедутся — MediaMTX пишет в один каталог, архив
    индексирует другой, и вся §5 (архив, retention, индикация диска)
    работает вхолостую: строк в `video_segments` нет, файлы никто не
    удаляет, диск заполняется до отказа.

    Сам шаблон в коде уже не захардкожен (`record_layer.record_path_template`,
    цикл 39) — здесь стережётся статический конфиг, до которого код не
    дотягивается."""
    media = _compose_media_path("worker").rstrip("/")
    assert MEDIAMTX["pathDefaults"]["recordPath"] == f"{media}/segments/%path_%s"


def test_all_services_share_one_media_path():
    """Бэкенд, воркер и апскейлер обязаны видеть один и тот же корень
    медиаданных: по нему считается свободное место (§5 «индикация
    заполнения диска»), по нему же отдаются файлы архива."""
    paths = {svc: _compose_media_path(svc)
             for svc in ("backend", "worker", "upscaler")
             if "MEDIA_PATH" in (COMPOSE["services"][svc].get("environment") or {})}
    assert len(set(paths.values())) == 1, paths


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


def _api_users() -> list[dict]:
    """Записи authInternalUsers, дающие право `api`."""
    return [u for u in (MEDIAMTX.get("authInternalUsers") or [])
            if any(p.get("action") == "api" for p in (u.get("permissions") or []))]


def test_mediamtx_grants_api_beyond_loopback():
    """Главная поломка цикла 34: воркер получал HTTP 401 на КАЖДЫЙ вызов
    Control API, и вместе с ним отваливались §4 и §5 целиком.

    Дефолт MediaMTX выдаёт право `api` только записи с
    `ips: ['127.0.0.1', '::1']`, а воркер живёт в отдельном контейнере и
    приходит с адреса docker-сети. Пока `authInternalUsers` в
    `mediamtx.yml` не было вовсе, действовал именно этот дефолт: пути
    камер не заводились (чёрный экран на всех камерах), статусы потоков не
    читались (все камеры «offline» навсегда), а healthcheck контейнера
    ходил с loopback и оставался зелёным.

    Проверено на настоящих бинарниках v1.9.3 и v1.16.0: запрос с
    не-loopback адреса → `HTTP 401 authentication error`; с явной записью
    и Basic-авторизацией → 200.
    """
    users = _api_users()
    assert users, (
        "в mediamtx.yml нет ни одной записи authInternalUsers с правом "
        "action: api — Control API останется доступен только с loopback, "
        "и воркер из соседнего контейнера получит HTTP 401"
    )
    remote = [u for u in users if not u.get("ips")]
    assert remote, (
        "право api выдано только конкретным адресам "
        f"({[u.get('ips') for u in users]}); воркер приходит с произвольного "
        "адреса docker-сети и будет получать HTTP 401"
    )
    for u in remote:
        assert u.get("pass"), (
            f"пользователь {u.get('user')!r} получает api с любого адреса без "
            "пароля — это открытый доступ к конфигурации всех камер"
        )
        assert u.get("user") != "any", (
            "право api с любого адреса выдано пользователю `any` — пароль "
            "для него MediaMTX не спрашивает"
        )


def test_mediamtx_does_not_grant_publish():
    """Камеры MediaMTX тянет сам (`source:` в пути), публиковать в него
    некому. С правом `publish` любой, кто дотянулся до внутренней сети, мог
    бы занять путь и подменить поток камеры."""
    for u in MEDIAMTX.get("authInternalUsers") or []:
        actions = {p.get("action") for p in (u.get("permissions") or [])}
        assert "publish" not in actions, (
            f"пользователь {u.get('user')!r} получил право publish"
        )


def test_worker_reaches_mediamtx_api_with_credentials():
    """Адрес Control API у воркера обязан нести учётку: без неё MediaMTX
    отвечает 401 (см. test_mediamtx_grants_api_beyond_loopback)."""
    env = COMPOSE["services"]["worker"]["environment"]
    url = env["MEDIAMTX_API_URL"]
    parts = urlsplit(url.replace("${MEDIAMTX_API_PASSWORD:-", "").replace("}", ""))
    assert parts.hostname == "mediamtx" and parts.port == 9997, url
    assert parts.username, (
        f"MEDIAMTX_API_URL={url!r} без логина — воркер пойдёт в Control API "
        "анонимно и получит 401"
    )
    api_users = {u.get("user") for u in _api_users()}
    assert parts.username in api_users, (
        f"логин {parts.username!r} не совпадает ни с одним пользователем "
        f"authInternalUsers с правом api ({api_users})"
    )


def test_mediamtx_and_worker_share_the_same_api_password():
    """Пароль задаётся в двух местах (переменная контейнера mediamtx и адрес
    воркера) и обязан быть одним и тем же. Рассогласование даёт ровно тот же
    401 и те же симптомы, что отсутствие прав вовсе, — а healthcheck при
    этом зелёный, потому что ходит с loopback."""
    mtx_env = COMPOSE["services"]["mediamtx"]["environment"]
    override = mtx_env["MTX_AUTHINTERNALUSERS_1_PASS"]
    worker_url = COMPOSE["services"]["worker"]["environment"]["MEDIAMTX_API_URL"]
    assert override in worker_url, (
        f"пароль mediamtx ({override!r}) не встречается в MEDIAMTX_API_URL "
        f"воркера ({worker_url!r})"
    )
    # Индекс в имени переменной обязан указывать на пользователя с правом api:
    # MTX_AUTHINTERNALUSERS_<i>_PASS адресует запись списка по её номеру, и
    # сдвиг записи наверх молча начнёт переопределять чужой пароль.
    idx = int(re.fullmatch(r"MTX_AUTHINTERNALUSERS_(\d+)_PASS",
                           "MTX_AUTHINTERNALUSERS_1_PASS").group(1))
    users = MEDIAMTX["authInternalUsers"]
    assert idx < len(users), f"в authInternalUsers нет записи с индексом {idx}"
    assert any(p.get("action") == "api" for p in users[idx]["permissions"]), (
        f"MTX_AUTHINTERNALUSERS_{idx}_PASS переопределяет пароль записи "
        f"{users[idx].get('user')!r}, у которой нет права api"
    )


def test_mediamtx_image_is_pinned():
    """`latest` ломает рабочее развёртывание по `docker compose pull`, без
    единого изменения в репозитории: набор прав Control API и имена полей
    рантайма между выпусками MediaMTX менялись."""
    image = COMPOSE["services"]["mediamtx"]["image"]
    assert not image.endswith(":latest") and ":" in image, (
        f"образ MediaMTX не зафиксирован: {image!r}"
    )


def test_worker_waits_for_mediamtx():
    assert "mediamtx" in COMPOSE["services"]["worker"]["depends_on"]
