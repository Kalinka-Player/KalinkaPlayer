#include "dsp/output/PackedFade.h"

#include "dsp/output/OutputFade.h"
#include "dsp/output/PlanarPcm.h"

#include <algorithm>
#include <stdexcept>
#include <string>

namespace {
constexpr size_t kChunkFrames = 1024;
constexpr unsigned kMaxChannels = 8;
} // namespace

PackedFade::PackedFade(uint64_t seed)
    : ramp(kFadeLength), quantiser(kChunkFrames, kMaxChannels, seed),
      planar(kMaxChannels, std::vector<double>(kChunkFrames)) {
  for (auto &channel : planar)
    planes.push_back(channel.data());
}

void PackedFade::begin(unsigned rate) {
  ramp.settle(1.0);
  ramp.aim(0.0, rate);
}

void PackedFade::out(uint8_t *data, AudioSampleFormat format, unsigned channels,
                     unsigned bits, size_t frames) {
  if (channels == 0 || channels > kMaxChannels)
    throw std::invalid_argument("No fade for " + std::to_string(channels) +
                                " channels");
  const size_t frameBytes = sampleSize(format) * channels;
  for (size_t done = 0; done < frames;) {
    size_t n = std::min(frames - done, kChunkFrames);
    if (ramp.left())
      n = std::min(n, ramp.left());
    const auto span = ramp.take(n);
    uint8_t *chunk = data + done * frameBytes;
    if (span.from != 0.0 || span.to != 0.0)
      readPlanar(chunk, format, channels, n, planes.data());
    quantiser.pack(planes.data(), channels, n, span, format, bits, chunk);
    done += n;
  }
}
