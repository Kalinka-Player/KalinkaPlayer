#include "config/DspProfile.h"
#include <cstdint>
#include <stdexcept>

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  try {
    dsp_profiles::parse({reinterpret_cast<const char *>(data), size});
  } catch (const std::runtime_error &) {
  }
  return 0;
}
