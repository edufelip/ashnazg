"""Pure-logic preflight for the iOS session benchmark.

These tests never pretend to be device data: every input is a synthetic
buffer, string, or stub produced here in the test, and nothing exercises
simctl, Xcode, a simulator, or a real process.
"""

import base64
import json
import os
import stat
import struct
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from scripts.native_cli_acceptance import AcceptanceError
from scripts.ios_session_benchmark import (
    APPLE_REQUIRED_TOOLS,
    ARM_CLASSES,
    AppleMcpArm,
    AshnazgMcpArm,
    BenchmarkError,
    DIRECT_XCTEST_TAP_QUALIFICATION,
    DirectXctestArm,
    MEER_MILESTONES,
    MemoryTracker,
    ObservationContext,
    ProcessGone,
    aggregate_numeric_stats,
    app_tree_sha256,
    assert_no_secrets,
    build_observation_receipt,
    build_receipt,
    cleanup_owned_simulator,
    drive_conditions,
    drive_trials,
    export_xctest_attachments,
    finish_operation,
    load_mock_credentials,
    normalize_memory_sample,
    parse_ashnazg_screenshot,
    parse_device_states,
    parse_rusage_buffer,
    parse_role_pids,
    parse_shared_services,
    parse_test_case_durations,
    prepare_cold_simulator,
    prepare_warm_simulator,
    preflight_app,
    qualify_meer_xctest,
    require_advancing_snapshot_id,
    require_apple_tools,
    require_owned_session_descriptor,
    run_condition,
    run_observation,
    scrub_secrets,
    skip_operation,
    summarize_trials,
    usable_apple_target,
    usable_meer_login_snapshot,
    usable_snapshot_target,
    validate_loopback_http_url,
    validate_meer_png_bytes,
    validate_receipt,
)

UDID = "AAAAAAAA-1111-2222-3333-444444444444"
XCODE_TREE = (
    "Application bundle identifier: dev.ashnazg.ios.fixture\n"
    " StaticText, {{24.0, 100.0}, {200.0, 30.0}}, label: 'Count: 0'\n"
    " Button, {{162.7, 440.7}, {76.7, 20.3}}, identifier: 'fixture-increment', "
    "label: 'Increment', hitPoint: {201.0, 450.8}\n"
    " TextField, {{24.0, 485.0}, {354.0, 34.0}}, identifier: 'fixture-input', "
    "placeholderValue: 'Probe text', hitPoint: {201.0, 502.0}\n"
)


def rusage_bytes(wired=1, resident=2, footprint=3, abstime=4):
    # UUID, then five 64-bit CPU, wakeup and paging counters before memory.
    return (b"\x00" * 16 + struct.pack("<9Q", 101, 102, 103, 104, 105,
                                       wired, resident, footprint, abstime) + b"\x00" * 56)


def make_context(output, condition="cold", arm="apple-mcp"):
    args = Namespace(
        app="/tmp/bench/Meer.app", bundle_id="dev.ashnazg.ios.fixture",
        tap_identifier="fixture-increment", tap_label="Increment",
        expected_before="Count: 0", expected_after="Count: 1",
        first_snapshot_timeout=1.0, arm_command=None, xctest_artifacts=None,
    )
    context = ObservationContext(args, UDID, condition, output, arm)
    context.simulator_scope = {"udid": UDID, "name": "Bench", "device_type": "iPhone-17",
                               "runtime": "iOS-26-5", "created_by_run": True}
    context.app_sha256 = "0" * 64
    context.source_revisions = {"repository": "d" * 40}
    return context


class RusageParsingTest(unittest.TestCase):
    def test_parses_the_fixed_v1_header_fields(self):
        reading = parse_rusage_buffer(rusage_bytes(wired=10, resident=20,
                                                   footprint=30, abstime=40))
        self.assertEqual(reading, {"ri_wired_size": 10, "ri_resident_size": 20,
                                   "ri_phys_footprint": 30, "ri_proc_start_abstime": 40})

    def test_rejects_short_buffers(self):
        with self.assertRaises(ValueError):
            parse_rusage_buffer(b"\x00" * 16)

    def test_first_read_establishes_identity(self):
        entry = normalize_memory_sample("app", 12, None, parse_rusage_buffer(rusage_bytes()))
        self.assertEqual(entry["status"], "sampled")
        self.assertTrue(entry["identity_established"])
        self.assertEqual(entry["ri_phys_footprint_bytes"], 3)
        self.assertEqual(entry["observed_start_abstime"], 4)

    def test_matching_abstime_samples_and_mismatch_is_a_missing_change(self):
        reading = parse_rusage_buffer(rusage_bytes(abstime=4))
        same = normalize_memory_sample("app", 12, 4, reading)
        self.assertEqual(same["status"], "sampled")
        changed = normalize_memory_sample("app", 12, 99, reading)
        self.assertEqual(changed["status"], "changed")
        self.assertNotIn("ri_phys_footprint_bytes", changed)
        self.assertEqual(changed["observed_start_abstime"], 4)

    def test_lost_and_unreadable_processes_are_missing_samples_not_zeros(self):
        lost = normalize_memory_sample("simulator", 7, 5, None, error_kind="gone")
        self.assertEqual(lost["status"], "lost")
        self.assertNotIn("ri_phys_footprint_bytes", lost)
        unreadable = normalize_memory_sample("helper", 8, 5, None, error_kind="errno 13")
        self.assertEqual(unreadable["status"], "unreadable")
        self.assertEqual(unreadable["error"], "errno 13")


PS_OUTPUT = (
    f"  456 /bin/launchd_sim --parent-of-pool {UDID}/data\n"
    f"  123 /Users/x/Library/Developer/CoreSimulator/Devices/{UDID}/data/"
    "Containers/Bundle/Application/UUID/Meer.app/Meer\n"
    "  789 /Applications/Xcode.app/Contents/MacOS/CoreSimulatorService\n"
    "  999 /usr/bin/unrelated --arg CoreSimulatorNope\n"
)


class MemoryTrackerTest(unittest.TestCase):
    def test_samples_roles_and_keeps_identity_across_stages(self):
        readings = {123: rusage_bytes(abstime=11), 456: rusage_bytes(abstime=22),
                    789: rusage_bytes(abstime=33)}

        def sampler(pid):
            return parse_rusage_buffer(readings[pid])

        tracker = MemoryTracker(sampler=sampler, ps_lister=lambda: PS_OUTPUT)
        tracker.sample_stage("post_arm_start", UDID, "Meer", helper_pids=())
        tracker.sample_stage("first_snapshot_boundary", UDID, "Meer", helper_pids=())
        self.assertEqual(tracker.identities[("app", 123)], 11)
        self.assertEqual(tracker.identities[("simulator", 456)], 22)
        for stage, samples in tracker.stages.items():
            by_role = {sample["role"]: sample for sample in samples}
            self.assertEqual(by_role["app"]["status"], "sampled", stage)
            self.assertEqual(by_role["app"]["ri_phys_footprint_bytes"], 3, stage)
        shared = tracker.shared_stages["post_arm_start"]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0]["scope"], "shared-coresimulator-host-service")
        self.assertEqual(shared[0]["pid"], 789)

    def test_lost_process_is_recorded_missing_and_identity_dropped(self):
        def sampler(pid):
            raise ProcessGone(f"process {pid} was gone")

        tracker = MemoryTracker(sampler=sampler, ps_lister=lambda: PS_OUTPUT)
        tracker.sample_stage("post_shutdown", UDID, "Meer", helper_pids=(77,))
        samples = {sample["role"]: sample for sample in tracker.stages["post_shutdown"]}
        self.assertEqual(samples["app"]["status"], "lost")
        self.assertEqual(samples["helper"]["status"], "lost")
        self.assertEqual(samples["helper"]["pid"], 77)
        self.assertNotIn(("helper", 77), tracker.identities)

    def test_changed_process_is_missing_then_reanchored(self):
        state = {"abstime": 5}

        def sampler(pid):
            return parse_rusage_buffer(rusage_bytes(abstime=state["abstime"]))

        tracker = MemoryTracker(sampler=sampler, ps_lister=lambda: PS_OUTPUT)
        tracker.sample_stage("post_arm_start", UDID, "Meer")
        state["abstime"] = 6
        tracker.sample_stage("tap_boundary", UDID, "Meer")
        samples = tracker.stages["tap_boundary"]
        app_sample = next(sample for sample in samples if sample["role"] == "app")
        self.assertEqual(app_sample["status"], "changed")
        self.assertEqual(tracker.identities[("app", 123)], 6)
        tracker.sample_stage("post_shutdown", UDID, "Meer")
        app_sample = next(sample for sample in tracker.stages["post_shutdown"]
                          if sample["role"] == "app")
        self.assertEqual(app_sample["status"], "sampled")


class ParsingTest(unittest.TestCase):
    def test_simctl_device_states(self):
        listing = json_text = ('{"devices": {"iOS 26.5": ['
                               f'{{"udid": "{UDID}", "state": "Shutdown"}},'
                               '{"udid": "other", "state": "Booted"}]}}')
        states = parse_device_states(listing)
        self.assertEqual(states, {UDID: "Shutdown", "other": "Booted"})

    def test_role_pids_resolve_launchd_sim_and_app_for_the_owned_udid(self):
        roles = parse_role_pids(PS_OUTPUT, UDID, "Meer")
        self.assertEqual(roles, {"app": 123, "simulator": 456})
        self.assertEqual(parse_role_pids(PS_OUTPUT, "OTHER-UDID", "Meer"),
                         {"app": None, "simulator": None})
        duplicate = PS_OUTPUT + PS_OUTPUT.splitlines()[1] + "\n"
        with self.assertRaises(BenchmarkError):
            parse_role_pids(duplicate, UDID, "Meer")

    def test_shared_services_match_coresimulator_only(self):
        services = parse_shared_services(PS_OUTPUT)
        self.assertEqual([service["pid"] for service in services], [789])

    def test_xcodebuild_durations_split_pass_and_fail(self):
        log = ("Test Case 'Fixture tap' passed (1.250 seconds)\n"
               "Test Case 'Fixture type' failed (0.500 seconds)\n")
        durations, failures = parse_test_case_durations(log)
        self.assertEqual(durations["Fixture tap"]["seconds"], 1.25)
        self.assertEqual(failures[0]["name"], "Fixture type")


class UsableAppleTargetTest(unittest.TestCase):
    def test_full_xcode_tree_target_is_usable_with_bounds(self):
        usable = usable_apple_target(XCODE_TREE, "dev.ashnazg.ios.fixture",
                                     "fixture-increment", "Increment", "Count: 0")
        self.assertTrue(usable["usable"])
        self.assertEqual(usable["point"], "201.0 450.8")
        self.assertEqual(usable["bounds"], {"x": 162.7, "y": 440.7,
                                            "width": 76.7, "height": 20.3})
        self.assertTrue(usable["app_identifier_seen"])

    def test_wrong_app_identifier_is_not_usable(self):
        usable = usable_apple_target(XCODE_TREE, "com.other.app",
                                     "fixture-increment", "Increment", "Count: 0")
        self.assertFalse(usable["usable"])
        self.assertFalse(usable["app_identifier_seen"])

    def test_missing_pre_action_state_blocks_usability(self):
        usable = usable_apple_target(XCODE_TREE, "dev.ashnazg.ios.fixture",
                                     "fixture-increment", "Increment", "Count: 9")
        self.assertFalse(usable["usable"])
        self.assertIn("pre-action state", usable["reason"])

    def test_disabled_token_and_bad_bounds_are_rejected(self):
        disabled = XCODE_TREE.replace(
            "identifier: 'fixture-increment'",
            "identifier: 'fixture-increment', enabled: false")
        usable = usable_apple_target(disabled, "dev.ashnazg.ios.fixture",
                                     "fixture-increment", "Increment", "Count: 0")
        self.assertFalse(usable["usable"])
        self.assertIn("disabled", usable["reason"])
        flat = XCODE_TREE.replace("{{162.7, 440.7}, {76.7, 20.3}}", "{{0, 0}, {0, 20.3}}")
        usable = usable_apple_target(flat, "dev.ashnazg.ios.fixture",
                                     "fixture-increment", "Increment", "Count: 0")
        self.assertFalse(usable["usable"])
        self.assertIn("bounds", usable["reason"])

    def test_duplicate_targets_are_not_usable(self):
        duplicated = XCODE_TREE + XCODE_TREE.splitlines()[2] + "\n"
        usable = usable_apple_target(duplicated, "dev.ashnazg.ios.fixture",
                                     "fixture-increment", "Increment", "Count: 0")
        self.assertFalse(usable["usable"])
        self.assertIn("uniquely", usable["reason"])


class UsableSnapshotTargetTest(unittest.TestCase):
    def test_enabled_unique_node_with_valid_rect_is_usable(self):
        snapshot = {"snapshotId": 1, "nodes": [
            {"testId": "fixture-increment", "enabled": True, "rect": [10, 20, 110, 60]},
            {"testId": "fixture-other", "enabled": True, "rect": [0, 0, 5, 5]},
        ]}
        usable = usable_snapshot_target(snapshot, "fixture-increment")
        self.assertTrue(usable["usable"])
        self.assertEqual(usable["rect"], [10, 20, 110, 60])
        self.assertEqual(usable["selector_basis"], "testId")

    def test_disabled_target_and_invalid_rect_are_rejected(self):
        disabled = {"nodes": [{"testId": "t", "enabled": False, "rect": [0, 0, 10, 10]}]}
        usable = usable_snapshot_target(disabled, "t")
        self.assertFalse(usable["usable"])
        self.assertIn("enabled", usable["reason"])
        inverted = {"nodes": [{"testId": "t", "enabled": True, "rect": [10, 10, 0, 20]}]}
        usable = usable_snapshot_target(inverted, "t")
        self.assertFalse(usable["usable"])
        self.assertIn("rect", usable["reason"])
        duplicated = {"nodes": [{"testId": "t", "enabled": True, "rect": [0, 0, 1, 1]},
                                {"testId": "t", "enabled": True, "rect": [0, 0, 1, 1]}]}
        self.assertIn("uniquely", usable_snapshot_target(duplicated, "t")["reason"])


class OperationRecordTest(unittest.TestCase):
    def test_success_keeps_elapsed_and_success_timing(self):
        timings = {}
        finish_operation(timings, "first_usable_snapshot", 100, 350)
        entry = timings["first_usable_snapshot"]
        self.assertEqual(entry["status"], "passed")
        self.assertEqual(entry["elapsed_ns"], 250)
        self.assertEqual(entry["success_timing_ns"], 250)
        self.assertNotIn("failure", entry)

    def test_failure_keeps_elapsed_and_failure_without_success_timing(self):
        timings = {}
        finish_operation(timings, "tap_to_fresh_frame", 100, 400,
                          error=BenchmarkError("the tap did not settle"))
        entry = timings["tap_to_fresh_frame"]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry["elapsed_ns"], 300)
        self.assertIn("the tap did not settle", entry["failure"])
        self.assertNotIn("success_timing_ns", entry)

    def test_skipped_operation_records_reason_not_elapsed(self):
        timings = {}
        skip_operation(timings, "tap_to_fresh_frame", "no qualified element")
        entry = timings["tap_to_fresh_frame"]
        self.assertEqual(entry["status"], "skipped")
        self.assertIsNone(entry["elapsed_ns"])
        self.assertEqual(entry["reason"], "no qualified element")


class ReceiptSchemaTest(unittest.TestCase):
    def memory_stages(self):
        sample = {"role": "app", "pid": 1, "expected_start_abstime": 4,
                  "observed_start_abstime": 4, "status": "sampled",
                  "ri_phys_footprint_bytes": 3072, "ri_resident_size_bytes": 20,
                  "ri_wired_size_bytes": 10}
        missing = {"role": "helper", "pid": 9, "expected_start_abstime": None,
                   "status": "lost"}
        return {stage: [dict(sample), dict(missing)]
                for stage in ("post_arm_start", "first_snapshot_boundary",
                              "tap_boundary", "post_shutdown")}

    def valid_receipt(self, status="passed", first_status="passed", tap_status="passed"):
        return build_receipt(
            scope={"arm": "apple-mcp", "condition": "cold",
                   "simulator": {"udid": UDID},
                   "app": {"bundle_id": "dev.ashnazg.ios.fixture", "sha256": "0" * 64},
                   "source_revisions": {"repository": "d" * 40}},
            timings={"first_usable_snapshot": self._timing(first_status),
                     "tap_to_fresh_frame": self._timing(tap_status)},
            memory={"stages": self.memory_stages(),
                    "shared_services": {"scope": "shared-coresimulator-host-services",
                                        "stages": {}}},
            shutdown={"simulator_shutdown": True, "owned_helpers_gone": True},
            artifacts={"after-tap.png": {"bytes": 10, "sha256": "e" * 64}},
            status=status,
        )

    @staticmethod
    def _timing(status):
        if status == "skipped":
            return {"status": "skipped", "elapsed_ns": None, "reason": "not qualified"}
        entry = {"status": status, "elapsed_ns": 1000}
        if status == "passed":
            entry["success_timing_ns"] = 1000
        else:
            entry["failure"] = "observed failure record"
        return entry

    def test_valid_receipt_has_no_problems(self):
        self.assertEqual(validate_receipt(self.valid_receipt()), [])

    def test_failed_operation_still_valid_when_failure_is_kept(self):
        receipt = self.valid_receipt(status="failed", first_status="failed",
                                     tap_status="skipped")
        self.assertEqual(validate_receipt(receipt), [])

    def test_missing_scope_fields_are_reported(self):
        receipt = self.valid_receipt()
        del receipt["scope"]["simulator"]
        del receipt["scope"]["source_revisions"]
        del receipt["shutdown"]
        self.assertIn("scope.simulator.udid", validate_receipt(receipt))
        self.assertIn("scope.source_revisions", validate_receipt(receipt))
        self.assertIn("shutdown", validate_receipt(receipt))

    def test_passed_operation_without_success_timing_is_invalid(self):
        receipt = self.valid_receipt()
        del receipt["timings"]["first_usable_snapshot"]["success_timing_ns"]
        self.assertIn("timings.first_usable_snapshot.success_timing_ns",
                      validate_receipt(receipt))

    def test_failed_operation_without_failure_record_is_invalid(self):
        receipt = self.valid_receipt(status="failed", first_status="failed")
        del receipt["timings"]["first_usable_snapshot"]["failure"]
        self.assertIn("timings.first_usable_snapshot.failure", validate_receipt(receipt))

    def test_missing_memory_stage_is_reported(self):
        receipt = self.valid_receipt()
        del receipt["memory"]["stages"]["tap_boundary"]
        self.assertIn("memory.stages.tap_boundary", validate_receipt(receipt))

    def test_sampled_entry_without_footprint_is_invalid(self):
        receipt = self.valid_receipt()
        receipt["memory"]["stages"]["post_arm_start"][0].pop("ri_phys_footprint_bytes")
        self.assertIn("memory.stages.post_arm_start.footprint", validate_receipt(receipt))


class FakeTracker:
    def __init__(self):
        self.stages = {stage: [{"role": "app", "pid": 111, "status": "sampled",
                                 "expected_start_abstime": 4, "observed_start_abstime": 4,
                                 "ri_phys_footprint_bytes": 3072}]
                       for stage in ("post_arm_start", "first_snapshot_boundary",
                                      "tap_boundary", "post_shutdown")}
        self.shared_stages = {stage: [] for stage in self.stages}

    def sample_stage(self, stage, udid, app_bundle_name, helper_pids=()):
        pass


class FakeArm:
    name = "apple-mcp"
    first_snapshot_boundaries = "synthetic boundaries"

    def __init__(self, context):
        self.context = context
        self.first_error = None
        self.tap_error = None

    def start(self):
        self.context.timings["arm_start"] = {"status": "passed", "elapsed_ns": 1}

    def first_usable_snapshot(self):
        return {"usable": {"point": "1 2"}}, self.first_error

    def tap_to_fresh_frame(self, first_record):
        return {"semantic_change": ["Count: 0", "Count: 1"]}, self.tap_error

    def shutdown(self):
        return {"end_session": "passed"}


class RunObservationTest(unittest.TestCase):
    """Stubbed driver test: no device, simctl, or real process is involved."""

    def observe(self, first_error=None, tap_error=None, start_error=None, end_session="passed",
                booted=False, owned=True):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))
            context.simulator_scope["created_by_run"] = owned
            arm = FakeArm
            arm.first_snapshot_boundaries = FakeArm.first_snapshot_boundaries
            state = "Booted" if booted else "Shutdown"

            def shutdown_device(*args, **kwargs):
                nonlocal state
                self.assertTrue(owned, "an unowned simulator was acted on")
                self.assertEqual(args, ("shutdown", UDID))
                state = "Shutdown"

            class ConfiguredArm(FakeArm):
                def __init__(self, context):
                    super().__init__(context)
                    self.first_error = first_error
                    self.tap_error = tap_error

                def start(self):
                    if start_error is not None:
                        raise start_error
                    super().start()

                def shutdown(self):
                    return {"end_session": end_session}

            with patch.dict(ARM_CLASSES, {"apple-mcp": ConfiguredArm}), \
                    patch("scripts.ios_session_benchmark.MemoryTracker", FakeTracker), \
                    patch("scripts.ios_session_benchmark.simctl", shutdown_device), \
                    patch("scripts.ios_session_benchmark.device_state",
                          lambda udid: state):
                receipt = run_observation("apple-mcp", context)
            return receipt

    def test_passing_observation_produces_a_valid_passed_receipt(self):
        receipt = self.observe()
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(validate_receipt(receipt), [])
        first = receipt["timings"]["first_usable_snapshot"]
        self.assertEqual(first["status"], "passed")
        self.assertEqual(first["success_timing_ns"], first["elapsed_ns"])
        self.assertEqual(receipt["shutdown"]["simulator_shutdown"], True)
        self.assertEqual(receipt["shutdown"]["owned_helpers_gone"], True)

    def test_failed_first_snapshot_keeps_elapsed_skips_tap_and_stays_valid(self):
        receipt = self.observe(first_error=BenchmarkError("no usable hierarchy"))
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(validate_receipt(receipt), [])
        first = receipt["timings"]["first_usable_snapshot"]
        self.assertEqual(first["status"], "failed")
        self.assertIsInstance(first["elapsed_ns"], int)
        self.assertIn("no usable hierarchy", first["failure"])
        self.assertNotIn("success_timing_ns", first)
        tap = receipt["timings"]["tap_to_fresh_frame"]
        self.assertEqual(tap["status"], "skipped")
        self.assertEqual(receipt["shutdown"]["simulator_shutdown"], True)

    def test_failed_tap_keeps_elapsed_and_failure_record(self):
        receipt = self.observe(tap_error=BenchmarkError("the tap did not settle"))
        self.assertEqual(receipt["status"], "failed")
        tap = receipt["timings"]["tap_to_fresh_frame"]
        self.assertEqual(tap["status"], "failed")
        self.assertIsInstance(tap["elapsed_ns"], int)
        self.assertIn("the tap did not settle", tap["failure"])
        self.assertNotIn("success_timing_ns", tap)
        self.assertEqual(receipt["timings"]["first_usable_snapshot"]["status"], "passed")

    def test_failed_apple_session_cleanup_disqualifies_the_observation(self):
        receipt = self.observe(end_session="failed")
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["shutdown"]["end_session"], "failed")
        self.assertEqual(receipt["shutdown"]["simulator_shutdown"], True)

    def test_benchmark_requests_shutdown_before_confirming_the_final_state(self):
        receipt = self.observe(booted=True)
        self.assertEqual(receipt["status"], "passed")
        self.assertTrue(receipt["shutdown"]["simulator_shutdown"])

    def test_benchmark_refuses_to_shut_down_an_unowned_simulator(self):
        receipt = self.observe(booted=True, owned=False)
        self.assertEqual(receipt["status"], "failed")
        self.assertFalse(receipt["shutdown"]["simulator_shutdown"])

    def test_bridge_acceptance_error_is_retained_not_raised(self):
        # StdioClient raises AcceptanceError, which is not a BenchmarkError;
        # the observation must retain it instead of escaping the driver.
        receipt = self.observe(
            start_error=AcceptanceError("CLI closed stdout before MCP response"))
        self.assertEqual(receipt["status"], "failed")
        self.assertIn("closed stdout", receipt["error"])


# Recorded cross-tool digest vectors from src/ios_app.rs (the pinned Meer
# hashApp convention): identical bytes, modes and link targets are rebuilt
# here and must reproduce the Rust digests exactly.
MEER_BASE = "bc5165568ee895beb0453a6cc62f679c3e6281d3aee02987455e4f3254d07cd2"
MEER_ALIAS_TO_MEER = "3c0c41dbf49a5596b566243372a9dc5910f4822d31bb9bb851b8328ff5127aa5"
MEER_CHMOD_0700 = "6e8334c1ce4f9a6fa425d0a3c50ef62e5348c068465b39b154a43b7630f01bd3"
REFERENCE_BASE = "9976ea2eac46c31103de5f24ab75ee1045708a2912297f20be0a6b83a21ec030"
REFERENCE_ALIAS_TO_OTHER = "78cf9325fdf2920055f431c3f673970abce0d54f8b8f315fa8b4363488f8b0f4"
REFERENCE_CHMOD_0644 = "14b057df05bb62ecbfdd6c2f8f21a2ab93cdea0999d96b5c6829382bd95b7e59"
PNG_HEAD = b"\x89PNG\r\n\x1a\n" + b"fixture-frame-bytes"


def build_meer_fixture(root):
    (root / "Sub").mkdir()
    os.chmod(root / "Sub", 0o755)
    (root / "Meer").write_bytes(b"#!/bin/true\n")
    os.chmod(root / "Meer", 0o755)
    (root / "Info.plist").write_bytes(b"plist-bytes")
    os.chmod(root / "Info.plist", 0o644)
    nested = root / "Sub" / "nested.txt"
    nested.write_bytes(b"one")
    os.chmod(nested, 0o600)
    os.symlink("Info.plist", root / "Alias.plist")


def build_reference_fixture(root):
    (root / "Contents").mkdir()
    os.chmod(root / "Contents", 0o755)
    for name in ("tool", "other"):
        path = root / "Contents" / name
        path.write_bytes(b"frozen\x00test\n")
        os.chmod(path, 0o755)
    os.symlink("Contents/tool", root / "Alias")


class AppTreeDigestTest(unittest.TestCase):
    """The .app identity must be the pinned hashApp tree digest, not a
    per-file read that crashes on the bundle directory."""

    def test_matches_the_recorded_meer_hashapp_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Meer.app"
            root.mkdir()
            build_meer_fixture(root)
            self.assertEqual(app_tree_sha256(root), MEER_BASE)
            os.remove(root / "Alias.plist")
            os.symlink("Meer", root / "Alias.plist")
            self.assertEqual(app_tree_sha256(root), MEER_ALIAS_TO_MEER)
            os.chmod(root / "Meer", 0o700)
            self.assertEqual(app_tree_sha256(root), MEER_CHMOD_0700)

    def test_matches_the_recorded_independent_reference_digests(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "App.app"
            root.mkdir()
            build_reference_fixture(root)
            self.assertEqual(app_tree_sha256(root), REFERENCE_BASE)
            os.remove(root / "Alias")
            os.symlink("Contents/other", root / "Alias")
            self.assertEqual(app_tree_sha256(root), REFERENCE_ALIAS_TO_OTHER)
            os.chmod(root / "Contents" / "tool", 0o644)
            self.assertEqual(app_tree_sha256(root), REFERENCE_CHMOD_0644)

    def test_rejects_invalid_content_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Bad.app"
            root.mkdir()
            (root / "real.bin").write_bytes(b"x")
            os.symlink("/etc/hosts", root / "absolute")
            with self.assertRaises(BenchmarkError) as caught:
                app_tree_sha256(root)
            self.assertIn("absolute symlink", str(caught.exception))

            os.remove(root / "absolute")
            os.symlink("nowhere", root / "dangling")
            with self.assertRaises(BenchmarkError) as caught:
                app_tree_sha256(root)
            self.assertIn("dangling or escaping", str(caught.exception))

            os.remove(root / "dangling")
            outside = Path(directory) / "outside.txt"
            outside.write_bytes(b"outside")
            os.symlink("../outside.txt", root / "escape")
            with self.assertRaises(BenchmarkError) as caught:
                app_tree_sha256(root)
            self.assertIn("escaping", str(caught.exception))

            os.remove(root / "escape")
            os.mkfifo(root / "fifo")
            with self.assertRaises(BenchmarkError) as caught:
                app_tree_sha256(root)
            self.assertIn("non-regular", str(caught.exception))

            os.remove(root / "fifo")
            open(os.path.join(str(root), os.fsdecode(b"bad\xff.txt")), "wb").close()
            with self.assertRaises(BenchmarkError) as caught:
                app_tree_sha256(root)
            self.assertIn("non-UTF-8", str(caught.exception))

            empty = Path(directory) / "Empty.app"
            empty.mkdir()
            with self.assertRaises(BenchmarkError) as caught:
                app_tree_sha256(empty)
            self.assertIn("no files to pin", str(caught.exception))


class PreflightAppTest(unittest.TestCase):
    def test_preflight_digests_a_real_app_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            app = Path(directory) / "Meer.app"
            (app / "Contents").mkdir(parents=True)
            (app / "Contents" / "Info.plist").write_bytes(b"plist-bytes")
            executable = app / "Contents" / "Meer"
            executable.write_bytes(b"#!/bin/true\n")
            os.chmod(executable, 0o755)
            identity = preflight_app(str(app))
            self.assertEqual(identity["sha256"], app_tree_sha256(app))
            self.assertEqual(len(identity["sha256"]), 64)
            self.assertTrue(identity["path"].endswith(".app"))
            self.assertIn("pinned hashApp", identity["sha256_basis"])

    def test_preflight_rejects_non_directories_and_wrong_suffixes(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "Not.app"
            plain.write_bytes(b"an ordinary file")
            with self.assertRaises(BenchmarkError) as caught:
                preflight_app(str(plain))
            self.assertIn("must be an existing .app bundle directory", str(caught.exception))
            bundle = Path(directory) / "bundle"
            bundle.mkdir()
            with self.assertRaises(BenchmarkError):
                preflight_app(str(bundle))


class AppleEnableFailClosedTest(unittest.TestCase):
    def test_refused_device_admission_keeps_the_owned_session_key_for_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            arm = AppleMcpArm(make_context(Path(directory)))
            calls = []

            def call(name, arguments):
                calls.append((name, arguments))
                return {
                    "deviceIsSimulator": True,
                    "deviceUUID": "unexpected-device",
                    "interactionSessionKey": "owned-test-session",
                }

            arm.call = call
            with self.assertRaisesRegex(BenchmarkError, "other than the owned simulator"):
                arm.first_usable_snapshot()
            self.assertEqual(arm.shutdown()["end_session"], "passed")
            self.assertEqual([name for name, _ in calls], [
                "DeviceInteractionStartSession", "DeviceInteractionEndSession",
            ])

    def test_end_session_uses_the_apple_fixture_parameter(self):
        with tempfile.TemporaryDirectory() as directory:
            arm = AppleMcpArm(make_context(Path(directory)))
            arm.session_key = "owned-test-session"
            calls = []
            arm.call = lambda name, arguments: calls.append((name, arguments))

            self.assertEqual(arm.shutdown()["end_session"], "passed")
            self.assertEqual(calls, [
                ("DeviceInteractionEndSession", {"interactionSessionKey": "owned-test-session"}),
            ])

    def test_full_device_interaction_surface_passes(self):
        self.assertIs(require_apple_tools(list(APPLE_REQUIRED_TOOLS) + ["Other"]), True)

    def test_missing_device_interaction_tool_fails_closed(self):
        with self.assertRaises(BenchmarkError) as caught:
            require_apple_tools(["DeviceInteractionStartSession", "SomethingElse"])
        self.assertIn("DeviceInteractionSynthesize", str(caught.exception))
        self.assertIn("enablement", str(caught.exception))

    def test_arm_start_fails_closed_before_any_session(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory))

            class FakeProcess:
                pid = 4321

            with patch("scripts.ios_session_benchmark.subprocess.Popen",
                       return_value=FakeProcess()), \
                    patch("scripts.ios_session_benchmark.StdioClient",
                          return_value=object()), \
                    patch("scripts.ios_session_benchmark.list_tools",
                          lambda client: {"tools": [{"name": "SomethingElse"}]}):
                arm = AppleMcpArm(context)
                with self.assertRaises(BenchmarkError) as caught:
                    arm.start()
            self.assertIn("enablement", str(caught.exception))
            self.assertNotIn("arm_start", context.timings)


class AshnazgContractTest(unittest.TestCase):
    def test_screenshot_blocks_shape_matches_the_bridge(self):
        # src/mcp.rs returns content blocks only: one base64 image plus one
        # text block of JSON metadata; there is no structuredContent.
        result = {"content": [
            {"type": "image", "data": base64.b64encode(PNG_HEAD).decode()},
            {"type": "text", "text": json.dumps(
                {"mimeType": "image/png", "snapshotId": 9, "marks": [],
                 "width": 3, "height": 4, "platform": "ios-app"})},
        ]}
        image, metadata = parse_ashnazg_screenshot(result)
        self.assertEqual(image, PNG_HEAD)
        self.assertEqual(metadata["snapshotId"], 9)

    def test_screenshot_fails_closed_on_wrong_shapes(self):
        structured_only = {"structuredContent": {"snapshotId": 9}, "content": []}
        with self.assertRaises(BenchmarkError) as caught:
            parse_ashnazg_screenshot(structured_only)
        self.assertIn("exactly one image", str(caught.exception))

        two_images = {"content": [
            {"type": "image", "data": base64.b64encode(PNG_HEAD).decode()},
            {"type": "image", "data": base64.b64encode(PNG_HEAD).decode()},
            {"type": "text", "text": json.dumps({"snapshotId": 9})},
        ]}
        with self.assertRaises(BenchmarkError):
            parse_ashnazg_screenshot(two_images)

        no_text = {"content": [{"type": "image",
                                "data": base64.b64encode(PNG_HEAD).decode()}]}
        with self.assertRaises(BenchmarkError):
            parse_ashnazg_screenshot(no_text)

        bad_base64 = {"content": [{"type": "image", "data": "!!!not base64!!!"},
                                  {"type": "text", "text": "{}"}]}
        with self.assertRaises(BenchmarkError):
            parse_ashnazg_screenshot(bad_base64)

        not_png = {"content": [
            {"type": "image", "data": base64.b64encode(b"GIF89a").decode()},
            {"type": "text", "text": json.dumps({"snapshotId": 9})}]}
        with self.assertRaises(BenchmarkError) as caught:
            parse_ashnazg_screenshot(not_png)
        self.assertIn("not a PNG", str(caught.exception))

        bad_metadata = {"content": [
            {"type": "image", "data": base64.b64encode(PNG_HEAD).decode()},
            {"type": "text", "text": "not json"}]}
        with self.assertRaises(BenchmarkError):
            parse_ashnazg_screenshot(bad_metadata)

    def test_fresh_frame_requires_an_advancing_snapshot_id(self):
        self.assertEqual(require_advancing_snapshot_id(5, 6, "frame"), 6)
        for stale in (5, None, True, "7", 0):
            with self.assertRaises(BenchmarkError) as caught:
                require_advancing_snapshot_id(5, stale, "frame")
            self.assertIn("did not advance", str(caught.exception))


class AshnazgDescriptorFailClosedTest(unittest.TestCase):
    def descriptor(self, udid=UDID, bundle="dev.ashnazg.ios.fixture",
                  sha256="0" * 64):
        return {"bundleId": bundle, "appSha256": sha256,
                "simulator": {"udid": udid, "runtime": "iOS-26-5",
                               "deviceType": "iPhone-17"}}

    def test_start_requests_and_validates_the_descriptor_before_sampling(self):
        with tempfile.TemporaryDirectory() as directory:
            context = make_context(Path(directory), arm="ashnazg-mcp")
            context.arm_command = ["ashnazg", "mcp", "{udid}"]
            arm = AshnazgMcpArm(context)
            descriptor = self.descriptor()
            descriptor["pid"] = 5678
            with (
                patch("scripts.ios_session_benchmark.subprocess.Popen") as spawn,
                patch("scripts.ios_session_benchmark.StdioClient") as client,
                patch.object(arm, "_request", side_effect=[
                    {"result": {"protocolVersion": "2024-11-05"}},
                    {"result": {"tools": [{"name": name}
                                          for name in ("ui_act", "ui_snapshot", "ui_screenshot")]}},
                ]),
            ):
                spawn.return_value.pid = 1234
                client.return_value.stderr = ("ios-app-session " + json.dumps(descriptor)).encode()
                arm.start()
            self.assertEqual(spawn.call_args.kwargs["env"]["RUST_LOG"], "info")
            self.assertEqual(context.helper_pids, [1234, 5678])
            self.assertEqual(context.receipt_extras["arm_selected_simulator"], context.udid)

    def test_matching_descriptor_pins_the_owned_simulator_and_app(self):
        record = require_owned_session_descriptor(
            self.descriptor(), udid=UDID.lower(),
            bundle_id="dev.ashnazg.ios.fixture", app_sha256="0" * 64)
        self.assertEqual(record["arm_selected_simulator"], UDID)
        self.assertEqual(record["descriptor_app_sha256"], "0" * 64)

    def test_mismatched_simulator_bundle_or_digest_fails_closed(self):
        with self.assertRaises(BenchmarkError) as caught:
            require_owned_session_descriptor(self.descriptor(udid="OTHER"),
                                              UDID, "dev.ashnazg.ios.fixture", "0" * 64)
        self.assertIn("other than the owned one", str(caught.exception))

        with self.assertRaises(BenchmarkError) as caught:
            require_owned_session_descriptor(self.descriptor(bundle="com.other"),
                                              UDID, "dev.ashnazg.ios.fixture", "0" * 64)
        self.assertIn("bundle other than", str(caught.exception))

        with self.assertRaises(BenchmarkError) as caught:
            require_owned_session_descriptor(self.descriptor(sha256="1" * 64),
                                              UDID, "dev.ashnazg.ios.fixture", "0" * 64)
        self.assertIn("app bytes other than", str(caught.exception))

        with self.assertRaises(BenchmarkError) as caught:
            require_owned_session_descriptor(None, UDID, "dev.ashnazg.ios.fixture", "0" * 64)
        self.assertIn("never reported", str(caught.exception))


class FakeRunnerProcess:
    def __init__(self, exit_code):
        self.exit_code = exit_code

    def poll(self):
        return self.exit_code


class TapArmContractTest(unittest.TestCase):
    def test_apple_returns_a_stage_record_after_one_verified_tap(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            arm = AppleMcpArm(make_context(output))
            arm.session_key = "test-session"
            after_tree = output / "exported-hierarchy.txt"
            after_frame = output / "exported-frame.png"
            after_tree.write_text("Count: 1")
            after_frame.write_bytes(PNG_HEAD)
            with patch.object(arm, "call", return_value={
                "hierarchyPath": str(after_tree), "screenshotPath": str(after_frame),
            }) as call:
                result, error = arm.tap_to_fresh_frame({
                    "usable": {"point": "1 2"}, "screenshot": {"sha256": "previous"},
                })
            self.assertIsNone(error)
            self.assertEqual(result["semantic_change"], ["Count: 0", "Count: 1"])
            call.assert_called_once()
            self.assertEqual(call.call_args.args[1]["interactionCommand"], "t 1 2")

    def test_ashnazg_returns_a_stage_record_after_one_verified_tap(self):
        with tempfile.TemporaryDirectory() as directory:
            arm = AshnazgMcpArm(make_context(Path(directory), arm="ashnazg-mcp"))
            with (
                patch.object(arm, "_call", return_value={"settled": True, "snapshot": {
                    "snapshotId": 2, "nodes": [{"text": "Count: 1"}],
                }}) as act,
                patch.object(arm, "_call_result", return_value={"content": [
                    {"type": "image", "data": base64.b64encode(PNG_HEAD).decode()},
                    {"type": "text", "text": json.dumps({"snapshotId": 3})},
                ]}),
            ):
                result, error = arm.tap_to_fresh_frame({"snapshot_id": 1})
            self.assertIsNone(error)
            self.assertEqual(result["boundaries"]["fresh_frame_snapshot_id"], 3)
            act.assert_called_once()


class AshnazgManifestHandoffTest(unittest.TestCase):
    def test_cold_and_warm_manifests_pin_the_same_owned_simulator_and_app(self):
        from scripts.ios_session_benchmark import write_ashnazg_arm_manifest

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            template = {"readySelector": {"testId": "login-email-input"}, "deepLinks": []}
            for condition in ("cold", "warm"):
                stage = output / condition
                stage.mkdir()
                context = make_context(stage, condition=condition, arm="ashnazg-mcp")
                context.simulator_scope["name"] = "AshnazgIosBenchmark-d38dcceaae01"
                result = write_ashnazg_arm_manifest(context, template)
                manifest = json.loads(result.read_text())
                self.assertEqual(manifest["benchmarkSimulator"], {
                    "udid": context.udid, "name": context.simulator_scope["name"]})
                self.assertEqual(manifest["appSha256"], context.app_sha256)
                self.assertEqual(manifest["appPath"], str(context.app_path))
                self.assertEqual(manifest["readySelector"], template["readySelector"])
                self.assertEqual(result.stat().st_mode & 0o777, 0o600)
            self.assertEqual(template, {"readySelector": {"testId": "login-email-input"},
                                        "deepLinks": []})
            context.simulator_scope["created_by_run"] = False
            with self.assertRaisesRegex(BenchmarkError, "outside this run"):
                write_ashnazg_arm_manifest(context, template)


class DirectXctestArmTest(unittest.TestCase):
    """No device or runner binary is involved: the process is a stub."""

    def arm_with(self, directory, exit_code, log_text, artifacts=None):
        output = Path(directory)
        (output / "xctest.log").write_text(log_text)
        context = make_context(output, arm="direct-xctest")
        context.xctest_artifacts = artifacts
        arm = DirectXctestArm(context)
        arm.process = FakeRunnerProcess(exit_code)
        return arm, context

    def test_reads_native_intervals_and_frames_from_the_runner_result_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            arm, context = self.arm_with(
                directory, exit_code=0, log_text="Test Case 'Fixture tap' passed (1.250 seconds)\n")
            arm.result_bundle = context.output / "trial.xcresult"
            arm.result_bundle.mkdir()
            attachments = {
                "fixture-observation-boundaries": json.dumps({
                    "schema": "simulator-probe-boundaries", "version": 1,
                    "clock": "dispatch-uptime-ns",
                    "firstUsableStartUptimeNs": 100, "firstUsableEndUptimeNs": 300,
                    "tapStartUptimeNs": 350, "tapEndUptimeNs": 450,
                    "buttonRect": [1, 2, 3, 4], "countRect": [2, 3, 4, 5],
                    "windowRect": [0, 0, 10, 20],
                    "captureExtent": "app", "capturePixels": [10, 20],
                    "preCapture": "before-increment", "postCapture": "after-increment",
                }).encode(),
                "before-increment": struct.pack(">8sI4sII", PNG_HEAD[:8], 13, b"IHDR", 10, 20)
                                    + b"before",
                "after-increment": struct.pack(">8sI4sII", PNG_HEAD[:8], 13, b"IHDR", 10, 20)
                                   + b"after",
            }

            def export(command, **kwargs):
                self.assertEqual(command[:4], ["xcrun", "xcresulttool", "export", "attachments"])
                self.assertEqual(command[command.index("--path") + 1], str(arm.result_bundle))
                output = Path(command[command.index("--output-path") + 1])
                metadata = []
                for i, (name, data) in enumerate(attachments.items()):
                    filename = f"attachment-{i}"
                    (output / filename).write_bytes(data)
                    metadata.append({"suggestedHumanReadableName": f"{name}_0",
                                     "exportedFileName": filename, "deviceId": context.udid,
                                     "isAssociatedWithFailure": False})
                (output / "manifest.json").write_text(json.dumps([{
                    "testIdentifier": "SimulatorProbeTests/testObserveTapObserveAndCapture()",
                    "attachments": metadata,
                }]))
                return subprocess.CompletedProcess(command, 0)

            with patch("scripts.ios_session_benchmark.subprocess.run", side_effect=export):
                first, error = arm.first_usable_snapshot()
                self.assertIsNone(error)
                self.assertEqual(first["native_elapsed_ns"], 200)
                tap, error = arm.tap_to_fresh_frame(first)
                self.assertIsNone(error)
                self.assertEqual(tap["native_elapsed_ns"], 100)
                self.assertEqual(tap["capture_pixels"], [10, 20])
                self.assertTrue((context.output / "xctest-boundaries.json").is_file())
                attachments["after-increment"] = attachments["before-increment"]
                with self.assertRaisesRegex(BenchmarkError, "repeated"):
                    arm.first_usable_snapshot()

    def test_nonzero_runner_exit_is_retained_unqualified_with_the_log(self):
        with tempfile.TemporaryDirectory() as directory:
            log = "Test Case 'Fixture tap' passed (1.250 seconds)\nboom\n"
            arm, context = self.arm_with(directory, exit_code=2, log_text=log)
            record, error = arm.first_usable_snapshot()
            self.assertIsNotNone(error)
            self.assertIn("exited with 2", str(error))
            self.assertIn("unqualified", str(error))
            self.assertEqual(context.receipt_extras["runner_exit_code"], 2)
            self.assertIn("Fixture tap", record["log_tail"])
            self.assertEqual(context.receipt_extras["runner_durations"]
                             ["Fixture tap"]["seconds"], 1.25)

    def test_zero_exit_with_passing_cases_qualifies_only_the_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            log = "Test Case 'Fixture tap' passed (1.250 seconds)\n"
            arm, context = self.arm_with(directory, exit_code=0, log_text=log)
            record, error = arm.first_usable_snapshot()
            self.assertIsNone(error)
            self.assertEqual(record["durations"]["Fixture tap"]["seconds"], 1.25)

    def test_tap_row_stays_unqualified_even_with_success_and_captures(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory) / "captures"
            artifacts.mkdir()
            (artifacts / "before.png").write_bytes(PNG_HEAD)
            (artifacts / "after.png").write_bytes(PNG_HEAD + b"-changed")
            arm, context = self.arm_with(directory, exit_code=0,
                                          log_text="", artifacts=str(artifacts))
            record, error = arm.tap_to_fresh_frame({})
            self.assertIsNotNone(error)
            self.assertIn("unqualified", str(error))
            self.assertFalse(record["comparable"])
            self.assertIn("boundary-scoped duration", record["required_for_comparable"])
            captures = record["declared_captures"]
            self.assertTrue(captures["present"])
            self.assertEqual(sorted(captures["pngs"]),
                             ["after.png", "before.png"])
            self.assertEqual(captures["invalid_pngs"], [])
            self.assertEqual(context.receipt_extras["tap_qualification"]["comparable"],
                             False)

    def test_missing_or_invalid_captures_are_recorded_not_qualified(self):
        with tempfile.TemporaryDirectory() as directory:
            arm, context = self.arm_with(directory, exit_code=0, log_text="")
            record, error = arm.tap_to_fresh_frame({})
            self.assertIsNotNone(error)
            self.assertFalse(record["declared_captures"]["present"])

            artifacts = Path(directory) / "captures"
            artifacts.mkdir()
            (artifacts / "fake.png").write_bytes(b"not really a png")
            arm, context = self.arm_with(directory, exit_code=0, log_text="",
                                         artifacts=str(artifacts))
            record, error = arm.tap_to_fresh_frame({})
            self.assertIsNotNone(error)
            self.assertEqual(record["declared_captures"]["invalid_pngs"], ["fake.png"])
            self.assertIn("not a PNG",
                          record["declared_captures"]["pngs"]["fake.png"]["invalid"])


class FakeSimctl:
    def __init__(self, states=None, fail_on=()):
        self.calls = []
        self.states = states or {}
        self.fail_on = set(fail_on)

    def __call__(self, *args, timeout=180):
        self.calls.append(args)
        if args[0] in self.fail_on:
            raise subprocess.CalledProcessError(1, ["xcrun", "simctl", *args])
        if args[:2] == ("list", "-j"):
            listing = {"devices": {"iOS 26.5": [
                {"udid": udid, "state": state} for udid, state in self.states.items()]}}
            return json.dumps(listing)
        return ""


class CleanupOwnedSimulatorTest(unittest.TestCase):
    def test_owned_simulator_is_shut_down_deleted_and_verified_absent(self):
        simctl = FakeSimctl(states={})
        record = cleanup_owned_simulator(
            {"udid": UDID, "created_by_run": True}, simctl)
        self.assertTrue(record["requested"])
        self.assertEqual(record["shutdown"], "passed")
        self.assertEqual(record["delete"], "passed")
        self.assertTrue(record["verified_absent"])
        self.assertIn(("shutdown", UDID), simctl.calls)
        self.assertIn(("delete", UDID), simctl.calls)
        self.assertIn(("list", "-j", "devices"), simctl.calls)

    def test_failed_delete_is_surfaced_and_absence_not_verified(self):
        simctl = FakeSimctl(states={UDID: "Shutdown"}, fail_on={"delete"})
        record = cleanup_owned_simulator(
            {"udid": UDID, "created_by_run": True}, simctl)
        self.assertTrue(record["requested"])
        self.assertIn("failed", record["delete"])
        self.assertFalse(record["verified_absent"])
        self.assertEqual(record["state_after"], "Shutdown")

    def test_unowned_simulators_are_never_touched(self):
        simctl = FakeSimctl()
        record = cleanup_owned_simulator(
            {"udid": UDID, "created_by_run": False}, simctl)
        self.assertFalse(record["requested"])
        self.assertEqual(simctl.calls, [])
        self.assertIn("never", record["reason"])

        empty = FakeSimctl()
        record = cleanup_owned_simulator({}, empty)
        self.assertFalse(record["requested"])
        self.assertEqual(empty.calls, [])
        self.assertIn("no simulator was selected", record["reason"])


class DriveConditionsTest(unittest.TestCase):
    def conditions_args(self, directory):
        return Namespace(arm="apple-mcp", output=Path(directory))

    def test_warm_restart_refuses_a_simulator_created_outside_this_run(self):
        slot = {"udid": UDID, "created_by_run": False}
        with patch("scripts.ios_session_benchmark.device_state") as device_state:
            with self.assertRaisesRegex(BenchmarkError, "cold run"):
                prepare_warm_simulator(Namespace(), slot)
        device_state.assert_not_called()

    def test_invalid_receipt_cannot_be_recorded_as_passed(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("scripts.ios_session_benchmark.prepare_cold_simulator", return_value=UDID),
            patch("scripts.ios_session_benchmark.ObservationContext"),
            patch("scripts.ios_session_benchmark.run_observation", return_value={"status": "passed"}),
            patch("scripts.ios_session_benchmark.validate_receipt", return_value=["missing timing"]),
        ):
            args = Namespace(device_type="phone", runtime="runtime", revisions={},
                             app_identity={"sha256": "0" * 64, "sha256_basis": "test"})
            output = Path(directory)
            receipt = run_condition(args, "apple-mcp", "cold", {}, output)
            self.assertEqual(receipt["status"], "failed")
            self.assertEqual(json.loads((output / "receipt.json").read_text())["status"], "failed")


    def test_cleanup_runs_in_finally_when_a_condition_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            slot = {"udid": UDID, "created_by_run": True}
            simctl = FakeSimctl(states={})

            def exploding_run_condition(*args, **kwargs):
                raise BenchmarkError("simctl boot failed on the owned device")

            cleanup, succeeded = drive_conditions(
                self.conditions_args(directory), ["cold"], slot,
                run_condition_fn=exploding_run_condition, simctl_runner=simctl)
            self.assertFalse(succeeded)
            self.assertTrue(cleanup["verified_absent"])
            self.assertIn(("delete", UDID), simctl.calls)

    def test_passing_conditions_with_verified_cleanup_succeed(self):
        with tempfile.TemporaryDirectory() as directory:
            slot = {"udid": UDID, "created_by_run": True}
            simctl = FakeSimctl(states={})
            cleanup, succeeded = drive_conditions(
                self.conditions_args(directory), ["cold", "warm"], slot,
                run_condition_fn=lambda *args, **kwargs: {"status": "passed"},
                simctl_runner=simctl)
            self.assertTrue(succeeded)
            self.assertTrue(cleanup["verified_absent"])

    def test_failed_cleanup_keeps_the_run_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            slot = {"udid": UDID, "created_by_run": True}
            simctl = FakeSimctl(states={UDID: "Shutdown"}, fail_on={"delete"})
            cleanup, succeeded = drive_conditions(
                self.conditions_args(directory), ["cold"], slot,
                run_condition_fn=lambda *args, **kwargs: {"status": "passed"},
                simctl_runner=simctl)
            self.assertTrue(succeeded)
            self.assertFalse(cleanup["verified_absent"])
            cleanup_ok = (cleanup.get("requested") is not True
                          or cleanup.get("verified_absent") is True)
            self.assertFalse(cleanup_ok)


if __name__ == "__main__":
    unittest.main()

class MeerCheckpointTests(unittest.TestCase):
    def test_acknowledges_the_next_milestone_after_sampling(self):
        from socket import create_connection
        from scripts.ios_session_benchmark import MeerCheckpointServer

        tracker = MemoryTracker(
            sampler=lambda pid: {"ri_phys_footprint": pid * 100,
                                 "ri_proc_start_abstime": pid * 10,
                                 "ri_resident_size": 0, "ri_wired_size": 0},
            ps_lister=lambda: (
                "10 /CoreSimulator/Devices/TEST/data/Meer.app/Meer\n"
                "20 launchd_sim /CoreSimulator/Devices/TEST\n"
            ),
        )
        with MeerCheckpointServer(tracker, "TEST", "Meer", [30]) as server:
            def send(name, sequence, uptime):
                row = {"op": "checkpoint", "milestone": name,
                       "sequence": sequence, "uptimeNs": uptime}
                with create_connection(("127.0.0.1", server.port), timeout=2) as client:
                    client.sendall(json.dumps(row).encode() + b"\n")
                    return client.makefile("rb").readline()

            self.assertEqual(send("submit_to_home", 2, 101), b"")
            self.assertEqual(send("first_valid_login", 1, 100), b'{"ok":true}\n')
            self.assertEqual(send("first_valid_login", 1, 100), b"")
            self.assertEqual(send("submit_to_home", 2, 99), b"")
            self.assertEqual(send("submit_to_home", 2, 200), b'{"ok":true}\n')
        self.assertEqual(list(server.records), ["first_valid_login", "submit_to_home"])
        self.assertEqual(server.records["submit_to_home"]["uptime_ns"], 200)
        self.assertEqual([sample["ri_phys_footprint_bytes"]
                          for sample in tracker.stages["first_valid_login"]],
                         [1000, 2000, 3000])

    def test_missing_process_remains_unqualified_and_is_never_zero_filled(self):
        from socket import create_connection
        from scripts.ios_session_benchmark import MeerCheckpointServer

        tracker = MemoryTracker(ps_lister=lambda: "")
        with MeerCheckpointServer(tracker, "TEST", "Meer", []) as server:
            with create_connection(("127.0.0.1", server.port), timeout=2) as client:
                client.sendall(b'{"op":"checkpoint","milestone":"first_valid_login",'
                               b'"sequence":1,"uptimeNs":100}\n')
                self.assertEqual(client.makefile("rb").readline(), b"")
        self.assertFalse(server.records["first_valid_login"]["complete"])
        samples = tracker.stages["first_valid_login"]
        self.assertEqual([sample["status"] for sample in samples], ["not-running"] * 2)
        self.assertTrue(all("ri_phys_footprint_bytes" not in sample for sample in samples))

    def test_native_clocks_and_live_samples_must_match_at_all_four_milestones(self):
        from scripts.ios_session_benchmark import MEER_MILESTONES, qualify_meer_xctest

        attachment = {
            "schema": "meer-journey-boundaries", "version": 1,
            "clock": "dispatch-uptime-ns", "navigation": "visible-controls",
            "bundleId": "com.edufelip.meer.dev", "initialUrlScope": "loopback-only",
            "emailReadback": "exact", "passwordMaskEvidence": "native-bullets-and-length",
            "typedStagesOnce": True, "mutationReplays": 0,
            "checkpoints": {"requested": True, "milestones": []},
        }
        samples = {}
        for sequence, (phase, prefix) in enumerate(zip(
            MEER_MILESTONES, ("firstValidLogin", "submitToHome", "search", "homeReturn")
        ), 1):
            attachment[prefix + "StartUptimeNs"] = sequence * 100
            attachment[prefix + "EndUptimeNs"] = sequence * 100 + 50
            attachment["checkpoints"]["milestones"].append(
                {"milestone": phase, "sequence": sequence, "status": "acked"})
            samples[phase] = {"complete": True, "sequence": sequence,
                              "uptime_ns": sequence * 100 + 50}
        result = qualify_meer_xctest(attachment, samples, "com.edufelip.meer.dev")
        self.assertEqual(result["milliseconds_by_milestone"]["submit_to_home"], 0.00005)
        self.assertNotIn("login_to_home", result["milliseconds_by_milestone"])
        self.assertTrue(result["memory_complete"])
        attachment["credentialEntryStartUptimeNs"] = 175
        result = qualify_meer_xctest(attachment, samples, "com.edufelip.meer.dev")
        self.assertEqual(result["milliseconds_by_milestone"]["login_to_home"], 0.000075)
        for invalid_start in (149, 200, True, None):
            with self.subTest(invalid_start=invalid_start), self.assertRaises(BenchmarkError):
                qualify_meer_xctest(
                    dict(attachment, credentialEntryStartUptimeNs=invalid_start),
                    samples, "com.edufelip.meer.dev")
        samples["submit_to_home"]["uptime_ns"] += 1
        result = qualify_meer_xctest(attachment, samples, "com.edufelip.meer.dev")
        self.assertFalse(result["memory_complete"])
        self.assertEqual(result["missing_memory_milestones"], ["submit_to_home"])
        with self.assertRaises(BenchmarkError):
            qualify_meer_xctest(attachment, samples, "some.other.dev")

    def test_libproc_success_with_no_process_identity_is_not_a_memory_sample(self):
        from scripts.ios_session_benchmark import read_proc_rusage

        with patch("scripts.ios_session_benchmark._libproc") as libproc:
            libproc.return_value.proc_pid_rusage.return_value = 0
            with self.assertRaisesRegex(BenchmarkError, "process start identity"):
                read_proc_rusage(123)

    def test_live_checkpoint_refuses_a_replacement_process(self):
        from scripts.ios_session_benchmark import MeerCheckpointServer

        pids = {"app": 10}
        tracker = MemoryTracker(
            sampler=lambda pid: {
                "ri_phys_footprint": pid * 100, "ri_proc_start_abstime": pid * 10,
                "ri_resident_size": 0, "ri_wired_size": 0,
            },
            ps_lister=lambda: (
                f"{pids['app']} /CoreSimulator/Devices/TEST/data/Meer.app/Meer\n"
                "20 launchd_sim /CoreSimulator/Devices/TEST\n"
            ),
        )
        server = MeerCheckpointServer(tracker, "TEST", "Meer", [30])
        self.assertTrue(server.record({
            "op": "checkpoint", "milestone": "first_valid_login",
            "sequence": 1, "uptimeNs": 100,
        }))
        pids["app"] = 11
        self.assertFalse(server.record({
            "op": "checkpoint", "milestone": "submit_to_home",
            "sequence": 2, "uptimeNs": 200,
        }))

    def test_meer_result_attachment_must_come_from_the_exact_test_and_simulator(self):
        from scripts.ios_session_benchmark import read_meer_xctest_attachment

        test_id = "MeerJourneyBenchmarkTests/testPinnedJourneyThroughVisibleControls()"
        device_id = "owned-uuid"

        def export(argv, **kwargs):
            output = Path(argv[argv.index("--output-path") + 1])
            self.assertEqual(argv[argv.index("--test-id") + 1], test_id)
            (output / "record.json").write_text('{"version":1}')
            (output / "manifest.json").write_text(json.dumps([{
                "testIdentifier": test_id,
                "attachments": [{
                    "deviceId": device_id, "exportedFileName": "record.json",
                    "suggestedHumanReadableName": "meer-journey-boundaries.json",
                    "isAssociatedWithFailure": False,
                }],
            }]))

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle = root / "journey.xcresult"
            bundle.mkdir()
            with patch("scripts.ios_session_benchmark.subprocess.run", side_effect=export):
                self.assertEqual(
                    read_meer_xctest_attachment(bundle, root, "OWNED-UUID"),
                    {"version": 1},
                )
                with self.assertRaisesRegex(BenchmarkError, "identity or device"):
                    read_meer_xctest_attachment(bundle, root, "foreign-uuid")


class MeerTwoWayComparisonTests(unittest.TestCase):
    EMAIL = "synthetic-bench-user@example.invalid"
    PASSWORD = "synthetic-bench-pass-9876"

    def _write_creds(self, directory):
        path = Path(directory) / "creds.json"
        path.write_text(json.dumps({"email": self.EMAIL, "password": self.PASSWORD}))
        os.chmod(path, 0o600)
        return path

    def _meer_context(self, directory, arm, condition="cold"):
        output = Path(directory) / arm / condition
        output.mkdir(parents=True, exist_ok=True)
        creds = self._write_creds(directory)
        args = Namespace(
            app="/tmp/bench/Meer.app", bundle_id="com.edufelip.meer.dev",
            tap_identifier="home-search-button", tap_label=None,
            expected_before="home-search-button", expected_after="home-search-button",
            first_snapshot_timeout=15.0, arm_command="echo {udid}",
            xctest_artifacts=None, workload="meer",
            credentials=creds, initial_url="http://127.0.0.1:8081",
        )
        context = ObservationContext(args, UDID, condition, output, arm)
        context.simulator_scope = {
            "udid": UDID, "name": "AshnazgIosBenchmark-d38dcceaae01",
            "device_type": "iPhone-17", "runtime": "iOS-26-5", "created_by_run": True,
        }
        context.app_sha256 = "6" * 64
        context.app_sha256_basis = "hashApp"
        context.source_revisions = {"repository": "2b19400" + "0" * 33}
        return context

    def _fake_png(self, width=402, height=874, payload=b"img"):
        return struct.pack(">8sI4sII", PNG_HEAD[:8], 13, b"IHDR", width, height) + payload

    def test_credentials_url_and_secret_scrubbing_contracts(self):
        with tempfile.TemporaryDirectory() as directory:
            creds = self._write_creds(directory)
            self.assertEqual(load_mock_credentials(creds), (self.EMAIL, self.PASSWORD))
            bad = Path(directory) / "bad.json"
            bad.write_text(json.dumps({"email": "", "password": self.PASSWORD}))
            with self.assertRaises(BenchmarkError):
                load_mock_credentials(bad)

        self.assertEqual(validate_loopback_http_url("http://127.0.0.1:8081"),
                         "http://127.0.0.1:8081")
        with self.assertRaises(BenchmarkError):
            validate_loopback_http_url("https://example.com")

        dirty = {"error": f"failed typing {self.EMAIL} / {self.PASSWORD}",
                 "nested": [{"msg": f"pwd={self.PASSWORD}"}]}
        cleaned = scrub_secrets(dirty, (self.EMAIL, self.PASSWORD))
        self.assertNotIn(self.EMAIL, json.dumps(cleaned))
        self.assertNotIn(self.PASSWORD, json.dumps(cleaned))
        assert_no_secrets(cleaned, (self.EMAIL, self.PASSWORD))
        with self.assertRaises(BenchmarkError):
            assert_no_secrets(dirty, (self.EMAIL, self.PASSWORD))

    def test_usable_meer_login_snapshot_requires_both_fields_without_secure_value(self):
        good = {"nodes": [
            {"testId": "login-email-input", "role": "textfield",
             "enabled": True, "rect": [10, 100, 300, 140]},
            {"testId": "login-password-input", "role": "securetextfield",
             "enabled": True, "rect": [10, 160, 300, 200]},
        ]}
        self.assertTrue(usable_meer_login_snapshot(good)["usable"])

        leaked = {"nodes": [
            good["nodes"][0],
            {**good["nodes"][1], "value": "bullets"},
        ]}
        res = usable_meer_login_snapshot(leaked)
        self.assertFalse(res["usable"])
        self.assertIn("serialized a value", res["reason"])

    def test_prepare_cold_simulator_grants_location_only_for_meer(self):
        calls = []
        args_meer = Namespace(
            simulator_name_prefix="AshnazgIosBenchmark", device_type="iPhone-17",
            runtime="iOS-26-5", boot_timeout=10.0, app="/tmp/Meer.app",
            bundle_id="com.edufelip.meer.dev", workload="meer",
        )
        args_fixture = Namespace(
            simulator_name_prefix="AshnazgIosBenchmark", device_type="iPhone-17",
            runtime="iOS-26-5", boot_timeout=10.0, app="/tmp/Fixture.app",
            bundle_id="dev.ashnazg.ios.fixture", workload="fixture",
        )
        with (
            patch("scripts.ios_session_benchmark.create_simulator", return_value=UDID),
            patch("scripts.ios_session_benchmark.wait_for_boot"),
            patch("scripts.ios_session_benchmark.simctl",
                  side_effect=lambda *a, **k: calls.append(a) or ""),
        ):
            prepare_cold_simulator(args_meer, {})
            self.assertIn(("privacy", UDID, "grant", "location", "com.edufelip.meer.dev"), calls)
            calls.clear()
            prepare_cold_simulator(args_fixture, {})
            self.assertFalse(any(c[0] == "privacy" for c in calls))

    def test_ashnazg_meer_journey_populates_milestones_memory_and_scrubs_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self._meer_context(directory, "ashnazg-mcp")
            tracker = MemoryTracker(
                sampler=lambda pid: {"ri_phys_footprint": pid * 1000,
                                     "ri_proc_start_abstime": pid * 10,
                                     "ri_resident_size": 0, "ri_wired_size": 0},
                ps_lister=lambda: (
                    f"101 /CoreSimulator/Devices/{UDID}/data/Meer.app/Meer\n"
                    f"102 launchd_sim /CoreSimulator/Devices/{UDID}\n"
                ),
            )
            context.memory = tracker
            context.register_helpers(103)
            arm = AshnazgMcpArm(context)
            email_node = {"testId": "login-email-input", "role": "textfield",
                          "enabled": True, "rect": [10, 100, 300, 140]}
            pw_node = {"testId": "login-password-input", "role": "securetextfield",
                       "enabled": True, "rect": [10, 160, 300, 200]}
            submit_node = {"testId": "login-submit-button", "role": "button",
                           "enabled": True, "rect": [10, 220, 300, 260]}
            home_node = {"testId": "home-search-button", "role": "button",
                         "enabled": True, "rect": [10, 80, 300, 120]}
            notif_btn = {"testId": "", "role": "button", "text": "Don’t Allow",
                         "enabled": True, "rect": [50, 400, 180, 440]}
            search_node = {"testId": "search-input", "role": "textfield",
                           "enabled": True, "rect": [10, 80, 300, 120]}
            back_node = {"testId": "search-back-button", "role": "button",
                         "enabled": True, "rect": [10, 40, 60, 75]}

            seq = [0]

            screen = ["login"]
            prompt_pending = [True]

            def next_id():
                seq[0] += 1
                return seq[0]

            def fake_call(name, arguments, timeout=240.0):
                if name == "ui_snapshot":
                    if screen[0] == "login":
                        return {"snapshotId": next_id(), "nodes": [email_node, pw_node, submit_node]}
                    if screen[0] == "home":
                        nodes = [home_node, notif_btn] if prompt_pending[0] else [home_node]
                        return {"snapshotId": next_id(), "nodes": nodes}
                    if screen[0] == "search":
                        return {"snapshotId": next_id(), "nodes": [search_node, back_node]}
                if name == "ui_act":
                    action = arguments["action"]
                    target = arguments.get("target") or {}
                    if action == "type" and target.get("testId") == "login-email-input":
                        return {"settled": True, "snapshot": {"snapshotId": next_id(), "nodes": [
                            {**email_node, "value": self.EMAIL}, pw_node]}}
                    if action == "scroll_to":
                        return {"settled": True, "snapshot": {"snapshotId": next_id(), "nodes": [
                            email_node, pw_node, submit_node]}}
                    if action == "type" and target.get("testId") == "login-password-input":
                        # Email stays visible in its field while password typing is checked.
                        return {"settled": True, "snapshot": {"snapshotId": next_id(), "nodes": [
                            {**email_node, "value": self.EMAIL}, pw_node]}}
                    if action == "tap" and target.get("testId") == "login-submit-button":
                        screen[0] = "home"
                        return {"settled": True, "snapshot": {"snapshotId": next_id(), "nodes": [
                            home_node, notif_btn]}}
                    if action == "tap" and target.get("text") == "Don’t Allow":
                        prompt_pending[0] = False
                        return {"settled": True, "snapshot": {"snapshotId": next_id(), "nodes": [home_node]}}
                    if action == "tap" and target.get("testId") == "home-search-button":
                        screen[0] = "search"
                        return {"settled": True, "snapshot": {"snapshotId": next_id(), "nodes": [search_node, back_node]}}
                    if action == "tap" and target.get("testId") == "search-back-button":
                        screen[0] = "home"
                        return {"settled": True, "snapshot": {"snapshotId": next_id(), "nodes": [home_node]}}
                raise AssertionError(f"unexpected call {name} {arguments}")

            pngs = iter([self._fake_png(payload=b"home1"), self._fake_png(payload=b"home2")])

            def fake_call_result(name, arguments, timeout=240.0):
                self.assertEqual(name, "ui_screenshot")
                return {"content": [
                    {"type": "image", "data": base64.b64encode(next(pngs)).decode()},
                    {"type": "text", "text": json.dumps({"snapshotId": next_id()})},
                ]}

            # Drive monotonic clock strictly forward on each check without real sleeps.
            now = [100.0]

            def tick():
                now[0] += 1.0
                return now[0]

            with (
                patch.object(arm, "_call", side_effect=fake_call),
                patch.object(arm, "_call_result", side_effect=fake_call_result),
                patch("scripts.ios_session_benchmark.time.sleep"),
                patch("scripts.ios_session_benchmark.time.monotonic", side_effect=tick),
            ):
                first, err = arm.first_usable_snapshot()
                self.assertIsNone(err)
                tap_rec, tap_err = arm.tap_to_fresh_frame(first)
                self.assertIsNone(tap_err)
            self.assertEqual(tap_rec["kind"], "meer")
            self.assertIn("submit_to_home", context.timings["meer_milestones"])
            self.assertIn("home_to_search", context.timings["meer_milestones"])
            self.assertIn("search_to_home", context.timings["meer_milestones"])
            self.assertEqual(context.receipt_extras["meer"]["system_prompts_dismissed"], ["Don’t Allow"])
            self.assertEqual([s["status"] for s in tracker.stages["submit_to_home"]], ["sampled"] * 3)
            self.assertEqual([s["status"] for s in tracker.stages["home_to_search"]], ["sampled"] * 3)
            receipt = build_observation_receipt(context, {
                "simulator_shutdown": True, "owned_helpers_gone": True}, "passed", None, 1.0)
            serialized = json.dumps(receipt)
            self.assertNotIn(self.EMAIL, serialized)
            self.assertNotIn(self.PASSWORD, serialized)

    def test_direct_xctest_meer_wires_env_cleans_xcresult_and_qualifies_memory(self):
        from socket import create_connection

        with tempfile.TemporaryDirectory() as directory:
            context = self._meer_context(directory, "direct-xctest")
            context.arm_command = [
                "xcodebuild", "test-without-building", "-xctestrun", "/tmp/Probe.xctestrun",
                "-destination", "id={udid}", "-resultBundlePath", "{xcresult}",
            ]
            context.memory = MemoryTracker(
                sampler=lambda pid: {"ri_phys_footprint": pid * 100,
                                     "ri_proc_start_abstime": pid * 10,
                                     "ri_resident_size": 0, "ri_wired_size": 0},
                ps_lister=lambda: (
                    f"10 /CoreSimulator/Devices/{UDID}/data/Meer.app/Meer\n"
                    f"20 launchd_sim /CoreSimulator/Devices/{UDID}\n"
                ),
            )
            arm = DirectXctestArm(context)
            captured_env = {}
            testcase = self

            class FakePopen:
                def __init__(self, cmd, stdout=None, stderr=None, env=None):
                    self.pid = 30
                    self.stdout = stdout
                    self.ran = False
                    captured_env.update(env or {})
                    result_bundle = Path(cmd[cmd.index("-resultBundlePath") + 1])
                    result_bundle.mkdir(parents=True)

                def poll(self):
                    if not self.ran:
                        self.ran = True
                        port = int(captured_env["TEST_RUNNER_ASHNAZG_MEER_CHECKPOINT_PORT"])
                        for idx, name in enumerate(MEER_MILESTONES, 1):
                            row = {"op": "checkpoint", "milestone": name,
                                   "sequence": idx, "uptimeNs": idx * 1000 + 500}
                            with create_connection(("127.0.0.1", port), timeout=2) as sock:
                                sock.sendall(json.dumps(row).encode() + b"\n")
                                testcase.assertEqual(sock.makefile("rb").readline(), b'{"ok":true}\n')
                        (context.output / "xctest.log").write_text(
                            "Test Case 'MeerJourneyBenchmarkTests/testPinnedJourneyThroughVisibleControls()' passed (5.000 seconds)\n"
                        )
                    return 0

            attachment = {
                "schema": "meer-journey-boundaries", "version": 1,
                "clock": "dispatch-uptime-ns", "navigation": "visible-controls",
                "bundleId": "com.edufelip.meer.dev", "initialUrlScope": "loopback-only",
                "emailReadback": "exact", "passwordMaskEvidence": "native-bullets-and-length",
                "typedStagesOnce": True, "mutationReplays": 0,
                "firstValidLoginStartUptimeNs": 1000, "firstValidLoginEndUptimeNs": 1500,
                "credentialEntryStartUptimeNs": 2000,
                "submitToHomeStartUptimeNs": 2200, "submitToHomeEndUptimeNs": 2500,
                "searchStartUptimeNs": 3000, "searchEndUptimeNs": 3500,
                "homeReturnStartUptimeNs": 4000, "homeReturnEndUptimeNs": 4500,
                "checkpoints": {
                    "requested": True,
                    "milestones": [{"milestone": m, "sequence": i, "status": "acked"}
                                   for i, m in enumerate(MEER_MILESTONES, 1)],
                },
            }

            with (
                patch("scripts.ios_session_benchmark.subprocess.Popen", FakePopen),
                patch("scripts.ios_session_benchmark.read_meer_xctest_attachment",
                      return_value=attachment),
            ):
                arm.start()
                self.assertEqual(captured_env["TEST_RUNNER_ASHNAZG_MEER_JOURNEY"], "1")
                self.assertEqual(captured_env["TEST_RUNNER_ASHNAZG_MEER_MOCK_EMAIL"], self.EMAIL)
                first, err = arm.first_usable_snapshot()
                self.assertIsNone(err)
                tap, tap_err = arm.tap_to_fresh_frame(first)
                self.assertIsNone(tap_err)

            # Raw .xcresult is deleted while sanitized JSON is retained cleanly.
            self.assertFalse((context.output / "trial.xcresult").exists())
            self.assertTrue((context.output / "meer-journey-boundaries.json").is_file())
            self.assertTrue(tap["memory_complete"])
            self.assertEqual(first["native_elapsed_ns"], 500)
            self.assertEqual(tap["native_elapsed_ns"], 500)
            self.assertEqual(list(context.timings["meer_milestones"]), [
                "first_valid_login", "submit_to_home", "home_to_search",
                "search_to_home", "login_to_home",
            ])
            for m in MEER_MILESTONES:
                self.assertEqual([s["status"] for s in context.memory.stages[m]], ["sampled"] * 3)
            arm.shutdown()

    def test_run_observation_direct_xctest_meer_produces_valid_receipt_with_live_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self._meer_context(directory, "direct-xctest")

            def fake_start(self_arm):
                context.timings["arm_start"] = {"status": "passed", "elapsed_ns": 100}
                context.register_helpers(30)

            def fake_first(self_arm):
                for m in MEER_MILESTONES:
                    context.memory.stages[m] = [
                        {"role": "app", "pid": 10, "status": "sampled",
                         "ri_phys_footprint_bytes": 1000, "observed_start_abstime": 10},
                        {"role": "simulator", "pid": 20, "status": "sampled",
                         "ri_phys_footprint_bytes": 2000, "observed_start_abstime": 20},
                        {"role": "helper", "pid": 30, "status": "sampled",
                         "ri_phys_footprint_bytes": 3000, "observed_start_abstime": 30},
                    ]
                context.timings["meer_milestones"] = {"first_valid_login": {"elapsed_ns": 500, "ms": 0.001}}
                return {"durations": {"t": {"verdict": "passed", "seconds": 1.0}},
                        "native_elapsed_ns": 500,
                        "meer_qualified": {"memory_complete": True, "missing_memory_milestones": []}}, None

            def fake_tap(self_arm, first_rec):
                return {"kind": "meer", "native_elapsed_ns": 400}, None

            sample = [{"role": "app", "pid": 10, "status": "sampled",
                       "ri_phys_footprint_bytes": 1000, "observed_start_abstime": 10}]
            with (
                patch.object(DirectXctestArm, "start", fake_start),
                patch.object(DirectXctestArm, "first_usable_snapshot", fake_first),
                patch.object(DirectXctestArm, "tap_to_fresh_frame", fake_tap),
                patch.object(DirectXctestArm, "shutdown", return_value={"mode": "runner-exit", "exit_code": 0}),
                patch("scripts.ios_session_benchmark.device_state", side_effect=["Booted", "Shutdown"]),
                patch("scripts.ios_session_benchmark.simctl"),
                patch("scripts.ios_session_benchmark._wait_helpers_gone", return_value=True),
                patch.object(MemoryTracker, "sample_stage",
                             side_effect=lambda s, *a, **k: context.memory.stages.__setitem__(s, sample)),
            ):
                receipt = run_observation("direct-xctest", context)
            self.assertEqual(receipt["status"], "passed")
            self.assertEqual(validate_receipt(receipt), [])
            self.assertEqual(receipt["scope"]["workload_kind"], "meer")
            self.assertNotIn("missing_memory_stages", receipt["scope"])
            self.assertEqual(len(receipt["memory"]["stages"]["first_snapshot_boundary"]), 3)
            self.assertEqual(len(receipt["memory"]["stages"]["tap_boundary"]), 3)

    def test_main_validates_meer_cli_requirements(self):
        from scripts.ios_session_benchmark import main as bench_main

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("scripts.ios_session_benchmark.require_macos_host"),
        ):
            root = Path(directory)
            app = root / "Meer.app"
            app.mkdir()
            (app / "Meer").write_bytes(b"bin")
            creds = self._write_creds(root)
            manifest = root / "tmpl.json"
            manifest.write_text(json.dumps({"readySelector": {"testId": "login-email-input"}, "deepLinks": []}))

            base = [
                "--arm", "ashnazg-mcp", "--workload", "meer",
                "--app", str(app), "--bundle-id", "com.edufelip.meer.dev",
                "--device-type", "iPhone-17", "--runtime", "iOS-26-5",
                "--arm-command", "ashnazg mcp --ios-app {manifest}",
                "--ashnazg-manifest-template", str(manifest),
                "--output", str(root / "out"),
            ]
            with self.assertRaises(SystemExit):
                bench_main(base + ["--initial-url", "http://127.0.0.1:8081"])  # missing --credentials
            with self.assertRaises(SystemExit):
                bench_main(base + ["--credentials", str(creds), "--initial-url", "https://example.com"])  # non-loopback
            with (
                patch("scripts.ios_session_benchmark.subprocess.run",
                      return_value=subprocess.CompletedProcess([], 0, stdout="mock\n")),
                patch("scripts.ios_session_benchmark.drive_conditions",
                      return_value=({"requested": False}, True)) as driver,
            ):
                rc = bench_main(base + ["--credentials", str(creds), "--initial-url", "http://127.0.0.1:8081"])
                self.assertEqual(rc, 0)
                parsed_args = driver.call_args.args[0]
                self.assertEqual(parsed_args.tap_identifier, "home-search-button")
                self.assertEqual(parsed_args.workload, "meer")

    def test_prepare_cold_simulator_restores_exec_modes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src_app = root / "src" / "Meer.app"
            (src_app / "Frameworks").mkdir(parents=True)
            src_bin = src_app / "Frameworks" / "Hermes.dylib"
            src_bin.write_bytes(b"dylib")
            src_bin.chmod(0o755)
            src_info = src_app / "Info.plist"
            src_info.write_bytes(b"plist")
            src_info.chmod(0o644)

            installed_app = root / "installed" / "Meer.app"
            (installed_app / "Frameworks").mkdir(parents=True)
            inst_bin = installed_app / "Frameworks" / "Hermes.dylib"
            inst_bin.write_bytes(b"dylib")
            inst_bin.chmod(0o644)
            inst_info = installed_app / "Info.plist"
            inst_info.write_bytes(b"plist")
            inst_info.chmod(0o644)

            args = Namespace(
                simulator_name_prefix="AshnazgIosBenchmark", device_type="iPhone-17",
                runtime="iOS-26-5", boot_timeout=10.0, app=str(src_app),
                bundle_id="com.edufelip.meer.dev", workload="meer",
            )

            def fake_simctl(*cmd_args, **kwargs):
                if cmd_args[0] == "get_app_container":
                    return f"{installed_app}\n"
                return ""

            with (
                patch("scripts.ios_session_benchmark.create_simulator", return_value=UDID),
                patch("scripts.ios_session_benchmark.wait_for_boot"),
                patch("scripts.ios_session_benchmark.simctl", side_effect=fake_simctl),
            ):
                prepare_cold_simulator(args, {})
            self.assertEqual(stat.S_IMODE(inst_bin.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE(inst_info.stat().st_mode), 0o644)

    def test_export_attachments_accepts_xcode27_uuid_and_rejects_bad_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            result_bundle = output_dir / "trial.xcresult"
            (result_bundle / "Data").mkdir(parents=True, exist_ok=True)
            test_id = "SimulatorProbeTests/testObserveTapObserveAndCapture()"

            def export(command, **kwargs):
                output = Path(command[command.index("--output-path") + 1])
                (output / "f0").write_bytes(b'{"a":1}')
                (output / "f1").write_bytes(self._fake_png(payload=b"before"))
                (output / "f2").write_bytes(self._fake_png(payload=b"after"))
                uuid_sfx = "12345678-1234-1234-1234-1234567890AB"
                metadata = [
                    {"suggestedHumanReadableName": f"xctest-boundaries_0_{uuid_sfx}.json",
                     "exportedFileName": "f0", "deviceId": UDID,
                     "isAssociatedWithFailure": False},
                    {"suggestedHumanReadableName": f"before-increment_0_{uuid_sfx}.png",
                     "exportedFileName": "f1", "deviceId": UDID,
                     "isAssociatedWithFailure": False},
                    {"suggestedHumanReadableName": f"after-increment_0_{uuid_sfx}.png",
                     "exportedFileName": "f2", "deviceId": UDID,
                     "isAssociatedWithFailure": False},
                ]
                (output / "manifest.json").write_text(json.dumps([{
                    "testIdentifier": test_id,
                    "attachments": metadata,
                }]))
                return subprocess.CompletedProcess(command, 0)

            with patch("scripts.ios_session_benchmark.subprocess.run", side_effect=export):
                extracted = export_xctest_attachments(
                    result_bundle, output_dir, UDID, test_id,
                    ["xctest-boundaries", "before-increment", "after-increment"],
                )
            self.assertEqual(set(extracted.keys()), {"xctest-boundaries", "before-increment", "after-increment"})

            def bad_export(command, **kwargs):
                output = Path(command[command.index("--output-path") + 1])
                (output / "f0").write_bytes(b'{"a":1}')
                (output / "manifest.json").write_text(json.dumps([{
                    "testIdentifier": test_id,
                    "attachments": [{
                        "suggestedHumanReadableName": "xctest-boundaries-extra_0.json",
                        "exportedFileName": "f0", "deviceId": UDID,
                        "isAssociatedWithFailure": False,
                    }],
                }]))
                return subprocess.CompletedProcess(command, 0)

            with patch("scripts.ios_session_benchmark.subprocess.run", side_effect=bad_export):
                with self.assertRaisesRegex(BenchmarkError, "native boundary attachments"):
                    export_xctest_attachments(result_bundle, output_dir, UDID, test_id, ["xctest-boundaries"])

    def test_ashnazg_arm_allows_over_twenty_mcp_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            context = self._meer_context(directory, "ashnazg-mcp")
            arm = AshnazgMcpArm(context)
            ids = [next(arm.ids) for _ in range(32)]
            self.assertEqual(ids, list(range(32)))

    def test_direct_xctest_meer_with_png_parity_validates_in_memory(self):
        from socket import create_connection

        with tempfile.TemporaryDirectory() as directory:
            context = self._meer_context(directory, "direct-xctest")
            context.arm_command = [
                "xcodebuild", "test-without-building", "-xctestrun", "/tmp/Probe.xctestrun",
                "-destination", "id={udid}", "-resultBundlePath", "{xcresult}",
            ]
            context.memory = MemoryTracker(
                sampler=lambda pid: {"ri_phys_footprint": pid * 1000,
                                     "ri_proc_start_abstime": pid * 10,
                                     "ri_resident_size": 0, "ri_wired_size": 0},
                ps_lister=lambda: (
                    f"101 /CoreSimulator/Devices/{UDID}/data/Meer.app/Meer\n"
                    f"102 launchd_sim /CoreSimulator/Devices/{UDID}\n"
                ),
            )
            arm = DirectXctestArm(context)
            testcase = self

            class FakePopen:
                def __init__(self, cmd, **kwargs):
                    self.pid = 103
                    self.ran = False
                    result_bundle = Path(cmd[cmd.index("-resultBundlePath") + 1])
                    result_bundle.mkdir(parents=True, exist_ok=True)
                    self.env = kwargs["env"]

                def poll(self):
                    if not self.ran:
                        self.ran = True
                        port = int(self.env["TEST_RUNNER_ASHNAZG_MEER_CHECKPOINT_PORT"])
                        for idx, (name, uptime) in enumerate(
                            zip(MEER_MILESTONES, (1500, 2500, 3500, 4500)), 1
                        ):
                            row = {"op": "checkpoint", "milestone": name,
                                   "sequence": idx, "uptimeNs": uptime}
                            with create_connection(("127.0.0.1", port), timeout=2) as sock:
                                sock.sendall(json.dumps(row).encode() + b"\n")
                                testcase.assertEqual(sock.makefile("rb").readline(), b'{"ok":true}\n')
                        (context.output / "xctest.log").write_text(
                            "Test Case '-[FixtureUITests.MeerJourneyBenchmarkTests testPinnedJourneyThroughVisibleControls]' passed (6.000 seconds)\n"
                        )
                    return 0

            attachment = {
                "schema": "meer-journey-boundaries", "version": 1,
                "clock": "dispatch-uptime-ns", "navigation": "visible-controls",
                "bundleId": "com.edufelip.meer.dev", "initialUrlScope": "loopback-only",
                "emailReadback": "exact", "passwordMaskEvidence": "native-bullets-and-length",
                "typedStagesOnce": True, "mutationReplays": 0,
                "firstValidLoginStartUptimeNs": 1000, "firstValidLoginEndUptimeNs": 1500,
                "credentialEntryStartUptimeNs": 1800,
                "submitToHomeStartUptimeNs": 2000, "submitToHomeEndUptimeNs": 2500,
                "homeScreenshotStartUptimeNs": 2500, "homeScreenshotEndUptimeNs": 2700,
                "searchStartUptimeNs": 3000, "searchEndUptimeNs": 3500,
                "homeReturnStartUptimeNs": 4000, "homeReturnEndUptimeNs": 4500,
                "finalHomeScreenshotStartUptimeNs": 4500, "finalHomeScreenshotEndUptimeNs": 4750,
                "homeCapture": "home-screenshot",
                "finalHomeCapture": "final-home-screenshot",
                "captureExtent": "app",
                "capturePixels": [1206, 2622],
                "checkpoints": {
                    "requested": True,
                    "milestones": [{"milestone": m, "sequence": i, "status": "acked"}
                                   for i, m in enumerate(MEER_MILESTONES, 1)],
                },
            }
            home_png = self._fake_png(width=1206, height=2622, payload=b"home")
            final_png = self._fake_png(width=1206, height=2622, payload=b"final")

            with (
                patch("scripts.ios_session_benchmark.subprocess.Popen", FakePopen),
                patch("scripts.ios_session_benchmark.read_meer_xctest_attachment",
                      return_value=attachment),
                patch("scripts.ios_session_benchmark.read_meer_xctest_captures",
                      return_value={"home-screenshot": home_png, "final-home-screenshot": final_png}),
            ):
                arm.start()
                first, err = arm.first_usable_snapshot()
                self.assertIsNone(err)
                tap, tap_err = arm.tap_to_fresh_frame(first)
                self.assertIsNone(tap_err)

            self.assertEqual(tap["native_elapsed_ns"], 750)
            self.assertEqual(tap["search_to_home_ns"], 500)
            self.assertEqual(tap["capture_pixels"], [1206, 2622])
            self.assertEqual(tap["home_screenshot"]["published"], False)
            self.assertEqual(tap["final_home_screenshot"]["dimensions"], [1206, 2622])
            self.assertEqual(list(context.output.glob("*.png")), [])
            self.assertEqual(context.timings["meer_milestones"]["search_to_home_with_screenshot"]["elapsed_ns"], 750)
            self.assertEqual(context.timings["meer_milestones"]["final_home_screenshot"]["elapsed_ns"], 250)
            self.assertEqual(context.timings["meer_milestones"]["home_screenshot"]["elapsed_ns"], 200)
            arm.shutdown()

    def test_qualify_meer_xctest_rejects_bad_capture_clocks_and_validate_png(self):
        samples = {
            m: {"complete": True, "sequence": i, "uptime_ns": u}
            for i, (m, u) in enumerate(zip(MEER_MILESTONES, (1500, 2500, 3500, 4500)), 1)
        }
        bad_attach = {
            "schema": "meer-journey-boundaries", "version": 1,
            "clock": "dispatch-uptime-ns", "navigation": "visible-controls",
            "bundleId": "com.edufelip.meer.dev", "initialUrlScope": "loopback-only",
            "emailReadback": "exact", "passwordMaskEvidence": "native-bullets-and-length",
            "typedStagesOnce": True, "mutationReplays": 0,
            "firstValidLoginStartUptimeNs": 1000, "firstValidLoginEndUptimeNs": 1500,
            "submitToHomeStartUptimeNs": 2000, "submitToHomeEndUptimeNs": 2500,
            "homeScreenshotStartUptimeNs": 2500, "homeScreenshotEndUptimeNs": 3100,  # > searchStart!
            "searchStartUptimeNs": 3000, "searchEndUptimeNs": 3500,
            "homeReturnStartUptimeNs": 4000, "homeReturnEndUptimeNs": 4500,
            "finalHomeScreenshotStartUptimeNs": 4500, "finalHomeScreenshotEndUptimeNs": 4700,
            "homeCapture": "home-screenshot", "finalHomeCapture": "final-home-screenshot",
            "captureExtent": "app", "capturePixels": [402, 874],
            "checkpoints": {
                "requested": True,
                "milestones": [{"milestone": m, "sequence": i, "status": "acked"}
                               for i, m in enumerate(MEER_MILESTONES, 1)],
            },
        }
        with self.assertRaisesRegex(BenchmarkError, "capture clocks"):
            qualify_meer_xctest(bad_attach, samples, "com.edufelip.meer.dev")
        with self.assertRaisesRegex(BenchmarkError, "not a PNG"):
            validate_meer_png_bytes(b"bad", [402, 874], "home-screenshot")
        with self.assertRaisesRegex(BenchmarkError, "extent"):
            validate_meer_png_bytes(self._fake_png(width=100, height=200), [402, 874], "home-screenshot")

    def test_aggregate_stats_and_multi_trial_summary(self):
        stats = aggregate_numeric_stats([10.0, 20.0, 30.0])
        self.assertEqual(stats["count"], 3)
        self.assertEqual(stats["min"], 10.0)
        self.assertEqual(stats["median"], 20.0)
        self.assertEqual(stats["mean"], 20.0)
        self.assertEqual(stats["max"], 30.0)
        self.assertEqual(stats["p95"], 29.0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out_dir = root / "out"
            out_dir.mkdir()
            args = Namespace(
                arm="ashnazg-mcp", workload="meer", trials=3, output=out_dir,
            )
            manifest = {
                "schema_version": 1, "arm": "ashnazg-mcp", "workload": "meer",
                "conditions": ["cold", "warm"],
            }
            per_trial_elapsed = {"cold": [10.0, 12.0, 14.0], "warm": [8.0, 9.0, 999.0]}
            seen_trial = [0]

            def fake_drive_conditions(t_args, conditions, udid_slot):
                seen_trial[0] += 1
                idx = seen_trial[0] - 1
                for cond in conditions:
                    cond_dir = t_args.output / cond
                    cond_dir.mkdir(parents=True, exist_ok=True)
                    failed = (cond == "warm" and idx == 2)
                    receipt = {
                        "schema_version": 1,
                        "status": "failed" if failed else "passed",
                        "elapsed_seconds": per_trial_elapsed[cond][idx],
                        "timings": {
                            "arm_start": {"elapsed_ns": (1000 + idx * 100) * 1_000_000},
                            "first_usable_snapshot": {"success_timing_ns": (300 + idx * 10) * 1_000_000},
                            "tap_to_fresh_frame": {"success_timing_ns": (2500 + idx * 50) * 1_000_000},
                            "meer_milestones": {
                                "home_to_search": {"ms": 2000.0 + idx * 100.0},
                            },
                        },
                        "shutdown": {"elapsed_ns": 5000 * 1_000_000},
                        "memory": {
                            "stages": {
                                "search_to_home": [
                                    {"role": "app", "status": "sampled", "ri_phys_footprint_bytes": 100 + idx},
                                    {"role": "simulator", "status": "sampled", "ri_phys_footprint_bytes": 50 + idx},
                                ]
                            }
                        },
                    }
                    (cond_dir / "receipt.json").write_text(json.dumps(receipt))
                return {"requested": True, "verified_absent": True}, (idx != 2)

            cleanup, ok, cleanup_ok, summary = drive_trials(
                args, manifest, drive_conditions_fn=fake_drive_conditions, secrets=(self.EMAIL, self.PASSWORD),
            )
            self.assertFalse(ok)
            self.assertTrue(cleanup_ok)
            self.assertTrue((out_dir / "summary.json").is_file())
            self.assertEqual(summary["trials_requested"], 3)
            self.assertEqual(summary["conditions"]["cold"]["trials_passed"], 3)
            self.assertEqual(summary["conditions"]["cold"]["elapsed_seconds"]["median"], 12.0)
            self.assertEqual(summary["conditions"]["cold"]["meer_milestones_ms"]["home_to_search"]["median"], 2100.0)
            self.assertEqual(summary["conditions"]["cold"]["peak_footprint_bytes"]["app"]["max"], 102.0)
            self.assertEqual(summary["conditions"]["warm"]["trials_passed"], 2)
            self.assertEqual(summary["conditions"]["warm"]["trials_failed"], 1)
            self.assertEqual(summary["conditions"]["warm"]["elapsed_seconds"]["max"], 9.0)

    def test_main_rejects_zero_trials_and_runs_multi_trial(self):
        from scripts.ios_session_benchmark import main as bench_main

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("scripts.ios_session_benchmark.require_macos_host"),
        ):
            root = Path(directory)
            app = root / "Meer.app"
            app.mkdir()
            (app / "Meer").write_bytes(b"bin")
            creds = self._write_creds(root)
            manifest = root / "tmpl.json"
            manifest.write_text(json.dumps({"readySelector": {"testId": "login-email-input"}, "deepLinks": []}))
            base = [
                "--arm", "ashnazg-mcp", "--workload", "meer", "--trials", "0",
                "--app", str(app), "--bundle-id", "com.edufelip.meer.dev",
                "--device-type", "iPhone-17", "--runtime", "iOS-26-5",
                "--credentials", str(creds), "--initial-url", "http://127.0.0.1:8095",
                "--arm-command", "ashnazg mcp --ios-app {manifest}",
                "--ashnazg-manifest-template", str(manifest),
                "--output", str(root / "out"),
            ]
            with self.assertRaises(SystemExit):
                bench_main(base)
