#pragma once

#include <array>
#include <condition_variable>
#include <cstdint>
#include <map>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

namespace ashnazg::hwc {

constexpr uint64_t kPrimaryDisplay = 0;
constexpr uint32_t kIdle1Hz = 0;
constexpr uint32_t kActive60Hz = 1;
constexpr int32_t kHwcErrorNone = 0;
constexpr int32_t kHwcErrorBadConfig = 1;
constexpr int32_t kHwcErrorBadDisplay = 2;
constexpr int32_t kHwcErrorBadLayer = 3;
constexpr int32_t kHwcErrorBadParameter = 4;
constexpr int32_t kHwcErrorHasChanges = 5;
constexpr int32_t kHwcErrorNoResources = 6;
constexpr int32_t kHwcErrorNotValidated = 7;
constexpr int32_t kHwcErrorUnsupported = 8;
constexpr int32_t kCompositionClient = 1;
constexpr int32_t kCompositionDevice = 2;
constexpr int32_t kCompositionSolidColor = 3;
constexpr int32_t kCompositionCursor = 4;
constexpr int32_t kCompositionSideband = 5;
constexpr int64_t kIdlePeriodNanos = 1'000'000'000;
constexpr int64_t kActivePeriodNanos = 16'666'667;
constexpr size_t kTelemetryCapacity = 256;

struct CallbackTelemetryEvent {
  int64_t timestamp_ns = 0;
  uint32_t config_id = 0;
  bool callback_enabled = false;
  uint64_t sequence = 0;
};

struct TelemetrySnapshot {
  std::array<CallbackTelemetryEvent, kTelemetryCapacity> events{};
  uint32_t event_count = 0;
  uint64_t callback_total = 0;
  uint64_t present_successes = 0;
  uint64_t present_timeouts = 0;
  uint32_t selected_config = kActive60Hz;
  bool callback_enabled = false;
};

enum class CallbackKind : uint8_t { kHotplug, kRefresh, kVsync };
using Callback = void (*)(void* callback_data, uint64_t display, int64_t timestamp);

class DisplayState {
 public:
  DisplayState();
  ~DisplayState();
  DisplayState(const DisplayState&) = delete;
  DisplayState& operator=(const DisplayState&) = delete;

  int32_t registerCallback(CallbackKind kind, void* data, Callback callback);
  int32_t setVsyncEnabled(bool enabled);
  int32_t setActiveConfig(uint32_t config);
  uint32_t activeConfig() const;
  int64_t vsyncPeriodNanos() const;
  TelemetrySnapshot telemetrySnapshot() const;

  int32_t createLayer(uint64_t* out_layer);
  int32_t destroyLayer(uint64_t layer);
  int32_t setLayerComposition(uint64_t layer, int32_t composition);
  int32_t validate();
  std::vector<std::pair<uint64_t, int32_t>> changedCompositionTypes() const;
  int32_t acceptChanges();
  bool hasLayer(uint64_t layer) const;
  bool validated() const;
  bool wasValidated() const;

  // The caller retains ownership of acquire_fence. This state object duplicates it.
  int32_t setClientTargetAcquireFence(int acquire_fence, bool target_present = true);
  // Waits at most three seconds for the acquire fence; closes the duplicate on every path.
  int32_t presentClientTarget();

 private:
  struct RegisteredCallback {
    void* data = nullptr;
    Callback callback = nullptr;
  };

  void vsyncLoop();
  static int64_t monotonicNanos();
  static int64_t periodForConfig(uint32_t config);

  mutable std::mutex mutex_;
  std::condition_variable condition_;
  std::map<uint64_t, int32_t> layers_;
  std::map<uint64_t, int32_t> changed_types_;
  RegisteredCallback callbacks_[3];
  uint64_t next_layer_ = 1;
  uint32_t active_config_ = kActive60Hz;
  int client_target_acquire_fence_ = -1;
  bool client_target_present_ = false;
  bool validated_ = false;
  bool validation_performed_ = false;
  bool vsync_enabled_ = false;
  bool stopping_ = false;
  uint32_t callbacks_in_flight_ = 0;
  std::array<CallbackTelemetryEvent, kTelemetryCapacity> telemetry_events_{};
  uint32_t telemetry_next_ = 0;
  uint32_t telemetry_count_ = 0;
  uint64_t callback_total_ = 0;
  uint64_t present_successes_ = 0;
  uint64_t present_timeouts_ = 0;
  std::thread vsync_thread_;
};

}  // namespace ashnazg::hwc
