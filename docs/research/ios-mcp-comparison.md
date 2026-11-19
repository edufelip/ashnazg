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

The linked Mac preflight found less than 1 GiB free on its writable Data volume, no alternate writable volume, and no simulator app tied to the requested Meer source. The two available builds reported app version 1.12.0. Matching a version string would still be insufficient to bind a build to a particular app source tree. The current Meer source also requires installing its JavaScript dependencies and CocoaPods before a fresh simulator build.

The existing Ashnazg bridge creates a fresh simulator and builds its fixture before MCP initialization. It deletes the simulator at close. Calls repeated within that session exercise a running bridge. They cannot supply a measurement of restart with retained app data. A matched benchmark needs a prebuilt app and test runner, explicit simulator selection, equivalent restart and shutdown boundaries, and one serial job on a shared host.

Keep the native XCTest bridge as the current Ashnazg fixture backend. Apple’s workspace approval, secure input behavior, and real Meer journey need qualification before changing the backend. Keep direct XCTest as the reference for native interactions.

Two controlled retries exposed separate harness failures. CoreSimulator rejected a device-specific iOS 27.2 runtime for iPhone 17, so the workflow now filters runtimes against the requested device type. In the compatible rerun, a second MCP connection through the already enabled daemon timed out during protocol negotiation. The job still verified that it deleted the simulator and disabled the daemon. The unattended leg now starts a fresh service for its operational test. [Incompatible runtime](https://github.com/edufelip/ashnazg/actions/runs/44521457985) · [Negotiation timeout](https://github.com/edufelip/ashnazg/actions/runs/44522065880)

Another Xcode 27.1 runner offered generic iOS 27.2 as available and device-compatible but could not boot it because the runtime image lacked `launchd_sim`. The unattended control pins the generic iOS 27.0 runtime for this reason. [Runtime-image failure](https://github.com/edufelip/ashnazg/actions/runs/44523900084)

The first hosted Ashnazg text-entry run observed `Ashnazg` in the correct text field. Its second type call returned an unsettled result and the run verified teardown. The verifier now takes a fresh readback on that failure without replaying the action. [Initial native text-entry run](https://github.com/edufelip/ashnazg/actions/runs/44525683713/job/135565509064)
