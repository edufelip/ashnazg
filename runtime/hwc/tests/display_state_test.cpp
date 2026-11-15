#include "ashnazg/hwc/display_state.h"

#include <array>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <fcntl.h>
#include <poll.h>
#include <thread>
#include <unistd.h>

namespace {
using namespace std::chrono_literals;
using ashnazg::hwc::CallbackKind;
using ashnazg::hwc::DisplayState;
using ashnazg::hwc::kActive60Hz;
using ashnazg::hwc::kIdle1Hz;
using ashnazg::hwc::kHwcErrorNone;
using ashnazg::hwc::kHwcErrorHasChanges;
using ashnazg::hwc::kTelemetryCapacity;

#define REQUIRE(condition) do { \
  if (!(condition)) { \
    std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
    return false; \
  } \
} while (false)

struct VsyncCapture {
  std::atomic<uint32_t> calls{0};
  std::atomic<int64_t> previous{0};
  std::atomic<bool> monotonic{true};
  std::atomic<int64_t> expected_period{0};
};

void onVsync(void* opaque, uint64_t display, int64_t timestamp) {
  auto& capture = *static_cast<VsyncCapture*>(opaque);
  const int64_t prior = capture.previous.exchange(timestamp);
  if (timestamp <= prior) capture.monotonic = false;
  capture.calls++;
  (void)display;
}

bool ConfigSwitchControlsVsyncPeriod() {
  DisplayState display;
  REQUIRE(display.activeConfig() == kActive60Hz);
  REQUIRE(display.vsyncPeriodNanos() == 16'666'667);
  REQUIRE(display.setActiveConfig(kIdle1Hz) == kHwcErrorNone);
  REQUIRE(display.activeConfig() == kIdle1Hz);
  REQUIRE(display.vsyncPeriodNanos() == 1'000'000'000);
  REQUIRE(display.setActiveConfig(99) != kHwcErrorNone);
  REQUIRE(display.activeConfig() == kIdle1Hz);
  return true;
}

bool TelemetryIsBoundedAndAttributesCallbacksToSelectedConfig() {
  DisplayState display;
  VsyncCapture capture;
  REQUIRE(display.registerCallback(CallbackKind::kVsync, &capture, onVsync) == kHwcErrorNone);
  REQUIRE(display.setActiveConfig(kActive60Hz) == kHwcErrorNone);
  REQUIRE(display.setVsyncEnabled(true) == kHwcErrorNone);
  const auto deadline = std::chrono::steady_clock::now() + 300ms;
  while (capture.calls.load() < 3 && std::chrono::steady_clock::now() < deadline) {
    std::this_thread::sleep_for(2ms);
  }
  REQUIRE(display.setVsyncEnabled(false) == kHwcErrorNone);
  auto telemetry = display.telemetrySnapshot();
  REQUIRE(telemetry.callback_enabled == false);
  REQUIRE(telemetry.selected_config == kActive60Hz);
  REQUIRE(telemetry.callback_total >= 3);
  REQUIRE(telemetry.event_count <= telemetry.events.size());
  REQUIRE(telemetry.event_count >= 3);
  for (uint32_t i = 0; i < telemetry.event_count; ++i) {
    REQUIRE(telemetry.events[i].timestamp_ns > 0);
    REQUIRE(telemetry.events[i].config_id == kActive60Hz);
    REQUIRE(telemetry.events[i].callback_enabled);
  }
  REQUIRE(display.setActiveConfig(kIdle1Hz) == kHwcErrorNone);
  REQUIRE(display.setVsyncEnabled(true) == kHwcErrorNone);
  const auto idle_deadline = std::chrono::steady_clock::now() + 1200ms;
  while (display.telemetrySnapshot().callback_total == telemetry.callback_total &&
         std::chrono::steady_clock::now() < idle_deadline) {
    std::this_thread::sleep_for(5ms);
  }
  REQUIRE(display.setVsyncEnabled(false) == kHwcErrorNone);
  telemetry = display.telemetrySnapshot();
  REQUIRE(telemetry.event_count <= kTelemetryCapacity);
  REQUIRE(telemetry.callback_total > 3);
  REQUIRE(telemetry.events[telemetry.event_count - 1].config_id == kIdle1Hz);
  return true;
}

bool TelemetryRingRemainsFixedSizeAfterMoreThanCapacityCallbacks() {
  DisplayState display;
  VsyncCapture capture;
  REQUIRE(display.registerCallback(CallbackKind::kVsync, &capture, onVsync) == kHwcErrorNone);
  REQUIRE(display.setVsyncEnabled(true) == kHwcErrorNone);
  const auto deadline = std::chrono::steady_clock::now() + 6s;
  while (capture.calls.load() <= kTelemetryCapacity && std::chrono::steady_clock::now() < deadline) {
    std::this_thread::sleep_for(5ms);
  }
  REQUIRE(display.setVsyncEnabled(false) == kHwcErrorNone);
  const auto telemetry = display.telemetrySnapshot();
  REQUIRE(telemetry.callback_total > kTelemetryCapacity);
  REQUIRE(telemetry.event_count == kTelemetryCapacity);
  for (uint32_t i = 1; i < telemetry.event_count; ++i) {
    REQUIRE(telemetry.events[i].timestamp_ns > telemetry.events[i - 1].timestamp_ns);
  }
  return true;
}

bool TelemetryCountsSuccessfulPresents() {
  DisplayState display;
  REQUIRE(display.presentClientTarget() == kHwcErrorNone);
  const auto telemetry = display.telemetrySnapshot();
  REQUIRE(telemetry.present_successes == 1);
  REQUIRE(telemetry.present_timeouts == 0);
  return true;
}

bool ValidationRequestsClientCompositionForEveryLayer() {
  DisplayState display;
  uint64_t first = 0;
  uint64_t second = 0;
  REQUIRE(display.createLayer(&first) == kHwcErrorNone);
  REQUIRE(display.createLayer(&second) == kHwcErrorNone);
  REQUIRE(display.setLayerComposition(first, ashnazg::hwc::kCompositionDevice) == kHwcErrorNone);
  REQUIRE(display.setLayerComposition(second, ashnazg::hwc::kCompositionSolidColor) == kHwcErrorNone);
  uint64_t cursor = 0;
  REQUIRE(display.createLayer(&cursor) == kHwcErrorNone);
  REQUIRE(display.setLayerComposition(cursor, ashnazg::hwc::kCompositionSideband) == kHwcErrorNone);
  REQUIRE(display.validate() == kHwcErrorHasChanges);
  auto changes = display.changedCompositionTypes();
  REQUIRE(changes.size() == 3);
  REQUIRE(changes[0].first == first && changes[0].second == ashnazg::hwc::kCompositionClient);
  REQUIRE(changes[1].first == second && changes[1].second == ashnazg::hwc::kCompositionClient);
  REQUIRE(changes[2].first == cursor && changes[2].second == ashnazg::hwc::kCompositionClient);
  REQUIRE(display.acceptChanges() == kHwcErrorNone);
  REQUIRE(display.validate() == kHwcErrorNone);
  REQUIRE(display.presentClientTarget() != kHwcErrorNone);
  REQUIRE(display.setClientTargetAcquireFence(-1) == kHwcErrorNone);
  REQUIRE(display.presentClientTarget() == kHwcErrorNone);
  REQUIRE(display.destroyLayer(first) == kHwcErrorNone);
  REQUIRE(display.destroyLayer(second) == kHwcErrorNone);
  REQUIRE(display.destroyLayer(cursor) == kHwcErrorNone);
  return true;
}

bool ClientTargetAcquireFenceIsDuplicatedWaitedAndClosed() {
  DisplayState display;
  std::array<int, 2> pipe_fds{};
  REQUIRE(pipe(pipe_fds.data()) == 0);
  const int caller_acquire = pipe_fds[0];
  const int signal_write = pipe_fds[1];
  REQUIRE(display.setClientTargetAcquireFence(caller_acquire) == kHwcErrorNone);
  REQUIRE(close(caller_acquire) == 0);
  std::atomic<bool> returned{false};
  std::thread present([&] {
    returned = display.presentClientTarget() == kHwcErrorNone;
  });
  std::this_thread::sleep_for(40ms);
  REQUIRE(!returned.load());
  const char signal = 'x';
  REQUIRE(write(signal_write, &signal, sizeof(signal)) == 1);
  present.join();
  REQUIRE(returned.load());
  REQUIRE(close(signal_write) == 0);
  return true;
}

bool UnsignaledClientFenceTimesOutAndClosesDuplicate() {
  std::signal(SIGPIPE, SIG_IGN);
  DisplayState display;
  std::array<int, 2> pipe_fds{};
  REQUIRE(pipe(pipe_fds.data()) == 0);
  REQUIRE(display.setClientTargetAcquireFence(pipe_fds[0]) == kHwcErrorNone);
  REQUIRE(close(pipe_fds[0]) == 0);
  const auto started = std::chrono::steady_clock::now();
  REQUIRE(display.presentClientTarget() == ashnazg::hwc::kHwcErrorNoResources);
  REQUIRE(display.telemetrySnapshot().present_timeouts == 1);
  REQUIRE(std::chrono::steady_clock::now() - started < 5s);
  const char byte = 'x';
  REQUIRE(write(pipe_fds[1], &byte, sizeof(byte)) == -1);
  REQUIRE(errno == EPIPE);
  REQUIRE(close(pipe_fds[1]) == 0);
  REQUIRE(display.setClientTargetAcquireFence(-1) == kHwcErrorNone);
  REQUIRE(display.presentClientTarget() == kHwcErrorNone);
  REQUIRE(display.telemetrySnapshot().present_successes == 1);
  return true;
}

bool VsyncUsesEnabledConfigPeriodAndMonotonicClock() {
  DisplayState display;
  VsyncCapture capture;
  REQUIRE(display.registerCallback(CallbackKind::kVsync, &capture, onVsync) == kHwcErrorNone);
  REQUIRE(display.setVsyncEnabled(true) == kHwcErrorNone);
  const auto deadline = std::chrono::steady_clock::now() + 500ms;
  while (capture.calls.load() < 2 && std::chrono::steady_clock::now() < deadline) {
    std::this_thread::sleep_for(2ms);
  }
  REQUIRE(capture.calls.load() >= 2);
  REQUIRE(capture.monotonic.load());
  REQUIRE(display.setVsyncEnabled(false) == kHwcErrorNone);
  const auto stopped_at = capture.calls.load();
  std::this_thread::sleep_for(50ms);
  REQUIRE(capture.calls.load() == stopped_at);
  REQUIRE(display.setActiveConfig(kIdle1Hz) == kHwcErrorNone);
  REQUIRE(display.vsyncPeriodNanos() == 1'000'000'000);
  return true;
}



bool SelectedConfigChangesVsyncCadence() {
  DisplayState display;
  VsyncCapture capture;
  REQUIRE(display.registerCallback(CallbackKind::kVsync, &capture, onVsync) == kHwcErrorNone);
  REQUIRE(display.setVsyncEnabled(true) == kHwcErrorNone);
  const auto active_deadline = std::chrono::steady_clock::now() + 400ms;
  while (capture.calls.load() == 0 && std::chrono::steady_clock::now() < active_deadline) {
    std::this_thread::sleep_for(2ms);
  }
  REQUIRE(capture.calls.load() > 0);
  REQUIRE(display.setActiveConfig(kIdle1Hz) == kHwcErrorNone);
  const auto count_at_idle_entry = capture.calls.load();
  std::this_thread::sleep_for(180ms);
  REQUIRE(capture.calls.load() == count_at_idle_entry);
  const auto idle_count = capture.calls.load();
  REQUIRE(display.setActiveConfig(kActive60Hz) == kHwcErrorNone);
  const auto resumed_deadline = std::chrono::steady_clock::now() + 200ms;
  while (capture.calls.load() == idle_count && std::chrono::steady_clock::now() < resumed_deadline) {
    std::this_thread::sleep_for(2ms);
  }
  REQUIRE(capture.calls.load() > idle_count);
  REQUIRE(display.setVsyncEnabled(false) == kHwcErrorNone);
  return true;
}

void onVsyncCount(void* opaque, uint64_t, int64_t) {
  static_cast<std::atomic<uint32_t>*>(opaque)->fetch_add(1);
}

bool CallbackReplacementWaitsForPriorInvocation() {
  DisplayState display;
  std::atomic<uint32_t> first{0};
  std::atomic<uint32_t> second{0};
  REQUIRE(display.registerCallback(CallbackKind::kVsync, &first, onVsyncCount) == kHwcErrorNone);
  REQUIRE(display.setVsyncEnabled(true) == kHwcErrorNone);
  const auto first_deadline = std::chrono::steady_clock::now() + 500ms;
  while (first.load() == 0 && std::chrono::steady_clock::now() < first_deadline) {
    std::this_thread::sleep_for(2ms);
  }
  REQUIRE(first.load() > 0);
  REQUIRE(display.registerCallback(CallbackKind::kVsync, &second, onVsyncCount) == kHwcErrorNone);
  const auto replaced_at = first.load();
  const auto second_deadline = std::chrono::steady_clock::now() + 250ms;
  while (second.load() == 0 && std::chrono::steady_clock::now() < second_deadline) {
    std::this_thread::sleep_for(2ms);
  }
  REQUIRE(second.load() > 0);
  REQUIRE(first.load() == replaced_at);
  return true;
}

bool TeardownStopsCallbacksAndReleasesPendingFence() {
  std::signal(SIGPIPE, SIG_IGN);
  VsyncCapture capture;
  std::array<int, 2> pipe_fds{};
  REQUIRE(pipe(pipe_fds.data()) == 0);
  const int writer = pipe_fds[1];
  {
    DisplayState display;
    REQUIRE(display.registerCallback(CallbackKind::kVsync, &capture, onVsync) == kHwcErrorNone);
    REQUIRE(display.setVsyncEnabled(true) == kHwcErrorNone);
    REQUIRE(display.setClientTargetAcquireFence(pipe_fds[0]) == kHwcErrorNone);
    REQUIRE(close(pipe_fds[0]) == 0);
    const auto deadline = std::chrono::steady_clock::now() + 200ms;
    while (capture.calls.load() == 0 && std::chrono::steady_clock::now() < deadline) {
      std::this_thread::sleep_for(1ms);
    }
    REQUIRE(capture.calls.load() > 0);
  }
  const auto stopped_at = capture.calls.load();
  std::this_thread::sleep_for(40ms);
  REQUIRE(capture.calls.load() == stopped_at);
  const char byte = 'x';
  REQUIRE(write(writer, &byte, sizeof(byte)) == -1);
  REQUIRE(errno == EPIPE);
  REQUIRE(close(writer) == 0);
  return true;
}

}  // namespace

int main() {
  const bool ok = ConfigSwitchControlsVsyncPeriod() &&
      TelemetryIsBoundedAndAttributesCallbacksToSelectedConfig() &&
      TelemetryRingRemainsFixedSizeAfterMoreThanCapacityCallbacks() &&
      TelemetryCountsSuccessfulPresents() &&
      ValidationRequestsClientCompositionForEveryLayer() &&
      ClientTargetAcquireFenceIsDuplicatedWaitedAndClosed() &&
      UnsignaledClientFenceTimesOutAndClosesDuplicate() &&
      VsyncUsesEnabledConfigPeriodAndMonotonicClock() &&
      SelectedConfigChangesVsyncCadence() &&
      CallbackReplacementWaitsForPriorInvocation() &&
      TeardownStopsCallbacksAndReleasesPendingFence();
  return ok ? 0 : 1;
}
