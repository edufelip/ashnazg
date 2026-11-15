#include "ashnazg/hwc/display_state.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <ctime>
#include <fcntl.h>
#include <poll.h>
#include <unistd.h>

namespace ashnazg::hwc {
namespace {
size_t callbackIndex(CallbackKind kind) {
  return static_cast<size_t>(kind);
}
}  // namespace

DisplayState::DisplayState() : vsync_thread_(&DisplayState::vsyncLoop, this) {}

DisplayState::~DisplayState() {
  {
    std::lock_guard<std::mutex> lock(mutex_);
    stopping_ = true;
    vsync_enabled_ = false;
  }
  condition_.notify_all();
  if (vsync_thread_.joinable()) vsync_thread_.join();
  std::lock_guard<std::mutex> lock(mutex_);
  if (client_target_acquire_fence_ >= 0) close(client_target_acquire_fence_);
}

int32_t DisplayState::registerCallback(CallbackKind kind, void* data, Callback callback) {
  const size_t index = callbackIndex(kind);
  if (index >= 3 || callback == nullptr) return kHwcErrorBadParameter;
  std::unique_lock<std::mutex> lock(mutex_);
  if (stopping_) return kHwcErrorBadDisplay;
  condition_.wait(lock, [this] { return callbacks_in_flight_ == 0 || stopping_; });
  if (stopping_) return kHwcErrorBadDisplay;
  callbacks_[index] = {data, callback};
  condition_.notify_all();
  return kHwcErrorNone;
}

int32_t DisplayState::setVsyncEnabled(bool enabled) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (stopping_) return kHwcErrorBadDisplay;
  vsync_enabled_ = enabled;
  condition_.notify_all();
  return kHwcErrorNone;
}

int32_t DisplayState::setActiveConfig(uint32_t config) {
  if (config != kIdle1Hz && config != kActive60Hz) return kHwcErrorBadConfig;
  std::lock_guard<std::mutex> lock(mutex_);
  if (stopping_) return kHwcErrorBadDisplay;
  active_config_ = config;
  condition_.notify_all();
  return kHwcErrorNone;
}

uint32_t DisplayState::activeConfig() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return active_config_;
}

int64_t DisplayState::vsyncPeriodNanos() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return periodForConfig(active_config_);
}

TelemetrySnapshot DisplayState::telemetrySnapshot() const {
  std::lock_guard<std::mutex> lock(mutex_);
  TelemetrySnapshot snapshot;
  snapshot.event_count = telemetry_count_;
  snapshot.callback_total = callback_total_;
  snapshot.present_successes = present_successes_;
  snapshot.present_timeouts = present_timeouts_;
  snapshot.selected_config = active_config_;
  snapshot.callback_enabled = vsync_enabled_;
  const uint32_t first = telemetry_count_ == kTelemetryCapacity ? telemetry_next_ : 0;
  for (uint32_t i = 0; i < telemetry_count_; ++i) {
    snapshot.events[i] = telemetry_events_[(first + i) % kTelemetryCapacity];
  }
  return snapshot;
}

int32_t DisplayState::createLayer(uint64_t* out_layer) {
  if (out_layer == nullptr) return kHwcErrorBadParameter;
  std::lock_guard<std::mutex> lock(mutex_);
  if (stopping_) return kHwcErrorBadDisplay;
  const uint64_t layer = next_layer_++;
  layers_.emplace(layer, kCompositionClient);
  *out_layer = layer;
  validated_ = false;
  validation_performed_ = false;
  return kHwcErrorNone;
}

int32_t DisplayState::destroyLayer(uint64_t layer) {
  std::lock_guard<std::mutex> lock(mutex_);
  if (layers_.erase(layer) == 0) return kHwcErrorBadLayer;
  changed_types_.erase(layer);
  validated_ = false;
  validation_performed_ = false;
  return kHwcErrorNone;
}

int32_t DisplayState::setLayerComposition(uint64_t layer, int32_t composition) {
  if (composition < kCompositionClient || composition > kCompositionSideband) {
    return kHwcErrorUnsupported;
  }
  std::lock_guard<std::mutex> lock(mutex_);
  auto found = layers_.find(layer);
  if (found == layers_.end()) return kHwcErrorBadLayer;
  if (found->second != composition) {
    found->second = composition;
    validated_ = false;
    validation_performed_ = false;
  }
  return kHwcErrorNone;
}

int32_t DisplayState::validate() {
  std::lock_guard<std::mutex> lock(mutex_);
  changed_types_.clear();
  for (const auto& [layer, composition] : layers_) {
    if (composition != kCompositionClient) changed_types_.emplace(layer, kCompositionClient);
  }
  validation_performed_ = true;
  validated_ = changed_types_.empty();
  return validated_ ? kHwcErrorNone : kHwcErrorHasChanges;
}

std::vector<std::pair<uint64_t, int32_t>> DisplayState::changedCompositionTypes() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return {changed_types_.begin(), changed_types_.end()};
}

int32_t DisplayState::acceptChanges() {
  std::lock_guard<std::mutex> lock(mutex_);
  for (const auto& [layer, composition] : changed_types_) {
    auto found = layers_.find(layer);
    if (found != layers_.end()) found->second = composition;
  }
  changed_types_.clear();
  validated_ = true;
  validation_performed_ = true;
  return kHwcErrorNone;
}

bool DisplayState::hasLayer(uint64_t layer) const {
  std::lock_guard<std::mutex> lock(mutex_);
  return layers_.find(layer) != layers_.end();
}

bool DisplayState::validated() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return validated_;
}

bool DisplayState::wasValidated() const {
  std::lock_guard<std::mutex> lock(mutex_);
  return validation_performed_;
}

int32_t DisplayState::setClientTargetAcquireFence(int acquire_fence, bool target_present) {
  int owned_fence = -1;
  if (acquire_fence >= 0) {
    owned_fence = fcntl(acquire_fence, F_DUPFD_CLOEXEC, 0);
    if (owned_fence < 0) return kHwcErrorNoResources;
  }
  std::lock_guard<std::mutex> lock(mutex_);
  if (stopping_) {
    if (owned_fence >= 0) close(owned_fence);
    return kHwcErrorBadDisplay;
  }
  if (!target_present && owned_fence >= 0) {
    close(owned_fence);
    return kHwcErrorBadParameter;
  }
  if (client_target_acquire_fence_ >= 0) close(client_target_acquire_fence_);
  client_target_acquire_fence_ = owned_fence;
  client_target_present_ = target_present;
  return kHwcErrorNone;
}

int32_t DisplayState::presentClientTarget() {
  int fence = -1;
  {
    std::lock_guard<std::mutex> lock(mutex_);
    if (stopping_) return kHwcErrorBadDisplay;
    const bool has_client_layers = std::any_of(layers_.begin(), layers_.end(), [](const auto& layer) {
      return layer.second == kCompositionClient;
    });
    if (has_client_layers && !client_target_present_) return kHwcErrorBadParameter;
    client_target_present_ = false;
    fence = client_target_acquire_fence_;
    client_target_acquire_fence_ = -1;
  }
  if (fence < 0) {
    std::lock_guard<std::mutex> lock(mutex_);
    ++present_successes_;
    return kHwcErrorNone;
  }
  pollfd descriptor{fence, POLLIN, 0};
  // A broken producer must not block Composer forever. EINTR does not reset
  // the absolute budget, and no successful present is reported on timeout.
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
  int result = 0;
  for (;;) {
    const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
        deadline - std::chrono::steady_clock::now()).count();
    if (remaining <= 0) break;
    result = poll(&descriptor, 1, static_cast<int>(remaining));
    if (result >= 0 || errno != EINTR) break;
  }
  const bool timed_out = result == 0 ||
      (result < 0 && errno == EINTR && std::chrono::steady_clock::now() >= deadline);
  const int saved_errno = timed_out ? ETIMEDOUT : errno;
  close(fence);
  if (result <= 0 || (descriptor.revents & (POLLERR | POLLNVAL)) != 0) {
    if (timed_out) {
      std::lock_guard<std::mutex> lock(mutex_);
      ++present_timeouts_;
    }
    errno = saved_errno;
    return kHwcErrorNoResources;
  }
  {
    std::lock_guard<std::mutex> lock(mutex_);
    ++present_successes_;
  }
  return kHwcErrorNone;
}

void DisplayState::vsyncLoop() {
  std::unique_lock<std::mutex> lock(mutex_);
  int64_t previous_timestamp = 0;
  while (!stopping_) {
    condition_.wait(lock, [this] { return stopping_ || vsync_enabled_; });
    if (stopping_) break;
    const uint32_t config = active_config_;
    const int64_t period = periodForConfig(config);
    auto deadline = std::chrono::steady_clock::now() + std::chrono::nanoseconds(period);
    while (vsync_enabled_ && active_config_ == config && !stopping_) {
      const bool interrupted = condition_.wait_until(lock, deadline, [this, config] {
        return stopping_ || !vsync_enabled_ || active_config_ != config;
      });
      if (stopping_) break;
      if (interrupted) break;
      const RegisteredCallback registered = callbacks_[callbackIndex(CallbackKind::kVsync)];
      if (registered.callback != nullptr) {
        int64_t timestamp = monotonicNanos();
        if (timestamp <= previous_timestamp) timestamp = previous_timestamp + 1;
        previous_timestamp = timestamp;
        ++callback_total_;
        telemetry_events_[telemetry_next_] = {timestamp, config, true, callback_total_};
        telemetry_next_ = (telemetry_next_ + 1) % kTelemetryCapacity;
        telemetry_count_ = std::min(telemetry_count_ + 1,
                                    static_cast<uint32_t>(kTelemetryCapacity));
        ++callbacks_in_flight_;
        lock.unlock();
        registered.callback(registered.data, kPrimaryDisplay, timestamp);
        lock.lock();
        --callbacks_in_flight_;
        condition_.notify_all();
      }
      deadline += std::chrono::nanoseconds(period);
      const auto now = std::chrono::steady_clock::now();
      if (deadline <= now) deadline = now + std::chrono::nanoseconds(period);
    }
  }
}

int64_t DisplayState::monotonicNanos() {
  timespec now{};
  if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
  return static_cast<int64_t>(now.tv_sec) * 1'000'000'000 + now.tv_nsec;
}

int64_t DisplayState::periodForConfig(uint32_t config) {
  return config == kIdle1Hz ? kIdlePeriodNanos : kActivePeriodNanos;
}

}  // namespace ashnazg::hwc
