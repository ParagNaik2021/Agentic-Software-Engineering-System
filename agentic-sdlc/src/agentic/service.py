"""Auto-start of the generated URL Shortener app as a detached background
process once a workflow run reaches SUCCEEDED (see cli.py's `run`/`resume`
commands). Kept entirely separate from core/engine.py: this is a
post-success convenience step, not part of orchestration or the
replayable event-log/state machine, so it must never be imported by
anything on the deterministic --mode replay path.

The launched process is workspace/tester/serve.py, not a bare
`uvicorn app.main:app` — serve.py imports the generated app and mounts
the manual-tester UI at /tester on the same origin (see that file's own
docstring for why this lives outside workspace/urlshortener/ entirely).
Launching the bare app would leave /tester 404ing, defeating the point
of auto-starting it in the first place.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8000
_HEALTH_TIMEOUT_S = 10.0
_HEALTH_POLL_INTERVAL_S = 0.25
_PORT_RELEASE_TIMEOUT_S = 5.0


@dataclass
class ServiceInfo:
    host: str
    port: int
    pid: int
    healthy: bool
    note: str | None = None


def _tracker_path(workspace_dir: Path) -> Path:
    """One tracker file per generated app, sitting next to it (not inside
    it) so it survives the workspace reset that regenerating the app
    performs, and never shows up in workspace_dir's own file listing."""
    return workspace_dir.parent / f".{workspace_dir.name}_service.json"


def _read_tracked(workspace_dir: Path) -> dict | None:
    path = _tracker_path(workspace_dir)
    if not path.exists():
        return None
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_tracked(workspace_dir: Path, pid: int, port: int) -> None:
    import json

    _tracker_path(workspace_dir).write_text(json.dumps({"pid": pid, "port": port}), encoding="utf-8")


def _port_in_use(port: int, host: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def _free_port(host: str) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _process_alive(pid: int) -> bool:
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"],
            capture_output=True, text=True, check=False,
        )
        return str(pid) in result.stdout
    import os

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _kill_process(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True, check=False)
        return
    import contextlib
    import os
    import signal

    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


def app_is_built(workspace_dir: Path) -> bool:
    """Whether `workspace_dir` actually holds a runnable generated app.
    The greenfield/brownfield workflows produce one; `ambiguous` ends at
    design.arch and never writes code, so after that workflow this is
    only true if an earlier run left an app behind to serve."""
    return (workspace_dir / "app" / "main.py").is_file()


def tracked_instance(workspace_dir: Path) -> tuple[int, int] | None:
    """(pid, port) of the agentic-managed instance still running for this
    workspace, or None. Used by `agentic stop` for clean shutdown."""
    tracked = _read_tracked(workspace_dir)
    if not tracked:
        return None
    pid, port = tracked.get("pid"), tracked.get("port")
    if not isinstance(pid, int) or not isinstance(port, int) or not _process_alive(pid):
        return None
    return pid, port


def stop_service(workspace_dir: Path) -> tuple[int, int] | None:
    """Kill the tracked instance and clear the tracker. Returns the
    (pid, port) that was stopped, or None if nothing was running."""
    instance = tracked_instance(workspace_dir)
    if instance is None:
        _tracker_path(workspace_dir).unlink(missing_ok=True)
        return None
    pid, port = instance
    _kill_process(pid)
    _tracker_path(workspace_dir).unlink(missing_ok=True)
    return pid, port


def stop_hint(pid: int) -> str:
    if sys.platform == "win32":
        return f"taskkill /PID {pid} /F"
    return f"kill {pid}"


def _wait_until(predicate, timeout: float, interval: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _wait_for_health(host: str, port: int) -> bool:
    def _check() -> bool:
        try:
            with urllib.request.urlopen(f"http://{host}:{port}/healthz", timeout=1.0) as resp:  # noqa: S310
                return resp.status == 200
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    return _wait_until(_check, _HEALTH_TIMEOUT_S, _HEALTH_POLL_INTERVAL_S)


def _spawn(serve_script: Path, host: str, port: int) -> int:
    args = [sys.executable, str(serve_script), "--host", host, "--port", str(port)]
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **kwargs)  # noqa: S603
    return proc.pid


def start_service(workspace_dir: Path, requested_port: int, host: str = "127.0.0.1") -> ServiceInfo:
    """Start (or reuse) the tester-UI-augmented app server for the app
    that was just generated at `workspace_dir`. Returns the ACTUAL host/
    port/pid used, which may differ from `requested_port` if it was
    occupied by something this module doesn't track."""
    serve_script = workspace_dir.parent / "tester" / "serve.py"
    tracked = _read_tracked(workspace_dir)
    port = requested_port
    note: str | None = None

    if _port_in_use(port, host):
        if tracked and tracked.get("port") == port and _process_alive(tracked["pid"]):
            old_pid = tracked["pid"]
            _kill_process(old_pid)
            _wait_until(lambda: not _port_in_use(port, host), _PORT_RELEASE_TIMEOUT_S, 0.2)
            note = (
                f"Port {port} was held by a previous agentic-managed instance "
                f"(pid {old_pid}) — stopped it and restarted against the current app."
            )
        else:
            free_port = _free_port(host)
            note = f"Port {port} is already in use by another process — started on free port {free_port} instead."
            port = free_port

    pid = _spawn(serve_script, host, port)
    _write_tracked(workspace_dir, pid, port)
    healthy = _wait_for_health(host, port)
    return ServiceInfo(host=host, port=port, pid=pid, healthy=healthy, note=note)
