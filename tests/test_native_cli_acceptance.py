import base64
import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import struct
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import native_cli_acceptance as acceptance
ORIGINAL_VERIFY_CANDIDATE = acceptance._verified_candidate


def png(value):
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(bytes((0, value, 0, 0, 255)))) + chunk(b'IEND', b''))


PNG = png(1)


FAKE = r'''#!/usr/bin/env python3
import base64, json, os, signal, sys, time, atexit
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
acted = False
credential = Path(os.environ['HOME']) / 'Library/Application Support/ashnazg/native-mcp' / ('a' * 40) / '.ashnazg-session-v1.json'
credential.parent.mkdir(parents=True, exist_ok=True)
if not cfg.get('missing_credential'):
    credential.write_text('{}')
    credential.chmod(0o600)
def cleanup():
    if not cfg.get('leak_credential'):
        credential.unlink(missing_ok=True)
atexit.register(cleanup)
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
        if name == "app_install": result = {"installed":True,"package":cfg.get("package", "dev.ashnazg.fixture"),"serial":"127.0.0.1:15555"}
        elif name == "ui_snapshot": result = {"snapshotId":1,"nodes":[{"text":"Count: 0","testId":"dev.ashnazg.fixture:id/increment","clickable":True,"enabled":True}]}
        elif name == "ui_act":
            acted = True
            result = {"settled":True,"snapshot":{"snapshotId":2,"nodes":[{"text":"Count: " + str(cfg.get('count_after', 1)),"testId":"dev.ashnazg.fixture:id/increment","clickable":True,"enabled":True}]}}
        elif name == "app_logs": result = {"lines":["rpc_token=private-log-secret"],"truncated":False}
        elif name == "ui_screenshot":
            image = cfg["png_after"] if acted and not cfg.get("stale_pixels") else cfg["png_before"]
            result = {"content":[{"type":"image","mimeType":"image/png","data":image},{"type":"text","text":json.dumps({"mimeType":"image/png","snapshotId":2 if acted else 1,"width":1,"height":1,"marks":[]})}]}
            emit({"jsonrpc":"2.0","id":request["id"],"result":result})
            continue
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"structuredContent":result,"content":[]}})
    elif method == "initialize":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"ashnazg","version":"test"}}})
    elif method == "tools/list":
        emit({"jsonrpc":"2.0","id":request["id"],"result":{"tools":[{"name":x,"inputSchema":cfg["schemas"][x]} for x in ["ui_snapshot","ui_act","ui_screenshot","app_logs","app_install"]]}})
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
        verified_candidate = patch.object(acceptance, '_verified_candidate', return_value={
            "source_sha": "a" * 40, "candidate_sha256": "b" * 64,
            "provenance_scope": "test-owned fake candidate; not runtime proof"})
        verified_candidate.start(); self.addCleanup(verified_candidate.stop)
        ports = patch.object(acceptance, '_ports_closed', side_effect=[
            {str(port): value for port in (15555, 7777, 17778)} for value in (True, False, True)])
        ports.start(); self.addCleanup(ports.stop)
        descendants = patch.object(acceptance, '_process_descendants', return_value={"status": "available", "processes": [
            {"pid": 2147483000, "ppid": 1, "command": "ashnazg-vm-fixture"},
            {"pid": 2147483001, "ppid": 1, "command": "ashnazg"}]})
        descendants.start(); self.addCleanup(descendants.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def configure_fake(self, **values):
        config = {"events": str(self.events), "args": str(self.root / "args.json"),
                  "environment": str(self.root / "environment.json"),
                  "png_before": base64.b64encode(png(0)).decode(), "png_after": base64.b64encode(PNG).decode(),
                  "schemas": json.loads(acceptance.SCHEMA_PATH.read_text()), **values}
        Path(str(self.binary) + ".json").write_text(json.dumps(config))

    def run_driver(self, mode="eof", *, use_home=True, **config):
        self.configure_fake(**config)
        output = self.root / ("receipt-" + mode)
        arguments = [
            "--candidate-dir", str(self.root / "candidate"), "--binary", str(self.binary), "--fixture-apk", str(self.apk),
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
        self.assertEqual(self.events.read_text().splitlines(), ["ui_snapshot", "app_install", "ui_snapshot", "ui_screenshot", "ui_act", "ui_screenshot", "app_logs"])
        calls = receipt["protocol"]["requests"]
        self.assertEqual(calls[0]["method"], "initialize")
        self.assertEqual(calls[1]["method"], "notifications/initialized")
        self.assertEqual(calls[2]["method"], "tools/list")
        self.assertEqual(calls[3]["params"]["arguments"], {"includeInvisible": False})
        self.assertEqual(calls[4]["params"]["arguments"], {"path": str(self.apk.resolve()), "launch": True})
        self.assertEqual(calls[5]["params"]["arguments"], {"includeInvisible": False})
        self.assertEqual(calls[7]["params"]["arguments"], {"action": "tap", "target": {"testId": "dev.ashnazg.fixture:id/increment"}})
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
        code = acceptance.main(["--candidate-dir", str(self.root / "candidate"), "--binary", str(self.binary), "--fixture-apk", str(self.apk),
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
            code = acceptance.main(["--candidate-dir", str(self.root / "candidate"), "--binary", str(self.binary), "--fixture-apk", str(self.apk),
                                   "--home", str(self.home), "--output-dir", str(output),
                                   "--mode", "eof", "--source-sha", "a" * 40])
        finally:
            acceptance.MAX_FRAME_BYTES = previous
        self.assertNotEqual(code, 0)
        receipt = json.loads((output / "receipt.json").read_text())
        self.assertEqual(receipt["status"], "failed")
        self.assertTrue(receipt["shutdown"]["joined"])
        self.assertIn("protocol bounds", receipt["error"])

    def test_retained_credentials_make_acceptance_fail(self):
        code, output = self.run_driver(leak_credential=True)
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads((output / "receipt.json").read_text())["status"], "failed")

    def test_retained_listener_makes_acceptance_fail(self):
        with patch.object(acceptance, '_ports_closed', side_effect=[
                {str(p): v for p in (15555, 7777, 17778)} for v in (True, False, False)]):
            code, _ = self.run_driver()
        self.assertNotEqual(code, 0)

    def test_missing_native_readiness_evidence_fails(self):
        code, _ = self.run_driver(missing_credential=True)
        self.assertNotEqual(code, 0)

    def test_unavailable_descendant_inventory_fails(self):
        with patch.object(acceptance, '_process_descendants', return_value={"status": "unavailable", "processes": []}):
            code, _ = self.run_driver()
        self.assertNotEqual(code, 0)

    def test_wrong_fixture_counter_effect_fails(self):
        code, _ = self.run_driver(count_after=7)
        self.assertNotEqual(code, 0)

    def test_wrong_inferred_package_fails(self):
        code, _ = self.run_driver(package="com.other.app")
        self.assertNotEqual(code, 0)

    def test_unchanged_pixels_fail_even_with_new_snapshot_id(self):
        code, _ = self.run_driver(stale_pixels=True)
        self.assertNotEqual(code, 0)

    def test_schema_property_names_do_not_mask_wrong_types(self):
        schemas = json.loads(acceptance.SCHEMA_PATH.read_text())
        schemas['ui_act']['properties']['action'] = {"type": "integer"}
        code, _ = self.run_driver(schemas=schemas)
        self.assertNotEqual(code, 0)

    def test_interrupt_retains_failed_receipt_after_joined_cleanup(self):
        original = acceptance.StdioClient.request
        def interrupt(client, message, **kwargs):
            if message.get('method') == 'tools/list':
                raise KeyboardInterrupt()
            return original(client, message, **kwargs)
        with patch.object(acceptance.StdioClient, 'request', interrupt):
            code, output = self.run_driver()
        self.assertNotEqual(code, 0)
        receipt = json.loads((output / 'receipt.json').read_text())
        self.assertEqual(receipt['status'], 'failed')
        self.assertTrue(receipt['shutdown']['joined'])
        self.assertEqual(receipt['tools_called'], [])

    def test_existing_nonprivate_home_is_refused_without_chmod(self):
        self.home.chmod(0o755)
        code, _ = self.run_driver()
        self.assertNotEqual(code, 0)
        self.assertEqual(stat.S_IMODE(self.home.stat().st_mode), 0o755)

    def test_candidate_provenance_must_match_exact_tested_binary_and_apk(self):
        import zipfile
        candidate = self.root / 'verified-candidate'
        candidate.mkdir()
        binary_bytes = self.binary.read_bytes()
        metadata = {"files": {"bin/ashnazg": {"size": len(binary_bytes), "sha256": acceptance.sha256_bytes(binary_bytes)}}}
        with zipfile.ZipFile(candidate / 'ashnazg-v0.3.0-macos-aarch64.zip', 'w') as archive:
            archive.writestr('manifest.json', json.dumps(metadata))
        (candidate / 'ashnazg-runtime-v0.3.0.json').write_text(json.dumps({"artifacts": {"fixture": {
            "size": self.apk.stat().st_size, "sha256": acceptance.sha256_file(self.apk)}}}))
        (candidate / 'candidate.json').write_text('{}')
        with patch('runtime_release.verify_release', return_value=True):
            proof = ORIGINAL_VERIFY_CANDIDATE(candidate, self.binary, self.apk, 'a' * 40)
            self.assertEqual(proof['candidate_sha256'], acceptance.sha256_bytes(b'{}'))
            self.binary.write_bytes(binary_bytes + b'changed')
            with self.assertRaisesRegex(acceptance.AcceptanceError, 'binary differs'):
                ORIGINAL_VERIFY_CANDIDATE(candidate, self.binary, self.apk, 'a' * 40)
            self.binary.write_bytes(binary_bytes)
            self.apk.write_bytes(b'changed APK')
            with self.assertRaisesRegex(acceptance.AcceptanceError, 'fixture differs'):
                ORIGINAL_VERIFY_CANDIDATE(candidate, self.binary, self.apk, 'a' * 40)

    def test_png_rgb_and_opaque_rgba_have_identical_normalized_pixels(self):
        def chunk(kind, body):
            return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
        rgb = (b"\x89PNG\r\n\x1a\n" + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
               + chunk(b'IDAT', zlib.compress(bytes((0, 1, 0, 0)))) + chunk(b'IEND', b''))
        self.assertEqual(acceptance.decode_png(rgb)['pixel_sha256'], acceptance.decode_png(PNG)['pixel_sha256'])

    def test_png_decode_checks_pixels_crc_and_complete_deflate(self):
        self.assertEqual(acceptance.decode_png(PNG)['pixel_sha256'], acceptance.sha256_bytes(bytes((1, 0, 0, 255))))
        for broken in (PNG[:8], PNG[:-1], PNG[:25] + bytes((PNG[25] ^ 1,)) + PNG[26:]):
            with self.subTest(length=len(broken)), self.assertRaises(acceptance.AcceptanceError):
                acceptance.decode_png(broken)

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
