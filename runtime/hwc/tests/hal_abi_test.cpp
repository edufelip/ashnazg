#include "ashnazg/hwc/aosp_hwc2_abi.h"
#include "ashnazg/hwc/display_state.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <string>
#include <thread>

namespace {
using namespace std::chrono_literals;
using namespace ashnazg::hwc::abi;

struct HwcModule { hw_module_t common; };
extern "C" HwcModule HMI;

#define REQUIRE(condition) do { \
  if (!(condition)) { \
    std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
    return false; \
  } \
} while (false)

std::atomic<uint32_t> vsync_calls{0};
int32_t hotplug_connection = -1;
void onHotplug(void*, hwc2_display_t display, int32_t connected) {
  if (display == 0) hotplug_connection = connected;
}
void onVsync(void*, hwc2_display_t display, int64_t timestamp) {
  if (display == 0 && timestamp > 0) ++vsync_calls;
}

bool OpensHwcModuleAndReportsItsStaticModes() {
  REQUIRE(HMI.common.tag == kHardwareModuleTag);
  REQUIRE(HMI.common.module_api_version == kHwcModuleApiVersion);
  REQUIRE(HMI.common.hal_api_version == kHardwareHalApiVersion);
  REQUIRE(HMI.common.methods != nullptr);
  hw_device_t* common = nullptr;
  REQUIRE(HMI.common.methods->open(&HMI.common, kHwcDeviceId, &common) == 0);
  REQUIRE(common != nullptr);
  auto* device = reinterpret_cast<hwc2_device_t*>(common);
  REQUIRE(device->common.version == kHwcDeviceApiVersion2_0);
  REQUIRE(device->getFunction != nullptr);

  auto get_configs = reinterpret_cast<PfnGetDisplayConfigs>(device->getFunction(device, kGetDisplayConfigs));
  auto get_attribute = reinterpret_cast<PfnGetDisplayAttribute>(device->getFunction(device, kGetDisplayAttribute));
  auto set_config = reinterpret_cast<PfnSetActiveConfig>(device->getFunction(device, kSetActiveConfig));
  auto get_active = reinterpret_cast<PfnGetActiveConfig>(device->getFunction(device, kGetActiveConfig));
  REQUIRE(get_configs != nullptr && get_attribute != nullptr && set_config != nullptr && get_active != nullptr);
  uint32_t count = 0;
  REQUIRE(get_configs(device, 0, &count, nullptr) == 0 && count == 2);
  hwc2_config_t configs[2]{};
  count = 2;
  REQUIRE(get_configs(device, 0, &count, configs) == 0 && count == 2);
  REQUIRE(configs[0] == ashnazg::hwc::kIdle1Hz && configs[1] == ashnazg::hwc::kActive60Hz);
  int32_t period = 0;
  REQUIRE(get_attribute(device, 0, configs[0], kAttributeVsyncPeriod, &period) == 0);
  REQUIRE(period == 1'000'000'000);
  REQUIRE(set_config(device, 0, configs[0]) == 0);
  hwc2_config_t active = ashnazg::hwc::kActive60Hz;
  REQUIRE(get_active(device, 0, &active) == 0 && active == ashnazg::hwc::kIdle1Hz);
  REQUIRE(get_attribute(device, 0, configs[1], kAttributeVsyncPeriod, &period) == 0);
  REQUIRE(period == 16'666'667);

  auto register_callback = reinterpret_cast<PfnRegisterCallback>(device->getFunction(device, kRegisterCallback));
  auto set_vsync = reinterpret_cast<PfnSetVsyncEnabled>(device->getFunction(device, kSetVsyncEnabled));
  auto validate = reinterpret_cast<PfnValidateDisplay>(device->getFunction(device, kValidateDisplay));
  auto present = reinterpret_cast<PfnPresentDisplay>(device->getFunction(device, kPresentDisplay));
  auto get_requests = reinterpret_cast<PfnGetDisplayRequests>(device->getFunction(device, kGetDisplayRequests));
  auto set_target = reinterpret_cast<PfnSetClientTarget>(device->getFunction(device, kSetClientTarget));
  REQUIRE(register_callback != nullptr && set_vsync != nullptr && validate != nullptr && present != nullptr);
  REQUIRE(get_requests != nullptr);
  REQUIRE(register_callback(device, kCallbackHotplug, nullptr,
      reinterpret_cast<hwc2_function_pointer_t>(onHotplug)) == 0);
  REQUIRE(hotplug_connection == kHotplugConnected);
  REQUIRE(register_callback(device, kCallbackVsync, nullptr,
      reinterpret_cast<hwc2_function_pointer_t>(onVsync)) == 0);
  REQUIRE(set_vsync(device, 0, kVsyncEnable) == 0);
  const auto deadline = std::chrono::steady_clock::now() + 1500ms;
  while (vsync_calls.load() == 0 && std::chrono::steady_clock::now() < deadline) {
    std::this_thread::sleep_for(5ms);
  }
  REQUIRE(vsync_calls.load() > 0);
  REQUIRE(set_vsync(device, 0, kVsyncDisable) == 0);

  hwc2_layer_t layer = 0;
  auto create_layer = reinterpret_cast<PfnCreateLayer>(device->getFunction(device, kCreateLayer));
  auto set_composition = reinterpret_cast<PfnSetLayerCompositionType>(device->getFunction(device, kSetLayerCompositionType));
  auto changes = reinterpret_cast<PfnGetChangedCompositionTypes>(device->getFunction(device, kGetChangedCompositionTypes));
  auto accept = reinterpret_cast<PfnAcceptDisplayChanges>(device->getFunction(device, kAcceptDisplayChanges));
  REQUIRE(create_layer != nullptr && set_composition != nullptr && changes != nullptr && accept != nullptr);
  REQUIRE(create_layer(device, 0, &layer) == 0);
  REQUIRE(set_composition(device, 0, layer, kCompositionDevice) == 0);
  int32_t display_requests = 0;
  uint32_t display_request_count = 0;
  REQUIRE(get_requests(device, 0, &display_requests, &display_request_count, nullptr, nullptr) ==
          ashnazg::hwc::kHwcErrorNotValidated);
  uint32_t change_count = 0, request_count = 0;
  REQUIRE(validate(device, 0, &change_count, &request_count) == ashnazg::hwc::kHwcErrorHasChanges);
  REQUIRE(change_count == 1 && request_count == 0);
  hwc2_layer_t changed_layer = 0;
  int32_t changed_type = 0;
  change_count = 1;
  REQUIRE(changes(device, 0, &change_count, &changed_layer, &changed_type) == 0);
  REQUIRE(change_count == 1 && changed_layer == layer && changed_type == kCompositionClient);
  REQUIRE(accept(device, 0) == 0);
  REQUIRE(get_requests(device, 0, &display_requests, &display_request_count, nullptr, nullptr) == 0);
  REQUIRE(display_requests == 0 && display_request_count == 0);
  REQUIRE(validate(device, 0, &change_count, &request_count) == 0);
  REQUIRE(set_target(device, 0, reinterpret_cast<buffer_handle_t>(uintptr_t{1}), -1,
                     kDataspaceUnknown, hwc_region_t{}) == 0);
  int32_t present_fence = 123;
  REQUIRE(present(device, 0, &present_fence) == 0 && present_fence == -1);

  auto dump = reinterpret_cast<PfnDump>(device->getFunction(device, kDump));
  REQUIRE(dump != nullptr);
  uint32_t dump_size = 0;
  dump(device, &dump_size, nullptr);
  REQUIRE(dump_size > 0 && dump_size < 32768);
  std::string dump_text(dump_size, '\0');
  dump(device, &dump_size, dump_text.data());
  REQUIRE(dump_text.find("ASHNAZG_HWC_TELEMETRY_V1 {\"schema\":1") != std::string::npos);
  REQUIRE(dump_text.find("\"selected_config\":0") != std::string::npos);
  REQUIRE(dump_text.find("\"callback_total\":") != std::string::npos);
  REQUIRE(dump_text.find("\"present_successes\":1") != std::string::npos);

  auto destroy_layer = reinterpret_cast<PfnDestroyLayer>(device->getFunction(device, kDestroyLayer));
  REQUIRE(destroy_layer != nullptr && destroy_layer(device, 0, layer) == 0);
  REQUIRE(common->close(common) == 0);
  return true;
}
}  // namespace

int main() { return OpensHwcModuleAndReportsItsStaticModes() ? 0 : 1; }
