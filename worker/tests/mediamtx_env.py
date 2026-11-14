"""Окружение для MediaMTX в живых тестах — без RTSP_* и MTX_*.

MediaMTX (наследие rtsp-simple-server) читает переменные с префиксом RTSP_
как собственные настройки. `RTSP_ENCRYPTION_KEY` — ключ шифрования учёток
камер в окружении проекта (.env, testenv разработчика) — разбирается им как
настройка `encryption`, и сервер не стартует: «RTSP_ENCRYPTION: json:
Unmarshal(nil *conf.alias)» (MediaMTX 1.16.1). Тест видит только «Control
API не поднялся» или «запись не началась» — без единого указания на причину.

Так в цикле 65 упали 17 живых тестов при полном прогоне с окружением
проекта и прошли при прогоне без него. В CI джоба record-layer-live этой
переменной не задаёт, поэтому там дефект не проявлялся. В пакете не грозит:
у юнита MediaMTX свой mediamtx.env (backend/tests/test_deb_packaging.py).
"""
import os


def mediamtx_env() -> dict:
    return {k: v for k, v in os.environ.items() if not k.startswith(("RTSP_", "MTX_"))}
