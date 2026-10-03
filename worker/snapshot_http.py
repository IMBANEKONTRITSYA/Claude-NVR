"""Скачивание JPEG-снимка камеры по HTTP (ONVIF GetSnapshotUri).

Отдельный модуль от worker.py по той же причине, что и onvif_client.py:
здесь только stdlib, поэтому логика тестируется в CI против настоящего
HTTP-сервера, а не мока (worker.py тянет cv2/insightface, которых в
CI-джобе воркера нет — см. .github/workflows/ci.yml). worker.py остаётся
тонкой обёрткой: скачать байты этим модулем, декодировать их через
cv2.imdecode.

Учётные данные камеры приходят внутри URI: GetStreamUri/GetSnapshotUri
отдают адрес без них, а inject_credentials() подставляет — ffmpeg и OpenCV
читают логин с паролем только из самого URL. Для HTTP это не работает:
urllib.request.urlopen("http://user:pass@host/snap") не разбирает
userinfo, а передаёт всю строку "user:pass@host" как имя хоста и падает на
резолве DNS (`URLError: Name or service not known`). Поэтому userinfo
отделяется здесь и превращается в нормальную HTTP-аутентификацию — Basic
или Digest, смотря что запросит камера (ONVIF Profile S допускает оба, и
на практике встречаются оба).
"""
import logging
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import unquote, urlsplit, urlunsplit

logger = logging.getLogger("facewatch.worker")

# Как часто повторять предупреждение об одном и том же недоступном снимке.
# Без throttle'а на камере с потоком событий лог заполнялся бы одной и той
# же строкой; без предупреждения вообще (как было до цикла 19) сломанный
# путь снимков был не виден никак — снимки молча резались из субпотока.
WARN_INTERVAL_SEC = 300.0

_warned_at: dict[str, float] = {}
_warned_lock = threading.Lock()


def split_credentials(url: str) -> tuple[str, str | None, str | None]:
    """Вынимает userinfo из URL: (адрес без учётных данных, логин, пароль).

    Логин и пароль percent-декодируются: inject_credentials() кодирует их
    через quote(safe=""), чтобы пароль вида "p@ss:w/ord" не развалил разбор
    URL, — здесь выполняется обратное преобразование.
    """
    parts = urlsplit(url)
    if not parts.username:
        return url, None, None
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    clean = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    return clean, unquote(parts.username), unquote(parts.password or "")


def build_opener(url: str) -> tuple[str, urllib.request.OpenerDirector]:
    """Готовит (адрес без учётных данных, opener с аутентификацией).

    Оба обработчика — и Basic, и Digest — вешаются сразу: какой из них
    сработает, решает сама камера своим ответом 401 WWW-Authenticate.
    Оба отправляют учётные данные только после этого ответа, поэтому
    лишний обработчик ничего не стоит и ничего не раскрывает.
    """
    clean, user, password = split_credentials(url)
    if user is None:
        return clean, urllib.request.build_opener()
    mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, clean, user, password)
    return clean, urllib.request.build_opener(
        urllib.request.HTTPBasicAuthHandler(mgr),
        urllib.request.HTTPDigestAuthHandler(mgr),
    )


def _warn_throttled(clean_url: str, reason: str) -> None:
    now = time.monotonic()
    with _warned_lock:
        last = _warned_at.get(clean_url)
        if last is not None and now - last < WARN_INTERVAL_SEC:
            return
        _warned_at[clean_url] = now
    # Логируется адрес БЕЗ учётных данных — они отделены split_credentials.
    logger.warning(
        "снимок с камеры недоступен, лицо будет вырезано из кадра аналитики",
        extra={"snapshot_url": clean_url, "reason": reason},
    )


def fetch_snapshot_bytes(url: str, timeout: float = 4.0) -> bytes | None:
    """Скачивает один кадр JPEG. None при любой проблеме.

    None, а не исключение: снимок в полном разрешении — улучшение качества
    кропа, а не обязательный шаг обработки события, и недоступная камера не
    должна ронять нить. Но, в отличие от прежнего молчаливого
    `except Exception: return None`, отказ теперь виден в логе (с throttle,
    см. WARN_INTERVAL_SEC): сломанный путь снимков иначе не отличить от
    камеры, которая GetSnapshotUri просто не поддерживает.
    """
    try:
        clean, opener = build_opener(url)
    except ValueError as e:  # мусор вместо URL в ответе камеры
        _warn_throttled(url, f"некорректный адрес: {e}")
        return None
    try:
        with opener.open(clean, timeout=timeout) as resp:  # noqa: S310 — камера локальной сети
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        _warn_throttled(clean, str(e))
        return None
    if not data:
        _warn_throttled(clean, "камера вернула пустой ответ")
        return None
    return data
