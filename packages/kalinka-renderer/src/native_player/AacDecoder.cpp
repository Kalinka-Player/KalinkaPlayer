#include "AacDecoder.h"

#include <neaacdec.h>

#include <stdexcept>
#include <string>

namespace {
constexpr unsigned CHANNELS = 2;

[[noreturn]] void unsupported() {
  throw std::runtime_error("Unsupported AAC configuration");
}

/// Whether a faad2 status failed: it is a plain char, unsigned on ARM.
bool failed(char status) { return static_cast<signed char>(status) < 0; }
} // namespace

void AacDecoder::Close::operator()(void *handle) const {
  NeAACDecClose(handle);
}

AacDecoder::AacDecoder(std::span<const uint8_t> config)
    : config(config.begin(), config.end()) {
  mp4AudioSpecificConfig parsed{};
  if (this->config.empty() ||
      failed(NeAACDecAudioSpecificConfig(this->config.data(),
                                         this->config.size(), &parsed)) ||
      !parsed.samplingFrequency)
    unsupported();
  open();
  // faad2 also upsamples a low-rate stream that may carry implicit SBR.
  const auto doubled =
      (parsed.sbr_present_flag == 1 && !parsed.downSampledSBR) ||
      parsed.forceUpSampling;
  frameLength = (parsed.frameLengthFlag ? 960u : 1024u) * (doubled ? 2u : 1u);
}

AacDecoder::~AacDecoder() = default;

void AacDecoder::open() {
  handle.reset(NeAACDecOpen());
  if (!handle)
    throw std::runtime_error("Could not initialize the AAC decoder");
  auto *settings = NeAACDecGetCurrentConfiguration(handle.get());
  settings->outputFormat = FAAD_FMT_16BIT;
  settings->downMatrix = 1;
  if (!NeAACDecSetConfiguration(handle.get(), settings))
    throw std::runtime_error("Could not initialize the AAC decoder");
  unsigned long opened = 0;
  unsigned char channels = 0;
  if (failed(NeAACDecInit2(handle.get(), config.data(), config.size(), &opened,
                           &channels)) ||
      !opened || opened > 192000 || (rate && opened != rate))
    unsupported();
  rate = opened;
}

StreamAudioFormat AacDecoder::format() const {
  return {unsigned(rate), CHANNELS, 16, PCM16_LE};
}

void AacDecoder::reset() { open(); }

std::span<const uint8_t> AacDecoder::decode(std::span<const uint8_t> unit) {
  NeAACDecFrameInfo info{};
  // faad2 takes a mutable pointer but only reads the unit.
  const auto *samples = static_cast<const int16_t *>(NeAACDecDecode(
      handle.get(), &info, const_cast<uint8_t *>(unit.data()), unit.size()));
  if (info.error)
    throw std::runtime_error(std::string("Invalid AAC audio: ") +
                             NeAACDecGetErrorMessage(info.error));
  if (!info.samples)
    return {};
  if (!samples || (info.channels != 1 && info.channels != CHANNELS) ||
      info.samplerate != rate || info.samples != frameLength * info.channels)
    throw std::runtime_error("AAC audio format changed during playback");
  // An HE-AAC v2 broadcast may start with mono frames until its next
  // parametric-stereo header arrives. Preserve those frames and keep the
  // advertised stereo output stable, just as for a wholly mono stream.
  const auto outputSamples = frameLength * CHANNELS;
  pcm.resize(outputSamples * sizeof(int16_t));
  for (size_t i = 0; i < outputSamples; ++i) {
    const auto sample = uint16_t(samples[info.channels == 1 ? i / CHANNELS : i]);
    pcm[2 * i] = uint8_t(sample);
    pcm[2 * i + 1] = uint8_t(sample >> 8);
  }
  return pcm;
}
