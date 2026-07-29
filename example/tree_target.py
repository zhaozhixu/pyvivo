"""Root of a small process tree, for the pyvivo tree example.

Spawns child workers (each ``example/target.py``) and then runs the same tick
loop itself, so the whole tree is homogeneous: every process exposes
``__main__:compute`` over its own ``counter``. Children are terminated when
this root receives SIGTERM, so tearing the root down never orphans workers.

    python example/tree_target.py --children 2
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

counter = 0
multiplier = 2


def compute(value: int) -> int:
    return value * multiplier


def _spawn_children(count: int, interval: float) -> list[subprocess.Popen]:
    target = str(Path(__file__).resolve().parent / "target.py")
    children = []
    for _ in range(count):
        children.append(
            subprocess.Popen(
                [sys.executable, target, "--interval", str(interval)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
    return children


def main() -> None:
    global counter
    parser = argparse.ArgumentParser()
    parser.add_argument("--children", type=int, default=2)
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()

    children = _spawn_children(args.children, args.interval)

    def _terminate(_signum, _frame):
        for child in children:
            child.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _terminate)

    print(
        f"tree root pid={os.getpid()} children={[c.pid for c in children]}",
        flush=True,
    )
    try:
        while True:
            compute(counter)
            counter += 1
            time.sleep(args.interval)
    finally:
        for child in children:
            child.terminate()


if __name__ == "__main__":
    main()
