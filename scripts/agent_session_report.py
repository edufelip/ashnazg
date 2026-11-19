"""Recompute paired Android session results from the raw per-trial receipts."""

import argparse
import json
import statistics
from pathlib import Path


def trial_metrics(trial):
    timings = trial["probe"]["timings_ns"]
    samples = trial["probe"]["memory"]
    idle = [row for row in samples if row["phase"] == "idle"]
    interaction = [row["memory_current_bytes"] for row in samples
                   if row["phase"] in ("interaction", "interaction_start", "interaction_end")]
    post_agent = [row["memory_current_bytes"] for row in samples if row["phase"] == "post_agent"]
    return {
        "launch_to_snapshot_ns": timings["first_usable_snapshot"],
        "tap_rpc_ns": timings["tap_ns"],
        "tap_response_to_fresh_png_ns": timings["tap_to_fresh_png_ns"],
        "tap_to_fresh_png_ns": timings["tap_ns"] + timings["tap_to_fresh_png_ns"],
        "shutdown_ns": trial["shutdown_ns"],
        "idle_memory_median_bytes": statistics.median(row["memory_current_bytes"] for row in idle),
        "idle_anon_median_bytes": statistics.median(row["memory_stat"]["anon"] for row in idle),
        "idle_file_median_bytes": statistics.median(row["memory_stat"]["file"] for row in idle),
        "interaction_memory_sampled_max_bytes": max(interaction),
        "post_agent_memory_median_bytes": statistics.median(post_agent),
    }


def report(root):
    manifest = json.loads((root / "manifest.json").read_text())
    runtimes = ("avd", "redroid")
    result = {"complete": True, "incomplete": [], "pairs": {"cold": [], "warm": []},
              "valid_pair_counts": {}, "median": {}}
    for mode in ("cold", "warm"):
        pairs = result["pairs"][mode]
        for index in range(1, manifest["pairs_requested"] + 1):
            pair = {"index": index}
            for runtime in runtimes:
                path = root / f"pair-{index}" / runtime / mode / "trial.json"
                if not path.is_file():
                    status = "not_run"
                else:
                    trial = json.loads(path.read_text())
                    status = trial["status"]
                if status == "passed":
                    pair[runtime] = trial_metrics(trial)
                else:
                    result["incomplete"].append({"path": str(path.relative_to(root)), "status": status})
            if all(runtime in pair for runtime in runtimes):
                pairs.append(pair)
        result["valid_pair_counts"][mode] = len(pairs)
        if pairs:
            result["median"][mode] = {
                runtime: {metric: statistics.median(pair[runtime][metric] for pair in pairs)
                          for metric in pairs[0][runtime]} for runtime in runtimes
            }
    result["complete"] = not result["incomplete"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    result = report(parser.parse_args().directory)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
