import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.integration import verify_mcp


class McpIntegrationTests(unittest.TestCase):
    def test_mcp_installs_use_fixture_apk_supplied_by_root(self):
        harness = Mock(token="ci-test-token", serial="127.0.0.1:15555")
        process = Mock()
        process.poll.return_value = None
        requests = []

        def respond(_process, message, response_id=None, *, allow_error=False):
            if response_id is None:
                return None
            requests.append(message)
            params = message.get("params", {})
            if message["id"] == 1:
                result = {"protocolVersion": "2025-03-26"}
            elif message["id"] == 2:
                result = {"tools": [{"name": name} for name in
                           ("ui_snapshot", "ui_act", "ui_screenshot", "app_logs", "app_install")]}
            elif params.get("name") == "ui_screenshot":
                result = {"content": [{"type": "image", "mimeType": "image/png",
                                       "data": base64.b64encode(b"png").decode("ascii")}]}
            elif params.get("name") == "app_install" and params["arguments"]["launch"]:
                result = {"structuredContent": {"installed": True, "package": "dev.ashnazg.fixture"}}
            elif params.get("name") == "ui_snapshot":
                result = {"structuredContent": {"nodes": [
                    {"testId": "dev.ashnazg.fixture:id/increment"}, {"text": "Count: 0"}
                ]}}
            elif allow_error:
                return {"id": response_id, "error": {"message": "expected invalid request"}}
            else:
                result = {}
            return {"id": response_id, "result": result}

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_apk = root / "agent-android/fixture/build/outputs/apk/debug/fixture-debug.apk"
            fixture_apk.parent.mkdir(parents=True)
            fixture_apk.write_bytes(b"root fixture artifact")
            artifact_dir = root / "artifacts"
            with patch("scripts.integration.RpcClient"), \
                 patch("scripts.integration.prepare_native_fixture"), \
                 patch("scripts.integration.subprocess.Popen", return_value=process), \
                 patch("scripts.integration.mcp_send", side_effect=respond):
                verify_mcp("ashnazg", harness, artifact_dir, fixture_apk.resolve())

        install_arguments = [message["params"]["arguments"] for message in requests
                             if message.get("params", {}).get("name") == "app_install"]
        self.assertEqual(install_arguments, [
            {"path": str(fixture_apk.resolve()), "launch": False},
            {"path": str(fixture_apk.resolve()), "launch": True},
        ])
        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)


if __name__ == "__main__":
    unittest.main()
