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
