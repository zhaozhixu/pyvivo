"""Tests for the command-line argument parser and helpers.

These exercise ``build_parser`` and ``code_input`` without attaching to any
process.
"""

import argparse
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyvivo import cli


class BuildParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def parse(self, argv):
        return self.parser.parse_args(argv)

    def test_remote_attach_timeout(self):
        args = self.parse(["remote", "123", "attach", "--timeout", "3"])
        self.assertEqual(args.mode, "remote")
        self.assertEqual(args.pid, 123)
        self.assertEqual(args.command, "attach")
        self.assertEqual(args.timeout, 3.0)

    def test_remote_patch_target_and_file(self):
        args = self.parse(["remote", "123", "patch", "mod:fn", "file.py"])
        self.assertEqual(args.command, "patch")
        self.assertEqual(args.target, "mod:fn")
        self.assertEqual(args.file, "file.py")

    def test_remote_eval_defaults(self):
        args = self.parse(["remote", "123", "eval", "1 + 1"])
        self.assertEqual(args.command, "eval")
        self.assertEqual(args.code, "1 + 1")
        self.assertEqual(args.thread, "main")
        self.assertEqual(args.frame, 0)

    def test_remote_detach_rollback_flag(self):
        args = self.parse(["remote", "123", "detach", "--rollback"])
        self.assertTrue(args.rollback)
        self.assertFalse(args.keep_patches)

    def test_tree_descendants_only_stack(self):
        args = self.parse(["tree", "5", "--descendants-only", "stack", "--thread", "main"])
        self.assertEqual(args.mode, "tree")
        self.assertEqual(args.root_pid, 5)
        self.assertTrue(args.descendants_only)
        self.assertEqual(args.tree_command, "stack")

    def test_tree_patch(self):
        args = self.parse(["tree", "5", "patch", "m:f", "p.py"])
        self.assertEqual(args.tree_command, "patch")
        self.assertEqual(args.target, "m:f")
        self.assertEqual(args.file, "p.py")

    def test_missing_mode_exits(self):
        with self.assertRaises(SystemExit):
            self.parse([])

    def test_remote_without_command_exits(self):
        with self.assertRaises(SystemExit):
            self.parse(["remote", "123"])


class CodeInputTests(unittest.TestCase):
    def test_inline_code(self):
        args = argparse.Namespace(file=None, code="1 + 1", command="eval")
        source, filename = cli.code_input(args)
        self.assertEqual(source, "1 + 1")
        self.assertEqual(filename, "<pyvivo eval>")

    def test_file_source_uses_absolute_path(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "snippet.py"
            path.write_text("counter + 1\n")
            args = argparse.Namespace(file=str(path), code=None, command="eval")
            source, filename = cli.code_input(args)
        self.assertEqual(source, "counter + 1\n")
        self.assertEqual(filename, str(path.resolve()))

    def test_requires_code_or_file(self):
        args = argparse.Namespace(file=None, code=None, command="exec")
        with self.assertRaises(ValueError):
            cli.code_input(args)


if __name__ == "__main__":
    unittest.main()
