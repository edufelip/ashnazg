import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.agent_session_benchmark import cgroup_path, memory_sample, exercise_fixture


def snapshot(sequence, count):
    return {"snapshotId": sequence, "nodes": [{
        "testId": "dev.ashnazg.fixture:id/increment", "text": f"Count: {count}",
        "enabled": True, "rect": [0, 0, 100, 40],
    }]}


class AgentSessionBenchmarkTest(unittest.TestCase):
    def test_cgroup_resolution_requires_exact_v2_subgroup(self):
        self.assertEqual(cgroup_path("0::/system.slice/runtime.scope\n"),
                         Path("/sys/fs/cgroup/system.slice/runtime.scope"))
        for invalid in ("0::/\n", "3:memory:/somewhere\n", "0::/test/../other\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cgroup_path(invalid)

    def test_memory_sample_does_not_zero_missing_counters(self):
        with tempfile.TemporaryDirectory() as work:
            group = Path(work)
            (group / "memory.current").write_text("123")
            (group / "memory.stat").write_text("anon 60\nfile 40\n")
            partial = memory_sample(group)
            self.assertEqual(partial["memory_current_bytes"], 123)
            self.assertIsNone(partial["memory_swap_current_bytes"])
            self.assertIn("memory.swap.current", partial["errors"])
            (group / "memory.swap.current").write_text("0")
            self.assertEqual(memory_sample(group)["errors"], {})
            self.assertEqual(memory_sample(group)["memory_stat"]["anon"], 60)

    def test_counts_and_pixels_must_be_fresh(self):
        mcp = Mock()
        mcp.call_tool.side_effect = [
            {"structuredContent": {"snapshot": snapshot(2, 1), "settled": True}},
        ]
        mcp.screenshot.side_effect = [b"before", b"after"]
        frames = Mock(side_effect=[0, 1])
        record = {}
        exercise_fixture(mcp, snapshot(1, 0), record, frames)
        self.assertEqual(record["count_after"], 1)
        self.assertEqual(mcp.call_tool.call_count, 1)
        self.assertEqual(frames.call_count, 2)

    def test_stale_action_is_not_replayed(self):
        mcp = Mock()
        mcp.call_tool.return_value = {
            "structuredContent": {"snapshot": snapshot(1, 1), "settled": True},
        }
        mcp.screenshot.return_value = b"image"
        with self.assertRaisesRegex(RuntimeError, "fresh"):
            exercise_fixture(mcp, snapshot(1, 0), {}, Mock(return_value=0))
        self.assertEqual(mcp.call_tool.call_count, 1)
        self.assertEqual(mcp.screenshot.call_count, 1)

    def test_read_only_capture_can_retry_after_stale_frame(self):
        mcp = Mock()
        mcp.call_tool.return_value = {
            "structuredContent": {"snapshot": snapshot(2, 1), "settled": True},
        }
        mcp.screenshot.side_effect = [b"before", b"stale", b"fresh"]
        record = {}
        exercise_fixture(mcp, snapshot(1, 0), record, Mock(side_effect=[0, 0, 1]))
        self.assertEqual([row["decoded_count"] for row in record["capture_attempts"]], [0, 1])
        self.assertEqual(mcp.call_tool.call_count, 1)
