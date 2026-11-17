#include "dsp/output/GainRamp.h"

#include <algorithm>
#include <cstdint>

GainRamp::GainRamp(std::chrono::microseconds length) : length(length) {}

size_t GainRamp::framesFor(std::chrono::microseconds length, unsigned rate) {
  const int64_t frames = int64_t{length.count()} * rate / 1'000'000;
  return std::max<size_t>(1, size_t(frames));
}

void GainRamp::aim(double newTarget, unsigned rate) {
  if (newTarget == target)
    return;
  target = newTarget;
  framesLeft = framesFor(length, rate);
}

void GainRamp::settle(double newTarget) {
  level = target = newTarget;
  framesLeft = 0;
}

GainRamp::Span GainRamp::take(size_t frames) {
  if (framesLeft == 0)
    return {level, level, frames};
  const size_t n = std::min(frames, framesLeft);
  const double reached =
      n == framesLeft
          ? target
          : level + (target - level) * double(n) / double(framesLeft);
  const Span span{level, reached, n};
  level = reached;
  framesLeft -= n;
  return span;
}
