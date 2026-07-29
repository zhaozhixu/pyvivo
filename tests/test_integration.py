"""Live integration test: attach to a real process and drive the subcommands.

Unlike the unit tests, this spawns an actual target process, attaches an agent
through ``sys.remote_exec``, and runs the CLI subcommands end to end
(ping/threads/stack/locals/eval/exec/patch/patches/rollback/detach).

It needs permission to trace the target — ``sudo`` on macOS, or a permissive
ptrace policy on Linux. Without that (or a working agent) the attach raises and
the test fails; there is deliberately no skip. Run it on its own with:

    python -m unittest tests.test_integration
"""

import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyvivo import cli, client

REPO = Path(__file__).resolve().parent.parent
TARGET = REPO / "example" / "target.py"
PATCH = REPO / "example" / "patch_compute.py"


def _run(pid: int, argv: list[str]):
    """Parse and execute a ``remote PID ...`` command, returning its result."""
    args = cli.build_parser().parse_args(["remote", str(pid), *argv])
    return cli.execute_command(pid, args)


class LiveAttachTests(unittest.TestCase):
    def setUp(self):
        self.proc = subprocess.Popen(
            [sys.executable, str(TARGET), "--interval", "0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        # Cleanups run even when setUp fails, LIFO: detach, then terminate.
        self.addCleanup(self._cleanup)
        self.addCleanup(self._detach)
        time.sleep(0.5)  # let the interpreter come up before remote_exec
        client.attach(self.proc.pid, timeout=8.0)

    def _detach(self):
        # Best-effort: roll back any tracked patch and detach.
        try:
            if client.is_attached(self.proc.pid):
                client.request(
                    self.proc.pid, {"op": "detach", "policy": "rollback"}
                )
        except Exception:
            pass

    def _cleanup(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        client.socket_path(self.proc.pid).unlink(missing_ok=True)

    def test_full_subcommand_cycle(self):
        pid = self.proc.pid

        # inspection
        self.assertEqual(_run(pid, ["ping"])["pid"], pid)
        self.assertTrue(any(t["main"] for t in _run(pid, ["threads"])["threads"]))
        self.assertTrue(_run(pid, ["stack"])["stack"])
        self.assertIsInstance(_run(pid, ["locals"])["locals"], dict)

        # eval before patch: compute(3) == 3 * multiplier(2) == 6
        self.assertEqual(_run(pid, ["eval", "compute(3)"])["repr"], "6")

        # patch compute() to value * 100 (from example/patch_compute.py)
        patched = _run(pid, ["patch", "__main__:compute", str(PATCH)])
        self.assertTrue(patched["active"])
        patch_id = patched["patch_id"]
        self.assertEqual(_run(pid, ["eval", "compute(3)"])["repr"], "300")

        # the patch is tracked and listed
        listed = _run(pid, ["patches"])["patches"]
        self.assertEqual([p["patch_id"] for p in listed], [patch_id])

        # exec mutates a module global; the patched code ignores it
        _run(pid, ["exec", "import __main__; __main__.multiplier = 5"])
        self.assertEqual(_run(pid, ["eval", "multiplier"])["repr"], "5")
        self.assertEqual(_run(pid, ["eval", "compute(3)"])["repr"], "300")

        # rollback restores original code, which now uses the new multiplier
        self.assertEqual(_run(pid, ["rollback", patch_id])["status"], "rolled back")
        self.assertEqual(_run(pid, ["eval", "compute(3)"])["repr"], "15")

        # detach cleanly (no tracked patches remain after rollback)
        self.assertEqual(_run(pid, ["detach"])["status"], "detaching")
        self.assertFalse(client.is_attached(pid))


if __name__ == "__main__":
    unittest.main()
