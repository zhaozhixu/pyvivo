"""Command-line interface for pyvivo."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path
from typing import Any

from pyvivo.client import attach, ensure_attached, is_attached, request
from pyvivo.process_tree import discover_tree, node_dict


def print_json(value: Any) -> None:
    print(json.dumps(value, indent=2, sort_keys=True))


def code_input(args: argparse.Namespace) -> tuple[str, str]:
    """Return source and its diagnostic filename for eval/exec commands."""
    if args.file is not None:
        path = Path(args.file).resolve()
        return path.read_text(), str(path)
    if args.code is not None:
        return args.code, f"<pyvivo {args.command}>"
    raise ValueError(f"{args.command} requires inline CODE or --file PATH")


def execute_command(pid: int, args: argparse.Namespace) -> Any:
    operation = args.command
    if operation == "attach":
        return attach(pid, args.timeout)
    if operation == "detach" and not is_attached(pid):
        return {"status": "not attached", "pid": pid}
    ensure_attached(pid)
    if operation in ("ping", "threads"):
        return request(pid, {"op": operation})
    if operation == "patches":
        return request(
            pid,
            {"op": "patches", "patch_id": args.patch_id, "full": args.full},
        )
    if operation == "detach":
        policy = (
            "rollback"
            if args.rollback
            else "keep"
            if args.keep_patches
            else "refuse"
        )
        return request(pid, {"op": "detach", "policy": policy})
    if operation == "stack":
        return request(pid, {"op": "stack", "thread": args.thread})
    if operation == "locals":
        return request(
            pid, {"op": "locals", "thread": args.thread, "frame": args.frame}
        )
    if operation in ("eval", "exec"):
        source, filename = code_input(args)
        return request(
            pid,
            {
                "op": operation,
                "thread": args.thread,
                "frame": args.frame,
                "code": source,
                "filename": filename,
            },
        )
    if operation == "patch":
        path = Path(args.file).resolve()
        return request(
            pid,
            {
                "op": "patch",
                "target": args.target,
                "source": path.read_text(),
                "filename": str(path),
            },
        )
    if operation == "rollback":
        return request(pid, {"op": "rollback", "patch_id": args.patch_id})
    raise AssertionError(operation)


def execute_tree(root_pid: int, args: argparse.Namespace) -> dict[str, Any]:
    nodes = discover_tree(root_pid, include_root=not args.descendants_only)
    if args.tree_command == "list":
        return {"root_pid": root_pid, "processes": [node_dict(node) for node in nodes]}

    shared_patch_id = uuid.uuid4().hex[:12] if args.tree_command == "patch" else None

    results = []
    for node in nodes:
        item = node_dict(node)
        try:
            if args.tree_command == "attach":
                value = attach(node.pid, args.timeout)
            elif args.tree_command == "detach" and not is_attached(node.pid):
                value = {"status": "not attached", "pid": node.pid}
            else:
                ensure_attached(node.pid)
                if args.tree_command == "stack":
                    value = request(node.pid, {"op": "stack", "thread": args.thread})
                elif args.tree_command in ("eval", "exec"):
                    source, filename = code_input(args)
                    value = request(
                        node.pid,
                        {
                            "op": args.tree_command,
                            "thread": args.thread,
                            "frame": args.frame,
                            "code": source,
                            "filename": filename,
                        },
                    )
                elif args.tree_command == "patch":
                    path = Path(args.file).resolve()
                    value = request(
                        node.pid,
                        {
                            "op": "patch",
                            "target": args.target,
                            "source": path.read_text(),
                            "filename": str(path),
                            "patch_id": shared_patch_id,
                        },
                    )
                elif args.tree_command == "patches":
                    value = request(
                        node.pid,
                        {
                            "op": "patches",
                            "patch_id": args.patch_id,
                            "full": args.full,
                        },
                    )
                elif args.tree_command == "rollback":
                    value = request(
                        node.pid, {"op": "rollback", "patch_id": args.patch_id}
                    )
                elif args.tree_command == "detach":
                    policy = (
                        "rollback"
                        if args.rollback
                        else "keep"
                        if args.keep_patches
                        else "refuse"
                    )
                    value = request(node.pid, {"op": "detach", "policy": policy})
                else:
                    raise AssertionError(args.tree_command)
        except Exception as exc:
            item.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        else:
            item.update(ok=True, result=value)
        results.append(item)
    response = {"root_pid": root_pid, "processes": results}
    if shared_patch_id is not None:
        response["patch_id"] = shared_patch_id
    return response


def add_remote_commands(parser: argparse.ArgumentParser) -> None:
    parser.epilog = (
        "Every command except attach auto-attaches an agent if one is not "
        "already running. detach does not attach merely to report that a "
        "process is already detached."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    attach_parser = commands.add_parser(
        "attach",
        help="attach an agent",
        description="Bootstrap the in-process agent with sys.remote_exec and "
        "wait for it to answer.",
    )
    attach_parser.add_argument(
        "--timeout",
        type=float,
        default=8.0,
        help="seconds to wait for the agent to answer (default: 8)",
    )
    commands.add_parser("ping", help="check agent availability")
    commands.add_parser("threads", help="list Python threads and each top frame")

    detach = commands.add_parser(
        "detach",
        help="remove the in-process agent",
        description="Detach the agent. Refuses while any patch is tracked "
        "unless a policy is given. Arbitrary exec side effects are not undone.",
    )
    detach_policy = detach.add_mutually_exclusive_group()
    detach_policy.add_argument(
        "--rollback",
        action="store_true",
        help="roll back all tracked patches, in reverse order, before detaching",
    )
    detach_policy.add_argument(
        "--keep-patches",
        action="store_true",
        help="leave code patched but discard rollback records; the original "
        "code can no longer be reconstructed",
    )

    patches = commands.add_parser(
        "patches",
        help="list tracked patches",
        description="List patches tracked in target-process memory. Records are "
        "not durable across process exit or detach --keep-patches.",
    )
    patches.add_argument("patch_id", nargs="?", help="show only one patch")
    patches.add_argument(
        "--full",
        action="store_true",
        help="also include recovered old source, submitted source, and a diff",
    )

    stack = commands.add_parser(
        "stack",
        help="show a thread stack",
        description="Show a thread's stack as a concurrent snapshot; the thread "
        "is not paused. Each frame reports file, line, function, module, and the "
        "current source line when linecache can retrieve it.",
    )
    stack.add_argument(
        "--thread", default="main", help="'main' or a numeric thread id (default: main)"
    )

    locals_parser = commands.add_parser(
        "locals",
        help="show frame locals",
        description="Show a frame's locals as safe, length-limited reprs.",
    )
    locals_parser.add_argument(
        "--thread", default="main", help="'main' or a numeric thread id (default: main)"
    )
    locals_parser.add_argument(
        "--frame",
        type=int,
        default=0,
        help="frame depth from the top of the stack (default: 0)",
    )

    for name in ("eval", "exec"):
        command = commands.add_parser(
            name,
            help=f"{name} source in a frame",
            description="Run source as "
            f"{name}(code, frame.f_globals, frame.f_locals) in the selected "
            "thread and frame, so a bare name resolves through frame locals, "
            "frame globals, then builtins. --file reads source on the controller "
            "and keeps its absolute path as the compiled filename, so tracebacks "
            "point at the original file."
            + ("" if name == "eval" else " Ordinary statements are allowed."),
        )
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("code", nargs="?", help="inline Python source")
        source.add_argument("--file", help="read source from a local file")
        command.add_argument(
            "--thread",
            default="main",
            help="'main' or a numeric thread id (default: main)",
        )
        command.add_argument(
            "--frame",
            type=int,
            default=0,
            help="frame depth from the top of the stack (default: 0)",
        )

    patch = commands.add_parser(
        "patch",
        help="replace a function code object",
        description="Replace a function's __code__ in place, so every existing "
        "reference sees the new behavior. The file must define a function of the "
        "same name with a compatible call shape and free variables.",
    )
    patch.add_argument("target", help="MODULE:ATTRIBUTE[.ATTRIBUTE...]")
    patch.add_argument("file", help="file defining the replacement function")

    rollback = commands.add_parser(
        "rollback",
        help="roll back one patch",
        description="Restore a patched function's original code. Refuses if the "
        "function changed since the patch was installed.",
    )
    rollback.add_argument("patch_id")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pyvivo",
        description="Inspect and modify a running CPython 3.14+ process through "
        "an agent bootstrapped with PEP 768 sys.remote_exec. Research prototype: "
        "attaching grants arbitrary code execution in the target.",
    )
    top = parser.add_subparsers(dest="mode", required=True)

    remote = top.add_parser(
        "remote",
        help="attach to or command an existing PID",
        description="Attach to or command a single existing process by PID.",
    )
    remote.add_argument("pid", type=int)
    add_remote_commands(remote)

    tree = top.add_parser(
        "tree",
        help="command a local process and its descendants",
        description="Discover a process and its descendants (via /proc on Linux, "
        "ps elsewhere) and run one command against each, aggregating per-process "
        "ok/error results. A tree patch shares one patch id across processes for "
        "a later tree-wide rollback.",
    )
    tree.add_argument("root_pid", type=int)
    tree.add_argument(
        "--descendants-only", action="store_true", help="exclude the root process"
    )

    tree_commands = tree.add_subparsers(dest="tree_command", required=True)
    tree_commands.add_parser("list", help="list the process tree")
    tree_attach = tree_commands.add_parser("attach", help="attach to each process")
    tree_attach.add_argument("--timeout", type=float, default=8.0)
    tree_stack = tree_commands.add_parser("stack", help="show every process stack")
    tree_stack.add_argument("--thread", default="main")
    for name in ("eval", "exec"):
        command = tree_commands.add_parser(name, help=f"{name} on every process")
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("code", nargs="?", help="inline Python source")
        source.add_argument("--file", help="read source from a local file")
        command.add_argument("--thread", default="main")
        command.add_argument("--frame", type=int, default=0)
        command.set_defaults(command=name)
    tree_patch = tree_commands.add_parser("patch", help="patch every process")
    tree_patch.add_argument("target", help="MODULE:ATTRIBUTE[.ATTRIBUTE...]")
    tree_patch.add_argument("file", help="file defining the replacement function")
    tree_patches = tree_commands.add_parser("patches", help="list patches in every process")
    tree_patches.add_argument("patch_id", nargs="?", help="show one patch ID")
    tree_patches.add_argument("--full", action="store_true")
    tree_rollback = tree_commands.add_parser("rollback", help="roll back a patch in every process")
    tree_rollback.add_argument("patch_id")
    tree_detach = tree_commands.add_parser("detach", help="detach each process")
    tree_policy = tree_detach.add_mutually_exclusive_group()
    tree_policy.add_argument("--rollback", action="store_true")
    tree_policy.add_argument("--keep-patches", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.mode == "tree":
            print_json(execute_tree(args.root_pid, args))
            return 0
        print_json(execute_command(args.pid, args))
        return 0
    except PermissionError as exc:
        print(f"permission denied while attaching: {exc}", file=sys.stderr)
        if sys.platform == "darwin":
            print(
                "On macOS, taking the target's Mach task port requires root: "
                "rerun the controller with sudo, and keep using sudo for "
                "follow-up commands so they compute the same socket path.",
                file=sys.stderr,
            )
        else:
            print(
                "On Linux, run the controller as a permitted tracer or grant "
                "CAP_SYS_PTRACE to a private copied Python executable.",
                file=sys.stderr,
            )
        return 2
    except Exception as exc:
        print(f"pyvivo: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
