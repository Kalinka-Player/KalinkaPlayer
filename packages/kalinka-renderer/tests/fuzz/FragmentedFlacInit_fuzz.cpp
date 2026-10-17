#include "FragmentedFlac.h"

#include <stdexcept>

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  try {
    const auto config = FragmentedFlac::parseInitialization({data, size});
    if (!config.trackId || !config.timescale || !config.format.sampleRate)
      __builtin_trap();
  } catch (const std::runtime_error &) {
  }
  return 0;
}
