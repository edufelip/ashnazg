import base64
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_cli_acceptance as acceptance


PNG = b"\x89PNG\r\n\x1a\nfixture-image"


FAKE = r'''#!/usr/bin/env python3
import base64, json, os, signal, sys, time
from pathlib import Path
cfg = json.loads(Path(__file__ + ".json").read_text())
SCHEMAS = {"ui_snapshot":{"includeInvisible":{}},"ui_act":{"action":{},"target":{},"text":{},"direction":{}},"ui_screenshot":{"format":{},"somOverlay":{}},"app_logs":{"sinceMs":{},"tagFilter":{}},"app_install":{"path":{},"launch":{},"package":{}}}
Path(cfg["events"]).write_text("")
Path(cfg["args"]).write_text(json.dumps(sys.argv[1:]))
Path(cfg["environment"]).write_text(json.dumps({k: os.environ.get(k) for k in ["ASHNAZG_TOKEN", "ANDROID_SERIAL", "ADB", "ASHNAZG_ADB", "ASHNAZG_AAPT", "AAPT", "ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_SDK_HOME", "ANDROID_SDK_PATH", "SDKPATH", "PATH", "HOME"]}))
def emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()
calls = 0
snapshots = 0
def stop(*_): sys.exit(0)
signal.signal(signal.SIGTERM, stop)
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "notifications/initialized":
        continue
    if method == "initialize" and cfg.get("oversize_initialize"):
        sys.stdout.write("x" * 512 + "\n")
        sys.stdout.flush()
        continue
    calls += 1
    if method == "tools/call":
        name = request["params"]["name"]
        if name == "ui_snapshot": snapshots += 1
        with open(cfg["events"], "a") as f: f.write(name + "\n")
        if name == "ui_snapshot" and snapshots <= cfg.get("startup_failures", 0):
            emit({"jsonrpc":"2.0", "id":request["id"], "error":{"code":-32000,"message":"startup unavailable"}})
            continue
        if name == cfg.get("error_tool"):
            emit({"jsonrpc":"2.0","id":request["id"],"result":{"isError":True,"content":[{"type":"text","text":"privateToken=never-write-this"}]}})
            continue
        if name == "app_install": result = {"installed":True,"package":"dev.ashnazg.fixture","serial":"127.0.0.1:15555"}
        elif name == "ui_snapshot": result = {"snapshotId":1,"nodes":[{"text":"Count: 0","testId":"dev.ashnazg.fixture:id/increment","clickable":True,"enabled":True}]}
        elif name == "ui_act": result = {"settled":True,"snapshot":{"snapshotId":2,"nodes":[{"text":"Count: 1","testId":"dev.ashnazg.fixture:id/increment","clickable":True,"enabled":True}]}}
        elif name == "app_logs": result = {"lines":["rpc_token=private-log-secret"],"truncated":False}
        elif name == "ui_screenshot":
            image = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture-image").decode()
            result = {"content":[{"type":"image","mimeType":"image/png","data":image},{"type":"text","text":json.dumps({"mimeType":"image/png","snapshotId":2,"width":1,"height":1,"marks":[]})}]}
            emit({"jsonrpc":"2.0","id":request["id"],"result":result})
            continue
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"structuredContent":result,"content":[]}})
    elif method == "initialize":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"ashnazg","version":"test"}}})
    elif method == "tools/list":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"tools":[{"name":x,"inputSchema":{"type":"object","properties":SCHEMAS[x]}} for x in ["ui_snapshot","ui_act","ui_screenshot","app_logs","app_install"]]}})
sys.exit(0)
'''


class NativeCliAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir(mode=0o700)
        os.chmod(self.home, 0o700)
        self.apk = self.root / "fixture.apk"
        self.apk.write_bytes(b"fake apk bytes")
        self.binary = self.root / "ashnazg"
        self.binary.write_text(FAKE.replace("#!/usr/bin/env python3", "#!" + sys.executable))
        self.binary.chmod(0o700)
        self.events = self.root / "events.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def configure_fake(self, **values):
        config = {"events": str(self.events), "args": str(self.root / "args.json"),
                  "environment": str(self.root / "environment.json"), **values}
        Path(str(self.binary) + ".json").write_text(json.dumps(config))

    def run_driver(self, mode="eof", *, use_home=True, **config):
        self.configure_fake(**config)
        output = self.root / ("receipt-" + mode)
        arguments = [
            "--binary", str(self.binary), "--fixture-apk", str(self.apk),
            "--output-dir", str(output),
            "--mode", mode, "--source-sha", "a" * 40,
        ]
        if use_home:
            arguments.extend(["--home", str(self.home)])
        code = acceptance.main(arguments)
        return code, output

    def test_eof_runs_real_mcp_stdio_contract_and_binds_receipt(self):
        code, output = self.run_driver()
        self.assertEqual(code, 0)
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(receipt["source_sha"], "a" * 40)
        self.assertEqual(receipt["tools_called"], ["app_install", "ui_snapshot", "ui_act", "ui_screenshot", "app_logs"])
        self.assertEqual(receipt["screenshot"]["sha256"], acceptance.sha256_bytes(PNG))
        self.assertNotIn("private-log-secret", json.dumps(receipt))
        for evidence_file in output.iterdir():
            if evidence_file.is_file():
                self.assertNotIn(b"private-log-secret", evidence_file.read_bytes())
        self.assertEqual(receipt["protocol"]["sanitized_tool_results"]["app_logs"]["line_count"], 1)
        self.assertEqual((output / "screenshot.png").read_bytes(), PNG)
        self.assertEqual(self.events.read_text().splitlines(), ["ui_snapshot", "app_install", "ui_snapshot", "ui_act", "ui_screenshot", "app_logs"])
        calls = receipt["protocol"]["requests"]
        self.assertEqual(calls[0]["method"], "initialize")
        self.assertEqual(calls[1]["method"], "notifications/initialized")
        self.assertEqual(calls[2]["method"], "tools/list")
        self.assertEqual(calls[3]["params"]["arguments"], {"includeInvisible": False})
        self.assertEqual(calls[4]["params"]["arguments"], {"path": str(self.apk.resolve()), "launch": True})
        self.assertEqual(calls[5]["params"]["arguments"], {"includeInvisible": False})
        self.assertEqual(calls[6]["params"]["arguments"], {"action": "tap", "target": {"testId": "dev.ashnazg.fixture:id/increment"}})
        self.assertNotIn("ASHNAZG_TOKEN", receipt["environment"])
        self.assertEqual(json.loads((self.root / "args.json").read_text()), ["mcp"])
        environment = json.loads((self.root / "environment.json").read_text())
        for variable in ["ASHNAZG_TOKEN", "ANDROID_SERIAL", "ADB", "ASHNAZG_ADB", "ASHNAZG_AAPT",
                         "AAPT", "ANDROID_HOME", "ANDROID_SDK_ROOT", "ANDROID_SDK_HOME",
                         "ANDROID_SDK_PATH", "SDKPATH"]:
            self.assertIsNone(environment[variable])
        self.assertEqual(list(Path(environment["PATH"]).iterdir()), [])

    def test_sigterm_keeps_stdin_open_until_ready_then_joins_child(self):
        code, output = self.run_driver("sigterm")
        self.assertEqual(code, 0)
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertEqual(receipt["shutdown"]["mode"], "sigterm")
        self.assertEqual(receipt["shutdown"]["stdin_open_at_signal"], True)
        self.assertTrue(receipt["shutdown"]["joined"])

    def test_startup_unavailable_is_retried_only_with_harmless_snapshot(self):
        acceptance.STARTUP_RETRY_INTERVAL = 0
        code, output = self.run_driver(startup_failures=2)
        self.assertEqual(code, 0)
        self.assertEqual(self.events.read_text().splitlines()[:3], ["ui_snapshot"] * 3)

    def test_tool_error_is_written_as_sanitized_failure_receipt(self):
        self.configure_fake(error_tool="ui_act")
        output = self.root / "failed"
        code = acceptance.main(["--binary", str(self.binary), "--fixture-apk", str(self.apk),
                                "--home", str(self.home), "--output-dir", str(output),
                                "--mode", "eof", "--source-sha", "a" * 40])
        self.assertNotEqual(code, 0)
        receipt_text = (output / "receipt.json").read_text()
        self.assertNotIn("never-write-this", receipt_text)
        self.assertEqual(json.loads(receipt_text)["status"], "failed")

    def test_oversized_stdout_frame_fails_with_a_joined_owned_child(self):
        self.configure_fake(oversize_initialize=True)
        output = self.root / "oversize"
        previous = acceptance.MAX_FRAME_BYTES
        acceptance.MAX_FRAME_BYTES = 300
        try:
            code = acceptance.main(["--binary", str(self.binary), "--fixture-apk", str(self.apk),
                                   "--home", str(self.home), "--output-dir", str(output),
                                   "--mode", "eof", "--source-sha", "a" * 40])
        finally:
            acceptance.MAX_FRAME_BYTES = previous
        self.assertNotEqual(code, 0)
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["shutdown"]["joined"])
        self.assertIn("protocol bounds", receipt["error"])

    def test_environment_is_minimal_and_private_paths_are_recorded_without_secret_bytes(self):
        code, output = self.run_driver()
        self.assertEqual(code, 0)
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertTrue(receipt["environment"]["path_is_private_empty_dir"])
        self.assertTrue(receipt["environment"]["home_mode_700"])
        self.assertTrue(receipt["cleanup"]["credential_absent"])
        self.assertEqual(receipt["cache"]["claim"], "not_observable")
        self.assertNotIn("credential_contents", receipt)

    def test_default_home_uses_private_production_cache_and_source_scoped_state_paths(self):
        code, output = self.run_driver(use_home=False)
        self.assertEqual(code, 0)
        receipt = json.loads((output / "receipt.json").read_text())
        home = output / "home"
        self.assertEqual(Path(receipt["environment"]["home"]), home)
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertEqual(Path(receipt["cache"]["path"]), home / "Library/Caches/ashnazg/runtime")
        self.assertEqual(Path(receipt["state"]["path"]), home / "Library/Application Support/ashnazg/native-mcp" / ("a" * 40))


if __name__ == "__main__":
    unittest.main()
