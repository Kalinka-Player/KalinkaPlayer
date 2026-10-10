#include "WavContainer.h"

#include <algorithm>
#include <array>
#include <cstring>

namespace {
using container::MAX_HEADER_BYTES;
using container::number;
using container::require;
using container::tagged;
} // namespace

ContainerFormat::Header WavContainer::parseHeader(ByteReader &source) {
  nextFrame = 0;
  std::array<uint8_t, 12> header;
  source.readExact(header.data(), header.size());
  require(tagged(header.data(), "RIFF") && tagged(header.data() + 8, "WAVE"),
          "Expected a RIFF/WAVE file (RF64 and RIFX are not supported)");
  const uint64_t end = number(header.data() + 4, 4) + 8;
  require(end >= 12, "Invalid WAV RIFF size");
  bool haveFormat = false;
  while (true) {
    require(source.position() <= end && end - source.position() >= 8,
            "Missing WAV audio data");
    std::array<uint8_t, 8> chunk;
    source.readExact(chunk.data(), chunk.size());
    const uint64_t size = number(chunk.data() + 4, 4);
    const auto padded = size + (size & 1);
    require(padded <= end - source.position(), "Invalid WAV chunk size");
    if (tagged(chunk.data(), "data")) {
      require(haveFormat, "WAV data precedes its format");
      require(size % sourceFrameBytes == 0, "Incomplete WAV audio frame");
      frames = size / sourceFrameBytes;
      return {{format, StreamType::FRAMES, static_cast<unsigned long>(frames)},
              size};
    }
    require(source.position() + padded <= MAX_HEADER_BYTES,
            "WAV header is too large");
    if (tagged(chunk.data(), "fmt ")) {
      require(!haveFormat && size >= 16, "Invalid WAV format chunk");
      parseFormat(source, size);
      haveFormat = true;
    } else
      source.skip(padded);
  }
}

void WavContainer::parseFormat(ByteReader &source, uint64_t size) {
  std::array<uint8_t, 40> fmt{};
  const auto count = std::min<uint64_t>(size, fmt.size());
  source.readExact(fmt.data(), count);
  auto encoding = number(fmt.data(), 2);
  const unsigned channels = number(fmt.data() + 2, 2);
  const unsigned rate = number(fmt.data() + 4, 4);
  const auto byteRate = number(fmt.data() + 8, 4);
  sourceFrameBytes = number(fmt.data() + 12, 2);
  containerBits = number(fmt.data() + 14, 2);
  unsigned bits = containerBits;
  if (encoding == 0xfffe) {
    require(size >= 40 && number(fmt.data() + 16, 2) >= 22 &&
                number(fmt.data() + 16, 2) <= size - 18,
            "Invalid extensible WAV format");
    constexpr std::array<uint8_t, 16> pcmGuid{
        1, 0, 0, 0, 0, 0, 0x10, 0, 0x80, 0, 0, 0xaa, 0, 0x38, 0x9b, 0x71};
    require(std::equal(pcmGuid.begin() + 1, pcmGuid.end(), fmt.data() + 25),
            "Unsupported WAV encoding");
    encoding = fmt[24];
    bits = number(fmt.data() + 18, 2);
    const auto mask = number(fmt.data() + 20, 4);
    require(mask == 0 || (channels == 1 && mask == 4) ||
                (channels == 2 && mask == 3),
            "Unsupported WAV channel layout");
  }
  require(encoding == 1 || encoding == 3,
          "WAV supports integer or IEEE float PCM only");
  require(channels >= 1 && channels <= 2, "WAV supports mono/stereo only");
  if (encoding == 3)
    require((bits == 32 || bits == 64) && bits == containerBits,
            "WAV float samples must be 32 or 64 bits");
  else
    require((bits == 16 || bits == 24 || bits == 32) &&
                (containerBits == 16 || containerBits == 24 ||
                 containerBits == 32) &&
                bits <= containerBits,
            "WAV supports 16/24/32-bit integer PCM precision");
  require(rate > 0 && sourceFrameBytes == channels * (containerBits / 8) &&
              uint64_t(rate) * sourceFrameBytes == byteRate,
          "Invalid WAV sample rate or block alignment");
  const auto sampleFormat = encoding == 3
                                ? (bits == 32 ? PCM_FLOAT32_LE : PCM_FLOAT64_LE)
                                : (bits == 16   ? PCM16_LE
                                   : bits == 24 ? PCM24_LE
                                                : PCM32_LE);
  format = {rate, channels, bits, sampleFormat};
  source.skip(size + (size & 1) - count);
}

uint64_t WavContainer::seek(uint64_t frame) {
  nextFrame = frame;
  return frame * sourceFrameBytes;
}

std::vector<uint8_t> WavContainer::nextBlock(ByteReader &source) {
  const auto count = std::min<uint64_t>(4096, frames - nextFrame);
  if (!count)
    return {};
  std::vector<uint8_t> raw(count * sourceFrameBytes);
  source.readExact(raw.data(), raw.size());
  nextFrame += count;
  const auto sourceWidth = containerBits / 8;
  const auto validWidth = format.bitsPerSample / 8;
  const auto outputWidth = sampleSize(format.sampleFormat);
  if (sourceWidth == validWidth && validWidth == outputWidth)
    return raw;
  std::vector<uint8_t> pcm(count * format.channels * outputWidth);
  for (size_t i = 0; i < count * format.channels; ++i)
    // Extensible PCM stores valid bits at the high end of its container.
    std::memcpy(pcm.data() + i * outputWidth,
                raw.data() + i * sourceWidth + sourceWidth - validWidth,
                validWidth);
  return pcm;
}
