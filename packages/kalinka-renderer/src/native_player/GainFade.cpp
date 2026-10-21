#include "GainFade.h"

#include <algorithm>
#include <cstdint>

GainFade::GainFade(std::chrono::microseconds length) : length(length) {}

void GainFade::settle(float gain) {
  level = gain;
  target = gain;
  framesLeft = 0;
}

void GainFade::apply(void *buffer, size_t bytes, AudioSampleFormat format,
                     unsigned channels, unsigned sampleRate, float newTarget) {
  if (newTarget != target) {
    target = newTarget;
    // 64-bit: a 32-bit size_t overflows at high rates.
    const int64_t frames = int64_t{length.count()} * sampleRate / 1'000'000;
    framesLeft = std::max<size_t>(1, static_cast<size_t>(frames));
  }
  auto *frames = static_cast<uint8_t *>(buffer);
  const size_t frameBytes = sampleSize(format) * channels;
  const size_t fading = std::min(bytes / frameBytes, framesLeft);
  if (fading > 0) {
    const float reached =
        fading == framesLeft
            ? target
            : level + (target - level) * static_cast<float>(fading) /
                          static_cast<float>(framesLeft);
    applyGainFadeInPlace(frames, fading * frameBytes, format, channels, level,
                         reached);
    level = reached;
    framesLeft -= fading;
  }
  applyGainInPlace(frames + fading * frameBytes, bytes - fading * frameBytes,
                   format, level);
}
