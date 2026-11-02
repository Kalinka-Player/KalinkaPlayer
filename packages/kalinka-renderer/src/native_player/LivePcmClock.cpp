#include "LivePcmClock.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <stdexcept>

namespace {
void check(soxr_error_t error) {
  if (error)
    throw std::runtime_error(std::string("Live clock correction: ") + error);
}
} // namespace

double LiveClockServo::update(size_t queuedFrames, size_t inputFrames) {
  frames += inputFrames;
  // Fifteen milliseconds in the decoder, plus the hardware's live reserve.
  const double error = double(queuedFrames) / rate - .015;
  const double alpha = 1 - std::exp(-double(inputFrames) / (rate * 5.0));
  errorSeconds += alpha * (error - errorSeconds);
  return std::clamp(1 + errorSeconds / 30, .999, 1.001);
}

LivePcmClock::LivePcmClock(StreamAudioFormat format)
    : format(format), servo(format.sampleRate) {}

double LivePcmClock::pendingInputFrames() const {
  return engine ? soxr_delay(engine.get()) * ratio : 0;
}

std::span<const uint8_t> LivePcmClock::process(std::span<const uint8_t> pcm,
                                               size_t queuedFrames) {
  const auto width = sampleSize(format.sampleFormat) * format.channels;
  const auto frames = pcm.size() / width;
  ratio = servo.update(queuedFrames, frames);
  if (!servo.active())
    ratio = 1;
  const bool floating = format.sampleFormat == PCM_FLOAT32_LE;
  if (!engine) {
    const auto type = floating ? SOXR_FLOAT32_I : SOXR_INT32_I;
    const auto io = soxr_io_spec(type, type);
    const auto quality = soxr_quality_spec(SOXR_HQ, SOXR_VR);
    const auto runtime = soxr_runtime_spec(1);
    soxr_error_t error = nullptr;
    // For SOXR_VR the creation ratio is the maximum ratio we will request.
    engine.reset(soxr_create(1.001, 1, format.channels, &error, &io, &quality,
                             &runtime));
    check(error);
    check(soxr_set_io_ratio(engine.get(), ratio, 0));
  } else {
    check(soxr_set_io_ratio(engine.get(), ratio, frames));
  }
  const size_t count = frames * format.channels;
  const size_t capacity = frames + 256;
  const void *input;
  void *dest;
  if (floating) {
    floatIn.resize(count);
    std::memcpy(floatIn.data(), pcm.data(), pcm.size());
    floatOut.resize(capacity * format.channels);
    input = floatIn.data();
    dest = floatOut.data();
  } else {
    integerIn.resize(count);
    integerOut.resize(capacity * format.channels);
    convertSampleFormat(pcm.data(), format.sampleFormat, count,
                        integerIn.data(), PCM32_LE, count * sizeof(int32_t));
    input = integerIn.data();
    dest = integerOut.data();
  }
  size_t used = 0, produced = 0;
  check(soxr_process(engine.get(), input, frames, &used, dest, capacity,
                     &produced));
  if (used != frames)
    throw std::runtime_error("Live clock correction exceeded its output bound");
  output.resize(produced * width);
  if (floating)
    std::memcpy(output.data(), floatOut.data(), output.size());
  else
    convertSampleFormat(integerOut.data(), PCM32_LE, produced * format.channels,
                        output.data(), format.sampleFormat, output.size());
  return output;
}
