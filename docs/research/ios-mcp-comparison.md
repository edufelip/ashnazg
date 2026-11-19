# iOS automation comparison

The comparison covers Apple’s Xcode MCP bridge, Ashnazg’s native XCTest bridge, and direct XCTest. Each uses the native iOS Simulator. Keep the fixture checks separate from the Meer mock journey and from performance qualification.

## Run contract

Use one Mac and the same Xcode build, iOS runtime, simulator device type, app build, and mock dataset for all comparable arms. Record source revisions and artifact hashes. Prepare the app and test runner before timing. Use an explicit owned simulator identifier for every command. Start each cold observation with a newly created simulator. For a warm observation, shut down and restart the same simulator with its installed app and retained data. Report the two conditions separately.

| Measurement | Start | End |
| --- | --- | --- |
| Cold or warm first usable snapshot | Start the runtime operation | The first hierarchy with the expected app identifier, enabled target and valid bounds |
| Tap to fresh frame | Submit one action targeting that observed element | The expected semantic change and a newly captured PNG showing the change |
| Process memory | Sample each identified process at the same lifecycle stages | Report the app, simulator and automation helper separately with PID identity and metric |
| Shutdown | Request runtime teardown | The owned simulator is shut down and all owned helpers are gone |

Record tool discovery and builds separately. Count an Apple capture that combines action, hierarchy and PNG through its final validated frame. Count the Ashnazg tap and subsequent screenshot through the equivalent frame. Keep direct XCTest’s analogous assertion and capture as the local baseline. Keep host-wide and shared services outside per-process totals and report their scope separately if observed. A failed operation keeps its elapsed time and failure record. It has no success timing.

For the Meer workload, use the same simulator app, pinned mock API data, and local mock/Metro inputs on every arm. Match the existing iOS Detox auth journey with three observed checkpoints:

1. Sign in through the UI and confirm the mock login response and Home search control.
2. Open Search and wait for the search input.
3. Return by the same Home deep link and verify the Home search control again.

Use a fresh login state for each journey. Confirm typing against observable app state and exclude credentials and secure-field contents from the receipt. Any delayed or ambiguous mutation ends that trial.

## Verified availability

| Environment | Observed |
| --- | --- |
| Linked Mac, macOS 26.5, Xcode 26.6 | No running Xcode process. Apple’s `mcpbridge` exited on `initialize` and reported that it could not find an Xcode process. `xcrun --find mcp-server` exited 72. |
| Hosted macOS 27.0.1, Xcode 27.1 (27A9275) | With Xcode closed, the stock bridge exited before replying. After ordinary `mcp-server enable`, the bridge negotiated MCP 2025-06-18 and listed 53 tools. Unsafe blanket permission was false. The process exited with status 1 after stdin closed. |

On Xcode 27.1 the schema advertises `DeviceInteractionStartSession`, `DeviceInteractionStartWorkspaceSession`, `DeviceInteractionInstallAndRun`, `DeviceInteractionSynthesize`, and `DeviceInteractionEndSession`. The synthesis tool describes coordinate taps, typing, hierarchy capture and screenshots. The schema alone does not establish usable selection, typing or fresh frame evidence. [Hosted inventory](https://github.com/edufelip/ashnazg/actions/runs/44519101548/job/135542788641)

Two direct XCTest fixture cases passed on Xcode 26.6, covering the counter tap, screenshot change, and text entered into a uniquely identified field. Their reported durations include the whole test cases. [Direct XCTest cases](https://github.com/edufelip/ashnazg/actions/runs/44519728136/job/135544972182)

Apple’s documented GUI setup requires access enabled in Xcode Intelligence settings and an open workspace. The linked Mac’s preflight kept its Xcode settings unchanged. Apple’s Xcode 27 requirements start at macOS 26.6. [GUI setup](https://developer.apple.com/documentation/xcode/giving-external-agents-access-to-xcode) · [Xcode 27 release notes](https://developer.apple.com/documentation/xcode-release-notes/xcode-27-release-notes)

With normal headless permission, Apple rejected `DeviceInteractionStartSession` for the unapproved client before the fixture received input. The run confirmed that the owned simulator was removed and the service disabled. The workflow also has an explicit unattended control for a disposable public runner, which explicitly selects Apple’s documented blanket approval option for the fixture call. [Normal-permission fixture result](https://github.com/edufelip/ashnazg/actions/runs/44519728136/job/135544971758)

Ashnazg’s hosted fixture run on Xcode 26.6 passed semantic selection, `Count: 0 → Count: 1`, distinct PNGs, and EOF cleanup of the process, work directory, and simulator. The two retained 960 × 1440 PNGs visibly show the counter change and crop the lower form. Compare the same capture extent in any timed trial. The published fixture legs used separate runners. They provide functional evidence with no matched performance result. [Ashnazg MCP fixture](https://github.com/edufelip/ashnazg/actions/runs/44519728136/job/135544972223)

## Apple unattended fixture result

The isolated hosted run at `ece016b` used Xcode 27.1 and a dedicated iPhone 17 simulator on iOS 27.0. It found `fixture-increment` and `fixture-input` by accessibility identifier with reported bounds and hit points. A tap produced `Count: 1` from `Count: 0`. Text entry returned the exact synthetic marker as the value of `fixture-input`. The three 402 × 874 PNGs captured the full fixture display. Apple reported `applicationState: NotRun` for the externally launched app, so the probe verified the foreground bundle and semantic state separately. Apple accepted EndSession, the harness confirmed simulator deletion, and the service was disabled. [Hosted Apple fixture and artifacts](https://github.com/edufelip/ashnazg/actions/runs/44526579859/job/135568646219)

The approved unattended runner was a disposable GitHub-hosted environment using the public fixture. It establishes the functional Apple tool surface for that profile. A separately approved project session, secure input, the full Meer workflow and matched lifecycle or resource measurements still need their own evidence.

## Qualification gaps

The initial linked Mac preflight found less than 1 GiB free and no simulator app tied to the requested Meer source. After storage was freed, the Mac had 152 GiB available and the runner prepared an isolated source archive pinned to `e6aca586`. The linked Mac still reported macOS 26.5 and Xcode 26.6. This supports a local native baseline, while the verified Apple MCP fixture used Xcode 27.1 on a separate hosted runner.

The existing Ashnazg bridge creates a fresh simulator and builds its fixture before MCP initialization. It deletes the simulator at close. Calls repeated within that session exercise a running bridge. They cannot supply a measurement of restart with retained app data. A matched benchmark needs a prebuilt app and test runner, explicit simulator selection, equivalent restart and shutdown boundaries, and one serial job on a shared host.

The linked Mac preflight compiled and exercised `proc_pid_rusage` with `RUSAGE_INFO_V1` against the macOS 26.5 SDK without elevated privileges. The same read returned the live synthetic process's `ri_phys_footprint` in bytes and its `ri_proc_start_abstime`. The probe rejected an incorrect start identity and an exited process. For live measurements, sample each owned role at the same stage in every arm, check the start identity with each read, record lost or changed processes as missing samples, and report shared CoreSimulator services separately. The preflight did not sample a simulator or app.

Keep the native XCTest bridge as the current Ashnazg fixture backend. Apple’s workspace approval, secure input behavior, and real Meer journey need qualification before changing the backend. Keep direct XCTest as the reference for native interactions.

Two controlled retries exposed separate harness failures. CoreSimulator rejected a device-specific iOS 27.2 runtime for iPhone 17, so the workflow now filters runtimes against the requested device type. In the compatible rerun, a second MCP connection through the already enabled daemon timed out during protocol negotiation. The job still verified that it deleted the simulator and disabled the daemon. The unattended leg now starts a fresh service for its operational test. [Incompatible runtime](https://github.com/edufelip/ashnazg/actions/runs/44521457985) · [Negotiation timeout](https://github.com/edufelip/ashnazg/actions/runs/44522065880)

Another Xcode 27.1 runner offered generic iOS 27.2 as available and device-compatible but could not boot it because the runtime image lacked `launchd_sim`. The unattended control pins the generic iOS 27.0 runtime for this reason. [Runtime-image failure](https://github.com/edufelip/ashnazg/actions/runs/44523900084)

The first hosted Ashnazg text-entry run inserted `Ashnazg`. The fresh readback after a rejected second request showed only `-fixture`, which confirmed that the original text had been replaced. Keeping focus on the verified target across consecutive requests restored the expected `Ashnazg-fixture` value in both direct XCTest and the Ashnazg snapshot. Neither diagnostic run replayed a rejected mutation. [Replacement receipt](https://github.com/edufelip/ashnazg/actions/runs/44527724348/job/135572631169) · [Append receipt](https://github.com/edufelip/ashnazg/actions/runs/44529171783)

At `2f85dad`, hosted direct XCTest and the Ashnazg MCP fixture both passed. The MCP receipt verified semantic tap and a fresh counter PNG, append and empty-text no-op, secure-field value omission in both the action response and the next direct snapshot, rejected invalid operations, and simulator cleanup. This redaction check covers bridge snapshots and owned acceptance artifacts. Native XCTest diagnostics were outside that check. [Ashnazg text-entry acceptance](https://github.com/edufelip/ashnazg/actions/runs/44531797477)

## Linked Mac Meer baseline

The Mac built Meer `e6aca58675085368f1eb21fa92db4446e8581e6b` with an isolated synthetic Firebase configuration and disabled collection and updates. The frozen simulator app has both ARM64 and x86-64 slices, bundle ID `com.edufelip.meer.dev`, SDK 26.5, and Meer tree digest `7e444332948cfff1bc58898e80953bd04e0ee799f7892254166d17ecdb4ceb6f`. The archive and build remained in the Mac scratch directory.

The cold trial timed out before the login form appeared. The second trial reused the warmed Metro transform cache and started a new loopback Metro listener. It reached the mock environment endpoint and received HTTP 200, then failed to show `login-email-input`. The pinned Jest setup ran one retry in that trial. The mock recorded zero login POST requests. Both owned simulators and listeners were removed and the local overlays were restored.

The subsequent single-attempt Detox run used a fresh owned iPhone 17 simulator on iOS 26.5 and prewarmed the bundle in the same loopback Metro process that served the app. The pinned `logs in with email and reaches home` test passed, including its Search and return-to-Home assertions. Jest reported one pass and two skips, and the runner exited zero. The app digest matched before and after the run. The harness verified simulator deletion, closed both listeners, and restored the scratch overlays to their previous hashes. The Mac retains `receipts/visual-diag-receipt.json` and `receipts/diag-auth-ios-classification.json`. The app console relayed a POST to the `auth` endpoint group and HTTP 200 responses in that group. The mock server did not retain a route-level request receipt for the run. The earlier timeout's cause remains undetermined.

## Linked Mac Ashnazg real-app attempt

Ashnazg `3450d23` opened `--ios-app` with the same pinned Meer app on a fresh iPhone 17 simulator on iOS 26.5. The first snapshot resolved both login fields. The independently instrumented mock recorded HTTP 200 for the app's mobile configuration request. The single email entry returned `action-readback-not-confirmed`, and the verifier stopped without a retry. The mock recorded no login request. The verifier exited 1 and confirmed deletion of the simulator and its private work directory. The scratch wrapper initially left mock and Metro listeners running. Ownership was verified and both listeners were closed afterward. The Mac retains `evidence/receipt.json` and `receipts/app-acceptance-receipt.json` in the Ashnazg acceptance scratch directory.

The verifier now takes one read-only snapshot if the plain-text action reports an unconfirmed readback. It records the comparison outcome and observed length without keeping the typed content. The run still ends as a failure with an uncertain action result. The original attempt did not have this diagnostic, so it cannot establish what text reached the field.

A separate fresh run at `2191223` reproduced the failure at the same first action. The read-only snapshot then found one email field with an eight-character value that differed from the requested 13-character input. The receipt kept the classification and length only. The mock recorded one app configuration response with HTTP 200 and no login request. The corrected wrapper closed both owned listeners, and the simulator was absent on a separate device-list check. The app digest and restored overlay digest matched their pre-run values. The Mac retains the two receipts in the `ashnazg-mcp-acc2.z9hBSB7y` scratch directory. This result narrows the failure to native text entry and readback on this app. It leaves the cause of the differing value unresolved.

### Text delivery alternatives

Meer's successful Detox journey invokes `replaceText` on both login fields. The frozen lockfile resolves Detox `20.46.0`. Its app-side iOS implementation resolves the first responder, replaces the UIKit text range in one operation and emits editing callbacks. The Ashnazg route taps the XCTest element and sends one bulk `typeText` call, then requires exact readback. The passing Detox journey proves that whole-value replacement works for this app and test setup. It cannot establish how the XCTest burst lost or changed text. [Pinned Detox implementation](https://github.com/wix/Detox/blob/20.46.0/detox/ios/Detox/Actions/NSObject%2BDetoxActions.m#L565-L624) · [XCTest focus contract](https://developer.apple.com/documentation/xcuiautomation/xcuielement/typetext(_:))

- **Public XCTest candidate.** Resolve one editable target, type its first grapheme, and wait for that exact prefix before sending the remainder once. If a separate paced trial is needed, verify each new prefix before sending the next grapheme. Stop at the first unconfirmed prefix and retain the failed receipt. Callstack's runner already uses a first-character observation and has a paced delivery mode. [Pinned agent-device text entry](https://github.com/callstack/agent-device/blob/v0.21.36/apple/runner/AgentDeviceRunner/AgentDeviceRunnerUITests/RunnerTests%2BTextTyping.swift#L246-L317)
- **Single insertion through Paste.** On a disposable simulator, the visible Paste action into a plain field would exercise one insertion while preserving the app's editing path. Use a synthetic marker and exact native readback. Any temporary clipboard content needs cleanup. This candidate is unverified for Meer and would need separate evidence for selection and editing callbacks. [UIKit pasteboard](https://developer.apple.com/documentation/uikit/uipasteboard)
- **Alternate runner.** The installed agent-device version also has a target-process XCTest text synthesis route. Record the actual route on a live run because pacing alone does not identify it. Retain it as an opt-in simulator experiment if needed. A private XCTest interface introduces version and capability dependencies that a public XCTest implementation avoids. [Pinned agent-device synthesis](https://github.com/callstack/agent-device/blob/v0.21.36/apple/runner/AgentDeviceRunner/AgentDeviceRunnerUITests/RunnerSynthesizedTextEntry.m)

Measure exact field readback on a fresh app launch for every test arm. Keep plain email-field probes and secure-field evidence separate. A typing workaround must also pass the full mock login with the intended app-side value and one observed login request before it can qualify a Meer journey.

The agent-device default-versus-paced probe stopped before opening the app. Twice, the Device panel could not find the owned simulator while it was shut down, although CoreSimulator listed it. The panel discovered it after a lifecycle boot. No text-entry arm ran. The app digest stayed pinned, both disposable simulators created during setup were deleted, both owned listeners closed, and the scratch Metro overlay was restored by hash.

### Prebooted Mac text probes

A later run booted the owned iPhone 17 simulator before discovery and verified the frozen Meer app and the loopback services. With agent-device, default `type` returned an empty field and `TEXT_ENTRY_MISMATCH`. `type --delay-ms 80` returned the exact 29-character synthetic marker. The default arm also dismissed a development-menu overlay. The two arms therefore establish the observed outcomes with that setup difference and cannot isolate pacing by themselves.

The Ashnazg follow-up used separate fresh simulators, the same pinned app and marker, and the same bridge with two fixture source copies. The bulk XCTest control returned 22 of 29 characters and failed settlement. The first-character observation candidate returned all 29 in both the action response and an independent snapshot. It sent the remainder once after reading the expected first character. The successful arm took 56.7 seconds from dispatch through confirmation. The failed control took 14.7 seconds through rejection. These are individual diagnostic timings with different outcomes. Both simulators and their owned services were removed. The Mac retains `receipts/gate3-arm1.json` and `receipts/gate3-arm2.json` in the `ashnazg-mcp-acc2.z9hBSB7y` scratch directory.

The Paste probe confirmed an exact marker in the disposable fixture's plain text field. It did not exercise Meer's login form. The probe restored the Mac clipboard's plain-text value by hash. It did not preserve any earlier rich clipboard formats.

### Full journey with observed-prefix typing

The full mock journey used the same pinned Meer app and the candidate Swift bridge. Opening the app and reading back the email succeeded. The password operation stopped before dispatch with `login/password: target-not-enabled-hittable`. No password typing, login submission, Search, or Home frame was observed. The mock logged zero login requests. The verifier confirmed deletion of its simulator and work directory, both service ports were closed, the Metro overlay was restored, and the app digest stayed pinned. The Mac retains `receipts/gate5-journey-receipt.json` in the `ashnazg-mcp-acc2.z9hBSB7y` scratch directory.

Meer's pinned Detox flow dismisses the keyboard after each field and scrolls `login-scroll` to expose the submit button. The Ashnazg attempt did not perform those steps. The native rejection establishes that the password target failed the enabled-or-hittable check. The specific obstruction needs a live layout observation before choosing how to reach the field.

The subsequent layout probe verified the password field was empty and visible on a fresh launch. After a synthetic email was confirmed, the keyboard reduced the scroll viewport to y 62–539 points. The password field started at its clipped edge, and the enabled submit button was below it. One upward swipe on `login-scroll` moved the password to y 240–287 and submit to y 353–402, both above the keyboard. The probe typed no password and submitted no login. Its two screenshots remain in the owned Mac scratch directory `ashnazg-g5-lt1-888714971/shots/`. The worker closed a stale Device session before opening its own. The owned simulator and listeners were removed and the app and overlay hashes matched on teardown.

### Full journey with native scroll reveal

The linked Mac built `f8c4b8e` and ran one trial against the same pinned Meer app. The verifier requested one reveal before password entry and one before selecting Submit. The native log recorded one scroll-view swipe. The email matched exactly, and the password field passed the native mask-length check without exposing its value. The unique Submit target passed XCTest's enabled and hittable checks, and its one tap returned an advancing snapshot.

The independent mock recorded zero login POSTs. Six fresh snapshots failed to find a usable Home search control within the 240-second observation window. The verifier exited 1 after 954.8 seconds, and the run stopped before Search or any Home capture. The native trace logged roughly 10,000 element queries during the run. That count motivates measuring a batched accessibility snapshot; this failed run supplies no successful latency sample.

The Mac retains `ashnazg-g6-trial-28069/evidence/receipt.json`. The verifier and harness confirmed that the simulator and bridge work directory were removed, the two owned ports closed, the synthetic input file was removed, and the app and Expo overlay matched their original digests. The scratch verifier added a final Home capture check and accepted the pinned source identity from the archive wrapper. The bridge used the archived candidate Swift and Rust sources.

The verifier now records `tap_completed` until it observes Home. If Home times out, it retains only allowlisted login selector IDs and the email comparison outcome from the last snapshot. This adds evidence for field validation and app errors without retaining labels or values. The cause of zero login POSTs remains open.

### Batched native accessibility capture

The next isolated Mac trial used `1b727eb` and the same pinned Meer app. It replaced the per-element bridge queries with one `XCUIApplication.snapshot()` per response and used the same action sequence with fresh synthetic inputs. The email read back exactly and the secure field returned mask-only evidence. The mock recorded exactly one login POST with HTTP 200. The verifier observed Home and captured a fresh 1206 × 2622 PNG, which it validated in memory.

The first login snapshot took 29.275 seconds with the per-element reader and 0.163 seconds with the batched reader. The pre-submit snapshot took 40.799 and 0.171 seconds, respectively. These were two diagnostic runs with different outcomes. The receipt timing covers each stdio request and response.

The subsequent `exp+meer://search` action returned an advancing snapshot while the app was in the foreground. The Search input did not appear in 169 fresh polls over 180 seconds. The verifier stopped without replay, exited 1, and did not issue the Home return action. The app-side URL delivery remains unverified. The Mac retains `ashnazg-g7-trial-33713/evidence/receipt.json`. The simulator and bridge work directory were removed, the owned ports closed, and the app and overlay digests matched. The archive verifier again added a pinned source identity and a check for a final Home PNG, which the run never reached.

Detox sends `openURL` through its in-app client. The Ashnazg candidate used `XCUIApplication.open`. The next candidate sends the same allowlisted URL via the native system URL handler and records the last observed Home, Search and login selector IDs if navigation times out. [Pinned Detox dispatch](https://github.com/wix/Detox/blob/20.46.0/detox/src/devices/runtime/RuntimeDevice.js#L266-L271) · [Apple system URL API](https://developer.apple.com/documentation/xcuiautomation/xcuisystem/open(_:))

The system-handler trial at `3051528` again reached Home with one successful login POST and a valid 1206 × 2622 PNG. The single `exp+meer://search` request returned an advancing snapshot, but Search timed out after 169 polls in 180 seconds. The final snapshot contained none of the four allowlisted IDs (`login-email-input`, `home-search-button`, `search-input`, `search-back-button`). It could not establish the post-link screen or whether the app processed the URL. No Home return action was sent. The receipt in `ashnazg-g8-trial-38558/evidence/receipt.json` records a failure and verified cleanup. The two snapshot calls before submit took 0.285 and 0.169 seconds.

### Direct XCTest timing attachment

The hosted fixture at `0313b4b` recorded 14.900 seconds from `app.launch()` invocation through verified initial controls, and 2.045 seconds from one tap through the counter assertion and capture validation. Both app-scoped PNGs were 960 × 1440. The intervals came from the native monotonic attachment in the passing XCTest result. These are one fixture's observations. The matched comparison still needs the same host, workload and capture extent for all arms. [Hosted XCTest result](https://github.com/edufelip/ashnazg/actions/runs/44620413260/job/135900249617)
