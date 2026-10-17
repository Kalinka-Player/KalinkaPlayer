#include "FragmentedMp4.h"

#include <stdexcept>

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  try {
    const auto init = FragmentedMp4::parseInitialization({data, size});
    if (!init.track.id || !init.track.timescale || init.track.config.empty())
      __builtin_trap();
  } catch (const std::runtime_error &) {
  }
  return 0;
}
