#include "DelayLine.h"

#include <algorithm>
#include <cstring>

DelayLine::DelayLine(std::chrono::milliseconds delay) : delay(delay) {}

bool DelayLine::handles(const StreamAudioFormat &format) const {
  return !isDsd(format.sampleFormat);
}

size_t DelayLine::start(const StreamAudioFormat &format) {
  frameBytes = format.channels * sampleSize(format.sampleFormat);
  const size_t frames = uint64_t(delay.count()) * format.sampleRate / 1000;
  held.assign(frames * frameBytes, 0);
  oldest = 0;
  return frames;
}

void DelayLine::process(const uint8_t *in, uint8_t *out, size_t frames) {
  size_t bytes = frames * frameBytes;
  if (held.empty()) {
    in ? std::memcpy(out, in, bytes) : std::memset(out, 0, bytes);
    return;
  }
  while (bytes > 0) {
    const size_t n = std::min(bytes, held.size() - oldest);
    std::memcpy(out, held.data() + oldest, n);
    if (in) {
      std::memcpy(held.data() + oldest, in, n);
      in += n;
    } else {
      std::memset(held.data() + oldest, 0, n);
    }
    out += n;
    bytes -= n;
    oldest = (oldest + n) % held.size();
  }
}
