#include "DsdContainer.h"
#include "DsdFormat.h"

#include <algorithm>
#include <stdexcept>

namespace {
using container::MAX_HEADER_BYTES;
using container::number;
using container::require;
using container::tagged;
constexpr auto big = std::endian::big;

uint8_t reverse(uint8_t b) {
  b = ((b & 0x55) << 1) | ((b >> 1) & 0x55);
  b = ((b & 0x33) << 2) | ((b >> 2) & 0x33);
  return (b << 4) | (b >> 4);
}

std::vector<uint8_t> readBytes(ByteReader &source, size_t bytes) {
  require(bytes <= 1024 * 1024, "DSD metadata chunk too large");
  std::vector<uint8_t> result(bytes);
  source.readExact(result.data(), bytes);
  return result;
}
} // namespace

DsdContainer::DsdContainer(SelectOutput select) : select(std::move(select)) {}

ContainerFormat::Header DsdContainer::parseHeader(ByteReader &source) {
  sampleCount = nextByte = 0;
  channels = rate = blockSize = 0;
  reverseBits = false;
  const auto id = readBytes(source, 4);
  dsf = tagged(id.data(), "DSD ");
  require(dsf || tagged(id.data(), "FRM8"), "Not a DSF or DSDIFF file");
  if (dsf)
    parseDsf(source);
  else
    parseDsdiff(source);
  require(rate >= 2822400 && rate <= 49152000 && rate % 8 == 0,
          "Unsupported DSD sample rate");
  const auto format = select(rate, channels);
  require(isDsd(format.sampleFormat) && format.channels == channels &&
              format.dsdSampleRate == rate,
          "Invalid DSD transport selection");
  transport = format.sampleFormat;
  const auto bitsPerFrame = dsdBitsPerFrame(transport);
  return {{format, StreamType::FRAMES,
           static_cast<unsigned long>((sampleCount + bitsPerFrame - 1) /
                                      bitsPerFrame)},
          (sampleCount + 7) / 8 * channels};
}

void DsdContainer::parseDsf(ByteReader &source) {
  const auto head = readBytes(source, 24);
  require(number(head.data(), 8) == 28, "Invalid DSF header size");
  const auto fileSize = number(head.data() + 8, 8);
  require(fileSize >= 92, "Invalid DSF file size");
  const auto fmt = readBytes(source, 52);
  require(tagged(fmt.data(), "fmt ") && number(fmt.data() + 4, 8) == 52,
          "Invalid DSF format chunk");
  require(number(fmt.data() + 12, 4) == 1 && number(fmt.data() + 16, 4) == 0,
          "Unsupported DSF version or encoding");
  channels = number(fmt.data() + 24, 4);
  require(channels >= 1 && channels <= 2 &&
              number(fmt.data() + 20, 4) == channels,
          "DSF supports mono/stereo channel layouts only");
  rate = number(fmt.data() + 28, 4);
  const auto order = number(fmt.data() + 32, 4);
  require(order == 1 || order == 8, "Invalid DSF bit order");
  reverseBits = order == 1;
  sampleCount = number(fmt.data() + 36, 8);
  blockSize = number(fmt.data() + 44, 4);
  require(blockSize == 4096, "Unsupported DSF block size");
  const auto data = readBytes(source, 12);
  const auto length = number(data.data() + 4, 8);
  require(tagged(data.data(), "data") && length >= 12 &&
              length <= fileSize - 80,
          "Invalid DSF data chunk");
  require(sampleCount > 0 && sampleCount <= (uint64_t(1) << 48),
          "Invalid DSF sample count");
  const auto groups = ((sampleCount + 7) / 8 + blockSize - 1) / blockSize;
  // Some encoders write blocks past the sample count; those are never read.
  require(groups * blockSize * channels <= length - 12,
          "DSF sample count exceeds data size");
}

void DsdContainer::parseDsdiff(ByteReader &source) {
  const auto head = readBytes(source, 12);
  const auto formSize = number(head.data(), 8, big);
  require(formSize >= 4 && formSize <= (uint64_t(1) << 50),
          "Invalid DSDIFF form size");
  require(tagged(head.data() + 8, "DSD "), "Unsupported DSDIFF form");
  bool uncompressed = false;
  while (true) {
    require(source.position() <= formSize, "Missing DSDIFF audio data");
    const auto chunk = readBytes(source, 12);
    const auto size = number(chunk.data() + 4, 8, big);
    require(size <= formSize + 12 - source.position(),
            "Invalid DSDIFF chunk size");
    if (tagged(chunk.data(), "DST "))
      throw std::runtime_error("DST-compressed DSDIFF is not supported");
    if (tagged(chunk.data(), "DSD ")) {
      require(uncompressed && channels >= 1 && channels <= 2 && size > 0 &&
                  size % channels == 0,
              "Invalid DSDIFF audio properties");
      sampleCount = size / channels * 8;
      return;
    }
    require(source.position() + size <= MAX_HEADER_BYTES,
            "DSD header is too large");
    if (tagged(chunk.data(), "PROP")) {
      const auto prop = readBytes(source, size);
      require(prop.size() >= 4 && tagged(prop.data(), "SND "),
              "Invalid DSDIFF sound properties");
      size_t at = 4;
      while (at < prop.size()) {
        require(prop.size() - at >= 12, "Truncated DSDIFF property");
        const auto n = number(prop.data() + at + 4, 8, big);
        require(n <= prop.size() - at - 12, "Invalid DSDIFF property size");
        const auto *p = prop.data() + at + 12;
        if (tagged(prop.data() + at, "FS  ")) {
          require(n == 4, "Invalid DSDIFF sample rate");
          rate = number(p, 4, big);
        } else if (tagged(prop.data() + at, "CHNL")) {
          require(n >= 2, "Invalid DSDIFF channels");
          channels = number(p, 2, big);
          require(channels >= 1 && channels <= 2 && n == 2 + 4 * channels,
                  "DSDIFF supports mono/stereo only");
          require(channels == 1 ||
                      (tagged(p + 2, "SLFT") && tagged(p + 6, "SRGT")),
                  "Unsupported DSDIFF channel order");
        } else if (tagged(prop.data() + at, "CMPR")) {
          require(n >= 4 && tagged(p, "DSD "),
                  "Compressed DSDIFF (DST) is not supported");
          uncompressed = true;
        }
        at += 12 + n + (n & 1);
      }
      require(at == prop.size(), "Invalid DSDIFF property padding");
    } else
      source.skip(size);
    if (size & 1)
      source.skip(1);
  }
}

uint64_t DsdContainer::seek(uint64_t frame) {
  nextByte = frame * dsdBitsPerFrame(transport) / 8;
  const auto byte = dsf ? (nextByte / blockSize) * blockSize : nextByte;
  return byte * channels;
}

std::vector<uint8_t> DsdContainer::nextBlock(ByteReader &source) {
  const auto totalBytes = (sampleCount + 7) / 8;
  if (nextByte >= totalBytes)
    return {};
  std::vector<uint8_t> raw;
  if (dsf) {
    const auto blocked = readBytes(source, blockSize * channels);
    const auto skipBytes = nextByte % blockSize;
    const auto valid =
        std::min<uint64_t>(blockSize - skipBytes, totalBytes - nextByte);
    raw.resize(valid * channels);
    for (size_t b = 0; b < valid; ++b)
      for (unsigned c = 0; c < channels; ++c) {
        const auto v = blocked[c * blockSize + skipBytes + b];
        raw[b * channels + c] = reverseBits ? reverse(v) : v;
      }
  } else
    raw = readBytes(source,
                    std::min<uint64_t>(4096, totalBytes - nextByte) * channels);
  if (sampleCount % 8 && nextByte + raw.size() / channels == totalBytes) {
    const uint8_t mask = 0xff << (8 - sampleCount % 8);
    for (unsigned c = 0; c < channels; ++c)
      raw[raw.size() - channels + c] =
          (raw[raw.size() - channels + c] & mask) | (0x69 & ~mask);
  }
  auto packed = packDsd(raw, channels, transport,
                        nextByte * 8 / dsdBitsPerFrame(transport));
  nextByte += raw.size() / channels;
  return packed;
}
