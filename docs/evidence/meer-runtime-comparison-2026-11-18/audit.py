import gzip
import hashlib
import json
import math
import statistics
import sys
from pathlib import Path

root=Path(sys.argv[1])
summary=json.loads((root/"summary.json").read_text())
observations=json.loads((root/"workload-observations.json").read_text())
binding=observations["artifact_manifest"]
published_manifest=(root/"artifacts.json").read_bytes()
assert hashlib.sha256(published_manifest).hexdigest()==binding["published_redaction_sha256"]
if len(sys.argv)>2:
    original_manifest=Path(sys.argv[2]).read_bytes()
    assert hashlib.sha256(original_manifest).hexdigest()==binding["run_sha256"]
    redacted_fields=set(binding["redacted_fields"])
    assert {
        name:{key:value for key,value in entry.items() if key not in redacted_fields}
        for name,entry in json.loads(original_manifest).items()
    }==json.loads(published_manifest)
expected_reviewed_starts={f"pair{i}-{runtime}" for i in range(1,5) for runtime in ["avd","redroid"]}
assert set(summary["arms"])==set(observations["arms"])==expected_reviewed_starts
boot_keys=set()
identities=set()
measurements={runtime:{name:[] for name in ["journey_total_ms","open_search_ms","login_with_email_ms","pss_plus_swap_kib"]}
              for runtime in ["avd","redroid"]}
for arm, info in summary["arms"].items():
    boot=json.dumps(info["controls"]["boot"],sort_keys=True)
    assert boot not in boot_keys
    boot_keys.add(boot)
    assert info["controls"]["location_mode"]=="0"
    assert set(info["controls"]["animations"].values())=={"0"}
    assert "SwiftShader" in info["controls"]["renderer"]
    raw=json.loads(gzip.decompress((root/f"{arm}.json.gz").read_bytes()))
    assert raw["warmup.json"]["status"]=="passed"
    for i,s in enumerate(info["samples"],start=1):
        assert s["trial"]==f"trial-{i}"
        raw_run=raw[f"trial-{i}.json"]
        assert raw_run["status"]=="passed" and raw_run["input_profile"]=="feed-ready-stock30"
        identities.add(raw_run["artifact_identity"])
        assert raw_run["artifact_identity"]==s["artifact_identity"]
        assert raw_run["timings"]=={key:s[key] for key in raw_run["timings"]}
        observation=observations["arms"][arm][s["trial"]]
        assert observation["exit_code"]==0
        nearby={tuple(request["coordinates"]) for request in observation["nearby_requests"]}
        assert [list(point) for point in sorted(nearby)]==s["nearby_coordinates"]==[["-23.5561782","-46.6375468"]]
        candidates=[name for name,v in raw.items() if name.startswith(f"trial-{i}-memory-") and v["valid"]]
        accepted_sweep=1 if arm.endswith("-avd") else 2
        assert candidates==[f"trial-{i}-memory-{accepted_sweep}.json"]
        if accepted_sweep==2:
            assert not raw[f"trial-{i}-memory-1.json"]["valid"]
        v=raw[candidates[0]]
        rows=v["rows_pid_start_ticks_pss_kib_swap_pss_kib"]
        assert len(rows)==len({p[0] for p in rows})==s["process_count"]
        assert all(pid>0 and start>0 and pss>=0 and swap>=0 for pid,start,pss,swap in rows)
        assert sum(row[2] for row in rows)==v["sum_pss_kib"]
        assert sum(row[3] for row in rows)==s["swap_kib"]==v["sum_swap_pss_kib"]
        assert sum(row[2]+row[3] for row in rows)==s["pss_plus_swap_kib"]
        for k,samples in measurements[arm.rsplit("-",1)[1]].items():
            samples.append(s[k])
    assert i==5
assert identities=={binding["run_sha256"]} and len(boot_keys)==8
for runtime, metrics in measurements.items():
    for k, values in metrics.items():
        assert len(values)==20 and all(math.isfinite(v) and v>0 for v in values)
        expected=summary["pooled"][runtime][k]
        assert expected["min"]==min(values) and expected["max"]==max(values)
        assert expected["p50"]==statistics.median(values)
        assert expected["p95"]==sorted(values)[18]
print("Recomputed all 40 timings/memory endpoints and 8 boot identities from archived records.")
