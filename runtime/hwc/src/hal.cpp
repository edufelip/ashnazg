#include "ashnazg/hwc/aosp_hwc2_abi.h"
#include "ashnazg/hwc/display_state.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstring>
#include <memory>
#include <new>
#include <cerrno>
#include <mutex>
#include <string>
#include <type_traits>
#include <vector>

namespace ashnazg::hwc {
namespace {
using namespace abi;
constexpr uint32_t kDisplayWidth = 720;
constexpr uint32_t kDisplayHeight = 1280;
constexpr int32_t kDpiMilli = 320'000;
constexpr int32_t kErrorNone = kHwcErrorNone;
constexpr int32_t kErrorBadParameter = kHwcErrorBadParameter;
constexpr int32_t kErrorBadDisplay = kHwcErrorBadDisplay;
constexpr int32_t kErrorBadLayer = kHwcErrorBadLayer;
constexpr int32_t kErrorBadConfig = kHwcErrorBadConfig;
constexpr int32_t kErrorNotValidated = kHwcErrorNotValidated;
constexpr int32_t kErrorUnsupported = kHwcErrorUnsupported;

using HotplugCallback = void (*)(hwc2_callback_data_t, hwc2_display_t, int32_t);
using RefreshCallback = void (*)(hwc2_callback_data_t, hwc2_display_t);
using VsyncCallback = void (*)(hwc2_callback_data_t, hwc2_display_t, int64_t);

struct Device;
struct VsyncRegistration {
  Device* device;
  hwc2_callback_data_t callback_data;
  VsyncCallback callback;
};
struct Device {
  hwc2_device_t hwc{};
  std::mutex callback_mutex;
  hwc2_callback_data_t hotplug_data = nullptr;
  HotplugCallback hotplug = nullptr;
  hwc2_callback_data_t refresh_data = nullptr;
  RefreshCallback refresh = nullptr;
  bool powered_on = true;
  bool vsync_requested = false;
  std::vector<std::unique_ptr<VsyncRegistration>> vsync_registrations;
  // Destroy first so its worker joins before callback contexts are released.
  DisplayState display;
};

Device* from(hwc2_device_t* device) { return reinterpret_cast<Device*>(device); }
int32_t checkDisplay(hwc2_display_t display) {
  return display == kPrimaryDisplay ? kErrorNone : kErrorBadDisplay;
}
int32_t checkLayer(Device* device, hwc2_display_t display, hwc2_layer_t layer) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  return device->display.hasLayer(layer) ? kErrorNone : kErrorBadLayer;
}

int closeDevice(hw_device_t* common) {
  auto* device = reinterpret_cast<Device*>(common);
  delete device;
  return 0;
}

void getCapabilities(hwc2_device_t*, uint32_t* out_count, int32_t*) {
  if (out_count != nullptr) *out_count = 0;
}

// The adapter owns callback contexts until the vsync thread is joined at close.
void callRegisteredVsync(void* opaque, uint64_t display, int64_t timestamp) {
  auto* registration = static_cast<VsyncRegistration*>(opaque);
  registration->callback(registration->callback_data, display, timestamp);
}

int32_t registerCallback(hwc2_device_t* raw, int32_t descriptor,
                         hwc2_callback_data_t callback_data,
                         hwc2_function_pointer_t pointer) {
  auto* device = from(raw);
  if (pointer == nullptr) return kErrorBadParameter;
  if (descriptor == kCallbackHotplug) {
    auto callback = reinterpret_cast<HotplugCallback>(pointer);
    {
      std::lock_guard<std::mutex> lock(device->callback_mutex);
      device->hotplug_data = callback_data;
      device->hotplug = callback;
    }
    callback(callback_data, kPrimaryDisplay, kHotplugConnected);
    return kErrorNone;
  }
  if (descriptor == kCallbackRefresh) {
    std::lock_guard<std::mutex> lock(device->callback_mutex);
    device->refresh_data = callback_data;
    device->refresh = reinterpret_cast<RefreshCallback>(pointer);
    return kErrorNone;
  }
  if (descriptor == kCallbackVsync) {
    auto callback = reinterpret_cast<VsyncCallback>(pointer);
    // Keep registration storage stable for the complete device lifetime.
    // HWC callback registration is normally performed once during init.
    auto registration = std::unique_ptr<VsyncRegistration>(
        new (std::nothrow) VsyncRegistration{device, callback_data, callback});
    if (!registration) return kHwcErrorNoResources;
    std::lock_guard<std::mutex> lock(device->callback_mutex);
    VsyncRegistration* context = registration.get();
    const int32_t result = device->display.registerCallback(
        CallbackKind::kVsync, context, callRegisteredVsync);
    if (result == kErrorNone) device->vsync_registrations.push_back(std::move(registration));
    return result;
  }
  return kErrorBadParameter;
}

int32_t createLayer(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t* out_layer) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  return from(raw)->display.createLayer(out_layer);
}
int32_t destroyLayer(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer) {
  auto* device = from(raw);
  const int32_t valid = checkLayer(device, display, layer);
  return valid == kErrorNone ? device->display.destroyLayer(layer) : valid;
}
int32_t createVirtualDisplay(hwc2_device_t*, uint32_t, uint32_t, int32_t*, hwc2_display_t*) {
  return kErrorUnsupported;
}
int32_t destroyVirtualDisplay(hwc2_device_t*, hwc2_display_t) { return kErrorBadDisplay; }
void dump(hwc2_device_t* raw, uint32_t* out_size, char* out_buffer) {
  if (out_size == nullptr) return;
  const auto telemetry = from(raw)->display.telemetrySnapshot();
  std::string text = "Ashnazg source-owned headless HWC2; client composition sink; mode switching is external policy.\n";
  text += "ASHNAZG_HWC_TELEMETRY_V1 {\"schema\":1,\"selected_config\":" +
      std::to_string(telemetry.selected_config) + ",\"callback_enabled\":" +
      (telemetry.callback_enabled ? "true" : "false") + ",\"callback_total\":" +
      std::to_string(telemetry.callback_total) + ",\"present_successes\":" +
      std::to_string(telemetry.present_successes) + ",\"present_timeouts\":" +
      std::to_string(telemetry.present_timeouts) + ",\"callbacks\":[";
  for (uint32_t i = 0; i < telemetry.event_count; ++i) {
    if (i != 0) text += ',';
    const auto& event = telemetry.events[i];
    text += "{\"timestamp_ns\":" + std::to_string(event.timestamp_ns) +
        ",\"config_id\":" + std::to_string(event.config_id) +
        ",\"callback_enabled\":" + (event.callback_enabled ? "true" : "false") +
        ",\"sequence\":" + std::to_string(event.sequence) + "}";
  }
  text += "]}\n";
  const uint32_t needed = static_cast<uint32_t>(text.size());
  if (out_buffer == nullptr) {
    *out_size = needed;
  } else {
    const uint32_t written = std::min(*out_size, needed);
    std::memcpy(out_buffer, text.data(), written);
    *out_size = written;
  }
}
int32_t getActiveConfig(hwc2_device_t* raw, hwc2_display_t display, hwc2_config_t* out_config) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_config == nullptr) return kErrorBadParameter;
  *out_config = from(raw)->display.activeConfig();
  return kErrorNone;
}
int32_t getChangedCompositionTypes(hwc2_device_t* raw, hwc2_display_t display,
                                   uint32_t* out_count, hwc2_layer_t* out_layers,
                                   int32_t* out_types) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_count == nullptr) return kErrorBadParameter;
  if (!from(raw)->display.wasValidated()) return kErrorNotValidated;
  const auto changes = from(raw)->display.changedCompositionTypes();
  if (out_layers == nullptr || out_types == nullptr) {
    *out_count = static_cast<uint32_t>(changes.size());
    return kErrorNone;
  }
  const uint32_t capacity = *out_count;
  if (capacity < changes.size()) return kErrorBadParameter;
  *out_count = static_cast<uint32_t>(changes.size());
  for (uint32_t i = 0; i < *out_count; ++i) {
    out_layers[i] = changes[i].first;
    out_types[i] = changes[i].second;
  }
  return kErrorNone;
}
int32_t acceptDisplayChanges(hwc2_device_t* raw, hwc2_display_t display) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  return from(raw)->display.acceptChanges();
}
int32_t getClientTargetSupport(hwc2_device_t*, hwc2_display_t display,
                               uint32_t width, uint32_t height, int32_t format, int32_t) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  return width == kDisplayWidth && height == kDisplayHeight && format == kPixelFormatRgba8888
      ? kErrorNone : kErrorUnsupported;
}
int32_t getColorModes(hwc2_device_t*, hwc2_display_t display, uint32_t* out_count,
                      int32_t* out_modes) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_count == nullptr) return kErrorBadParameter;
  if (out_modes == nullptr) { *out_count = 1; return kErrorNone; }
  if (*out_count == 0) return kErrorNone;
  out_modes[0] = kColorModeNative;
  *out_count = 1;
  return kErrorNone;
}
int32_t getDisplayAttribute(hwc2_device_t* raw, hwc2_display_t display,
                            hwc2_config_t config, int32_t attribute, int32_t* out_value) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_value == nullptr) return kErrorBadParameter;
  if (config != kIdle1Hz && config != kActive60Hz) return kErrorBadConfig;
  (void)raw;
  switch (attribute) {
    case kAttributeWidth: *out_value = kDisplayWidth; break;
    case kAttributeHeight: *out_value = kDisplayHeight; break;
    case kAttributeVsyncPeriod:
      *out_value = static_cast<int32_t>(config == kIdle1Hz ? kIdlePeriodNanos : kActivePeriodNanos);
      break;
    case 4: case 5: *out_value = kDpiMilli; break;
    default: return kErrorBadParameter;
  }
  return kErrorNone;
}
int32_t getDisplayConfigs(hwc2_device_t*, hwc2_display_t display, uint32_t* out_count,
                          hwc2_config_t* out_configs) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_count == nullptr) return kErrorBadParameter;
  if (out_configs == nullptr) { *out_count = 2; return kErrorNone; }
  const uint32_t capacity = *out_count;
  *out_count = std::min(capacity, 2u);
  if (*out_count > 0) out_configs[0] = kIdle1Hz;
  if (*out_count > 1) out_configs[1] = kActive60Hz;
  return kErrorNone;
}
int32_t getDisplayName(hwc2_device_t*, hwc2_display_t display, uint32_t* out_size, char* out_name) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_size == nullptr) return kErrorBadParameter;
  constexpr char name[] = "Ashnazg Headless Sink";
  constexpr uint32_t length = sizeof(name) - 1;
  if (out_name == nullptr) { *out_size = length; return kErrorNone; }
  const uint32_t written = std::min(*out_size, length);
  std::memcpy(out_name, name, written);
  *out_size = written;
  return kErrorNone;
}
int32_t getDisplayRequests(hwc2_device_t* raw, hwc2_display_t display, int32_t* out_requests,
                           uint32_t* out_count, hwc2_layer_t*, int32_t*) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_requests == nullptr || out_count == nullptr) return kErrorBadParameter;
  if (!from(raw)->display.wasValidated()) return kErrorNotValidated;
  *out_requests = 0;
  *out_count = 0;
  return kErrorNone;
}
int32_t getDisplayType(hwc2_device_t*, hwc2_display_t display, int32_t* out_type) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_type == nullptr) return kErrorBadParameter;
  *out_type = kDisplayTypePhysical;
  return kErrorNone;
}
int32_t getDozeSupport(hwc2_device_t*, hwc2_display_t display, int32_t* out_support) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_support == nullptr) return kErrorBadParameter;
  *out_support = 0;
  return kErrorNone;
}
int32_t getHdrCapabilities(hwc2_device_t*, hwc2_display_t display, uint32_t* out_count,
                           int32_t*, float* out_max, float* out_average, float* out_min) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_count == nullptr || out_max == nullptr || out_average == nullptr || out_min == nullptr) {
    return kErrorBadParameter;
  }
  *out_count = 0;
  *out_max = *out_average = *out_min = 0.0f;
  return kErrorNone;
}
uint32_t getMaxVirtualDisplayCount(hwc2_device_t*) { return 0; }
int32_t getReleaseFences(hwc2_device_t*, hwc2_display_t display, uint32_t* out_count,
                         hwc2_layer_t*, int32_t*) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_count == nullptr) return kErrorBadParameter;
  *out_count = 0;
  return kErrorNone;
}
int32_t presentDisplay(hwc2_device_t* raw, hwc2_display_t display, int32_t* out_fence) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_fence == nullptr) return kErrorBadParameter;
  auto* device = from(raw);
  if (!device->display.validated()) return kErrorNotValidated;
  const int32_t result = device->display.presentClientTarget();
  if (result == kErrorNone) *out_fence = -1;  // Client target is no longer retained.
  return result;
}
int32_t setActiveConfig(hwc2_device_t* raw, hwc2_display_t display, hwc2_config_t config) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  return from(raw)->display.setActiveConfig(config);
}
int32_t setClientTarget(hwc2_device_t* raw, hwc2_display_t display, buffer_handle_t target,
                        int32_t acquire_fence, int32_t, hwc_region_t) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  return from(raw)->display.setClientTargetAcquireFence(acquire_fence, target != nullptr);
}
int32_t setColorMode(hwc2_device_t*, hwc2_display_t display, int32_t mode) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  return mode == kColorModeNative ? kErrorNone : kErrorUnsupported;
}
int32_t setColorTransform(hwc2_device_t*, hwc2_display_t display, const float*, int32_t) {
  return checkDisplay(display);
}
int32_t setCursorPosition(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, int32_t, int32_t) {
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerBlendMode(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, int32_t) {
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerBuffer(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer,
                       buffer_handle_t, int32_t) {
  // All layers are client-composed. HWC never reads layer buffers or their fences.
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerColor(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, hwc_color_t) {
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerCompositionType(hwc2_device_t* raw, hwc2_display_t display,
                                hwc2_layer_t layer, int32_t composition) {
  auto* device = from(raw);
  const int32_t valid = checkLayer(device, display, layer);
  return valid == kErrorNone ? device->display.setLayerComposition(layer, composition) : valid;
}
int32_t setLayerDataspace(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, int32_t) {
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerDisplayFrame(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, hwc_rect_t) {
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerPlaneAlpha(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, float alpha) {
  if (alpha < 0.0f || alpha > 1.0f) return kErrorBadParameter;
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerSidebandStream(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, const native_handle*) {
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerSourceCrop(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, hwc_frect_t) {
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerSurfaceDamage(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, hwc_region_t) {
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerTransform(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, int32_t) {
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerVisibleRegion(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, hwc_region_t) {
  return checkLayer(from(raw), display, layer);
}
int32_t setLayerZOrder(hwc2_device_t* raw, hwc2_display_t display, hwc2_layer_t layer, uint32_t) {
  return checkLayer(from(raw), display, layer);
}
int32_t setOutputBuffer(hwc2_device_t*, hwc2_display_t display, buffer_handle_t, int32_t) {
  return checkDisplay(display) == kErrorNone ? kErrorUnsupported : kErrorBadDisplay;
}
int32_t setPowerMode(hwc2_device_t* raw, hwc2_display_t display, int32_t mode) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  auto* device = from(raw);
  if (mode != kPowerOff && mode != kPowerOn) return kErrorUnsupported;
  std::lock_guard<std::mutex> lock(device->callback_mutex);
  device->powered_on = mode == kPowerOn;
  // The caller's setVsyncEnabled state is authoritative; power-off suppresses callbacks.
  return device->display.setVsyncEnabled(device->powered_on && device->vsync_requested);
}
int32_t setVsyncEnabled(hwc2_device_t* raw, hwc2_display_t display, int32_t enabled) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (enabled != kVsyncEnable && enabled != kVsyncDisable) return kErrorBadParameter;
  auto* device = from(raw);
  std::lock_guard<std::mutex> lock(device->callback_mutex);
  device->vsync_requested = enabled == kVsyncEnable;
  return device->display.setVsyncEnabled(device->powered_on && device->vsync_requested);
}
int32_t validateDisplay(hwc2_device_t* raw, hwc2_display_t display,
                        uint32_t* out_types, uint32_t* out_requests) {
  if (checkDisplay(display) != kErrorNone) return kErrorBadDisplay;
  if (out_types == nullptr || out_requests == nullptr) return kErrorBadParameter;
  *out_requests = 0;
  const int32_t result = from(raw)->display.validate();
  *out_types = static_cast<uint32_t>(from(raw)->display.changedCompositionTypes().size());
  return result;
}

// These declarations are ABI subsets, so compile-check every exported function
// against the signatures from the pinned AOSP hwcomposer2.h source.
#define CHECK_HWC2_SIGNATURE(function, signature) \
  static_assert(std::is_same_v<decltype(&function), signature>, #function " HWC2 ABI mismatch")
CHECK_HWC2_SIGNATURE(acceptDisplayChanges, PfnAcceptDisplayChanges);
CHECK_HWC2_SIGNATURE(createLayer, PfnCreateLayer);
CHECK_HWC2_SIGNATURE(createVirtualDisplay, PfnCreateVirtualDisplay);
CHECK_HWC2_SIGNATURE(destroyLayer, PfnDestroyLayer);
CHECK_HWC2_SIGNATURE(destroyVirtualDisplay, PfnDestroyVirtualDisplay);
CHECK_HWC2_SIGNATURE(dump, PfnDump);
CHECK_HWC2_SIGNATURE(getActiveConfig, PfnGetActiveConfig);
CHECK_HWC2_SIGNATURE(getChangedCompositionTypes, PfnGetChangedCompositionTypes);
CHECK_HWC2_SIGNATURE(getClientTargetSupport, PfnGetClientTargetSupport);
CHECK_HWC2_SIGNATURE(getColorModes, PfnGetColorModes);
CHECK_HWC2_SIGNATURE(getDisplayAttribute, PfnGetDisplayAttribute);
CHECK_HWC2_SIGNATURE(getDisplayConfigs, PfnGetDisplayConfigs);
CHECK_HWC2_SIGNATURE(getDisplayName, PfnGetDisplayName);
CHECK_HWC2_SIGNATURE(getDisplayRequests, PfnGetDisplayRequests);
CHECK_HWC2_SIGNATURE(getDisplayType, PfnGetDisplayType);
CHECK_HWC2_SIGNATURE(getDozeSupport, PfnGetDozeSupport);
CHECK_HWC2_SIGNATURE(getHdrCapabilities, PfnGetHdrCapabilities);
CHECK_HWC2_SIGNATURE(getMaxVirtualDisplayCount, PfnGetMaxVirtualDisplayCount);
CHECK_HWC2_SIGNATURE(getReleaseFences, PfnGetReleaseFences);
CHECK_HWC2_SIGNATURE(presentDisplay, PfnPresentDisplay);
CHECK_HWC2_SIGNATURE(registerCallback, PfnRegisterCallback);
CHECK_HWC2_SIGNATURE(setActiveConfig, PfnSetActiveConfig);
CHECK_HWC2_SIGNATURE(setClientTarget, PfnSetClientTarget);
CHECK_HWC2_SIGNATURE(setColorMode, PfnSetColorMode);
CHECK_HWC2_SIGNATURE(setColorTransform, PfnSetColorTransform);
CHECK_HWC2_SIGNATURE(setCursorPosition, PfnSetCursorPosition);
CHECK_HWC2_SIGNATURE(setLayerBlendMode, PfnSetLayerBlendMode);
CHECK_HWC2_SIGNATURE(setLayerBuffer, PfnSetLayerBuffer);
CHECK_HWC2_SIGNATURE(setLayerColor, PfnSetLayerColor);
CHECK_HWC2_SIGNATURE(setLayerCompositionType, PfnSetLayerCompositionType);
CHECK_HWC2_SIGNATURE(setLayerDataspace, PfnSetLayerDataspace);
CHECK_HWC2_SIGNATURE(setLayerDisplayFrame, PfnSetLayerDisplayFrame);
CHECK_HWC2_SIGNATURE(setLayerPlaneAlpha, PfnSetLayerPlaneAlpha);
CHECK_HWC2_SIGNATURE(setLayerSidebandStream, PfnSetLayerSidebandStream);
CHECK_HWC2_SIGNATURE(setLayerSourceCrop, PfnSetLayerSourceCrop);
CHECK_HWC2_SIGNATURE(setLayerSurfaceDamage, PfnSetLayerSurfaceDamage);
CHECK_HWC2_SIGNATURE(setLayerTransform, PfnSetLayerTransform);
CHECK_HWC2_SIGNATURE(setLayerVisibleRegion, PfnSetLayerVisibleRegion);
CHECK_HWC2_SIGNATURE(setLayerZOrder, PfnSetLayerZOrder);
CHECK_HWC2_SIGNATURE(setOutputBuffer, PfnSetOutputBuffer);
CHECK_HWC2_SIGNATURE(setPowerMode, PfnSetPowerMode);
CHECK_HWC2_SIGNATURE(setVsyncEnabled, PfnSetVsyncEnabled);
CHECK_HWC2_SIGNATURE(validateDisplay, PfnValidateDisplay);
#undef CHECK_HWC2_SIGNATURE

// Optional and post-2.0 functions remain nullptr so Composer sees exactly the
// features implemented here.
template <typename T> hwc2_function_pointer_t functionPointer(T function) {
  return reinterpret_cast<hwc2_function_pointer_t>(function);
}
hwc2_function_pointer_t getFunction(hwc2_device_t*, int32_t descriptor) {
#define HWC_FUNCTION(descriptor_name, function_name) \
  case descriptor_name: return functionPointer(function_name)
  switch (descriptor) {
    HWC_FUNCTION(kAcceptDisplayChanges, acceptDisplayChanges);
    HWC_FUNCTION(kCreateLayer, createLayer);
    HWC_FUNCTION(kCreateVirtualDisplay, createVirtualDisplay);
    HWC_FUNCTION(kDestroyLayer, destroyLayer);
    HWC_FUNCTION(kDestroyVirtualDisplay, destroyVirtualDisplay);
    HWC_FUNCTION(kDump, dump);
    HWC_FUNCTION(kGetActiveConfig, getActiveConfig);
    HWC_FUNCTION(kGetChangedCompositionTypes, getChangedCompositionTypes);
    HWC_FUNCTION(kGetClientTargetSupport, getClientTargetSupport);
    HWC_FUNCTION(kGetColorModes, getColorModes);
    HWC_FUNCTION(kGetDisplayAttribute, getDisplayAttribute);
    HWC_FUNCTION(kGetDisplayConfigs, getDisplayConfigs);
    HWC_FUNCTION(kGetDisplayName, getDisplayName);
    HWC_FUNCTION(kGetDisplayRequests, getDisplayRequests);
    HWC_FUNCTION(kGetDisplayType, getDisplayType);
    HWC_FUNCTION(kGetDozeSupport, getDozeSupport);
    HWC_FUNCTION(kGetHdrCapabilities, getHdrCapabilities);
    HWC_FUNCTION(kGetMaxVirtualDisplayCount, getMaxVirtualDisplayCount);
    HWC_FUNCTION(kGetReleaseFences, getReleaseFences);
    HWC_FUNCTION(kPresentDisplay, presentDisplay);
    HWC_FUNCTION(kRegisterCallback, registerCallback);
    HWC_FUNCTION(kSetActiveConfig, setActiveConfig);
    HWC_FUNCTION(kSetClientTarget, setClientTarget);
    HWC_FUNCTION(kSetColorMode, setColorMode);
    HWC_FUNCTION(kSetColorTransform, setColorTransform);
    HWC_FUNCTION(kSetCursorPosition, setCursorPosition);
    HWC_FUNCTION(kSetLayerBlendMode, setLayerBlendMode);
    HWC_FUNCTION(kSetLayerBuffer, setLayerBuffer);
    HWC_FUNCTION(kSetLayerColor, setLayerColor);
    HWC_FUNCTION(kSetLayerCompositionType, setLayerCompositionType);
    HWC_FUNCTION(kSetLayerDataspace, setLayerDataspace);
    HWC_FUNCTION(kSetLayerDisplayFrame, setLayerDisplayFrame);
    HWC_FUNCTION(kSetLayerPlaneAlpha, setLayerPlaneAlpha);
    HWC_FUNCTION(kSetLayerSidebandStream, setLayerSidebandStream);
    HWC_FUNCTION(kSetLayerSourceCrop, setLayerSourceCrop);
    HWC_FUNCTION(kSetLayerSurfaceDamage, setLayerSurfaceDamage);
    HWC_FUNCTION(kSetLayerTransform, setLayerTransform);
    HWC_FUNCTION(kSetLayerVisibleRegion, setLayerVisibleRegion);
    HWC_FUNCTION(kSetLayerZOrder, setLayerZOrder);
    HWC_FUNCTION(kSetOutputBuffer, setOutputBuffer);
    HWC_FUNCTION(kSetPowerMode, setPowerMode);
    HWC_FUNCTION(kSetVsyncEnabled, setVsyncEnabled);
    HWC_FUNCTION(kValidateDisplay, validateDisplay);
    default: return nullptr;
  }
#undef HWC_FUNCTION
}

int open(const hw_module_t* module, const char* id, hw_device_t** out_device) {
  if (module == nullptr || id == nullptr || out_device == nullptr || std::strcmp(id, kHwcDeviceId) != 0) {
    return -EINVAL;
  }
  auto* device = new (std::nothrow) Device();
  if (device == nullptr) return -ENOMEM;
  device->hwc.common.tag = kHardwareDeviceTag;
  device->hwc.common.version = kHwcDeviceApiVersion2_0;
  device->hwc.common.module = const_cast<hw_module_t*>(module);
  device->hwc.common.close = closeDevice;
  device->hwc.getCapabilities = getCapabilities;
  device->hwc.getFunction = getFunction;
  *out_device = &device->hwc.common;
  return 0;
}

hw_module_methods_t gMethods{open};

}  // namespace
}  // namespace ashnazg::hwc

#define HAL_MODULE_INFO_SYM HMI
extern "C" {
struct hwc_module_t {
  hw_module_t common;
};
ASHNAZG_HAL_EXPORT hwc_module_t HAL_MODULE_INFO_SYM = {
    {ashnazg::hwc::abi::kHardwareModuleTag,
     1,
     ashnazg::hwc::abi::kHardwareHalApiVersion,
     ashnazg::hwc::abi::kHwcModuleId,
     "Ashnazg headless HWC2 sink",
     "Ashnazg project",
     &ashnazg::hwc::gMethods,
     nullptr,
#if defined(__LP64__)
     {0},
#else
     {0},
#endif
    }};
}
