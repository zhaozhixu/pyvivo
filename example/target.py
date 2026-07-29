"""Long-running, initially uninstrumented process for the pyvivo experiment."""

from __future__ import annotations

import argparse
import os
import threading
import time

counter = 0
multiplier = 2


def compute(value: int) -> int:
    return value * multiplier


def background_worker() -> None:
    while True:
        time.sleep(10)


def main() -> None:
    global counter
    parser = argparse.ArgumentParser()
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    threading.Thread(target=background_worker, name="example-worker", daemon=True).start()
    print(f"target pid={os.getpid()}", flush=True)
    while True:
        result = compute(counter)
        print(f"tick counter={counter} result={result}", flush=True)
        counter += 1
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
