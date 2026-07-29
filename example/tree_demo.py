"""Programmatic multi-process (process-tree) demo of pyvivo.

Spawns ``example/tree_target.py`` (a root plus child workers), discovers the
tree from the OS, attaches to every process, hot-patches ``__main__:compute``
across all of them under one shared patch id, rolls back, and detaches. Each
step aggregates the independent per-process results.

    python example/tree_demo.py --children 2

On macOS attaching needs each target's task port, so run it with ``sudo``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

# Run straight from a checkout without installing the package first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyvivo.client import attach, request, socket_path
from pyvivo.process_tree import discover_tree, node_dict

HERE = Path(__file__).resolve().parent


def show(label: str, value: object) -> None:
    print(f"\n{label}")
    print(json.dumps(value, indent=2, sort_keys=True))


def eval_each(nodes: list, code: str) -> list[dict]:
    rows = []
    for node in nodes:
        try:
            result = request(
                node.pid, {"op": "eval", "thread": "main", "frame": 0, "code": code}
            )
            rows.append({"pid": node.pid, "repr": result["repr"]})
        except Exception as exc:
            rows.append({"pid": node.pid, "error": f"{type(exc).__name__}: {exc}"})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--children", type=int, default=2)
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()

    root = subprocess.Popen(
        [
            sys.executable,
            str(HERE / "tree_target.py"),
            "--children",
            str(args.children),
            "--interval",
            str(args.interval),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    node_pids: list[int] = []
    try:
        assert root.stdout is not None
        print(root.stdout.readline().rstrip())
        nodes = discover_tree(root.pid)
        node_pids = [node.pid for node in nodes]
        show(
            f"discovered {len(nodes)} process(es) under the root:",
            [node_dict(node) for node in nodes],
        )

        for node in nodes:
            attach(node.pid)
        show(
            "attached every process:",
            [{"pid": node.pid, "attached": True} for node in nodes],
        )
        show(
            "evaluate (counter, multiplier):",
            eval_each(nodes, "(counter, multiplier)"),
        )
        show("compute(2) before patch:", eval_each(nodes, "compute(2)"))

        # Patch compute() everywhere under one shared id (it returns value * 100).
        shared_patch_id = uuid.uuid4().hex[:12]
        patch_file = HERE / "patch_compute.py"
        patch_source = patch_file.read_text()
        patched = []
        for node in nodes:
            try:
                result = request(
                    node.pid,
                    {
                        "op": "patch",
                        "target": "__main__:compute",
                        "source": patch_source,
                        "filename": str(patch_file),
                        "patch_id": shared_patch_id,
                    },
                )
                patched.append(
                    {
                        "pid": node.pid,
                        "patch_id": result["patch_id"],
                        "active": result["active"],
                    }
                )
            except Exception as exc:
                patched.append(
                    {"pid": node.pid, "error": f"{type(exc).__name__}: {exc}"}
                )
        show(
            f"patch __main__:compute in every process with shared id "
            f"{shared_patch_id}:",
            patched,
        )
        show("compute(2) after patch:", eval_each(nodes, "compute(2)"))

        rolled = []
        for node in nodes:
            try:
                result = request(
                    node.pid, {"op": "rollback", "patch_id": shared_patch_id}
                )
                rolled.append({"pid": node.pid, "status": result["status"]})
            except Exception as exc:
                rolled.append(
                    {"pid": node.pid, "error": f"{type(exc).__name__}: {exc}"}
                )
        show(f"roll back shared id {shared_patch_id}:", rolled)
        show("compute(2) after rollback:", eval_each(nodes, "compute(2)"))

        detached = []
        for node in nodes:
            try:
                result = request(node.pid, {"op": "detach", "policy": "refuse"})
                detached.append({"pid": node.pid, "status": result["status"]})
            except Exception as exc:
                detached.append(
                    {"pid": node.pid, "error": f"{type(exc).__name__}: {exc}"}
                )
        show("detach every process:", detached)
        return 0
    finally:
        root.terminate()
        try:
            root.wait(timeout=3)
        except subprocess.TimeoutExpired:
            root.kill()
            root.wait()
        for pid in node_pids:
            socket_path(pid).unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PermissionError as exc:
        print(f"\npermission denied while attaching: {exc}", file=sys.stderr)
        print(
            "On macOS, attaching needs each target's task port: rerun with sudo "
            "(`sudo python example/tree_demo.py`). On Linux, ensure ptrace permits "
            "tracing (yama ptrace_scope).",
            file=sys.stderr,
        )
        raise SystemExit(2)
