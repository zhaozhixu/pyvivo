"""Tests for the in-process agent's pure helpers.

The agent's socket/remote machinery needs a live target, but its
inspection/patching helpers operate on ordinary objects and are testable
in-process.
"""

import hashlib
import json
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pyvivo import agent


class SafeReprTests(unittest.TestCase):
    def test_plain_value(self):
        self.assertEqual(agent._safe_repr("hi"), "'hi'")

    def test_truncates_to_limit_with_ellipsis(self):
        text = agent._safe_repr("x" * 1000, limit=10)
        self.assertEqual(len(text), 10)
        self.assertTrue(text.endswith("..."))

    def test_repr_failure_is_caught(self):
        class Bad:
            def __repr__(self):
                raise ValueError("boom")

        text = agent._safe_repr(Bad())
        self.assertIn("repr failed", text)
        self.assertIn("ValueError", text)


class HashAndDiffTests(unittest.TestCase):
    def test_source_hash_none(self):
        self.assertIsNone(agent._source_hash(None))

    def test_source_hash_matches_sha256(self):
        self.assertEqual(
            agent._source_hash("abc"),
            hashlib.sha256(b"abc").hexdigest(),
        )

    def test_source_diff_none_when_no_old_source(self):
        self.assertIsNone(agent._source_diff(None, "new\n", "mod:fn"))

    def test_source_diff_labels_and_content(self):
        diff = agent._source_diff("a\n", "b\n", "mod:fn")
        self.assertIn("mod:fn (before)", diff)
        self.assertIn("mod:fn (after)", diff)
        self.assertIn("-a", diff)
        self.assertIn("+b", diff)


class ResolveTargetTests(unittest.TestCase):
    def test_resolves_module_function(self):
        owner, attr, function = agent._resolve_target("json:dumps")
        self.assertIs(function, json.dumps)
        self.assertEqual(attr, "dumps")

    def test_requires_colon(self):
        with self.assertRaises(ValueError):
            agent._resolve_target("nocolon")

    def test_rejects_non_function(self):
        with self.assertRaises(TypeError):
            agent._resolve_target("json:JSONEncoder")


class DispatchTests(unittest.TestCase):
    def test_unknown_operation_raises(self):
        with self.assertRaises(ValueError):
            agent._dispatch({"op": "does-not-exist"})

    def test_ping_reports_pid(self):
        import os

        result = agent._dispatch({"op": "ping"})
        self.assertEqual(result["pid"], os.getpid())

    def test_threads_includes_main(self):
        result = agent._threads({})
        self.assertTrue(any(t["main"] for t in result["threads"]))


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.mod = types.ModuleType("pyvivo_patch_target")
        exec("def compute(v):\n    return v * 2\n", self.mod.__dict__)
        sys.modules["pyvivo_patch_target"] = self.mod

    def tearDown(self):
        agent._PATCHES.clear()
        sys.modules.pop("pyvivo_patch_target", None)

    def test_patch_then_rollback_restores_behavior(self):
        self.assertEqual(self.mod.compute(3), 6)

        info = agent._patch(
            {
                "target": "pyvivo_patch_target:compute",
                "source": "def compute(v):\n    return v * 10\n",
                "patch_id": "p1",
            }
        )
        self.assertEqual(info["patch_id"], "p1")
        self.assertEqual(self.mod.compute(3), 30)

        agent._rollback({"patch_id": "p1"})
        self.assertEqual(self.mod.compute(3), 6)
        self.assertNotIn("p1", agent._PATCHES)

    def test_incompatible_call_shape_is_rejected(self):
        with self.assertRaises(ValueError):
            agent._patch(
                {
                    "target": "pyvivo_patch_target:compute",
                    "source": "def compute(a, b):\n    return a + b\n",
                    "patch_id": "bad",
                }
            )
        # A rejected patch must not be recorded or applied.
        self.assertNotIn("bad", agent._PATCHES)
        self.assertEqual(self.mod.compute(3), 6)


if __name__ == "__main__":
    unittest.main()
