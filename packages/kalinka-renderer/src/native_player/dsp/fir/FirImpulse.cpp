#include "dsp/fir/FirImpulse.h"
#include "WavContainer.h"
#include "dsp/fir/FirRoomCorrection.h"
#include "dsp/output/PlanarPcm.h"

#include <cmath>
#include <fstream>

namespace {
class ImpulseReader final : public ByteReader {
public:
  explicit ImpulseReader(const std::filesystem::path &path)
      : file(path, std::ios::binary) {
    if (!file)
      throw std::runtime_error("Cannot open FIR impulse WAV");
  }
  void readExact(void *dest, size_t count) override {
    if (!file.read(static_cast<char *>(dest), count))
      throw std::runtime_error("Incomplete FIR impulse WAV");
    offset += count;
  }
  uint64_t position() const override { return offset; }

private:
  std::ifstream file;
  uint64_t offset = 0;
};

class MemoryReader final : public ByteReader {
public:
  explicit MemoryReader(std::string_view data) : data(data) {}
  void readExact(void *dest, size_t n) override {
    if (n > data.size() - offset)
      throw std::runtime_error("Incomplete FIR impulse WAV");
    std::memcpy(dest, data.data() + offset, n);
    offset += n;
  }
  uint64_t position() const override { return offset; }

private:
  std::string_view data;
  size_t offset = 0;
};

template <typename T> void checkFinite(const std::vector<uint8_t> &block) {
  for (size_t i = 0; i < block.size(); i += sizeof(T)) {
    T sample;
    std::memcpy(&sample, block.data() + i, sizeof(sample));
    if (!std::isfinite(sample))
      throw std::runtime_error("FIR impulse contains a non-finite coefficient");
  }
}
} // namespace

unsigned FirImpulse::sampleRate(const std::filesystem::path &path) {
  ImpulseReader reader(path);
  return WavContainer{}.parseHeader(reader).info.format.sampleRate;
}

static FirImpulse readImpulse(ByteReader &reader) {
  WavContainer wav;
  const auto info = wav.parseHeader(reader).info;
  const auto &format = info.format;
  if (!info.streamSize || info.streamSize > FirRoomCorrection::kMaxTaps)
    throw std::runtime_error("FIR impulse must contain 1 to 262144 frames");
  FirImpulse impulse{format.sampleRate, {}};
  for (unsigned c = 0; c < format.channels; ++c)
    impulse.channels[c].resize(info.streamSize);
  for (size_t at = 0; at < info.streamSize;) {
    const auto block = wav.nextBlock(reader);
    const size_t frames =
        block.size() / (format.channels * sampleSize(format.sampleFormat));
    if (format.sampleFormat == PCM_FLOAT32_LE)
      checkFinite<float>(block);
    if (format.sampleFormat == PCM_FLOAT64_LE)
      checkFinite<double>(block);
    double *planes[]{impulse.channels[0].data() + at,
                     format.channels == 2 ? impulse.channels[1].data() + at
                                          : nullptr};
    readPlanar(block.data(), format.sampleFormat, format.channels, frames,
               planes);
    at += frames;
  }
  return impulse;
}

FirImpulse FirImpulse::load(const std::filesystem::path &path) {
  ImpulseReader reader(path);
  return readImpulse(reader);
}

FirImpulse FirImpulse::decode(std::string_view wav) {
  MemoryReader reader(wav);
  return readImpulse(reader);
}
