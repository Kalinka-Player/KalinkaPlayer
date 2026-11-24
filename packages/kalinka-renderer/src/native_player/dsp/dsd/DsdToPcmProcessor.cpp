#include "dsp/dsd/DsdToPcmProcessor.h"

#include "dsp/dsd/DsdFilterSetting.h"
#include "dsp/dsd/DsdPcmRate.h"
#include "dsp/fir/LiveFir.h"

#include <algorithm>

namespace {
/// Bytes per channel converted at a time, which bounds the buffers.
constexpr size_t kChunkBytes = 4096;
constexpr uint8_t kIdle = 0x69;

StreamAudioFormat pcmFormat(const DsdToPcm::Spec &spec) {
  return {spec.pcmRate, spec.channels, 64, PCM_FLOAT64_LE, 0};
}
} // namespace

DsdToPcmProcessor::DsdToPcmProcessor(std::vector<StreamAudioFormat> device,
                                     std::optional<unsigned> multiple,
                                     std::optional<DsdFilter> filter,
                                     std::shared_ptr<LiveFir> fir)
    : device(std::move(device)), multiple(multiple), filter(filter),
      fir(std::move(fir)) {}

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
  return {pcmFormat(converted), converted.dsdRate / converted.pcmRate / 8};
}

SignalPath DsdToPcmProcessor::describe(const StreamAudioFormat &format) const {
  const auto converted = spec(format).value();
  std::string summary =
      dsdRateName(converted.dsdRate) + " to " + pcmRateText(converted.pcmRate);
  if (converted.pcmRate != dsdBaseRate(converted.dsdRate))
    summary += ", " + dsdFilterText(converted.filter);
  SignalPath path{{{signal_step::kDsdToPcm, "DSD to PCM", summary}},
                  1000.0 * double(DsdToPcm::latencyOf(converted)) /
                      converted.pcmRate};
  if (fir) {
    const auto correction = fir->describe(pcmFormat(converted));
    path.steps.insert(path.steps.end(), correction.steps.begin(),
                      correction.steps.end());
    path.latencyMs += correction.latencyMs;
  }
  return path;
}

size_t DsdToPcmProcessor::start(const StreamAudioFormat &format) {
  const auto converted = spec(format).value();
  converter.emplace(converted);
  channels = format.channels;
  planar.assign(channels,
                std::vector<double>(converter->maxFrames(kChunkBytes)));
  planes.clear();
  for (auto &channel : planar)
    planes.push_back(channel.data());
  idle.assign(kChunkBytes * channels, kIdle);
  const auto pcm = pcmFormat(converted);
  runFir = fir && fir->handles(pcm) ? fir.get() : nullptr;
  return converter->latency() + (runFir ? runFir->start(pcm) : 0);
}

size_t DsdToPcmProcessor::historyFor(const StreamAudioFormat &format) const {
  const auto converted = spec(format).value();
  return DsdToPcm::latencyOf(converted) + 1 +
         (fir ? fir->historyFor(pcmFormat(converted)) : 0);
}

size_t DsdToPcmProcessor::history() const {
  // Linear phase: a frame reaches back as far as it lags, rounded either way.
  return converter ? converter->latency() + 1 + (runFir ? runFir->history() : 0)
                   : 0;
}

size_t DsdToPcmProcessor::settledOutputFrames() const {
  return runFir ? runFir->settledOutputFrames()
                : DspProcessor::settledOutputFrames();
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
  if (runFir && made)
    runFir->process(out, out, made);
  return made;
}
