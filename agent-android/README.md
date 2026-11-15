# Android agent v0.1

Build and check the agent APK and native fixture with Java 17 and Android SDK 35:

```sh
./gradlew test lint assembleDebug
```

The APKs are written to `agent/build/outputs/apk/debug/agent-debug.apk` and
`fixture/build/outputs/apk/debug/fixture-debug.apk`. Install both on an API 34+
device or emulator, start the fixture, then forward the guest loopback port:

```sh
export ANDROID_SERIAL=emulator-5554 # choose your test device
adb -s "$ANDROID_SERIAL" install -r fixture/build/outputs/apk/debug/fixture-debug.apk
adb -s "$ANDROID_SERIAL" install -r agent/build/outputs/apk/debug/agent-debug.apk
adb -s "$ANDROID_SERIAL" shell am start -n dev.ashnazg.fixture/.MainActivity
adb -s "$ANDROID_SERIAL" forward --no-rebind tcp:7777 tcp:7777
```

Use a dedicated test device and check that port 7777 has no existing forward before installing. Stop your other inspection tools first: Android permits one UiAutomation owner. Start the persistent instrumentation with a fresh secret token. Supply the same
token at the top level of every newline-delimited JSON-RPC 2.0 request sent to
`127.0.0.1:7777`:

```sh
export ASHNAZG_TOKEN="$(openssl rand -hex 32)"
adb -s "$ANDROID_SERIAL" shell am instrument -w -e token "$ASHNAZG_TOKEN" \
  dev.ashnazg.agent/dev.ashnazg.agent.AgentInstrumentation
```

The instrumentation process stays alive while serving requests. The server
accepts up to four active TCP clients and keeps a bounded queue of four more;
UI operations are serialized. Idle clients expire after 120 seconds and a partial
frame must complete within 30 seconds. It binds only to guest `127.0.0.1`; expose it to a host only with explicit ADB
forwarding. Requests and responses are capped at 8 MiB. Numeric node IDs are
valid only with the snapshot ID that produced them. Password text is omitted
from snapshots, and the exact token is redacted from returned log lines; typing
into a nonempty secure field requires clearing it first.
Android allows one active `UiAutomation` owner at a time. Stop competing Maestro,
Appium, agent-device, or other instrumentation automation sessions before
starting this agent; a conflict exits with a nonzero instrumentation result.

The fixture includes increment, editable and password fields, duplicate text
for ambiguity checks, a long-press target, scrollable rows, a `Scroll target`,
and the `ashnazg://fixture` deep link. Screenshot capture and injected input use
public `UiAutomation` APIs. Back and deep-link actions wait up to five seconds
for a window event and then use a bounded accessibility quiet window. Timing
and zero-copy performance claims are not made by this implementation.

Keep the instrumentation terminal open while the host connects from another
terminal/client using the same token. To stop your own agent and remove its
forward:

```sh
adb -s "$ANDROID_SERIAL" shell am force-stop dev.ashnazg.agent
adb -s "$ANDROID_SERIAL" forward --remove tcp:7777
```

The Python integration/benchmark harness manages this setup and cleanup itself.
