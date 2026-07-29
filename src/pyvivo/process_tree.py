"""Framework-neutral local process-tree discovery.

Reads ``/proc`` where it exists (Linux); elsewhere (macOS, BSDs) falls back
to the portable ``ps`` command.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_PROC = Path("/proc")


@dataclass(frozen=True)
class ProcessNode:
    pid: int
    ppid: int | None
    command: str
    depth: int


def _read_ppid(path: Path) -> int | None:
    for line in path.read_text().splitlines():
        if line.startswith("PPid:"):
            return int(line.split()[1])
    return None


def _read_command(proc: Path) -> str:
    return proc.joinpath("cmdline").read_bytes().replace(b"\0", b" ").decode().strip()


def _proc_table(proc_root: Path) -> dict[int, tuple[int | None, str]]:
    table = {}
    for proc in proc_root.iterdir():
        if not proc.name.isdigit():
            continue
        try:
            command = _read_command(proc)
            table[int(proc.name)] = (_read_ppid(proc / "status"), command)
        except (FileNotFoundError, PermissionError, ProcessLookupError, UnicodeDecodeError):
            continue
    return table


def _parse_ps_lines(lines: list[str]) -> dict[int, tuple[int | None, str]]:
    table = {}
    for line in lines:
        parts = line.split(None, 2)
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            continue
        command = parts[2].strip() if len(parts) == 3 else ""
        table[pid] = (ppid, command)
    return table


def _ps_table() -> dict[int, tuple[int | None, str]]:
    output = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,command="],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return _parse_ps_lines(output.splitlines())


def process_table(
    proc_root: Path | None = None,
) -> dict[int, tuple[int | None, str]]:
    """Map every visible pid to ``(ppid, command)``.

    An explicit ``proc_root`` always selects the ``/proc`` layout so tests can
    point at a synthetic tree on any OS. The default uses the real ``/proc``
    when present and ``ps`` otherwise.
    """
    if proc_root is not None:
        return _proc_table(proc_root)
    if _DEFAULT_PROC.is_dir():
        return _proc_table(_DEFAULT_PROC)
    return _ps_table()


def discover_tree(
    root_pid: int,
    *,
    include_root: bool = True,
    proc_root: Path | None = None,
) -> list[ProcessNode]:
    """Return root and all descendants in breadth-first tree order."""
    table = process_table(proc_root)
    if root_pid not in table:
        raise ProcessLookupError(root_pid)

    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    for values in children.values():
        values.sort()

    result = []
    queue = [(root_pid, 0)]
    seen = set()
    while queue:
        pid, depth = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        ppid, command = table[pid]
        if include_root or depth > 0:
            result.append(ProcessNode(pid=pid, ppid=ppid, command=command, depth=depth))
        queue.extend((child, depth + 1) for child in children.get(pid, []))
    return result


def node_dict(node: ProcessNode) -> dict[str, object]:
    return {
        "pid": node.pid,
        "ppid": node.ppid,
        "depth": node.depth,
        "command": node.command,
    }
