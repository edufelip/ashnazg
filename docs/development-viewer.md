# Development device viewer

On Linux, Ashnazg's external session can remain owned by the MCP/runtime while a separate, externally installed Android platform-tools `adb` connection and `scrcpy` show a physical device or emulator. This helper never starts, stops, or attaches to the MCP runtime. Select the exact ADB serial every time; it does not use `ANDROID_SERIAL` or choose a default device.

Install `adb` (Android SDK platform-tools) and `scrcpy` using your Linux distribution's supported package source. Connect and authorize the device, then inspect `adb devices -l` and copy the exact serial. Run:

```sh
python3 scripts/dev_viewer.py --serial 'YOUR_SERIAL'
```

The helper checks Linux, both executable versions, and that the selected serial is in the `device` state before opening scrcpy. Multiple attached devices are fine because both commands name the selected serial. USB reconnects can change a device's serial/state: close the viewer, reconnect/reauthorize, inspect `adb devices -l` again, and rerun with the current exact serial. The helper does not retry or select another device.

For a host development server, use the [real-app preparation helper](real-app-development.md) with `--server-port 8081`. It uses ADB **reverse** so device localhost reaches the host server. The viewer creates no additional mappings and never removes runtime or developer-server mappings.

## macOS boundary

This workflow is currently supported only on Linux hosts with external ADB. Ashnazg's native Apple Silicon macOS VM/MCP session uses its owned loopback ADB relay internally and intentionally runs without host ADB (`ASHNAZG_ADB` is unavailable in its acceptance environment). Its guest serial is not automatically exposed to macOS platform-tools. Installing scrcpy on macOS alone therefore does not make this workflow work for that native session. Use the MCP tools for that session, or connect a separately addressable Android device to a Linux host and run this helper there. No native macOS ADB bridge or scrcpy integration is claimed.
