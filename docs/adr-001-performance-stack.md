# ADR 001: Native host with a thin Android framework bridge

Status: accepted for v0.1, 2026-11-15.

The user prioritizes runtime performance over familiarity with a language. The specification names Kotlin and TypeScript/Go as suggestions, rather than requirements. No language is universally fastest; our choice must remove measured bottlenecks without inventing benchmark results.

## Decision

Use Rust for the host: native compilation, no tracing garbage collector, explicit ownership and bounded I/O. Use the official Rust MCP SDK for protocol negotiation, tools, stdio and Streamable HTTP. The Tokio executor handles transport; a single serialized guest connection avoids concurrent UiAutomation access and ambiguous input ordering.

Use Java 17 in a small persistent Android instrumentation process. Android accessibility, windows and input control are framework/Binder APIs. Reimplementing that boundary through C++/JNI would add a transition without eliminating the Binder operations. Java and Kotlin both run on ART; the thin Java bridge avoids extra runtime dependencies. Reserve C/C++ or Rust Android NDK components for future measured rendering/encoding bottlenecks that the public framework cannot address.

The performance mechanism in v0.1 is keeping the process and socket alive, walking accessibility nodes in memory and avoiding XML/files/process launch on each action. This is an architectural decision, not proof of a speedup or the claim that Rust beats all equivalent C/C++ implementations.

## Corrections to the initial blueprint

- The Android agent is instrumentation with UiAutomation privileges, not an ordinary APK magically running as UID 2000. It must be explicitly installed and started for the selected device.
- Retrieval flags belong to AccessibilityServiceInfo; they are not UiAutomation constants. Compose tags require the app to expose them through `testTagsAsResourceId`. `FLAG_INCLUDE_NOT_IMPORTANT_VIEWS` does not manufacture semantics omitted by the app.
- Capture uses public UiAutomation.takeScreenshot. The bitmap is copied/encoded and may be copied again for overlay composition. There is no zero-copy claim, hidden SurfaceControl dependency or custom HWC governor in v0.1.
- A 120 ms quiet window precludes 25 ms end-to-end settled actions. Report RPC/encoding samples separately from application settling; a bounded quiet window is not proof that asynchronous network activity or all rendering has finished.
- Streamable HTTP replaces the obsolete standalone SSE transport. Stdio remains the simplest local interface. The HTTP endpoint and guest socket bind loopback and require explicit tokens.
- Runtime boot/RAM/idle-CPU targets and custom kernel/container work belong to later milestones. v0.1 uses an existing emulator/device and benchmarks it as such.
- Android flow success does not validate iOS behavior. Mach-O compatibility, modern iOS ABI/framework/runtime support and Apple's redistribution constraints are unresolved research, not a solved product capability.
- Shared hosted runners introduce noise. Raw samples, device/host metadata and percentile summaries are required; performance is not a hard threshold until a representative repeatable baseline exists.

## Evidence and references

- [Official Rust MCP SDK](https://github.com/modelcontextprotocol/rust-sdk): supported transports and negotiation.
- [Android UiAutomation](https://developer.android.com/reference/android/app/UiAutomation): public input injection, window retrieval and screenshot APIs.
- [AccessibilityServiceInfo](https://developer.android.com/reference/android/accessibilityservice/AccessibilityServiceInfo): retrieval flags.
- [Compose interoperability with UiAutomator](https://developer.android.com/develop/ui/compose/testing/interoperability#uiautomator-interop): opt-in test-tag exposure.

These references establish API behavior, not achieved Ashnazg performance.
