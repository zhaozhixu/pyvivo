"""Tests for controller-side helpers that don't require a live target."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyvivo import client


class SocketPathTests(unittest.TestCase):
    def test_path_includes_uid_and_pid(self):
        path = client.socket_path(4321)
        self.assertEqual(path.parent, Path(tempfile.gettempdir()))
        self.assertEqual(path.name, f"pyvivo-{os.getuid()}-4321.sock")

    def test_distinct_pids_distinct_paths(self):
        self.assertNotEqual(client.socket_path(1), client.socket_path(2))


class IsAttachedTests(unittest.TestCase):
    def test_missing_socket_reports_not_attached(self):
        # No agent has ever bound this socket, so connecting fails and
        # is_attached must swallow the OSError and return False.
        self.assertFalse(client.is_attached(999999))


if __name__ == "__main__":
    unittest.main()
