import json
import tempfile
import unittest
from pathlib import Path

from scripts.agent_session_report import report


class AgentSessionReportTest(unittest.TestCase):
    def test_failed_arms_remain_in_report_and_do_not_enter_paired_medians(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "manifest.json").write_text(json.dumps({"pairs_requested": 2}))
            for index in (1, 2):
                for runtime in ("redroid", "avd"):
                    for mode in ("cold", "warm"):
                        folder = root / f"pair-{index}" / runtime / mode
                        folder.mkdir(parents=True)
                        failed = index == 2 and mode == "cold" and runtime == "avd"
                        if index == 2 and mode == "warm" and runtime == "avd":
                            continue
                        duration = 100 if index == 1 else 600
                        (folder / "trial.json").write_text(json.dumps({
                            "status": "failed" if failed else "passed",
                            "shutdown_ns": duration,
                            "probe": {"timings_ns": {name: duration for name in (
                                "first_usable_snapshot", "tap_ns", "tap_to_fresh_png_ns",
                            )}, "memory": [
                                {"phase": "idle", "memory_current_bytes": duration,
                                 "memory_stat": {"anon": 50, "file": 10}},
                                {"phase": "interaction", "memory_current_bytes": duration},
                                {"phase": "post_agent", "memory_current_bytes": duration},
                            ]},
                        }))
            results = report(root)
            self.assertFalse(results["complete"])
            self.assertEqual(results["valid_pair_counts"], {"cold": 1, "warm": 1})
            self.assertEqual(len(results["incomplete"]), 2)
            self.assertEqual(results["median"]["cold"]["redroid"]["launch_to_snapshot_ns"], 100)
