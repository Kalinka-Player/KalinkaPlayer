#include "dsp/output/OutputFormat.h"

#include "AudioSampleFormat.h"

#include <algorithm>

OutputChoice chooseOutput(const StreamAudioFormat &source,
                          const std::vector<StreamAudioFormat> &device,
                          unsigned channels) {
  const AudioSampleFormat format = source.sampleFormat;
  const OutputChoice own{format, unsigned(sampleBits(format))};
  if (isDsd(format) || format == PCM_FLOAT32_LE || format == PCM_FLOAT64_LE)
    return own;
  // A source that does not say its bits keeps every bit of its container.
  const unsigned bits = source.bitsPerSample ? source.bitsPerSample : own.bits;
  for (const auto candidate : pcmOutputFormats(format, bits)) {
    const auto taken = std::ranges::find_if(device, [&](const auto &offered) {
      return offered.sampleFormat == candidate &&
             offered.sampleRate == source.sampleRate &&
             offered.channels == channels;
    });
    if (taken == device.end())
      continue;
    const auto container = unsigned(sampleBits(candidate));
    const unsigned kept = taken->bitsPerSample;
    return {candidate, kept > 0 ? std::min(kept, container) : container};
  }
  return own;
}
