#include "LivePcmStreamDecoder.h"

#include <array>
#include <cstring>
#include <stdexcept>

namespace {
uint64_t little(const uint8_t *p, unsigned count) {
  uint64_t value = 0;
  for (unsigned i = 0; i < count; ++i)
    value |= uint64_t(p[i]) << (8 * i);
  return value;
}
} // namespace

LivePcmStreamDecoder::LivePcmStreamDecoder(std::optional<StreamId> id)
    : AudioGraphNode(id), SourceStreamDecoder(1, 0) {}

LivePcmStreamDecoder::~LivePcmStreamDecoder() { stop(); }
void LivePcmStreamDecoder::forget() {
  nextFrame.reset();
  clock.reset();
  outputFrames = 0;
  mappedSource = 0;
  std::lock_guard lock(mapMutex);
  mappings.clear();
  readSourceFrame.reset();
}

StreamInfo LivePcmStreamDecoder::open() {
  std::array<uint8_t, 32> header;
  readExact(header.data(), header.size());
  if (std::memcmp(header.data(), "KALPCM1\0", 8) != 0 ||
      little(header.data() + 28, 4))
    throw std::runtime_error("Unsupported live PCM header");
  const auto rate = unsigned(little(header.data() + 8, 4));
  const auto channels = unsigned(little(header.data() + 12, 2));
  const auto code = little(header.data() + 14, 2);
  const auto target = unsigned(little(header.data() + 20, 4));
  const auto maximum = unsigned(little(header.data() + 24, 4));
  if (rate < 8000 || rate > 768000 || channels < 1 || channels > 2 ||
      code < 1 || code > 5 || target < 20 || target > 500 ||
      maximum <= target || maximum > 2000 || !little(header.data() + 16, 4))
    throw std::runtime_error("Invalid live PCM format or latency policy");
  constexpr AudioSampleFormat formats[] = {PCM16_LE, PCM24_3LE, PCM24_LE,
                                           PCM32_LE, PCM_FLOAT32_LE};
  const auto format = formats[code - 1];
  const auto width = sampleSize(format) * channels;
  clock = std::make_unique<LivePcmClock>(
      StreamAudioFormat{rate, channels, unsigned(sampleBits(format)), format});
  // The output device supplies most of the latency reserve. Decoder storage
  // stays small and is derived from the actual rate, not a fixed byte quota.
  setBufferSize(std::max<size_t>(1, uint64_t(rate) * 30 / 1000) * width);
  return {{rate, channels, unsigned(sampleBits(format)), format},
          FRAMES,
          0,
          LiveAudioPolicy{target, maximum}};
}

bool LivePcmStreamDecoder::decodeNext() {
  std::array<uint8_t, 16> record;
  const auto got = readSome(record.data(), record.size());
  if (!got)
    return false;
  if (got != record.size())
    throw std::runtime_error("Truncated live PCM record");
  const auto first = little(record.data(), 8);
  const auto frames = little(record.data() + 8, 4);
  const auto bytes = little(record.data() + 12, 4);
  const auto &format = streamInfo().format;
  const auto width = format.channels * sampleSize(format.sampleFormat);
  if (!frames || frames > format.sampleRate / 100 || bytes != frames * width ||
      (nextFrame && first != *nextFrame) || first > UINT64_MAX - frames)
    throw std::runtime_error("Invalid or discontinuous live PCM record");
  if (!nextFrame)
    mappedSource = double(first);
  nextFrame = first + frames;
  samples.resize(bytes);
  readExact(samples.data(), samples.size());
  const auto corrected = clock->process(samples, bufferedBytes() / width);
  const auto produced = corrected.size() / width;
  if (produced) {
    const double sourceEnd = std::max(
        mappedSource, double(*nextFrame) - clock->pendingInputFrames());
    {
      std::lock_guard lock(mapMutex);
      mappings.push_back(
          {outputFrames, outputFrames + produced, mappedSource, sourceEnd});
    }
    outputFrames += produced;
    mappedSource = sourceEnd;
    write(corrected);
  }
  return true;
}

size_t LivePcmStreamDecoder::read(void *dest, size_t bytes) {
  const auto count = BufferedStreamDecoder::read(dest, bytes);
  if (!count)
    return 0;
  const auto consumed = uint64_t(*streamReadPosition());
  std::lock_guard lock(mapMutex);
  while (mappings.size() > 1 && mappings.front().outputEnd < consumed)
    mappings.pop_front();
  if (!mappings.empty()) {
    const auto &m = mappings.front();
    const auto fraction = std::clamp(double(consumed - m.outputBegin) /
                                         (m.outputEnd - m.outputBegin),
                                     0.0, 1.0);
    readSourceFrame =
        uint64_t(m.sourceBegin + fraction * (m.sourceEnd - m.sourceBegin));
  }
  return count;
}

std::optional<uint64_t> LivePcmStreamDecoder::liveSourceFrame() const {
  std::lock_guard lock(mapMutex);
  return readSourceFrame;
}

void LivePcmStreamDecoder::seekSource(uint64_t) {
  throw std::runtime_error("Live PCM cannot seek");
}
