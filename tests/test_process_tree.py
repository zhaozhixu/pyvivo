"""Tests for framework-neutral process-tree discovery.

``discover_tree`` accepts a ``proc_root`` argument, so these point it at a
synthetic ``/proc`` built in a temporary directory instead of the real one and
run on any OS.
"""

import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyvivo import process_tree


def _make_proc(root: Path, procs: dict) -> Path:
    """Build a fake /proc. ``procs`` maps pid -> (ppid|None, cmdline)."""
    proc_root = root / "proc"
    proc_root.mkdir()
    for pid, (ppid, cmdline) in procs.items():
        entry = proc_root / str(pid)
        entry.mkdir()
        (entry / "cmdline").write_bytes(cmdline)
        status = "Name:\tprocess\nState:\tS (sleeping)\n"
        if ppid is not None:
            status += f"PPid:\t{ppid}\n"
        (entry / "status").write_text(status)
    return proc_root


# A small tree:
#   100 (root, ppid 1)
#     +-- 200
#     +-- 300
#           +-- 400
#   500 (unrelated, ppid 999)
TREE = {
    100: (1, b"python\x00target.py\x00"),
    200: (100, b"python\x00worker.py\x00"),
    300: (100, b"python\x00worker.py\x00"),
    400: (300, b"python\x00leaf.py\x00"),
    500: (999, b"unrelated\x00"),
}


class ProcessTableTests(unittest.TestCase):
    def test_process_table_maps_pid_to_ppid_and_command(self):
        with TemporaryDirectory() as tmp:
            proc_root = _make_proc(Path(tmp), TREE)
            table = process_tree.process_table(proc_root)
        self.assertEqual(table[100], (1, "python target.py"))
        self.assertEqual(table[400], (300, "python leaf.py"))
        self.assertIn(500, table)

    def test_non_numeric_entries_are_ignored(self):
        with TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            proc_root.mkdir()
            (proc_root / "cpuinfo").write_text("junk")
            (proc_root / "self").mkdir()
            table = process_tree.process_table(proc_root)
        self.assertEqual(table, {})

    def test_entry_missing_files_is_skipped(self):
        with TemporaryDirectory() as tmp:
            proc_root = Path(tmp) / "proc"
            proc_root.mkdir()
            (proc_root / "700").mkdir()  # no cmdline / status
            table = process_tree.process_table(proc_root)
        self.assertNotIn(700, table)

    def test_read_command_replaces_nulls_and_strips(self):
        with TemporaryDirectory() as tmp:
            entry = Path(tmp)
            (entry / "cmdline").write_bytes(b"a\x00b\x00c\x00")
            self.assertEqual(process_tree._read_command(entry), "a b c")

    def test_read_ppid_returns_none_when_absent(self):
        with TemporaryDirectory() as tmp:
            status = Path(tmp) / "status"
            status.write_text("Name:\tx\nState:\tR\n")
            self.assertIsNone(process_tree._read_ppid(status))


class PsTableTests(unittest.TestCase):
    def test_parse_ps_lines_alignment_and_argument_spaces(self):
        lines = [
            "    1     0 /sbin/launchd",
            "  845     1 /usr/sbin/cupsd -l",
            " 1200   845 python worker.py --name a b",
            "not a process line",
            "",
        ]
        table = process_tree._parse_ps_lines(lines)
        self.assertEqual(table[1], (0, "/sbin/launchd"))
        self.assertEqual(table[845], (1, "/usr/sbin/cupsd -l"))
        self.assertEqual(table[1200], (845, "python worker.py --name a b"))
        self.assertEqual(len(table), 3)

    def test_default_table_contains_this_process(self):
        table = process_tree.process_table()
        self.assertIn(os.getpid(), table)
        ppid, command = table[os.getpid()]
        self.assertEqual(ppid, os.getppid())
        self.assertTrue(command)

    def test_default_discover_tree_finds_this_process(self):
        nodes = process_tree.discover_tree(os.getpid())
        self.assertEqual(nodes[0].pid, os.getpid())
        self.assertEqual(nodes[0].depth, 0)


class PsBackendTests(unittest.TestCase):
    """Exercise the ``ps`` backend directly.

    On Linux the default discovery path uses ``/proc``, so these force the
    macOS/BSD code path (a real ``ps`` invocation) to keep it covered there too.
    """

    def test_ps_table_live_contains_this_process(self):
        table = process_tree._ps_table()
        self.assertIn(os.getpid(), table)
        ppid, command = table[os.getpid()]
        self.assertEqual(ppid, os.getppid())
        self.assertTrue(command)

    def test_process_table_falls_back_to_ps_without_proc(self):
        original = process_tree._DEFAULT_PROC
        process_tree._DEFAULT_PROC = Path("/pyvivo-no-such-proc")
        try:
            table = process_tree.process_table()
        finally:
            process_tree._DEFAULT_PROC = original
        self.assertIn(os.getpid(), table)
        self.assertEqual(table[os.getpid()][0], os.getppid())

    def test_discover_tree_over_ps_backend(self):
        original = process_tree._DEFAULT_PROC
        process_tree._DEFAULT_PROC = Path("/pyvivo-no-such-proc")
        try:
            nodes = process_tree.discover_tree(os.getpid())
        finally:
            process_tree._DEFAULT_PROC = original
        self.assertEqual(nodes[0].pid, os.getpid())
        self.assertEqual(nodes[0].depth, 0)


class DiscoverTreeTests(unittest.TestCase):
    def test_breadth_first_order_and_depths(self):
        with TemporaryDirectory() as tmp:
            proc_root = _make_proc(Path(tmp), TREE)
            nodes = process_tree.discover_tree(100, proc_root=proc_root)
        self.assertEqual([n.pid for n in nodes], [100, 200, 300, 400])
        self.assertEqual([n.depth for n in nodes], [0, 1, 1, 2])
        # 500 is unrelated and must not appear.
        self.assertNotIn(500, [n.pid for n in nodes])

    def test_descendants_only_excludes_root(self):
        with TemporaryDirectory() as tmp:
            proc_root = _make_proc(Path(tmp), TREE)
            nodes = process_tree.discover_tree(
                100, include_root=False, proc_root=proc_root
            )
        self.assertEqual([n.pid for n in nodes], [200, 300, 400])
        self.assertTrue(all(n.depth >= 1 for n in nodes))

    def test_node_carries_ppid_and_command(self):
        with TemporaryDirectory() as tmp:
            proc_root = _make_proc(Path(tmp), TREE)
            nodes = process_tree.discover_tree(300, proc_root=proc_root)
        root = nodes[0]
        self.assertEqual(root.pid, 300)
        self.assertEqual(root.ppid, 100)
        self.assertEqual(root.command, "python worker.py")

    def test_unknown_root_raises(self):
        with TemporaryDirectory() as tmp:
            proc_root = _make_proc(Path(tmp), TREE)
            with self.assertRaises(ProcessLookupError):
                process_tree.discover_tree(9999, proc_root=proc_root)

    def test_node_dict_shape(self):
        with TemporaryDirectory() as tmp:
            proc_root = _make_proc(Path(tmp), TREE)
            nodes = process_tree.discover_tree(100, proc_root=proc_root)
        d = process_tree.node_dict(nodes[0])
        self.assertEqual(set(d), {"pid", "ppid", "depth", "command"})
        self.assertEqual(d["pid"], 100)


if __name__ == "__main__":
    unittest.main()
