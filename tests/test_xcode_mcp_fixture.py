import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts.xcode_mcp_fixture import observe_fixture


class XcodeMcpFixtureTest(unittest.TestCase):
    def test_capture_preserves_fixture_state_and_ends_owned_session(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            png = output / "server.png"
            png.write_bytes(b"\x89PNG\r\n\x1a\nfixture pixels")
            hierarchy = output / "server.txt"
            hierarchy.write_text("fixture-count: Count: 0")
            call = Mock(side_effect=[
                {"deviceUUID": "AB-CD", "deviceIsSimulator": True, "interactionSessionKey": "private"},
                {"screenshotPath": str(png), "hierarchyPath": str(hierarchy), "applicationState": "Running"},
                {"userMessage": "closed"},
            ])
            receipt = observe_fixture(call, "ab-cd", output)
            self.assertEqual(receipt["status"], "captured")
            self.assertEqual(receipt["application_state"], "Running")
            self.assertEqual((output / "before.png").read_bytes(), png.read_bytes())
            self.assertEqual((output / "before-hierarchy.txt").read_text(), hierarchy.read_text())
            call.assert_called_with("DeviceInteractionEndSession", {"interactionSessionKey": "private"})
            self.assertNotIn("private", repr(receipt))

    def test_never_captures_a_different_device_and_ends_the_session(self):
        call = Mock(side_effect=[
            {"deviceUUID": "foreign", "deviceIsSimulator": True, "interactionSessionKey": "private"},
            {"userMessage": "closed"},
        ])
        receipt = observe_fixture(call, "owned", Path("."))
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["end_session"], "passed")
        self.assertEqual(call.call_count, 2)
        call.assert_called_with("DeviceInteractionEndSession", {"interactionSessionKey": "private"})

    def test_resolves_latest_unique_target_and_prefers_accessibility_identifier(self):
        from scripts.xcode_mcp_fixture import target_point

        hierarchy = '''
Button, label: "Increment", identifier: "fixture-increment", hitPoint: {100.5, 202}
Button, label: "Increment", identifier: "other-increment", hitPoint: {50, 50}
'''
        self.assertEqual(target_point(hierarchy, "fixture-increment", "Increment"), ("100.5 202", "identifier"))
        with self.assertRaisesRegex(RuntimeError, "unique"):
            target_point(hierarchy, "unavailable", "Increment")

    def test_records_label_probe_without_claiming_id_support(self):
        from scripts.xcode_mcp_fixture import target_point

        hierarchy = 'UIButton "Increment" {{110, 205}, {30, 20}}, center: {125.0, 215.0}'
        self.assertEqual(target_point(hierarchy, "fixture-increment", "Increment"), ("125.0 215.0", "label"))
        with self.assertRaisesRegex(RuntimeError, "point"):
            target_point('Button "Increment"', "fixture-increment", "Increment")

    def test_exercises_a_single_observed_tap_then_types(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)

            def capture(name, hierarchy):
                png = output / f"{name}.png"
                png.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
                tree = output / f"{name}.txt"
                tree.write_text(hierarchy)
                return {"screenshotPath": str(png), "hierarchyPath": str(tree)}

            call = Mock(side_effect=[
                {"deviceUUID": "owned", "deviceIsSimulator": True, "interactionSessionKey": "private"},
                capture("a", 'StaticText "Count: 0"\nButton "Increment", center: {120, 30}'),
                capture("b", 'StaticText "Count: 1"\nTextField "Probe text", center: {100, 80}'),
                capture("c", 'TextField "Probe text", value: "AshnazgAppleProbe"'),
                {"userMessage": "closed"},
            ])
            receipt = observe_fixture(call, "owned", output, exercise=True)
            self.assertEqual(receipt["status"], "exercised")
            self.assertEqual(receipt["counter_transition"], [0, 1])
            self.assertTrue(receipt["typing_observed"])
            self.assertEqual(receipt["counter_selector_basis"], "label")
            actions = [
                invocation.args[1]["interactionCommand"]
                for invocation in call.call_args_list
                if "interactionCommand" in invocation.args[1]
            ]
            self.assertEqual(actions, ["t 120 30", "t 100 80 sender keyboard kbd AshnazgAppleProbe"])
            self.assertNotIn("private", repr(receipt))
