#include "FragmentedMp4.h"

#include <fstream>
#include <iterator>
#include <numeric>
#include <stdexcept>
#include <vector>

namespace {
FragmentedMp4 init;
}

extern "C" int LLVMFuzzerInitialize(int *, char ***) {
  std::ifstream in(KALINKA_DASH_INIT_SEGMENT, std::ios::binary);
  const std::vector<uint8_t> bytes(std::istreambuf_iterator<char>(in), {});
  init = FragmentedMp4::parseInitialization(bytes);
  return 0;
}

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  try {
    size_t copied = 0;
    for (const auto &fragment : init.parseSegments({data, size})) {
      if (fragment.durations.empty() ||
          fragment.durations.size() != fragment.sizes.size() ||
          std::accumulate(fragment.sizes.begin(), fragment.sizes.end(),
                          size_t(0)) != fragment.data.size())
        __builtin_trap();
      copied += fragment.data.size();
    }
    // Every sample byte is copied out of the input, never invented.
    if (copied > size)
      __builtin_trap();
  } catch (const std::runtime_error &) {
  }
  return 0;
}
