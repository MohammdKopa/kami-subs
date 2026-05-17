"""
Kami Subs — Native Messaging host.

Spawned by Chrome (via launcher.bat) when the extension calls
chrome.runtime.connectNative('com.kamisubs.host'). Speaks Chrome's
length-prefixed JSON protocol on stdin/stdout, launches the backend
server as a subprocess, and shuts it down when Chrome disconnects.

Protocol (Chrome <-> host):
  Each message is a 4-byte little-endian uint32 length, followed by that
  many bytes of UTF-8 JSON.

Messages from extension -> host:
  { "type": "start", "model"?: str, "device"?: str, "compute"?: str,
    "translator"?: str, "host"?: str, "port"?: int }
  { "type": "stop" }
  { "type": "status" }

Messages from host -> extension:
  { "type": "started",      "pid": int, "wsUrl": str }
  { "type": "already_up",   "wsUrl": str }            # port was already bound
  { "type": "stopped" }
  { "type": "status",       "running": bool, "pid": int|null }
  { "type": "error",        "message": str }
  { "type": "log",          "line": str }              # backend stdout/stderr
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
EXT_DIR = HERE.parent
BACKEND_DIR = (EXT_DIR.parent / "backend").resolve()
VENV_PY = BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
SERVER_PY = BACKEND_DIR / "server.py"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Windows: prevent spawned backend from popping a console window.
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def read_message() -> Optional[dict]:
    raw_len = sys.stdin.buffer.read(4)
    if not raw_len:
        return None  # stdin closed = Chrome disconnected
    if len(raw_len) < 4:
        return None
    n = struct.unpack("<I", raw_len)[0]
    payload = sys.stdin.buffer.read(n)
    if len(payload) < n:
        return None
    try:
        return json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        return {"type": "__bad_json__"}


def send_message(obj: dict[str, Any]) -> None:
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(data)))
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        try:
            s.connect((host, port))
            return True
        except OSError:
            return False


class BackendManager:
    def __init__(self) -> None:
        self.proc: Optional[subprocess.Popen] = None
        self.host: str = DEFAULT_HOST
        self.port: int = DEFAULT_PORT
        self._log_thread: Optional[threading.Thread] = None

    @property
    def ws_url(self) -> str:
        return f"ws://{self.host}:{self.port}/ws"

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, cfg: dict) -> dict:
        self.host = cfg.get("host") or DEFAULT_HOST
        self.port = int(cfg.get("port") or DEFAULT_PORT)

        # If something is already bound on the port, assume it's the backend
        # (manually started) and just point the extension at it.
        if port_in_use(self.host, self.port):
            return {"type": "already_up", "wsUrl": self.ws_url}

        if not VENV_PY.exists():
            return {"type": "error",
                    "message": f"venv python not found at {VENV_PY}. Run pip install in backend/.venv first."}
        if not SERVER_PY.exists():
            return {"type": "error", "message": f"server.py not found at {SERVER_PY}"}

        env = os.environ.copy()
        for k_env, k_cfg in (
            ("KAMI_MODEL", "model"),
            ("KAMI_DEVICE", "device"),
            ("KAMI_COMPUTE", "compute"),
            ("KAMI_TRANSLATOR", "translator"),
            ("KAMI_HOST", "host"),
            ("KAMI_PORT", "port"),
        ):
            v = cfg.get(k_cfg)
            if v is not None and v != "":
                env[k_env] = str(v)

        try:
            self.proc = subprocess.Popen(
                [str(VENV_PY), "-u", str(SERVER_PY)],
                cwd=str(BACKEND_DIR),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as e:
            return {"type": "error", "message": f"failed to spawn backend: {e}"}

        # Pipe backend stdout/stderr lines back to extension as 'log' messages.
        # Useful for surfacing errors in the popup without leaving a console.
        self._log_thread = threading.Thread(
            target=self._pump_logs, daemon=True, name="kami-backend-logs"
        )
        self._log_thread.start()

        # CRITICAL: wait for the server to actually accept connections before
        # declaring "started". Popen returns the moment the process forks —
        # well before uvicorn binds the port (whisper model load takes 1-4s,
        # large-v3 on cold disk ~10s). If we announce 'started' early, the
        # extension's WS open call races the model load and fails.
        deadline = time.monotonic() + 60.0   # generous: covers large model cold load
        while time.monotonic() < deadline:
            # If the backend crashed during startup, surface that instead of
            # waiting the full timeout.
            if self.proc.poll() is not None:
                code = self.proc.returncode
                self.proc = None
                return {"type": "error",
                        "message": f"backend exited during startup (code={code}). "
                                   "Check log lines above for the traceback."}
            if port_in_use(self.host, self.port):
                return {"type": "started", "pid": self.proc.pid, "wsUrl": self.ws_url}
            time.sleep(0.15)

        # Hit the deadline — leave the process running but tell the extension.
        return {"type": "error",
                "message": f"backend did not open port {self.port} within 60s; "
                           "model load may be stuck or cuda init failed."}

    def _pump_logs(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            for raw in self.proc.stdout:
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip()
                except Exception:
                    continue
                if not line:
                    continue
                try:
                    send_message({"type": "log", "line": line})
                except (BrokenPipeError, OSError):
                    return  # extension disconnected; let main loop tear down
        except Exception:
            pass

    def stop(self) -> dict:
        if not self.running():
            self.proc = None
            return {"type": "stopped"}
        assert self.proc is not None
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        except Exception:
            pass
        self.proc = None
        return {"type": "stopped"}

    def status(self) -> dict:
        return {
            "type": "status",
            "running": self.running(),
            "pid": self.proc.pid if self.running() and self.proc else None,
            "wsUrl": self.ws_url,
        }


def main() -> int:
    backend = BackendManager()
    try:
        while True:
            msg = read_message()
            if msg is None:
                break
            mtype = msg.get("type")
            if mtype == "start":
                send_message(backend.start(msg))
            elif mtype == "stop":
                send_message(backend.stop())
            elif mtype == "status":
                send_message(backend.status())
            elif mtype == "__bad_json__":
                send_message({"type": "error", "message": "malformed JSON from extension"})
            else:
                send_message({"type": "error", "message": f"unknown message type: {mtype!r}"})
    finally:
        # Chrome closed the pipe (or we crashed) — kill the backend so we
        # don't leak a process every time the popup closes mid-session.
        backend.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
