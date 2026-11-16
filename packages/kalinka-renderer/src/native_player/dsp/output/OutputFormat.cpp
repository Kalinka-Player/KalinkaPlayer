#include "dsp/output/OutputFormat.h"

#include "AudioSampleFormat.h"

#include <algorithm>

OutputChoice chooseOutput(const StreamAudioFormat &source,
                          const std::vector<StreamAudioFormat> &device,
                          unsigned channels) {
  const AudioSampleFormat format = source.sampleFormat;
  const OutputChoice own{format, unsigned(sampleBits(format))};
  if (isDsd(format))
    return own;
  const auto taken = [&](AudioSampleFormat candidate) {
    return std::ranges::find_if(device, [&](const auto &offered) {
      return offered.sampleFormat == candidate &&
             offered.sampleRate == source.sampleRate &&
             offered.channels == channels;
    });
  };
  const auto kept = [](auto offered, AudioSampleFormat candidate) {
    const auto container = unsigned(sampleBits(candidate));
    const unsigned bits = offered->bitsPerSample;
    return OutputChoice{candidate,
                        bits > 0 ? std::min(bits, container) : container};
  };
  if (isFloat(format)) {
    if (taken(format) != device.end())
      return own;
    // The widest integer container the device takes, or the float as it is.
    for (const auto candidate : {PCM32_LE, PCM24_LE, PCM24_3LE, PCM16_LE})
      if (const auto offered = taken(candidate); offered != device.end())
        return kept(offered, candidate);
    return own;
  }
  // A source that does not say its bits keeps every bit of its container.
  const unsigned bits = source.bitsPerSample ? source.bitsPerSample : own.bits;
  for (const auto candidate : pcmOutputFormats(format, bits))
    if (const auto offered = taken(candidate); offered != device.end())
      return kept(offered, candidate);
  return own;
}
