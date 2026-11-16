#include "dsp/dsd/DsdToPcmProcessor.h"

#include <algorithm>

namespace {
/// Bytes per channel converted at a time, which bounds the buffers.
constexpr size_t kChunkBytes = 4096;
constexpr uint8_t kIdle = 0x69;
} // namespace

DsdToPcmProcessor::DsdToPcmProcessor(std::vector<StreamAudioFormat> device,
                                     double gain)
    : device(std::move(device)), gain(gain) {}

DsdToPcm::Spec DsdToPcmProcessor::spec(const StreamAudioFormat &format) const {
  const unsigned dsdRate = format.dsdSampleRate;
  const unsigned base = dsdRate % 44100 == 0 ? 44100 : 48000;
  const auto taken = [this](unsigned rate) {
    return std::ranges::any_of(device, [rate](const StreamAudioFormat &f) {
      return !isDsd(f.sampleFormat) && f.sampleRate == rate &&
             f.channels == kPcmDeviceChannels;
    });
  };
  unsigned multiple = 4;
  for (const unsigned candidate : {4u, 8u, 2u}) {
    if (taken(base * candidate)) {
      multiple = candidate;
      break;
    }
  }
  return {dsdRate, base * multiple, format.channels,
          dsdRate / base > 64 ? DsdFilter::Cut50k : DsdFilter::Cut30k,
          DsdBitOrder::MsbFirst};
}

bool DsdToPcmProcessor::handles(const StreamAudioFormat &format) const {
  return format.sampleFormat == DSD_U8 && DsdToPcm::converts(spec(format));
}

DspProcessor::Output
DsdToPcmProcessor::output(const StreamAudioFormat &format) const {
  const auto converted = spec(format);
  return {{converted.pcmRate, format.channels, 64, PCM_FLOAT64_LE, 0},
          converted.dsdRate / converted.pcmRate / 8};
}

size_t DsdToPcmProcessor::start(const StreamAudioFormat &format) {
  converter.emplace(spec(format));
  channels = format.channels;
  planar.assign(channels,
                std::vector<double>(converter->maxFrames(kChunkBytes)));
  planes.clear();
  for (auto &channel : planar)
    planes.push_back(channel.data());
  idle.assign(kChunkBytes * channels, kIdle);
  return converter->latency();
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
        samples[(made + i) * channels + c] = planar[c][i] * gain;
    made += got;
    at += bytes;
  }
  return made;
}
