"""JWT из рукопожатия WebSocket не оседает в журнале backend (D10).

DEPLOY_FINDINGS D10 (развёртывание 2026-10-28): при открытом интерфейсе
строка `"WebSocket /ws/faces?token=<JWT>" [accepted]` попадала в журнал
uvicorn с полным действующим токеном. Редактирование query в nginx не
защищает журнал самого backend, а `RedactedAccessFormatter` закрывал только
`uvicorn.access` — рукопожатие WS uvicorn пишет логгером `uvicorn.error`.

Проверка идёт production path: настоящий uvicorn в отдельном процессе с
**боевым** `app/logging_config.json` (тот, что подставляют Dockerfile и
systemd-юнит), настоящее рукопожатие, разбор того, что процесс напечатал.
Юнит-тест форматтера здесь не годится: дефект сидел не в форматтере, а в
том, через какой логгер uvicorn пишет строку, — и в следующей версии
uvicorn может сменить его снова.
"""
import asyncio
import os
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

websockets = pytest.importorskip("websockets", reason="нужен клиент WebSocket (ставится с uvicorn[standard])")

BACKEND = Path(__file__).resolve().parents[1]
SECRET = "eyJTOKEN.must-not-reach-the-log.d10"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def server(tmp_path):
    """uvicorn с боевым log-config и минимальным WS-приложением.

    Приложение своё, а не app.main: рукопожатие логирует uvicorn до
    обработчика, и для проверки журнала база, Redis и авторизация не
    нужны. Принимающий и отвергающий маршруты — оба пути логирования.
    """
    (tmp_path / "wsprobe.py").write_text(textwrap.dedent('''
        from fastapi import FastAPI, WebSocket
        app = FastAPI()

        @app.websocket("/ws/ok")
        async def ok(ws: WebSocket):
            await ws.accept()
            await ws.close()

        @app.websocket("/ws/deny")
        async def deny(ws: WebSocket):
            await ws.close(code=4401)
    '''))
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": f"{tmp_path}{os.pathsep}{BACKEND}"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "wsprobe:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-config", str(BACKEND / "app" / "logging_config.json")],
        cwd=BACKEND, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            if proc.poll() is not None:
                pytest.fail(f"uvicorn не поднялся:\n{proc.stdout.read()}")
            time.sleep(0.1)
    try:
        yield port, proc
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _handshakes(port):
    async def run():
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws/ok?token={SECRET}&x=1"):
            pass
        try:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws/deny?token={SECRET}"):
                pass
        except Exception:
            pass
    asyncio.run(run())


def test_ws_handshake_line_has_no_token(server):
    port, proc = server
    _handshakes(port)
    time.sleep(0.5)
    proc.terminate()
    out, _ = proc.communicate(timeout=10)

    ws_lines = [line for line in out.splitlines() if "WebSocket" in line]
    # Сначала — что строки рукопожатия вообще есть. Иначе тест был бы
    # зелёным и на конфиге, который глушит логирование целиком.
    assert len(ws_lines) >= 2, f"не видно строк рукопожатия WS в журнале:\n{out}"
    assert SECRET not in out, (
        "JWT из ?token= попал в журнал backend:\n"
        + "\n".join(line for line in out.splitlines() if SECRET in line)
    )
    assert all("token=REDACTED" in line for line in ws_lines), ws_lines
    # Остальные параметры query не трогаются — диагностика не теряется.
    assert any("x=1" in line for line in ws_lines), ws_lines
