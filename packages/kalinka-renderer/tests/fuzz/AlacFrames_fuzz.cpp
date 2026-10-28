#include "AlacDecoder.h"

#include <stdexcept>

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  if (!size)
    return 0;
  // The first byte picks the configuration, so one corpus covers them all.
  const uint8_t choice = data[0];
  const uint8_t bits = choice % 3 == 0 ? 16 : choice % 3 == 1 ? 24 : 32;
  const uint8_t channels = choice & 4 ? 2 : 1;
  const uint32_t frameLength = choice & 8 ? 4096 : 16;
  const uint8_t riceLimit = choice & 16 ? 0 : 14;
  uint8_t config[24] = {0,  0,  0,         0,        0, bits,
                        40, 10, riceLimit, channels, 0, 255};
  for (int i = 0; i < 4; ++i)
    config[i] = uint8_t(frameLength >> (24 - 8 * i));
  config[22] = 0xac;
  config[23] = 0x44;
  AlacDecoder decoder(config);
  const auto frameBytes = channels * sampleSize(decoder.format().sampleFormat);
  try {
    const auto pcm = decoder.decode({data + 1, size - 1});
    if (pcm.empty() || pcm.size() % frameBytes ||
        pcm.size() > frameLength * frameBytes)
      __builtin_trap();
  } catch (const std::runtime_error &) {
  }
  return 0;
}
