"""Controller-side client and PEP 768 bootstrap support."""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from pyvivo import agent


def socket_path(pid: int) -> Path:
    return Path(tempfile.gettempdir()) / f"pyvivo-{os.getuid()}-{pid}.sock"


def request(
    pid: int, payload: dict[str, Any], timeout: float = 5.0
) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path(pid)))
        file = client.makefile("rwb")
        file.write(json.dumps(payload).encode() + b"\n")
        file.flush()
        response = json.loads(file.readline())
    if not response["ok"]:
        raise RuntimeError(response["error"] + "\n" + response["traceback"])
    return response["result"]


def is_attached(pid: int) -> bool:
    try:
        request(pid, {"op": "ping"}, timeout=0.2)
        return True
    except (OSError, RuntimeError):
        return False


def attach(pid: int, timeout: float = 8.0) -> dict[str, Any]:
    if not hasattr(sys, "remote_exec"):
        raise RuntimeError("the controller must run with CPython 3.14 or newer")
    if is_attached(pid):
        return request(pid, {"op": "ping"})

    path = socket_path(pid)
    path.unlink(missing_ok=True)
    agent_path = Path(agent.__file__).resolve()
    script_path: str | None = None
    remove_script = True
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="pyvivo-bootstrap-", delete=False
        ) as script:
            # The self-unlink is best effort: when the controller runs as root
            # (e.g. sudo on macOS) but the target runs as a normal user, the
            # target cannot delete this root-owned file from a sticky temp
            # directory. Swallowing that lets the agent install anyway; the
            # controller removes the file once the agent answers.
            bootstrap = (
                "import os, runpy\n"
                "try:\n"
                f"    os.unlink({script.name!r})\n"
                "except OSError:\n"
                "    pass\n"
                f"_pyvivo_ns = runpy.run_path({str(agent_path)!r})\n"
                f"_pyvivo_ns['install']({str(path)!r})\n"
            )
            script.write(bootstrap)
            script.flush()
            os.chmod(script.name, 0o644)
            script_path = script.name

        sys.remote_exec(pid, script_path)
        # The target may read the script at any later safe point, so until it
        # confirms the agent installed we must not remove a file it might still
        # read; the best-effort self-unlink covers the give-up case.
        remove_script = False
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                result = request(pid, {"op": "ping"}, timeout=0.2)
                # The agent answered, so the bootstrap ran to completion and the
                # target finished reading the script. The controller can now
                # remove it even when the target could not.
                remove_script = True
                return result
            except OSError as exc:
                last_error = exc
                time.sleep(0.05)
        raise TimeoutError(
            "remote script was submitted but the agent did not answer; "
            "the target may not have reached a safe evaluation point"
        ) from last_error
    finally:
        if script_path is not None and remove_script:
            Path(script_path).unlink(missing_ok=True)


def ensure_attached(pid: int) -> None:
    if not is_attached(pid):
        attach(pid)
