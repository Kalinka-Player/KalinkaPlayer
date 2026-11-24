#include "dsp/fir/LiveFir.h"

#include <algorithm>
#include <cmath>
#include <cstring>

void LiveFir::select(std::shared_ptr<const FirFilterBank> bank) {
  if (bank && std::ranges::find(retained, bank) == retained.end())
    retained.push_back(bank);
  requested.store(std::move(bank));
  std::erase_if(retained, [](const auto &old) {
    return old.use_count() == 1 &&
           std::ranges::none_of(old->filters, [](const auto &entry) {
             return entry.second.use_count() > 1;
           });
  });
}

std::shared_ptr<FirRoomCorrection>
LiveFir::filterFor(const StreamAudioFormat &f) const {
  const auto bank = requested.load();
  if (!bank || !handles(f))
    return {};
  const auto it = bank->filters.find(f.sampleRate);
  return it == bank->filters.end() ? nullptr : it->second;
}

bool LiveFir::handles(const StreamAudioFormat &f) const {
  return f.channels == 2 && !isDsd(f.sampleFormat);
}

DspProcessor::Output LiveFir::output(const StreamAudioFormat &f) const {
  auto result = f;
  result.bitsPerSample = 64;
  result.sampleFormat = PCM_FLOAT64_LE;
  return {result, 1};
}

SignalPath LiveFir::describe(const StreamAudioFormat &f) const {
  const auto plan = filterFor(f);
  return plan ? plan->describe(f) : SignalPath{};
}

size_t LiveFir::start(const StreamAudioFormat &f) {
  input = f;
  active = filterFor(f);
  if (active)
    active->start(input);
  wet.settle(active ? 1.0 : 0.0);
  dry.fill({});
  cursor = warmup = settledFrames = 0;
  return lag;
}

bool LiveFir::changeInputFormat(const StreamAudioFormat &f) {
  if (f.sampleRate != input.sampleRate || !handles(f))
    return false;
  input = f;
  return !active || active->changeInputFormat(f);
}

size_t LiveFir::historyFor(const StreamAudioFormat &format) const {
  const auto plan = filterFor(format);
  return plan ? plan->preparedHistory() : 0;
}

size_t LiveFir::settledOutputFrames() const {
  if (filterFor(input) != active)
    return 0;
  return std::min<uint64_t>(settledFrames, std::numeric_limits<size_t>::max());
}

size_t LiveFir::process(const uint8_t *in, uint8_t *out, size_t frames) {
  auto next = filterFor(input);
  auto planes = scratch.planes();
  for (size_t at = 0; at < frames;) {
    if (active != next && wet.holding(0)) {
      active = next;
      if (active) {
        active->start(input);
        // Let an arriving filter see its entire history before it becomes
        // audible.
        warmup = lag + active->preparedHistory();
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
      active->process(source, reinterpret_cast<uint8_t *>(filtered.data()), n);
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
