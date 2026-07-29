"""Programmatic single-process demo of pyvivo.

Drives pyvivo's client API end to end: spawn ``example/target.py`` as a child,
attach an in-process agent with PEP 768 ``sys.remote_exec``, inspect the target,
hot-patch ``__main__:compute``, roll the patch back, and detach.

    python example/demo.py

On macOS attaching needs the target's task port, so run it with ``sudo``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

# Run straight from a checkout without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyvivo.client import attach, request, socket_path

HERE = Path(__file__).resolve().parent


def show(label: str, value: object) -> None:
    print(f"\n{label}")
    print(json.dumps(value, indent=2, sort_keys=True))


def main() -> int:
    target = subprocess.Popen(
        [sys.executable, str(HERE / "target.py"), "--interval", "0.25"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        assert target.stdout is not None
        print(target.stdout.readline().rstrip())
        print(f"controller is parent of target; attaching to pid {target.pid}")
        show("attach:", attach(target.pid))
        show("threads:", request(target.pid, {"op": "threads"}))
        show(
            "main-thread stack:",
            request(target.pid, {"op": "stack", "thread": "main"}),
        )
        show(
            "evaluate (counter, multiplier) through the main frame:",
            request(
                target.pid,
                {
                    "op": "eval",
                    "thread": "main",
                    "frame": 0,
                    "code": "(counter, multiplier)",
                },
            ),
        )

        # Hot-patch compute() using the sibling patch file (it returns value * 100).
        patch_file = HERE / "patch_compute.py"
        patch = request(
            target.pid,
            {
                "op": "patch",
                "target": "__main__:compute",
                "source": patch_file.read_text(),
                "filename": str(patch_file),
            },
        )
        show("installed patch:", patch)
        show(
            "evaluate compute(3) after patch:",
            request(
                target.pid,
                {"op": "eval", "thread": "main", "frame": 0, "code": "compute(3)"},
            ),
        )

        show(
            "rollback:",
            request(target.pid, {"op": "rollback", "patch_id": patch["patch_id"]}),
        )
        show(
            "evaluate compute(3) after rollback:",
            request(
                target.pid,
                {"op": "eval", "thread": "main", "frame": 0, "code": "compute(3)"},
            ),
        )
        show("detach:", request(target.pid, {"op": "detach", "policy": "refuse"}))
        return 0
    finally:
        target.terminate()
        try:
            target.wait(timeout=3)
        except subprocess.TimeoutExpired:
            target.kill()
            target.wait()
        socket_path(target.pid).unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PermissionError as exc:
        print(f"\npermission denied while attaching: {exc}", file=sys.stderr)
        print(
            "On macOS, attaching needs the target's task port: rerun with sudo "
            "(`sudo python example/demo.py`). On Linux, ensure ptrace permits "
            "tracing (yama ptrace_scope).",
            file=sys.stderr,
        )
        raise SystemExit(2)
