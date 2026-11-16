#include "dsp/dsd/ByteHistory.h"

#include "dsp/dsd/DsdKernels.h"

#include <algorithm>

ByteHistory::ByteHistory(unsigned channels, size_t carried, size_t capacity,
                         DsdBitOrder order, bool keepReversed)
    : carried(carried),
      idle(order == DsdBitOrder::LsbFirst ? kBitReversed[0x69] : 0x69),
      direct(channels, std::vector<uint8_t>(carried + capacity +
                                            dsd_kernels::kByteSlack)) {
  if (keepReversed)
    mirrored = direct;
  reset();
}

void ByteHistory::append(const uint8_t *in, size_t bytes) {
  const size_t channels = direct.size();
  for (size_t c = 0; c < channels; ++c) {
    uint8_t *block = direct[c].data() + carried;
    for (size_t m = 0; m < bytes; ++m)
      block[m] = in[m * channels + c];
    if (!mirrored.empty())
      std::transform(block, block + bytes, mirrored[c].data() + carried,
                     [](uint8_t byte) { return kBitReversed[byte]; });
  }
}

void ByteHistory::advance(size_t bytes) {
  for (auto *side : {&direct, &mirrored})
    for (auto &channel : *side)
      std::copy(channel.begin() + bytes, channel.begin() + bytes + carried,
                channel.begin());
}

void ByteHistory::reset() {
  for (auto &channel : direct)
    std::ranges::fill(channel, idle);
  for (auto &channel : mirrored)
    std::ranges::fill(channel, kBitReversed[idle]);
}
