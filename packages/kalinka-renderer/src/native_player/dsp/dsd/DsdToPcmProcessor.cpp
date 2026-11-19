#include "dsp/dsd/DsdToPcmProcessor.h"

#include "dsp/dsd/DsdPcmRate.h"

#include <algorithm>

namespace {
/// Bytes per channel converted at a time, which bounds the buffers.
constexpr size_t kChunkBytes = 4096;
constexpr uint8_t kIdle = 0x69;
} // namespace

DsdToPcmProcessor::DsdToPcmProcessor(std::vector<StreamAudioFormat> device,
                                     std::optional<unsigned> multiple,
                                     std::optional<DsdFilter> filter)
    : device(std::move(device)), multiple(multiple), filter(filter) {}

std::optional<DsdToPcm::Spec>
DsdToPcmProcessor::spec(const StreamAudioFormat &format) const {
  const unsigned dsdRate = format.dsdSampleRate;
  const auto pcmRate = dsdPcmRate(dsdRate, device, multiple);
  if (!pcmRate)
    return std::nullopt;
  return DsdToPcm::Spec{
      dsdRate, *pcmRate, format.channels,
      filter.value_or(dsdRate / dsdBaseRate(dsdRate) > 64 ? DsdFilter::Cut50k
                                                          : DsdFilter::Cut30k),
      DsdBitOrder::MsbFirst};
}

bool DsdToPcmProcessor::handles(const StreamAudioFormat &format) const {
  if (format.sampleFormat != DSD_RAW)
    return false;
  const auto converted = spec(format);
  return converted && DsdToPcm::converts(*converted);
}

DspProcessor::Output
DsdToPcmProcessor::output(const StreamAudioFormat &format) const {
  const auto converted = spec(format).value();
  return {{converted.pcmRate, format.channels, 64, PCM_FLOAT64_LE, 0},
          converted.dsdRate / converted.pcmRate / 8};
}

size_t DsdToPcmProcessor::start(const StreamAudioFormat &format) {
  converter.emplace(spec(format).value());
  channels = format.channels;
  planar.assign(channels,
                std::vector<double>(converter->maxFrames(kChunkBytes)));
  planes.clear();
  for (auto &channel : planar)
    planes.push_back(channel.data());
  idle.assign(kChunkBytes * channels, kIdle);
  return converter->latency();
}

size_t DsdToPcmProcessor::history() const {
  // Linear phase: a frame reaches back as far as it lags, rounded either way.
  return converter ? converter->latency() + 1 : 0;
}

size_t DsdToPcmProcessor::process(const uint8_t *in, uint8_t *out,
                                  size_t frames) {
  auto *samples = reinterpret_cast<double *>(out);
  size_t made = 0;
  for (size_t at = 0; at < frames;) {
    const size_t bytes = std::min(frames - at, kChunkBytes);
    const uint8_t *from = in ? in + at * channels : idle.data();
    const size_t got = converter->process(from, bytes, planes.data());
    for (size_t i = 0; i < got; ++i)
      for (unsigned c = 0; c < channels; ++c)
        samples[(made + i) * channels + c] = planar[c][i];
    made += got;
    at += bytes;
  }
  return made;
}
