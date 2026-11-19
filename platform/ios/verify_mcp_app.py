#!/usr/bin/env python3
"""Drive the production `ashnazg mcp --ios-app <manifest>` server over stdio MCP.

macOS only. One live external app session pinned by a frozen Meer manifest:
the ios-app-session descriptor (bundleId, appSha256, owned simulator) is
verified against the manifest and the host device list with absence checked
before and after EOF, synthetic mock Meer credentials arrive through a local
input file and never argv, the login fields and submit button are selected
from live snapshots and typed/tapped exactly once each, one bounded
scroll_to reveal runs after each credential (the keyboard compresses the
login scroll container and clips the later controls), Home and Search are
confirmed by bounded read-only snapshot polling after the accepted actions,
and the pinned exp+meer://search and exp+meer://home allowlisted URIs carry
the journey. Receipts carry typed stage status and safe scalars only: no
raw request, response, snapshot, stdout, stderr, exception text or screenshot
is retained or printed, no screenshot is published, and nothing asserts that
Xcode-retained diagnostics enforce secrecy. Failed stages record a safe
reason code, safe observations and uncertainty; mutating interactions run
at most once and are never replayed. If a receipt would leak the mock
credentials it is discarded and replaced by a static failure receipt.

Usage:
    python3 platform/ios/verify_mcp_app.py \
        --binary target/debug/ashnazg \
        --manifest <frozen Meer app manifest.json> \
        --credentials <synthetic mock email/password.json> \
        --output-dir <empty owned dir>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# The sibling fixture module resolves through the script directory.
from verify_mcp_fixture import (
    AcceptanceError,
    StdioClient,
    device_udids,
    image_and_metadata,
    png_identity,
    sha256_bytes,
    structured,
)

PROTOCOL_VERSION = "2024-11-05"
FROZEN_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "runtime/mcp-tools.schema.json"
UI_TOOLS = ("ui_act", "ui_screenshot", "ui_snapshot")
SESSION_MARKER = "ios-app-session"

# The pinned Meer journey: selectors resolve from live snapshots only, and
# the deep links are the exact allowlisted URIs the frozen manifest carries.
LOGIN_EMAIL = "login-email-input"
LOGIN_PASSWORD = "login-password-input"
LOGIN_SUBMIT = "login-submit-button"
LOGIN_DIAGNOSTIC_IDS = frozenset({
    LOGIN_EMAIL, LOGIN_PASSWORD, LOGIN_SUBMIT,
    "login-email-error", "login-password-error", "login-error-box", "login-error-retry",
})
HOME_SEARCH = "home-search-button"
SEARCH_INPUT = "search-input"
NAVIGATION_DIAGNOSTIC_IDS = frozenset({LOGIN_EMAIL, HOME_SEARCH, SEARCH_INPUT, "search-back-button"})
PINNED_DEEP_LINKS = ("exp+meer://search", "exp+meer://home")
CREDENTIAL_PLACEHOLDER = "<redacted synthetic mock credential>"

# The server opens the app session (simulator, staging, install, boot)
# before it answers initialize, so early requests carry the cold deadline.
COLD_TIMEOUT = 900.0
REQUEST_TIMEOUT = 120.0
SHUTDOWN_TIMEOUT = 300.0
FIRST_LOGIN_TIMEOUT = 180.0
HOME_AFTER_SUBMIT_TIMEOUT = 240.0
SCREEN_AFTER_LINK_TIMEOUT = 180.0
POLL_PAUSE_SECONDS = 1.0
MAX_CREDENTIAL_CHARS = 256

# A failed exchange is uncertain only when the effect may or may not have
# happened; a definitive rejection is an observation, never uncertainty.
PRE_DISPATCH_REJECTIONS = {
    "selector-ambiguous", "target-not-enabled-hittable",
    "target-not-editable", "deep-link-not-allowlisted",
    "scroll-container-not-resolved", "scroll-direction-not-supported",
}
MANIFEST_KEYS = {
    "appPath",
    "appSha256",
    "bundleId",
    "runtime",
    "deviceType",
    "initialUrl",
    "readySelector",
    "deepLinks",
}
REQUIRED_MANIFEST_KEYS = MANIFEST_KEYS - {"initialUrl"}


def safe_code(message: str) -> str:
    """Maps one static server or bridge rejection message onto a safe
    reason code; the raw text is used only for this classification."""
    lowered = (message or "").lower()
    if "matched" in lowered and "elements" in lowered:
        return "selector-ambiguous"
    if "not an enabled hittable" in lowered:
        return "target-not-enabled-hittable"
    if "not an editable text" in lowered:
        return "target-not-editable"
    if "not in the app manifest allowlist" in lowered:
        return "deep-link-not-allowlisted"
    if "did not resolve to a unique containing scroll view" in lowered:
        return "scroll-container-not-resolved"
    if "requires the down direction" in lowered:
        return "scroll-direction-not-supported"
    if "timed out" in lowered:
        return "request-timed-out"
    if "closed stdout" in lowered:
        return "server-closed-stdout"
    if "stdout drain failed" in lowered:
        return "stdout-drain-failed"
    if (
        "did not settle" in lowered
        or "was not observed in the foreground after" in lowered
        or "unconfirmed prefix after first grapheme:" in lowered
    ):
        return "action-readback-not-confirmed"
    return "unclassified-server-error"


def safe_structured(result: dict) -> dict:
    """The imported helper's fallback would quote raw response content;
    this driver never records or prints response text."""
    try:
        return structured(result)
    except AcceptanceError:
        raise AcceptanceError("tool result has no structured content") from None


def safe_screenshot(result: dict) -> tuple[bytes, dict]:
    try:
        return image_and_metadata(result)
    except (AcceptanceError, ValueError):
        raise AcceptanceError("the screenshot result had unexpected content blocks") from None


def app_session_descriptor(client: StdioClient) -> dict:
    """Parses the ios-app-session descriptor from the drained stderr; the
    fixture client's own marker stays fixture-scoped."""
    for raw in b"".join(client.stderr_tail).splitlines():
        text = raw.decode("utf-8", "replace")
        marker = SESSION_MARKER + " "
        if marker in text:
            return json.loads(text.split(marker, 1)[1].strip())
    raise AcceptanceError("the server never reported its ios-app-session descriptor")


def load_mock_credentials(path: Path) -> tuple[str, str]:
    """Reads the separately supplied synthetic Meer mock credentials; the
    values never appear in argv, logs, artifacts or errors, and are never
    validated as real account credentials."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        raise AcceptanceError(f"the mock credentials file at {path} is unreadable or not JSON") from None
    if not isinstance(data, dict) or set(data) != {"email", "password"}:
        raise AcceptanceError("the mock credentials file must be JSON with exactly email and password")
    email, password = data["email"], data["password"]
    if not isinstance(email, str) or not isinstance(password, str):
        raise AcceptanceError("the mock email and password must be strings")
    if not email or not password:
        raise AcceptanceError("the mock email and password must be nonempty")
    if len(email) > MAX_CREDENTIAL_CHARS or len(password) > MAX_CREDENTIAL_CHARS:
        raise AcceptanceError("the mock credentials exceed the bounded length")
    return email, password


def validate_frozen_manifest(path: Path) -> dict:
    """The frozen-manifest pins: the pure shape checks the server
    re-validates authoritatively, plus this driver's journey pins (both
    allowlisted URIs present, ready selector exactly one field)."""
    try:
        manifest = json.loads(path.read_text())
    except (OSError, ValueError):
        raise AcceptanceError(f"the app manifest at {path} is unreadable or not JSON") from None
    if not isinstance(manifest, dict) or set(manifest) - MANIFEST_KEYS:
        raise AcceptanceError("the app manifest must be a JSON object with known fields only")
    missing = REQUIRED_MANIFEST_KEYS - set(manifest)
    if missing:
        raise AcceptanceError(f"the app manifest is missing required fields: {sorted(missing)}")
    if not Path(manifest["appPath"]).is_absolute() or not manifest["appPath"].endswith(".app"):
        raise AcceptanceError("appPath must be an absolute .app bundle directory")
    if len(manifest["appSha256"]) != 64 or manifest["appSha256"].strip("0123456789abcdef"):
        raise AcceptanceError("appSha256 must be exactly 64 lowercase hex characters")
    if not manifest["bundleId"] or any(not (char.isalnum() or char in ".-_") for char in manifest["bundleId"]):
        raise AcceptanceError("bundleId must be nonempty bundle identifier characters")
    if not manifest["runtime"] or not manifest["deviceType"]:
        raise AcceptanceError("runtime and deviceType must name explicit simulator identifiers")
    selector = manifest["readySelector"]
    kinds = [kind for kind in ("testId", "text") if isinstance(selector, dict) and selector.get(kind)]
    if not isinstance(selector, dict) or set(selector) - {"testId", "text"} or len(kinds) != 1:
        raise AcceptanceError("readySelector must be exactly one nonempty testId or text")
    links = manifest["deepLinks"]
    if not isinstance(links, list) or not all(isinstance(uri, str) and uri and ":" in uri for uri in links):
        raise AcceptanceError("deepLinks must be a list of absolute app-scheme URIs")
    for pinned in PINNED_DEEP_LINKS:
        if pinned not in links:
            raise AcceptanceError(f"the frozen manifest must allowlist the pinned journey URI {pinned}")
    return manifest


def require_macos_host() -> None:
    if sys.platform != "darwin":
        raise AcceptanceError("the external iOS app session requires macOS")
    for tool in ("xcodebuild", "xcrun", "xcodegen"):
        if subprocess.run(["sh", "-c", f"command -v {tool}"], capture_output=True).returncode:
            raise AcceptanceError(f"{tool} must be on PATH for the iOS app session")


def run_acceptance(
    binary: Path,
    manifest_path: Path,
    credentials_path: Path,
    output: Path,
    source_sha: str,
) -> dict:
    require_macos_host()
    binary = binary.resolve(strict=True)
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise AcceptanceError(f"--binary must be an executable file: {binary}")
    if not output.is_dir():
        output.mkdir(parents=True, mode=0o700)
    if any(output.iterdir()):
        raise AcceptanceError("--output-dir must be empty to preserve evidence")

    manifest = validate_frozen_manifest(manifest_path)
    frozen_schemas = json.loads(FROZEN_SCHEMA_PATH.read_text())
    email, password = load_mock_credentials(credentials_path)
    secrets = (email, password)

    environment = dict(os.environ)
    environment.pop("ASHNAZG_TOKEN", None)
    environment["RUST_LOG"] = "info"

    receipt: dict = {
        "schema_version": 1,
        "status": "running",
        "source_sha": source_sha,
        "binary": {"path": str(binary), "sha256": sha256_bytes(binary.read_bytes())},
        "manifest": {
            "path": str(manifest_path),
            "file_sha256": sha256_bytes(manifest_path.read_bytes()),
            "app_sha256": manifest["appSha256"],
            "bundle_id": manifest["bundleId"],
            "runtime": manifest["runtime"],
            "device_type": manifest["deviceType"],
            "ready_selector": manifest["readySelector"],
            "deep_links": manifest["deepLinks"],
            "initial_url_present": manifest.get("initialUrl") is not None,
        },
        "credentials": {
            "source": "local input file, never argv",
            "email_chars": len(email),
            "password_chars": len(password),
            "values_retained": False,
            "synthetic": "supplied out-of-band for the mock Meer login; never validated as real credentials",
        },
        "tool_sequence": [],
        "session": {},
        "journey": {},
        "readonly_evidence": {},
        "timings": {},
        "secrecy": {},
        "cleanup": {},
        "evidence_dir": str(output),
    }
    started = time.monotonic()
    descriptor: dict | None = None
    udid: str | None = None
    next_id = [1]
    last_sequence = [0]
    attempted: set[str] = set()

    pre_udids = device_udids()
    launch_started = time.monotonic()
    process = subprocess.Popen(
        [str(binary), "mcp", "--ios-app", str(manifest_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        bufsize=0,
    )
    client = StdioClient(process)
    client.start()

    def fail(stage: str, code: str, uncertain: bool = False, **observed) -> None:
        """Records one typed, safe stage failure and stops the journey;
        nothing is replayed afterwards."""
        receipt["failure"] = {
            "stage": stage,
            "reason_code": code,
            "uncertain": uncertain,
            "observed": observed,
            "replayed": False,
        }
        raise AcceptanceError(f"{stage}: {code}") from None

    def once(stage: str) -> None:
        """Exactly one mutating interaction per stage, ever."""
        if stage in attempted:
            raise AcceptanceError(f"{stage}: a mutating interaction was attempted twice") from None
        attempted.add(stage)

    def record(tool: str, arguments: dict, result: dict, elapsed: float) -> None:
        entry = {"tool": tool, "elapsed_seconds": round(elapsed, 3)}
        if tool == "ui_act":
            entry["action"] = arguments.get("action")
            target = arguments.get("target") or {}
            if target:
                entry["target_test_id"] = target.get("testId") or target.get("text")
            if arguments.get("action") == "type":
                entry["text"] = CREDENTIAL_PLACEHOLDER
            elif arguments.get("action") == "scroll_to":
                # A safe scalar; the reveal direction is part of the journey.
                entry["direction"] = arguments.get("direction")
            elif "text" in arguments:
                # Only the pinned allowlisted deep-link URI rides here.
                entry["text"] = arguments["text"]
        if "content" in result:
            entry["content_kinds"] = [block.get("type") for block in result["content"]]
        if result.get("isError"):
            entry["isError"] = True
        receipt["tool_sequence"].append(entry)

    def exchange(
        method: str, params: dict, timeout: float, stage: str, allow_error: bool = False
    ) -> dict:
        payload = {
            "jsonrpc": "2.0",
            "id": next_id[0],
            "method": method,
            "params": params,
        }
        try:
            result = client.request(payload, timeout, allow_error=allow_error)
        except AcceptanceError as error:
            # Raw server text never reaches a receipt: classify, detach.
            code = safe_code(str(error))
            fail(stage, code, uncertain=params.get("name") == "ui_act")
        next_id[0] += 1
        return result

    def call(name: str, arguments: dict, stage: str, allow_error: bool = False) -> dict:
        began = time.monotonic()
        result = exchange(
            "tools/call", {"name": name, "arguments": arguments}, REQUEST_TIMEOUT, stage, allow_error
        )
        record(name, arguments, result, time.monotonic() - began)
        return result

    def snapshot(stage: str) -> dict:
        result = call("ui_snapshot", {"includeInvisible": False}, stage)
        try:
            return safe_structured(result)
        except AcceptanceError:
            fail(stage, "tool-result-without-structured-content")

    def select(nodes: list, test_id: str, role: str, stage: str) -> dict:
        matches = [node for node in nodes if node.get("testId") == test_id]
        if len(matches) != 1:
            fail(stage, "selector-ambiguous-or-absent", matched=len(matches))
        node = matches[0]
        if node.get("role") != role or node.get("enabled") is not True:
            fail(stage, "selector-not-usable", matched=1, role=node.get("role"), enabled=node.get("enabled"))
        return node

    def poll_for(stage: str, test_id: str, role: str, bound: float) -> tuple[dict, dict, list]:
        """Bounded read-only snapshot polling after an accepted action; any
        error aborts the stage instead of being retried."""
        ids: list = []
        deadline = time.monotonic() + bound
        while True:
            snap = snapshot(stage)
            ids.append(snap.get("snapshotId"))
            if len(ids) > 1 and (ids[-1] is None or ids[-2] is None or ids[-1] <= ids[-2]):
                fail(stage, "polling-did-not-advance-sequence", uncertain=True, snapshot_id=ids[-1])
            nodes = snap.get("nodes") or []
            matches = [node for node in nodes if node.get("testId") == test_id]
            if len(matches) == 1 and matches[0].get("role") == role and matches[0].get("enabled") is True:
                return matches[0], snap, ids
            if time.monotonic() >= deadline:
                observed = dict(polls=len(ids), last_snapshot_id=ids[-1], bound_seconds=bound)
                if stage == "home/after-submit":
                    email_fields = [
                        node for node in nodes
                        if node.get("testId") == LOGIN_EMAIL and node.get("role") == "textfield"
                    ]
                    email_readback = "unobservable"
                    if len(email_fields) == 1 and isinstance(email_fields[0].get("value"), str):
                        email_readback = "exact" if email_fields[0]["value"] == email else "different"
                    observed["login_state"] = {
                        "observed_selectors": sorted({
                            node["testId"] for node in nodes if node.get("testId") in LOGIN_DIAGNOSTIC_IDS
                        }),
                        "email_readback": email_readback,
                    }
                elif stage in ("search/after-link", "home-return/after-link"):
                    observed["observed_selectors"] = sorted({
                        node["testId"] for node in nodes if node.get("testId") in NAVIGATION_DIAGNOSTIC_IDS
                    })
                fail(stage, "target-not-observed-within-bound", **observed)
            time.sleep(POLL_PAUSE_SECONDS)

    def observe_unsettled_text(stage: str, test_id: str, expected: str) -> dict:
        try:
            snap = snapshot(f"{stage}/readback")
        except (AcceptanceError, OSError):
            return {"status": "unavailable"}
        sequence = snap.get("snapshotId")
        if type(sequence) is not int or sequence <= last_sequence[0]:
            return {"status": "stale"}
        nodes = [node for node in (snap.get("nodes") or []) if node.get("testId") == test_id]
        if len(nodes) != 1 or nodes[0].get("role") != "textfield":
            return {"status": "target-not-usable", "matched": len(nodes)}
        observed = nodes[0].get("value")
        if not isinstance(observed, str) or not observed:
            return {"status": "observed", "value_relation": "empty-or-omitted"}
        if observed == expected:
            relation = "exact"
        elif observed.casefold() == expected.casefold():
            relation = "case-only"
        elif expected.startswith(observed):
            relation = "prefix-only"
        elif expected.endswith(observed):
            relation = "suffix-only"
        else:
            relation = "different"
        return {"status": "observed", "value_relation": relation, "observed_chars": len(observed)}

    def check_mutation_error(
        result: dict, stage: str, plain_text: tuple[str, str] | None = None
    ) -> None:
        if "error" in result:
            code = safe_code(result["error"].get("message", ""))
            if code == "action-readback-not-confirmed" and plain_text is not None:
                readback = observe_unsettled_text(stage, *plain_text)
                fail(stage, code, uncertain=True, readback=readback)
            fail(stage, code, uncertain=code not in PRE_DISPATCH_REJECTIONS)
        if result.get("isError"):
            fail(stage, "tool-returned-isError-post-dispatch", uncertain=True)

    def type_once(stage: str, test_id: str, role: str, value: str) -> dict:
        once(stage)
        result = call(
            "ui_act",
            {"action": "type", "target": {"testId": test_id}, "text": value},
            stage,
            allow_error=True,
        )
        check_mutation_error(result, stage, (test_id, value) if role == "textfield" else None)
        try:
            acted = safe_structured(result)
        except AcceptanceError:
            fail(stage, "tool-result-without-structured-content", uncertain=True)
        if acted.get("settled") is not True:
            fail(stage, "type-did-not-settle", uncertain=True)
        snap = acted.get("snapshot") or {}
        if snap.get("snapshotId", 0) <= last_sequence[0]:
            fail(stage, "type-did-not-advance-sequence", uncertain=True, snapshot_id=snap.get("snapshotId"))
        last_sequence[0] = snap.get("snapshotId", 0)
        node = select(snap.get("nodes") or [], test_id, role, stage)
        if role == "securetextfield":
            # Checked in memory only; the observed document is never retained.
            if "value" in node:
                fail(stage, "secure-field-serialized-a-value", value_key_present=True)
            if value in json.dumps(acted):
                fail(stage, "mock-credential-leaked-into-act-response")
            return {"typed_once": True, "settled": True, "value_key_absent": True}
        if node.get("value") != value:
            fail(stage, "typed-value-did-not-settle", value_observed_matches_typed=False)
        return {"typed_once": True, "settled": True, "value_observed_matches_typed": True}

    def scroll_to_once(stage: str, test_id: str) -> dict:
        """Exactly one bounded reveal scroll per stage. After a credential
        lands the keyboard compresses the login scroll container and clips
        the later controls; one `scroll_to` (direction down: one bounded
        native swipe on the unique containing scroll view) makes the exact
        target visibly hittable before the next interaction. Any rejection
        fails the journey closed; nothing is replayed."""
        once(stage)
        result = call(
            "ui_act",
            {"action": "scroll_to", "target": {"testId": test_id}, "direction": "down"},
            stage,
            allow_error=True,
        )
        check_mutation_error(result, stage)
        try:
            acted = safe_structured(result)
        except AcceptanceError:
            fail(stage, "tool-result-without-structured-content", uncertain=True)
        if acted.get("settled") is not True:
            fail(stage, "scroll-to-did-not-settle", uncertain=True)
        snap = acted.get("snapshot") or {}
        if snap.get("snapshotId", 0) <= last_sequence[0]:
            fail(stage, "scroll-to-did-not-advance-sequence", uncertain=True, snapshot_id=snap.get("snapshotId"))
        last_sequence[0] = snap.get("snapshotId", 0)
        return {
            "scroll_requested_once": True,
            "settled": True,
            "direction": "down",
            "post_scroll_snapshot_id": snap.get("snapshotId"),
        }

    def deep_link_once(stage: str, uri: str) -> dict:
        once(stage)
        result = call("ui_act", {"action": "deep_link", "text": uri}, stage, allow_error=True)
        check_mutation_error(result, stage)
        try:
            acted = safe_structured(result)
        except AcceptanceError:
            fail(stage, "tool-result-without-structured-content", uncertain=True)
        if acted.get("settled") is not True:
            fail(stage, "deep-link-did-not-settle", uncertain=True)
        snap = acted.get("snapshot") or {}
        if snap.get("snapshotId", 0) <= last_sequence[0]:
            fail(stage, "deep-link-did-not-advance-sequence", uncertain=True, snapshot_id=snap.get("snapshotId"))
        last_sequence[0] = snap.get("snapshotId", 0)
        return {
            "uri": uri,
            "accepted_once": True,
            "settled": True,
            "post_action_snapshot_id": snap.get("snapshotId"),
        }

    failure: str | None = None
    try:
        init = exchange(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ios-mcp-app-acceptance", "version": "1"},
            },
            COLD_TIMEOUT,
            "handshake/initialize",
        )
        if init.get("protocolVersion") != PROTOCOL_VERSION:
            fail("handshake/initialize", "protocol-version-not-negotiated")
        client.notify("notifications/initialized")
        listed = exchange("tools/list", {}, REQUEST_TIMEOUT, "handshake/tools-list")
        tools = {tool["name"]: tool for tool in listed.get("tools", [])}
        if set(tools) != set(UI_TOOLS):
            fail("handshake/tools-list", "tool-set-differs-from-frozen-contract", tools=sorted(tools))
        for name in UI_TOOLS:
            if tools[name].get("inputSchema") != frozen_schemas[name]:
                fail("handshake/tools-list", "input-schema-differs-from-frozen-contract", tool=name)

        # The session opens before initialize is answered, so the descriptor
        # is already on stderr; its identity must match the frozen manifest
        # exactly, and the owned simulator must be newly created.
        descriptor = app_session_descriptor(client)
        if descriptor.get("bundleId") != manifest["bundleId"]:
            fail("session/descriptor", "descriptor-bundle-id-differs-from-manifest")
        if descriptor.get("appSha256") != manifest["appSha256"]:
            fail("session/descriptor", "descriptor-app-sha256-differs-from-manifest")
        simulator = descriptor.get("simulator") or {}
        if simulator.get("runtime") != manifest["runtime"] or simulator.get("deviceType") != manifest["deviceType"]:
            fail("session/descriptor", "descriptor-simulator-target-differs-from-manifest")
        udid = simulator.get("udid")
        if not udid:
            fail("session/descriptor", "descriptor-omitted-owned-simulator-udid")
        if udid in pre_udids:
            fail("session/descriptor", "owned-simulator-existed-before-session")
        receipt["session"] = {
            "descriptor_marker": SESSION_MARKER,
            "bundle_id": descriptor.get("bundleId"),
            "app_sha256": descriptor.get("appSha256"),
            "simulator": simulator,
            "bridge_port": descriptor.get("bridgePort"),
            "work_dir": descriptor.get("workDir"),
            "pid": descriptor.get("pid"),
            "simulator_absent_before_session": True,
        }

        # First valid state: bounded read-only polling until both login
        # fields resolve uniquely in one live snapshot.
        email_node, first_snap, first_ids = poll_for(
            "login/first-state", LOGIN_EMAIL, "textfield", FIRST_LOGIN_TIMEOUT
        )
        password_node = select(
            first_snap.get("nodes") or [], LOGIN_PASSWORD, "securetextfield", "login/first-state"
        )
        if "value" in password_node:
            fail("login/first-state", "secure-field-serialized-a-value-before-typing", value_key_present=True)
        first_valid = time.monotonic()
        last_sequence[0] = first_snap.get("snapshotId", 0)
        login = receipt["journey"]["login"] = {"status": "running", "first_valid_poll_ids": first_ids}

        # Each credential is typed exactly once; the values are compared in
        # memory only and never recorded. The keyboard compresses the login
        # scroll container after the email lands, so one bounded reveal
        # scroll makes the exact next control visibly hittable before its
        # single interaction — one scroll per stage, never a retry.
        login["email_input"] = type_once("login/email", LOGIN_EMAIL, "textfield", email)
        login["email_reveal_scroll"] = scroll_to_once("login/email-reveal", LOGIN_PASSWORD)
        login["password_input"] = type_once("login/password", LOGIN_PASSWORD, "securetextfield", password)
        login["password_reveal_scroll"] = scroll_to_once("login/password-reveal", LOGIN_SUBMIT)

        # The submit tap happens once, and only if a fresh read-only snapshot
        # resolves one usable submit button. A native rejection (a visible
        # keyboard or overlay can make the element unhittable) is preserved
        # as a precise reason code; nothing is replayed or bypassed.
        submit_snap = snapshot("login/submit-selection")
        submit_id = submit_snap.get("snapshotId")
        if submit_id is None or submit_id <= last_sequence[0]:
            fail("login/submit-selection", "selection-snapshot-did-not-advance-sequence", uncertain=True, snapshot_id=submit_id)
        last_sequence[0] = submit_id
        select(submit_snap.get("nodes") or [], LOGIN_SUBMIT, "button", "login/submit-selection")
        once("login/submit")
        tap = call(
            "ui_act",
            {"action": "tap", "target": {"testId": LOGIN_SUBMIT}},
            "login/submit",
            allow_error=True,
        )
        check_mutation_error(tap, "login/submit")
        try:
            acted = safe_structured(tap)
        except AcceptanceError:
            fail("login/submit", "tool-result-without-structured-content", uncertain=True)
        if acted.get("settled") is not True:
            fail("login/submit", "submit-tap-did-not-settle", uncertain=True)
        state = acted.get("snapshot") or {}
        if state.get("snapshotId", 0) <= last_sequence[0]:
            fail("login/submit", "submit-tap-did-not-advance-sequence", uncertain=True)
        last_sequence[0] = state.get("snapshotId", 0)
        submit_accepted = time.monotonic()
        login["submit"] = {
            "tapped_once": True,
            "settled": True,
            "selection_snapshot_id": submit_id,
            "post_tap_snapshot_id": state.get("snapshotId"),
        }
        login["status"] = "tap_completed"

        # Actual Home: bounded read-only polling after the accepted submit.
        home_node, home_snap, home_ids = poll_for(
            "home/after-submit", HOME_SEARCH, "button", HOME_AFTER_SUBMIT_TIMEOUT
        )
        home_confirmed = time.monotonic()
        login["status"] = "verified"
        last_sequence[0] = home_snap.get("snapshotId", 0)
        receipt["journey"]["home"] = {
            "status": "verified",
            "home_search_button": {"matched": 1, "role": "button", "enabled": True},
            "poll_snapshot_ids": home_ids,
        }

        # Independent read-only evidence milestone at the confirmed Home
        # state: one screenshot validated in memory; the PNG bytes are
        # never retained or published anywhere.
        screenshot_result = call("ui_screenshot", {"format": "png"}, "home/screenshot-milestone")
        try:
            png, meta = safe_screenshot(screenshot_result)
        except AcceptanceError:
            fail("home/screenshot-milestone", "screenshot-content-unexpected")
        if png_identity(png) is None:
            fail("home/screenshot-milestone", "screenshot-not-a-valid-png")
        if meta.get("snapshotId", 0) <= last_sequence[0]:
            fail("home/screenshot-milestone", "screenshot-did-not-follow-confirmed-state", uncertain=True)
        last_sequence[0] = meta.get("snapshotId", 0)
        screenshot_verified = time.monotonic()
        receipt["readonly_evidence"]["home_screenshot"] = {
            "bytes": len(png),
            "sha256": sha256_bytes(png),
            "dimensions": png_identity(png),
            "snapshot_id": meta.get("snapshotId"),
            "published": False,
        }

        # Actual Search and the return Home, carried only by the exact
        # pinned allowlisted URIs from the frozen manifest.
        search = receipt["journey"]["search"] = {"status": "running"}
        search["deep_link"] = deep_link_once("search/link", PINNED_DEEP_LINKS[0])
        search_link_accepted = time.monotonic()
        search_node, search_snap, search_ids = poll_for(
            "search/after-link", SEARCH_INPUT, "textfield", SCREEN_AFTER_LINK_TIMEOUT
        )
        search_confirmed = time.monotonic()
        last_sequence[0] = search_snap.get("snapshotId", 0)
        search["search_input"] = {"matched": 1, "role": "textfield", "enabled": True}
        search["poll_snapshot_ids"] = search_ids
        search["status"] = "verified"

        home_return = receipt["journey"]["home_return"] = {"status": "running"}
        home_return["deep_link"] = deep_link_once("home-return/link", PINNED_DEEP_LINKS[1])
        home_link_accepted = time.monotonic()
        home_return_node, home_return_snap, home_return_ids = poll_for(
            "home-return/after-link", HOME_SEARCH, "button", SCREEN_AFTER_LINK_TIMEOUT
        )
        home_returned = time.monotonic()
        last_sequence[0] = home_return_snap.get("snapshotId", 0)
        home_return["home_search_button"] = {"matched": 1, "role": "button", "enabled": True}
        home_return["poll_snapshot_ids"] = home_return_ids
        home_return["status"] = "verified"

        # Descriptive single-run milestones; no performance claim. They
        # become performance evidence only behind a fully successful journey
        # and a qualified comparison, which this receipt never asserts.
        receipt["timings"] = {
            "launch_to_first_valid_snapshot": {
                "seconds": round(first_valid - launch_started, 3),
                "boundary": "spawn of the mcp --ios-app child -> first read-only snapshot with both login fields resolving uniquely",
            },
            "submit_accept_to_home_confirmed": {
                "seconds": round(home_confirmed - submit_accepted, 3),
                "boundary": "single submit tap settled -> first read-only snapshot resolving one usable home-search-button",
            },
            "home_confirmed_to_screenshot_identity": {
                "seconds": round(screenshot_verified - home_confirmed, 3),
                "boundary": "Home confirmed by read-only polling -> read-only Home screenshot verified as a valid PNG in memory",
            },
            "search_link_accept_to_search_confirmed": {
                "seconds": round(search_confirmed - search_link_accepted, 3),
                "boundary": "pinned exp+meer://search deep link settled -> first read-only snapshot resolving one usable search-input",
            },
            "home_link_accept_to_home_return_confirmed": {
                "seconds": round(home_returned - home_link_accepted, 3),
                "boundary": "pinned exp+meer://home deep link settled -> first read-only snapshot resolving one usable home-search-button again",
            },
            "note": "descriptive single-run monotonic milestones on this host only; not performance evidence and not a qualified comparison",
        }
        receipt["secrecy"] = {
            "credential_tool_called": False,
            "credential_tools_listed": "none; tools/list returned exactly ui_act, ui_screenshot, ui_snapshot",
            "no_raw_recording": "no raw request, response, snapshot, stdout, stderr or exception text is recorded; every entry is a typed stage status, reason code or scalar",
            "screenshots_published": False,
            "native_diagnostics_unverified": "xcodebuild/xcresult diagnostics retained by Xcode were not scanned and are never asserted to enforce secrecy; only bridge snapshots and this verifier's own evidence are proven redacted",
        }
        receipt["status"] = "tools_verified"
    except AcceptanceError as error:
        failure = scrub_text(str(error), secrets)
    except Exception:
        # Any unexpected local failure is recorded without its text.
        failure = "an unexpected local failure stopped the journey"
        receipt["failure"] = {"stage": "journey", "reason_code": failure, "uncertain": True, "observed": {}, "replayed": False}
    finally:
        # One bounded EOF/teardown path for the child this run owns; the
        # scrubbed stderr never enters the receipt, only the scalar scan.
        # Readback failures are recorded, never raised, so the safe receipt
        # is always produced.
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
        exit_code: int | None = None
        try:
            exit_code = process.wait(timeout=SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=30.0)
            failure = failure or "the server did not exit after EOF within the deadline"
        stderr_clean = not any(secret in client.stderr_text() for secret in secrets)
        receipt["cleanup"] = {
            "process_exit_code": exit_code,
            "stderr_credential_scan_clean": stderr_clean,
        }
        if not stderr_clean:
            failure = failure or "the synthetic mock credentials leaked into server stderr"
        if descriptor is None:
            try:
                descriptor = app_session_descriptor(client)
            except (AcceptanceError, ValueError):
                descriptor = None
        if descriptor is not None:
            try:
                simulator = descriptor["simulator"]
                owned_udid = simulator["udid"]
                if (
                    not owned_udid or owned_udid in pre_udids
                    or descriptor.get("bundleId") != manifest["bundleId"]
                    or descriptor.get("appSha256") != manifest["appSha256"]
                    or simulator.get("runtime") != manifest["runtime"]
                    or simulator.get("deviceType") != manifest["deviceType"]
                ):
                    raise ValueError("the owned session descriptor did not match the manifest")
                work_dir = Path(descriptor["workDir"])
                receipt["cleanup"]["work_dir_removed"] = not work_dir.exists()
                receipt["cleanup"]["simulator_deleted"] = owned_udid not in device_udids()
            except Exception:
                receipt["cleanup"]["readback_failed"] = True
        if failure is None:
            if exit_code != 0:
                failure = f"the server exited with {exit_code} on clean EOF"
            elif descriptor is None:
                failure = "the server never opened the iOS app session"
            elif receipt["cleanup"].get("readback_failed"):
                failure = "the cleanup readback failed; the scalar teardown state is preserved in the receipt"
            elif not receipt["cleanup"].get("simulator_deleted", False):
                failure = "the owned simulator was not deleted on close"
            elif not receipt["cleanup"].get("work_dir_removed", False):
                failure = "the owned work directory was not removed on close"

    receipt["status"] = "failed" if failure is not None else "passed"
    receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
    if failure is not None:
        receipt["error"] = failure
    text = json.dumps(receipt, indent=2, sort_keys=True)
    if any(secret and secret in text for secret in secrets):
        # The suspect receipt is discarded before any write: only a static
        # failure receipt reaches the disk, and the run exits nonzero.
        failure = "the synthetic mock credentials leaked into the sanitized receipt; the suspect receipt was discarded"
        receipt = {
            "schema_version": 1,
            "status": "failed",
            "source_sha": source_sha,
            "error": failure,
            "credentials": {"values_retained": False},
            "evidence_dir": str(output),
        }
        text = json.dumps(receipt, indent=2, sort_keys=True)
    (output / "receipt.json").write_text(text)
    if failure is not None:
        raise AcceptanceError(failure)
    return receipt


def scrub_text(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, CREDENTIAL_PLACEHOLDER)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--binary", type=Path, default=Path("target/debug/ashnazg"))
    parser.add_argument("--manifest", type=Path, required=True, help="frozen Meer app manifest")
    parser.add_argument(
        "--credentials", type=Path, required=True, help="local JSON file with the synthetic mock email and password"
    )
    parser.add_argument("--output-dir", type=Path, required=True, help="empty owned directory for the sanitized receipt")
    arguments = parser.parse_args()
    source_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    try:
        receipt = run_acceptance(
            arguments.binary,
            arguments.manifest,
            arguments.credentials,
            arguments.output_dir,
            source_sha,
        )
    except AcceptanceError as error:
        print(f"ios app mcp acceptance FAILED: {error}", file=sys.stderr)
        return 1
    except Exception as error:
        # Never print a raw exception message or traceback.
        print(f"ios app mcp acceptance FAILED: unexpected local {type(error).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(receipt, indent=2, sort_keys=True))
    print(f"ios app mcp acceptance PASSED; evidence at {arguments.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
