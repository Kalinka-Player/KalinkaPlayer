#include "dsp/output/PackedFade.h"

#include "dsp/output/PlanarPcm.h"

#include <algorithm>
#include <stdexcept>
#include <string>

namespace {
constexpr size_t kChunkFrames = 1024;
constexpr unsigned kMaxChannels = 8;
} // namespace

PackedFade::PackedFade(uint64_t seed)
    : quantiser(kChunkFrames, kMaxChannels, seed),
      planar(kMaxChannels, std::vector<double>(kChunkFrames)) {
  for (auto &channel : planar)
    planes.push_back(channel.data());
}

void PackedFade::out(uint8_t *data, AudioSampleFormat format, unsigned channels,
                     unsigned bits, size_t at, size_t frames, size_t length) {
  if (channels == 0 || channels > kMaxChannels)
    throw std::invalid_argument("No fade for " + std::to_string(channels) +
                                " channels");
  const size_t frameBytes = sampleSize(format) * channels;
  const auto level = [length](size_t frame) {
    return 1.0 - double(std::min(frame, length)) / double(length);
  };
  for (size_t done = 0; done < frames;) {
    const size_t n = std::min(frames - done, kChunkFrames);
    uint8_t *chunk = data + done * frameBytes;
    readPlanar(chunk, format, channels, n, planes.data());
    const size_t first = at + done;
    quantiser.pack(planes.data(), channels, n,
                   {level(first), level(first + n), n}, format, bits, chunk);
    done += n;
  }
}
