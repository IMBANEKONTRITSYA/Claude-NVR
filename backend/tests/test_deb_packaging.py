"""Статические проверки production-пакета (SPEC §26, режим 2: .deb + systemd).

Настоящая проверка упаковки — install-тест на раннере с systemd
(`packaging/install-test.sh`, джоба `deb` в `.github/workflows/package.yml`):
он ставит пакет, ждёт `active`, логинится и смотрит в базу. Но он идёт
десять минут и требует одноразовой машины, поэтому запускается только на
тег и на PR, трогающие упаковку.

Здесь — те же инварианты, которые ловятся чтением файлов за секунду и
поэтому гоняются в каждом прогоне. Класс дефектов ровно тот же, что у
`test_dockerfile_modules.py`: «собралось успешно, а на объекте не
стартует». Разница в том, что в Docker такой отказ виден на стенде при
первом `docker compose up`, а в .deb — только на объекте заказчика,
потому что в песочнице systemd нет вовсе.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "packaging"
DEB = PKG / "deb"
UNITS_DIR = DEB / "systemd"

SERVICES = ["backend", "worker", "upscaler", "mediamtx"]


def _units() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(UNITS_DIR.glob("*.service"))}


def _env_template() -> dict[str, str]:
    """Переменные из шаблона /etc/facewatch/facewatch.env."""
    out = {}
    for line in (DEB / "conf" / "facewatch.env.template").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------- окружение

# Значения по умолчанию, зашитые в код сервисов, писались под сеть
# docker-compose: `redis`, `postgres`, `mediamtx`, `worker` — это имена
# контейнеров, а `/media` — точка монтирования тома. Вне Docker любое из
# них означает отказ при старте, причём не сразу и не громко: сервис
# поднимется и будет молча не находить Redis или писать архив мимо диска.
# Поэтому каждая такая переменная ОБЯЗАНА быть переопределена в шаблоне
# конфигурации пакета.
DOCKERISMS = ("redis://redis", "@postgres:", "//mediamtx", "http://worker:", '"mediamtx"', "'mediamtx'")


def _env_defaults_in_code() -> dict[str, str]:
    """`os.environ.get("VAR", "default")` по коду worker'а и апскейла."""
    found = {}
    for path in sorted((ROOT / "worker").glob("*.py")) + sorted((ROOT / "upscaler").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"""os\.environ\.get\(\s*["'](\w+)["']\s*,\s*(["'][^"']*["'])""", text):
            found.setdefault(m.group(1), m.group(2))
    return found


def _mandatory_env_in_code() -> set[str]:
    """`os.environ["VAR"]` — без такой переменной сервис падает на импорте."""
    names = set()
    for path in sorted((ROOT / "worker").glob("*.py")) + sorted((ROOT / "upscaler").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        names.update(re.findall(r"""os\.environ\[\s*["'](\w+)["']\s*\]""", text))
    return names


def test_env_scanner_finds_known_variables():
    """Страховка от молчаливой поломки самих сканеров: если регулярка
    перестанет находить что-либо, проверки ниже пройдут на любом шаблоне."""
    defaults = _env_defaults_in_code()
    for expected in ("REDIS_URL", "MEDIA_PATH", "MEDIAMTX_HOST", "RETENTION_DAYS"):
        assert expected in defaults, f"сканер умолчаний не нашёл {expected}"
    assert "DATABASE_URL" in _mandatory_env_in_code(), "сканер обязательных переменных сломан"


def test_mandatory_env_vars_are_in_the_template():
    template = _env_template()
    missing = sorted(v for v in _mandatory_env_in_code() if v not in template)
    assert not missing, (
        f"в /etc/facewatch/facewatch.env нет обязательных переменных: {missing} — "
        "сервис упадёт с KeyError на импорте, и увидит это только оператор объекта"
    )


def test_docker_specific_defaults_are_overridden_in_the_template():
    template = _env_template()
    offenders = {}
    for var, default in _env_defaults_in_code().items():
        if any(d in default for d in DOCKERISMS) or default.strip("\"'") == "/media":
            if var not in template:
                offenders[var] = default
    assert not offenders, (
        "умолчание из docker-сети не переопределено в шаблоне пакета: "
        f"{offenders} — на объекте сервис пойдёт по несуществующему адресу"
    )


def test_template_has_no_leftover_placeholders_outside_secrets():
    """Плейсхолдеры @...@ подставляет скрипт первого запуска. Любой, о
    котором он не знает, доедет до конфигурации объекта как есть."""
    template_text = (DEB / "conf" / "facewatch.env.template").read_text(encoding="utf-8")
    script = (DEB / "scripts" / "facewatch-first-run").read_text(encoding="utf-8")
    placeholders = set(re.findall(r"@([A-Z_]+)@", template_text))
    unsubstituted = sorted(p for p in placeholders if f"@{p}@" not in script)
    assert not unsubstituted, (
        f"скрипт первого запуска не подставляет {unsubstituted} — "
        "в /etc/facewatch/facewatch.env останется буквальное @ИМЯ@"
    )


# ------------------------------------------------------------------ юниты

def test_every_service_has_a_unit():
    units = _units()
    for svc in SERVICES:
        assert f"facewatch-{svc}.service" in units, f"нет юнита для {svc}"
    assert "facewatch-first-run.service" in units


@pytest.mark.parametrize("unit", sorted(p.name for p in UNITS_DIR.glob("*.service")))
def test_units_are_enabled_by_postinst_and_stopped_by_prerm(unit):
    postinst = (DEB / "postinst").read_text(encoding="utf-8")
    prerm = (DEB / "prerm").read_text(encoding="utf-8")
    name = unit[: -len(".service")]
    assert name in postinst, f"postinst не включает {unit} — после установки сервис не поднимется"
    assert name in prerm, f"prerm не останавливает {unit} — после удаления пакета процесс останется жить"


@pytest.mark.parametrize("unit", sorted(p.name for p in UNITS_DIR.glob("*.service")))
def test_units_restart_always(unit):
    """SPEC §13 «Авторестарт сервисов при падении», SPEC §26
    «restart=always для всех сервисов facewatch»."""
    text = _units()[unit]
    if "Type=oneshot" in text:
        pytest.skip("oneshot-подготовка перезапускаться не должна")
    assert re.search(r"^Restart=always$", text, re.MULTILINE), (
        f"{unit} без Restart=always — упавший сервис останется лежать (SPEC §13, §26)"
    )


# SPEC §26: «Лимиты ресурсов через systemd (CPUQuota/MemoryMax) — аналог
# лимитов docker-compose». «Аналог» проверяется буквально: числа берутся из
# docker-compose.yml, чтобы стенд и объект вели себя одинаково, а не
# расходились по-тихому при правке одного из двух файлов.
def _compose_limits() -> dict[str, dict[str, str]]:
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    limits: dict[str, dict[str, str]] = {}
    service = None
    for line in text.splitlines():
        m = re.match(r"^  (\w[\w-]*):\s*$", line)
        if m:
            service = m.group(1)
            continue
        if service is None:
            continue
        m = re.match(r"^\s+cpus:\s*'?([\d.]+)'?", line)
        if m:
            limits.setdefault(service, {})["cpus"] = m.group(1)
        m = re.match(r"^\s+memory:\s*(\S+)", line)
        if m:
            limits.setdefault(service, {})["memory"] = m.group(1)
    return limits


def test_compose_limit_parser_works():
    limits = _compose_limits()
    assert limits.get("mediamtx", {}).get("cpus"), "парсер лимитов docker-compose сломан"


@pytest.mark.parametrize("svc", SERVICES)
def test_unit_limits_match_compose(svc):
    compose = _compose_limits().get(svc)
    if not compose:
        pytest.skip(f"у сервиса {svc} нет лимитов в docker-compose.yml")
    text = _units()[f"facewatch-{svc}.service"]
    quota = re.search(r"^CPUQuota=(\d+)%$", text, re.MULTILINE)
    memmax = re.search(r"^MemoryMax=(\S+)$", text, re.MULTILINE)
    assert quota and memmax, f"facewatch-{svc}.service без CPUQuota/MemoryMax (SPEC §26)"
    expected_quota = int(float(compose["cpus"]) * 100)
    assert int(quota.group(1)) == expected_quota, (
        f"CPUQuota у facewatch-{svc} = {quota.group(1)}%, а docker-compose даёт "
        f"{compose['cpus']} ядра ({expected_quota}%) — стенд и объект разъехались"
    )
    assert memmax.group(1) == compose["memory"], (
        f"MemoryMax у facewatch-{svc} = {memmax.group(1)}, в docker-compose {compose['memory']}"
    )


@pytest.mark.parametrize("unit", sorted(p.name for p in UNITS_DIR.glob("*.service")))
def test_unit_environment_files_are_created_somewhere(unit):
    """EnvironmentFile= с несуществующим путём — это отказ старта юнита
    целиком (systemd считает это фатальной ошибкой). Каждый такой файл
    обязан либо ставиться пакетом, либо создаваться скриптом первого
    запуска."""
    text = _units()[unit]
    first_run = (DEB / "scripts" / "facewatch-first-run").read_text(encoding="utf-8")
    build = (PKG / "build-deb.sh").read_text(encoding="utf-8")
    for path in re.findall(r"^EnvironmentFile=(?:-)?(\S+)$", text, re.MULTILINE):
        name = Path(path).name
        assert name in first_run or name in build, (
            f"{unit} читает {path}, но его никто не создаёт — юнит не стартует вовсе"
        )


@pytest.mark.parametrize("svc", ["backend", "worker", "upscaler"])
def test_units_run_the_bundled_runtime_not_the_system_python(svc):
    """SPEC §26: «самодостаточный Python-рантайм ... без зависимости от
    системного python»."""
    text = _units()[f"facewatch-{svc}.service"]
    exec_start = re.search(r"^ExecStart=(.+)$", text, re.MULTILINE).group(1)
    assert exec_start.startswith("@PREFIX@/venv/"), (
        f"facewatch-{svc} запускается не из вложенного venv: {exec_start}"
    )
    assert not re.search(r"^ExecStart=/usr/bin/python", text, re.MULTILINE)


def test_layers_are_not_wired_together_by_systemd():
    """SPEC §2: «Отказ аналитики НЕ влияет на запись, и наоборот».
    Requires/BindsTo между слоями превратили бы это в неправду: systemd
    останавливал бы один слой вслед за другим."""
    units = _units()
    record = "facewatch-mediamtx.service"
    for name in ("facewatch-worker.service", "facewatch-backend.service", "facewatch-upscaler.service"):
        hard = re.findall(r"^(?:Requires|BindsTo|Requisite)=(.+)$", units[name], re.MULTILINE)
        assert not any(record in dep for dep in hard), (
            f"{name} жёстко зависит от слоя записи — SPEC §2 требует независимости слоёв"
        )
    hard_on_analytics = re.findall(r"^(?:Requires|BindsTo|Requisite)=(.+)$", units[record], re.MULTILINE)
    assert not any("worker" in dep or "backend" in dep for dep in hard_on_analytics), (
        "слой записи жёстко зависит от аналитики — SPEC §2 требует обратного"
    )


# --------------------------------------------------------- скрипты dpkg

def test_purge_never_removes_the_archive():
    """Самое дорогое, что может сделать этот пакет, — стереть архив
    объекта. Записи инцидента бывают в единственном экземпляре, и
    пакетный менеджер не тот инструмент, которым их удаляют."""
    postrm = (DEB / "postrm").read_text(encoding="utf-8")
    for line in postrm.splitlines():
        code = line.split("#", 1)[0]
        assert not re.search(r"rm\s+-[rf]*\s+/var/lib/facewatch", code), (
            f"postrm удаляет каталог данных: {line.strip()}"
        )
    for line in (DEB / "prerm").read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0]
        assert not re.search(r"rm\s+-[rf]*\s+/var/lib/facewatch", code), (
            f"prerm удаляет каталог данных: {line.strip()}"
        )


def test_maintainer_scripts_guard_systemd_calls():
    """На машине без запущенного systemd (chroot сборки, контейнер,
    песочница CI) установка обязана дойти до конца — просто без запуска
    служб. Без проверки /run/systemd/system `systemctl` вернул бы ошибку,
    и `set -e` уронил бы postinst, то есть установку целиком."""
    for name in ("postinst", "prerm", "postrm"):
        # Комментарии выбрасываются: без этого проверку удовлетворяла бы
        # фраза «под проверкой /run/systemd/system» в шапке блока, то есть
        # тест был бы зелёным и на скрипте, где саму проверку убрали. Это
        # не гипотеза — ровно так он и повёл себя на верификации откатом.
        code = "\n".join(line.split("#", 1)[0] for line in
                         (DEB / name).read_text(encoding="utf-8").splitlines())
        if "systemctl" not in code:
            continue
        assert "/run/systemd/system" in code, (
            f"{name} зовёт systemctl без проверки /run/systemd/system"
        )


def test_first_run_is_idempotent_about_secrets():
    """SPEC §26: «повторный запуск скрипта первого запуска безопасен».
    Секреты обязаны генерироваться только при отсутствии файла — иначе
    апгрейд пакета обнулил бы ключ шифрования RTSP-учёток, и все камеры
    объекта разом стали бы нерасшифровываемыми."""
    text = (DEB / "scripts" / "facewatch-first-run").read_text(encoding="utf-8")
    assert re.search(r'if \[ ! -f "\$ETC/facewatch\.env" \]', text), (
        "генерация секретов не закрыта проверкой существования файла"
    )


def test_no_recursive_chown_over_the_archive():
    """`chown -R /var/lib/facewatch` на объекте — это обход сотен тысяч
    файлов сегментов на каждой загрузке (120 камер × 14 суток × 5 минут ≈
    480 000 файлов), то есть многоминутная задержка старта записи."""
    text = (DEB / "scripts" / "facewatch-first-run").read_text(encoding="utf-8")
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        assert not re.search(r"chown\s+-R\s+\S+\s+\"?\$DATA\"?\s*$", code), (
            f"рекурсивный chown по каталогу данных: {line.strip()}"
        )


# ------------------------------------------------------------ сборка

def test_mediamtx_version_matches_compose():
    """Пакет обязан везти ту версию сервера записи, против которой в CI
    гоняется джоба record-layer-live. Разойдясь, они проверяли бы разное."""
    versions = (PKG / "versions.env").read_text(encoding="utf-8")
    pinned = re.search(r"^MEDIAMTX_VERSION=(\S+)$", versions, re.MULTILINE).group(1)
    compose = re.search(r"image:\s*bluenviron/mediamtx:(\S+)", (ROOT / "docker-compose.yml").read_text(encoding="utf-8")).group(1)
    assert pinned == compose, (
        f"versions.env пинит MediaMTX {pinned}, docker-compose.yml — {compose}"
    )


def test_downloaded_artifacts_are_pinned_by_checksum():
    """Всё, что сборка тянет из сети, обязано сверяться по sha256:
    производственный пакет не может доверять тому, что сегодня лежит по
    ссылке."""
    versions = (PKG / "versions.env").read_text(encoding="utf-8")
    for var in ("PBS_SHA256", "MEDIAMTX_SHA256"):
        m = re.search(rf"^{var}=([0-9a-f]{{64}})$", versions, re.MULTILINE)
        assert m, f"{var} не задан 64-символьной sha256"
    build = (PKG / "build-deb.sh").read_text(encoding="utf-8")
    assert "sha256sum" in build and "не совпал" in build, (
        "build-deb.sh не сверяет контрольные суммы скачанного"
    )


def test_build_copies_service_modules_by_glob():
    """Тот же инвариант, что и в test_dockerfile_modules.py, и по той же
    причине: поимённый список модулей уже несколько раз расходился с
    реальностью, и сервис падал с ModuleNotFoundError при первом старте."""
    build = (PKG / "build-deb.sh").read_text(encoding="utf-8")
    for svc in ("worker", "upscaler"):
        assert re.search(rf'/{svc}/\*\.py', build), (
            f"build-deb.sh копирует модули {svc} не глобом — новый модуль однажды забудут"
        )


def test_conffiles_are_actually_shipped():
    """dpkg падает при установке, если файл объявлен conffile, но его нет
    в пакете. Отказ приходит на объекте и выглядит как «пакет битый»."""
    build = (PKG / "build-deb.sh").read_text(encoding="utf-8")
    for line in (DEB / "conffiles").read_text(encoding="utf-8").split():
        name = Path(line).name
        assert name in build, (
            f"{line} объявлен conffile, но build-deb.sh его не кладёт в дерево пакета"
        )


def test_package_declares_its_runtime_dependencies():
    """SPEC §26: «Зависимости пакета (из официальных репозиториев):
    postgresql + postgresql-XX-pgvector, redis, nginx»."""
    control = (DEB / "control").read_text(encoding="utf-8")
    depends = control.split("Depends:", 1)[1].split("\nDescription:", 1)[0]
    for required in ("postgresql", "pgvector", "redis-server", "nginx"):
        assert required in depends, f"в Depends нет {required} (SPEC §26)"


# ------------------------------------------------------------- install-тест


def _install_test() -> str:
    return (PKG / "install-test.sh").read_text(encoding="utf-8")


def test_install_test_checks_real_table_names():
    """Install-тест смотрел на таблицы `faces` и `events`, которых в схеме
    нет: лица лежат в `face_events`, записи архива — в `video_segments`.
    Проверка «миграции применились» из-за этого падала на здоровом пакете
    и молчала бы о настоящем отказе миграций. Список имён обязан сходиться
    с `__tablename__` моделей, иначе он разойдётся снова."""
    models = (ROOT / "backend" / "app" / "models.py").read_text(encoding="utf-8")
    real = set(re.findall(r'__tablename__\s*=\s*"([^"]+)"', models))
    assert real, "не нашёл ни одного __tablename__ — изменился формат models.py"

    checked = re.search(
        r"^for t in ([^;]+); do$", _install_test(), re.MULTILINE
    )
    assert checked, "не нашёл цикл проверки таблиц в install-test.sh"
    names = checked.group(1).split()
    assert names, "пустой список таблиц в install-test.sh"
    missing = sorted(set(names) - real)
    assert not missing, (
        f"install-test.sh ждёт таблицы, которых нет среди __tablename__: {missing}; "
        f"в схеме есть {sorted(real)}"
    )


def test_install_test_verifies_nginx_is_up():
    """Без явной проверки самого nginx все обращения к интерфейсу падают
    одинаковым «connection refused», по которому не отличить выключенный
    веб-сервер от неподнявшегося бэкенда — ровно на этом install-тест
    однажды и потерял полчаса."""
    body = _install_test()
    assert "systemctl is-active --quiet nginx" in body, (
        "install-тест не проверяет, что nginx вообще запущен"
    )


def test_postinst_starts_nginx_not_only_reloads_it():
    """Если nginx на машине уже стоял (штатное состояние сервера, где его
    поставили заранее, и раннеров CI), apt считает зависимость
    удовлетворённой и postinst самого nginx не отрабатывает — служба
    остаётся выключенной. Пакет обязан поднять её сам: SPEC §26 —
    «apt install ./facewatch_*.deb — все сервисы поднимаются автоматически»."""
    postinst = (DEB / "postinst").read_text(encoding="utf-8")
    assert "systemctl enable nginx" in postinst, (
        "postinst не включает nginx — после перезагрузки объекта интерфейс не поднимется"
    )
    assert "systemctl start nginx" in postinst, (
        "postinst только перезагружает nginx: остановленную службу reload не поднимает"
    )
