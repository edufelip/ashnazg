/*
 * Copyright (C) 2015 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 * Minimal ABI declarations derived from Android Open Source Project headers.
 * See ../UPSTREAM.md for pinned source revisions and the exact ABI subset.
 */
#pragma once

#include <cstddef>
#include <cstdint>

#if defined(__GNUC__)
#define ASHNAZG_HAL_EXPORT __attribute__((visibility("default")))
#else
#define ASHNAZG_HAL_EXPORT
#endif

extern "C" {

struct native_handle;
using native_handle_t = native_handle;
using buffer_handle_t = const native_handle_t*;

struct hw_module_t;
struct hw_device_t;
struct hw_module_methods_t {
  int (*open)(const hw_module_t*, const char*, hw_device_t**);
};
struct hw_module_t {
  uint32_t tag;
  uint16_t module_api_version;
  uint16_t hal_api_version;
  const char* id;
  const char* name;
  const char* author;
  hw_module_methods_t* methods;
  void* dso;
#if defined(__LP64__)
  uint64_t reserved[25];
#else
  uint32_t reserved[25];
#endif
};
struct hw_device_t {
  uint32_t tag;
  uint32_t version;
  hw_module_t* module;
  // Pinned AOSP places close after the reserved ABI padding.
#if defined(__LP64__)
  uint64_t reserved[12];
#else
  uint32_t reserved[12];
#endif
  int (*close)(hw_device_t*);
};

struct hwc_rect_t { int left, top, right, bottom; };
struct hwc_frect_t { float left, top, right, bottom; };
struct hwc_color_t { uint8_t r, g, b, a; };
struct hwc_region_t { size_t numRects; const hwc_rect_t* rects; };

using hwc2_function_pointer_t = void (*)();
using hwc2_callback_data_t = void*;
using hwc2_config_t = uint32_t;
using hwc2_display_t = uint64_t;
using hwc2_layer_t = uint64_t;

struct hwc2_device_t {
  hw_device_t common;
  void (*getCapabilities)(hwc2_device_t*, uint32_t*, int32_t*);
  hwc2_function_pointer_t (*getFunction)(hwc2_device_t*, int32_t);
};

}  // extern "C"

#if defined(__LP64__)
static_assert(sizeof(hw_module_t) == 248, "AOSP hw_module_t ABI layout changed");
static_assert(offsetof(hw_device_t, close) == 112, "AOSP hw_device_t close offset changed");
static_assert(sizeof(hw_device_t) == 120, "AOSP hw_device_t ABI layout changed");
static_assert(sizeof(hwc2_device_t) == 136, "AOSP hwc2_device_t ABI layout changed");
#else
static_assert(sizeof(hw_module_t) == 128, "AOSP hw_module_t ABI layout changed");
static_assert(offsetof(hw_device_t, close) == 60, "AOSP hw_device_t close offset changed");
static_assert(sizeof(hw_device_t) == 64, "AOSP hw_device_t ABI layout changed");
static_assert(sizeof(hwc2_device_t) == 72, "AOSP hwc2_device_t ABI layout changed");
#endif

namespace ashnazg::hwc::abi {
constexpr uint32_t kHardwareModuleTag = 0x48574d54;  // MAKE_TAG_CONSTANT('H','W','M','T')
constexpr uint32_t kHardwareDeviceTag = 0x48574454;  // MAKE_TAG_CONSTANT('H','W','D','T')
constexpr uint32_t kHwcModuleApiVersion = 0x0001;
constexpr uint32_t kHardwareHalApiVersion = 0x0100;
constexpr uint32_t kHwcDeviceApiVersion2_0 = 0x02000001;
constexpr const char* kHwcModuleId = "hwcomposer";
constexpr const char* kHwcDeviceId = "composer";
constexpr int32_t kDisplayTypePhysical = 1;
constexpr int32_t kDisplayConnectionInternal = 0;
constexpr int32_t kAttributeWidth = 1;
constexpr int32_t kAttributeHeight = 2;
constexpr int32_t kAttributeVsyncPeriod = 3;
constexpr int32_t kCompositionClient = 1;
constexpr int32_t kCompositionDevice = 2;
constexpr int32_t kCompositionSolidColor = 3;
constexpr int32_t kCompositionCursor = 4;
constexpr int32_t kCompositionSideband = 5;
constexpr int32_t kPowerOff = 0;
constexpr int32_t kPowerOn = 2;
constexpr int32_t kVsyncEnable = 1;
constexpr int32_t kVsyncDisable = 0;
constexpr int32_t kCallbackHotplug = 1;
constexpr int32_t kCallbackRefresh = 2;
constexpr int32_t kCallbackVsync = 3;
constexpr int32_t kHotplugConnected = 1;
constexpr int32_t kDataspaceUnknown = 0;
constexpr int32_t kPixelFormatRgba8888 = 1;
constexpr int32_t kColorModeNative = 0;

// HWC2_FUNCTION_* values are the stable sequential descriptors in the pinned
// AOSP hwcomposer2.h. Later descriptors are intentionally not exposed.
enum Function : int32_t {
  kAcceptDisplayChanges = 1,
  kCreateLayer = 2,
  kCreateVirtualDisplay = 3,
  kDestroyLayer = 4,
  kDestroyVirtualDisplay = 5,
  kDump = 6,
  kGetActiveConfig = 7,
  kGetChangedCompositionTypes = 8,
  kGetClientTargetSupport = 9,
  kGetColorModes = 10,
  kGetDisplayAttribute = 11,
  kGetDisplayConfigs = 12,
  kGetDisplayName = 13,
  kGetDisplayRequests = 14,
  kGetDisplayType = 15,
  kGetDozeSupport = 16,
  kGetHdrCapabilities = 17,
  kGetMaxVirtualDisplayCount = 18,
  kGetReleaseFences = 19,
  kPresentDisplay = 20,
  kRegisterCallback = 21,
  kSetActiveConfig = 22,
  kSetClientTarget = 23,
  kSetColorMode = 24,
  kSetColorTransform = 25,
  kSetCursorPosition = 26,
  kSetLayerBlendMode = 27,
  kSetLayerBuffer = 28,
  kSetLayerColor = 29,
  kSetLayerCompositionType = 30,
  kSetLayerDataspace = 31,
  kSetLayerDisplayFrame = 32,
  kSetLayerPlaneAlpha = 33,
  kSetLayerSidebandStream = 34,
  kSetLayerSourceCrop = 35,
  kSetLayerSurfaceDamage = 36,
  kSetLayerTransform = 37,
  kSetLayerVisibleRegion = 38,
  kSetLayerZOrder = 39,
  kSetOutputBuffer = 40,
  kSetPowerMode = 41,
  kSetVsyncEnabled = 42,
  kValidateDisplay = 43,
};

using PfnAcceptDisplayChanges = int32_t (*)(hwc2_device_t*, hwc2_display_t);
using PfnCreateLayer = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t*);
using PfnCreateVirtualDisplay = int32_t (*)(hwc2_device_t*, uint32_t, uint32_t, int32_t*, hwc2_display_t*);
using PfnDestroyLayer = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t);
using PfnDestroyVirtualDisplay = int32_t (*)(hwc2_device_t*, hwc2_display_t);
using PfnDump = void (*)(hwc2_device_t*, uint32_t*, char*);
using PfnGetActiveConfig = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_config_t*);
using PfnGetChangedCompositionTypes = int32_t (*)(hwc2_device_t*, hwc2_display_t, uint32_t*, hwc2_layer_t*, int32_t*);
using PfnGetClientTargetSupport = int32_t (*)(hwc2_device_t*, hwc2_display_t, uint32_t, uint32_t, int32_t, int32_t);
using PfnGetColorModes = int32_t (*)(hwc2_device_t*, hwc2_display_t, uint32_t*, int32_t*);
using PfnGetDisplayAttribute = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_config_t, int32_t, int32_t*);
using PfnGetDisplayConfigs = int32_t (*)(hwc2_device_t*, hwc2_display_t, uint32_t*, hwc2_config_t*);
using PfnGetDisplayName = int32_t (*)(hwc2_device_t*, hwc2_display_t, uint32_t*, char*);
using PfnGetDisplayRequests = int32_t (*)(hwc2_device_t*, hwc2_display_t, int32_t*, uint32_t*, hwc2_layer_t*, int32_t*);
using PfnGetDisplayType = int32_t (*)(hwc2_device_t*, hwc2_display_t, int32_t*);
using PfnGetDozeSupport = int32_t (*)(hwc2_device_t*, hwc2_display_t, int32_t*);
using PfnGetHdrCapabilities = int32_t (*)(hwc2_device_t*, hwc2_display_t, uint32_t*, int32_t*, float*, float*, float*);
using PfnGetMaxVirtualDisplayCount = uint32_t (*)(hwc2_device_t*);
using PfnGetReleaseFences = int32_t (*)(hwc2_device_t*, hwc2_display_t, uint32_t*, hwc2_layer_t*, int32_t*);
using PfnPresentDisplay = int32_t (*)(hwc2_device_t*, hwc2_display_t, int32_t*);
using PfnRegisterCallback = int32_t (*)(hwc2_device_t*, int32_t, hwc2_callback_data_t, hwc2_function_pointer_t);
using PfnSetActiveConfig = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_config_t);
using PfnSetClientTarget = int32_t (*)(hwc2_device_t*, hwc2_display_t, buffer_handle_t, int32_t, int32_t, hwc_region_t);
using PfnSetColorMode = int32_t (*)(hwc2_device_t*, hwc2_display_t, int32_t);
using PfnSetColorTransform = int32_t (*)(hwc2_device_t*, hwc2_display_t, const float*, int32_t);
using PfnSetCursorPosition = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, int32_t, int32_t);
using PfnSetLayerBlendMode = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, int32_t);
using PfnSetLayerBuffer = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, buffer_handle_t, int32_t);
using PfnSetLayerColor = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, hwc_color_t);
using PfnSetLayerCompositionType = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, int32_t);
using PfnSetLayerDataspace = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, int32_t);
using PfnSetLayerDisplayFrame = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, hwc_rect_t);
using PfnSetLayerPlaneAlpha = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, float);
using PfnSetLayerSidebandStream = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, const native_handle_t*);
using PfnSetLayerSourceCrop = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, hwc_frect_t);
using PfnSetLayerSurfaceDamage = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, hwc_region_t);
using PfnSetLayerTransform = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, int32_t);
using PfnSetLayerVisibleRegion = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, hwc_region_t);
using PfnSetLayerZOrder = int32_t (*)(hwc2_device_t*, hwc2_display_t, hwc2_layer_t, uint32_t);
using PfnSetOutputBuffer = int32_t (*)(hwc2_device_t*, hwc2_display_t, buffer_handle_t, int32_t);
using PfnSetPowerMode = int32_t (*)(hwc2_device_t*, hwc2_display_t, int32_t);
using PfnSetVsyncEnabled = int32_t (*)(hwc2_device_t*, hwc2_display_t, int32_t);
using PfnValidateDisplay = int32_t (*)(hwc2_device_t*, hwc2_display_t, uint32_t*, uint32_t*);
}  // namespace ashnazg::hwc::abi
