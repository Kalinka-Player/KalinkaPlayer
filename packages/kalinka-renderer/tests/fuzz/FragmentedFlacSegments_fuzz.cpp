#include "FragmentedFlac.h"

#include <cstdlib>
#include <fstream>
#include <iterator>
#include <stdexcept>
#include <vector>

namespace {
FragmentedFlac config;
}

extern "C" int LLVMFuzzerInitialize(int *, char ***) {
  std::ifstream in(KALINKA_DASH_INIT_SEGMENT, std::ios::binary);
  const std::vector<uint8_t> bytes(std::istreambuf_iterator<char>(in), {});
  config = FragmentedFlac::parseInitialization(bytes);
  return 0;
}

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  try {
    size_t frames = 0;
    for (const auto &fragment : config.parseSegments({data, size})) {
      if (fragment.durations.empty() || fragment.frames.empty())
        __builtin_trap();
      frames += fragment.frames.size();
    }
    // Every frame byte is copied out of the input, never invented.
    if (frames > size)
      __builtin_trap();
  } catch (const std::runtime_error &) {
  }
  return 0;
}
