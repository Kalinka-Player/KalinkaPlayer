#include "dsp/fir/LiveFir.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <format>

void LiveFir::select(std::shared_ptr<const FirFilterBank> bank) {
  std::lock_guard lock(setupMutex);
  const auto rate = runRate.load();
  auto next = bank && rate ? bank->at(rate) : nullptr;
  requested.store(std::move(bank));
  if (auto old = desired.exchange(next);
      old && old != next && std::ranges::find(retired, old) == retired.end())
    retired.push_back(std::move(old));
  std::erase_if(retired, [](const auto &old) { return old.use_count() == 1; });
}

std::shared_ptr<const FirVariant>
LiveFir::variantFor(const StreamAudioFormat &f) const {
  const auto bank = requested.load();
  return bank && handles(f) ? bank->at(f.sampleRate) : nullptr;
}

bool LiveFir::handles(const StreamAudioFormat &f) const {
  return f.channels == 2 && !isDsd(f.sampleFormat);
}

bool LiveFir::filtering(const StreamAudioFormat &f) const {
  return variantFor(f) != nullptr;
}

DspProcessor::Output LiveFir::output(const StreamAudioFormat &f) const {
  auto result = f;
  result.bitsPerSample = 64;
  result.sampleFormat = PCM_FLOAT64_LE;
  return {result, 1};
}

SignalPath LiveFir::describe(const StreamAudioFormat &f) const {
  const auto bank = requested.load();
  if (!bank || !handles(f))
    return {};
  const auto plan = bank->at(f.sampleRate);
  if (!plan) {
    return {
        {{signal_step::kFir, "FIR unavailable",
          std::format("Supply an impulse at {:g} kHz", f.sampleRate / 1000.),
          true}},
        0.0};
  }
  auto path = plan->filter->describe(f);
  if (plan->sourceRate != f.sampleRate) {
    path.steps.front().summary +=
        std::format(", resampled from {:g} kHz", plan->sourceRate / 1000.);
    path.latencyMs = 1000. * lag / f.sampleRate;
  }
  return path;
}

size_t LiveFir::start(const StreamAudioFormat &f) {
  input = f;
  {
    std::lock_guard lock(setupMutex);
    runRate.store(f.sampleRate);
    active = variantFor(f);
    desired.store(active);
  }
  if (active)
    active->filter->start(input);
  wet.settle(active ? 1.0 : 0.0);
  dry.fill({});
  cursor = warmup = settledFrames = 0;
  return lag;
}

bool LiveFir::changeInputFormat(const StreamAudioFormat &f) {
  if (f.sampleRate != input.sampleRate || !handles(f))
    return false;
  input = f;
  return !active || active->filter->changeInputFormat(f);
}

size_t LiveFir::historyFor(const StreamAudioFormat &format) const {
  const auto plan = variantFor(format);
  return plan ? plan->filter->preparedHistory() : 0;
}

size_t LiveFir::settledOutputFrames() const {
  if (desired.load() != active)
    return 0;
  return std::min<uint64_t>(settledFrames, std::numeric_limits<size_t>::max());
}

size_t LiveFir::process(const uint8_t *in, uint8_t *out, size_t frames) {
  auto next = desired.load();
  auto planes = scratch.planes();
  for (size_t at = 0; at < frames;) {
    if (active != next && wet.holding(0)) {
      active = next;
      if (active) {
        active->filter->start(input);
        // Let an arriving filter see its entire history before it becomes
        // audible.
        warmup = lag + active->filter->preparedHistory();
      } else {
        warmup = 0;
      }
    }
    wet.aim(active && active == next && !warmup ? 1.0 : 0.0, input.sampleRate);
    auto n = std::min(kPlanarFrames, frames - at);
    if (warmup)
      n = std::min(n, warmup);
    const auto mix = wet.take(n);
    n = mix.frames;
    const auto *source =
        in ? in + at * 2 * sampleSize(input.sampleFormat) : nullptr;
    if (source)
      readPlanar(source, input.sampleFormat, 2, n, planes);
    if (active)
      active->filter->process(source,
                              reinterpret_cast<uint8_t *>(filtered.data()), n);
    for (size_t i = 0; i < n; ++i) {
      auto sample = dry[cursor];
      dry[cursor] = source ? std::array{planes[0][i], planes[1][i]}
                           : std::array{0.0, 0.0};
      if (++cursor == lag)
        cursor = 0;
      if (mix.from != 0.0 || mix.to != 0.0) {
        const double g = mix.from + (mix.to - mix.from) * (double(i + 1) / n);
        for (size_t c = 0; c < 2; ++c)
          sample[c] = std::lerp(sample[c], filtered[i][c], g);
      }
      std::memcpy(out + (at + i) * sizeof(sample), sample.data(),
                  sizeof(sample));
    }
    // The output meter should judge the selected filter after the old filter
    // has faded out and the new one has acquired its history and faded in.
    const double target = next ? 1.0 : 0.0;
    if (mix.from == target && mix.to == target && active == next)
      settledFrames += n;
    else
      settledFrames = 0;
    if (warmup)
      warmup -= n;
    at += n;
  }
  return frames;
}
