#include "Measure.h"
#include "Simd.h"

#include "Analysis.h"
#include "AudioSampleFormat.h"
#include "ByteRing.h"
#include "ReferenceFilters.h"
#include "SigmaDelta.h"
#include "Signals.h"
#include "dsp/DelayLine.h"
#include "dsp/DspStage.h"
#include "dsp/output/OutputStage.h"

#include <soxr.h>

#include <algorithm>
#include <cmath>
#include <condition_variable>
#include <cstdio>
#include <cstring>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <random>
#include <thread>

namespace bench {
namespace {
constexpr size_t kBlock = 1024;

/// A stereo S32 source that never runs dry, for timing what reads it.
class EndlessSource : public AudioGraphOutputNode {
public:
  explicit EndlessSource(unsigned rate) : AudioGraphNode(StreamId(1)) {
    pattern.resize(kBlock * 2);
    for (size_t i = 0; i < pattern.size(); ++i) {
      pattern[i] = int32_t(i * 2654435761u);
    }
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.format = {.sampleRate = rate,
                                    .channels = 2,
                                    .bitsPerSample = 32,
                                    .sampleFormat = PCM32_LE},
                         .streamType = StreamType::FRAMES}});
  }
  size_t read(void *data, size_t size) override {
    size = std::min(size, pattern.size() * 4);
    std::memcpy(data, pattern.data(), size);
    position += size / 8;
    return size;
  }
  size_t waitForData(std::stop_token, size_t size) override { return size; }
  size_t waitForDataFor(std::stop_token, std::chrono::milliseconds,
                        size_t size) override {
    return size;
  }
  std::optional<long> streamReadPosition() const override {
    return long(position);
  }

private:
  std::vector<int32_t> pattern;
  size_t position = 0;
};

using Factory = std::function<std::function<void(size_t)>(unsigned rate)>;

/// The decoder's buffer before ByteRing: a deque under one lock, copied by
/// iterator and notifying the other side on every call.
class DequeBuffer {
public:
  explicit DequeBuffer(size_t capacity) : capacity(capacity) {}
  size_t write(const void *source, size_t bytes) {
    size_t count = 0;
    {
      std::lock_guard lock(mutex);
      count = std::min(bytes, capacity - data.size());
      const auto *from = static_cast<const uint8_t *>(source);
      data.insert(data.end(), from, from + count);
    }
    if (count)
      hasData.notify_one();
    return count;
  }
  size_t read(void *dest, size_t bytes) {
    size_t count = 0;
    {
      std::lock_guard lock(mutex);
      count = std::min(data.size(), bytes);
      std::copy_n(data.begin(), count, static_cast<uint8_t *>(dest));
      data.erase(data.begin(), data.begin() + count);
    }
    if (count)
      hasSpace.notify_one();
    return count;
  }
  size_t waitForData(std::stop_token token, size_t bytes) {
    std::unique_lock lock(mutex);
    hasData.wait(lock, token,
                 [&] { return data.size() >= std::min(bytes, capacity); });
    return data.size();
  }
  size_t waitForSpace(std::stop_token token, size_t bytes) {
    std::unique_lock lock(mutex);
    hasSpace.wait(lock, token, [&] {
      return capacity - data.size() >= std::min(bytes, capacity);
    });
    return capacity - data.size();
  }

private:
  std::deque<uint8_t> data;
  std::mutex mutex;
  std::condition_variable_any hasData, hasSpace;
  const size_t capacity;
};

/// Half a second of stereo S32 at 192 kHz, as a decoder's buffer holds.
constexpr size_t kQueueBytes = 1536000;

template <typename Queue> Factory oneThread() {
  return [](unsigned) {
    auto queue = std::make_shared<Queue>(kQueueBytes);
    auto bytes = std::make_shared<std::vector<uint8_t>>(kBlock * 8, 1);
    return [queue, bytes](size_t frames) {
      queue->write(bytes->data(), frames * 8);
      queue->read(bytes->data(), frames * 8);
    };
  };
}

/// A writer on a thread of its own keeping @p Queue full, as a decoder does.
template <typename Queue> class Fed {
public:
  Fed() : writer([this](std::stop_token token) { fill(token); }) {}
  /// Reads @p bytes, waiting for the writer where it must.
  void take(void *dest, size_t bytes) {
    for (size_t got = 0; got < bytes;) {
      queue.waitForData({}, bytes - got);
      got += queue.read(static_cast<uint8_t *>(dest) + got, bytes - got);
    }
  }

private:
  Queue queue{kQueueBytes};
  std::jthread writer;

  void fill(std::stop_token token) {
    const std::vector<uint8_t> block(kBlock * 8, 1);
    while (!token.stop_requested()) {
      queue.waitForSpace(token, block.size());
      queue.write(block.data(), block.size());
    }
  }
};

template <typename Queue> Factory twoThreads() {
  return [](unsigned) {
    auto fed = std::make_shared<Fed<Queue>>();
    auto out = std::make_shared<std::vector<uint8_t>>(kBlock * 8);
    return [fed, out](size_t frames) { fed->take(out->data(), frames * 8); };
  };
}

std::shared_ptr<peq::Profile> peakBands(unsigned count) {
  auto profile = std::make_shared<peq::Profile>();
  for (unsigned i = 0; i < count; ++i)
    profile->shared.push_back(
        {peq::Type::Peak, 30.0 * std::pow(1.35, i), i % 2 ? -3.0 : 2.0, 1.4});
  return profile;
}

std::shared_ptr<peq::Profile> originalKefR3() {
  auto profile = std::make_shared<peq::Profile>();
  using enum peq::Type;
  profile->shared = {{LowShelf, 69, 2.87, 0.66}, {Peak, 245, -0.46, 2.42},
                     {Peak, 391, -0.85, 2.98},   {Peak, 646, -1.08, 2.96},
                     {Peak, 1261, 1.26, 0.49},   {Peak, 2545, 0.34, 2.79},
                     {Peak, 4966, -1.7, 2.04},   {Peak, 10361, -0.75, 1.42},
                     {Peak, 12662, 1.29, 0.72}};
  return profile;
}

Factory peqCascade(std::shared_ptr<const peq::Profile> profile,
                   bool impulseTail = false) {
  return [profile = std::move(profile), impulseTail](unsigned rate) {
    auto cascade = peq::prepare(profile.get(), rate, 2).pairs.front();
    auto input = impulseTail ? std::vector<double>(kBlock, 0.0)
                             : harness::sine(997, rate, kBlock, 0.5);
    std::array<peq::State, peq::kBands> state{};
    auto left = input, right = input;
    if (impulseTail) {
      left.front() = right.front() = 1.0;
      // Let even the 30 Hz section decay into the subnormal range before
      // timing.
      for (size_t at = 0; at < 16u * rate; at += kBlock) {
        peq::filter(cascade, state.data(), left.data(), right.data(), kBlock);
        left = right = input;
      }
    }
    return [cascade, input, left, right, state](size_t frames) mutable {
      std::copy_n(input.begin(), frames, left.begin());
      std::copy_n(input.begin(), frames, right.begin());
      peq::filter(cascade, state.data(), left.data(), right.data(), frames);
    };
  };
}

/// Software volume at @p gain on a device that keeps 24 bits of S32.
Factory outputStage(double gain, std::shared_ptr<const peq::Profile> profile = {}) {
  return [gain, profile = std::move(profile)](unsigned rate) {
    auto stage = std::make_shared<OutputStage>(
        std::vector<StreamAudioFormat>{{rate, 2, 24, PCM32_LE, 0}}, 1);
    stage->setGain(gain);
    if (profile)
      stage->setRoomEq({profile, -6.0});
    stage->connectTo(std::make_shared<EndlessSource>(rate));
    auto out = std::make_shared<std::vector<uint8_t>>(kBlock * 8);
    return
        [stage, out](size_t frames) { stage->read(out->data(), frames * 8); };
  };
}

struct Kernel {
  const char *name;
  Factory make;
};

std::vector<Kernel> kernels() {
  return {
      {"std::deque buffer write + read, S32 stereo", oneThread<DequeBuffer>()},
      {"ByteRing write + read, S32 stereo", oneThread<ByteRing>()},
      {"std::deque buffer read, written from another thread",
       twoThreads<DequeBuffer>()},
      {"ByteRing read, written from another thread", twoThreads<ByteRing>()},
      {"DspStage without a processor, S32 stereo",
       [](unsigned rate) {
         auto source = std::make_shared<EndlessSource>(rate);
         auto stage = std::make_shared<DspStage>();
         stage->connectTo(source);
         auto out = std::make_shared<std::vector<uint8_t>>(kBlock * 8);
         return [stage, out](size_t frames) {
           stage->read(out->data(), frames * 8);
         };
       }},
      {"DspStage with a 300 ms DelayLine, S32 stereo",
       [](unsigned rate) {
         auto source = std::make_shared<EndlessSource>(rate);
         auto stage = std::make_shared<DspStage>(
             std::make_unique<DelayLine>(std::chrono::milliseconds(300)));
         stage->connectTo(source);
         auto out = std::make_shared<std::vector<uint8_t>>(kBlock * 8);
         return [stage, out](size_t frames) {
           stage->read(out->data(), frames * 8);
         };
       }},
      {"convertSampleFormat S16 to S32, stereo",
       [](unsigned) {
         auto in = std::make_shared<std::vector<int16_t>>(kBlock * 2, 1234);
         auto out = std::make_shared<std::vector<int32_t>>(kBlock * 2);
         return [in, out](size_t frames) {
           convertSampleFormat(in->data(), PCM16_LE, frames * 2, out->data(),
                               PCM32_LE, out->size() * 4);
         };
       }},
      {"OutputStage at unity, S32 stereo", outputStage(1.0)},
      {"OutputStage at -6 dB, S32 stereo, dithered at 24 bits",
       outputStage(0.5)},
      {"5 PEQ bands, planar float64 stereo", peqCascade(peakBands(5))},
      {"9 PEQ bands, original KEF R3", peqCascade(originalKefR3())},
      {"19 PEQ bands, planar float64 stereo", peqCascade(peakBands(19))},
      {"20 PEQ bands, planar float64 stereo", peqCascade(peakBands(20))},
      {"20 PEQ bands, silence after an impulse",
       peqCascade(peakBands(20), true)},
      {"OutputStage with 9 PEQ bands (original KEF R3), S32 stereo, dithered at 24 bits",
       outputStage(1.0, originalKefR3())},
      {"OutputStage with 20 PEQ bands, S32 stereo, dithered at 24 bits",
       outputStage(1.0, peakBands(20))},
      {"libsoxr VHQ, float64, 2x up, stereo",
       [](unsigned rate) {
         const auto io = soxr_io_spec(SOXR_FLOAT64_I, SOXR_FLOAT64_I);
         const auto quality = soxr_quality_spec(SOXR_VHQ, 0);
         std::shared_ptr<soxr> engine(
             soxr_create(rate, 2.0 * rate, 2, nullptr, &io, &quality, nullptr),
             soxr_delete);
         auto in = std::make_shared<std::vector<double>>(
             harness::sine(997, rate, kBlock * 2, 0.5));
         auto out = std::make_shared<std::vector<double>>(kBlock * 2 * 2 + 64);
         return [engine, in, out](size_t frames) {
           size_t used = 0, made = 0;
           soxr_process(engine.get(), in->data(), frames, &used, out->data(),
                        out->size() / 2, &made);
         };
       }},
  };
}

/// Left and right planar, with `taps` frames of history before frame 0.
struct Planar {
  std::vector<double> left, right;
};

Planar noise(size_t frames, size_t history) {
  std::mt19937_64 generator(7);
  std::uniform_real_distribution<double> uniform(-0.5, 0.5);
  Planar out{std::vector<double>(frames + history),
             std::vector<double>(frames + history)};
  for (size_t i = 0; i < frames + history; ++i) {
    out.left[i] = uniform(generator);
    out.right[i] = uniform(generator);
  }
  return out;
}

double worstDb(const std::vector<double> &a, const std::vector<double> &b) {
  double worst = 0;
  for (size_t i = 0; i < a.size(); ++i) {
    worst = std::max(worst, std::abs(a[i] - b[i]));
  }
  return worst > 0 ? 20 * std::log10(worst) : -400;
}
} // namespace

void runKernels(std::string_view filter, std::span<const unsigned> rates) {
  std::printf("| Kernel |");
  for (unsigned rate : rates) {
    std::printf(" %.1f kHz |", rate / 1000.0);
  }
  std::printf("\n|---|");
  for (size_t i = 0; i < rates.size(); ++i) {
    std::printf("---|");
  }
  std::printf("\n");
  for (const auto &kernel : kernels()) {
    if (!std::string_view(kernel.name).contains(filter))
      continue;
    std::printf("| %s |", kernel.name);
    for (unsigned rate : rates) {
      auto process = kernel.make(rate);
      const double ns = nsPerFrame(process, kBlock, std::max(rate, 200000u));
      std::printf(" %s |", cost(ns, rate).c_str());
    }
    std::printf("\n");
    std::fflush(stdout);
  }
}

void runPeqSettings() {
  constexpr unsigned rate = 192000;
  std::printf("| Active PEQ bands at 192 kHz | Change profile (us/edit) | "
              "Change preamp (us/edit) |\n|---:|---:|---:|\n");
  for (const auto &profile : {originalKefR3(), peakBands(20)}) {
    OutputStage stage({}, 1);
    stage.connectTo(std::make_shared<EndlessSource>(rate));
    auto alternate = std::make_shared<peq::Profile>(*profile);
    alternate->shared.back().db += 0.25;
    bool toggle = false;
    const double select = nsPerFrame(
        [&](size_t) {
          toggle = !toggle;
          stage.setRoomEq({toggle ? profile : alternate, -6.0});
        },
        1, 32);
    stage.setRoomEq({profile, -6.0});
    const double preamp = nsPerFrame(
        [&](size_t) {
          toggle = !toggle;
          stage.setRoomEq({profile, toggle ? -5.5 : -6.0});
        },
        1, 128);
    std::printf("| %zu | %.3f | %.3f |\n", profile->shared.size(),
                select / 1000, preamp / 1000);
  }
}

void runSimd() {
  const auto mechanisms = simdKernels();
  const size_t frames = 1 << 16;
  std::printf("Null against the scalar reference is the largest absolute "
              "difference, in dB below full scale.\n\n");
  std::printf(
      "| Mechanism | FIR 64 taps, f64 stereo | FIR 256 taps, f64 stereo "
      "| 10 biquads, f64 stereo | FIR null | biquad null |\n");
  std::printf("|---|---|---|---|---|---|\n");
  std::vector<double> referenceFir, referenceBiquad;
  for (const auto &m : mechanisms) {
    if (!m.available()) {
      std::printf("| %s | not on this CPU | | | | |\n", m.name);
      continue;
    }
    std::string fir[2];
    std::vector<double> firOut;
    for (int which = 0; which < 2; ++which) {
      const size_t taps = which == 0 ? 64 : 256;
      auto input = noise(frames, taps - 1);
      std::vector<double> h(taps);
      for (size_t k = 0; k < taps; ++k) {
        h[k] = std::sin(0.37 * double(k + 1)) / double(k + 1);
      }
      std::vector<double> left(frames), right(frames);
      const auto run = [&](size_t) {
        m.fir(input.left.data() + taps - 1, h.data(), taps, left.data(),
              frames);
        m.fir(input.right.data() + taps - 1, h.data(), taps, right.data(),
              frames);
      };
      fir[which] = cost(nsPerFrame(run, frames, frames, 5), 44100);
      if (which == 1) {
        firOut = left;
      }
    }
    const size_t sections = 10;
    std::vector<double> coefficients;
    for (size_t s = 0; s < sections; ++s) {
      const auto c = harness::peaking(100.0 * (s + 1), 3.0, 1.0, 44100);
      for (double v : {c.b0, c.b1, c.b2, c.a1, c.a2}) {
        coefficients.push_back(v);
        coefficients.push_back(v);
      }
    }
    auto input = noise(frames, 0);
    std::vector<double> interleaved(2 * frames), out(2 * frames),
        state(4 * sections, 0.0);
    for (size_t i = 0; i < frames; ++i) {
      interleaved[2 * i] = input.left[i];
      interleaved[2 * i + 1] = input.right[i];
    }
    const auto run = [&](size_t) {
      m.biquad(interleaved.data(), out.data(), frames, coefficients.data(),
               state.data(), sections);
    };
    const double biquadNs = nsPerFrame(run, frames, frames, 5);
    std::ranges::fill(state, 0.0);
    // In pieces, one shorter than the cascade, so the null also proves the
    // state carries from one call to the next.
    for (size_t from = 0; size_t piece : {frames / 2 + 3, size_t(5), frames}) {
      piece = std::min(piece, frames - from);
      m.biquad(interleaved.data() + 2 * from, out.data() + 2 * from, piece,
               coefficients.data(), state.data(), sections);
      from += piece;
    }
    if (referenceFir.empty()) {
      referenceFir = firOut;
      referenceBiquad = out;
    }
    std::printf("| %s | %s | %s | %s | %.1f dB | %.1f dB |\n", m.name,
                fir[0].c_str(), fir[1].c_str(), cost(biquadNs, 44100).c_str(),
                worstDb(firOut, referenceFir), worstDb(out, referenceBiquad));
    std::fflush(stdout);
  }
  std::printf("\nCosts in percent are of one core at 44.1 kHz; per frame they "
              "do not depend on the rate.\n");
}

void runFixtures() {
  std::printf("A 1 kHz tone at half modulation (-6 dB against full "
              "modulation) and silence, 0.5 s each, through the seventh-order "
              "modulator; THD+N and noise in 20 Hz - 20 kHz.\n\n");
  std::printf("| Rate | tone level | tone THD+N | silence noise |\n");
  std::printf("|---|---|---|---|\n");
  for (unsigned multiple : {64u, 128u, 256u}) {
    const unsigned rate = 44100 * multiple;
    harness::SigmaDelta tone(7, multiple), quiet(7, multiple);
    const auto bits = tone.modulate(harness::sine(1000, rate, rate / 2, 0.5));
    const auto idle = quiet.modulate(std::vector<double>(rate / 2, 0.0));
    std::vector<double> levels(bits.size()), idleLevels(idle.size());
    for (size_t i = 0; i < bits.size(); ++i) {
      levels[i] = bits[i] ? 1.0 : -1.0;
      idleLevels[i] = idle[i] ? 1.0 : -1.0;
    }
    const auto distortion = harness::thdPlusN(levels, rate, 1000);
    std::printf("| DSD%u | %.3f dBFS | %.1f dB | %.1f dBFS |\n", multiple,
                distortion.fundamentalDbfs, distortion.thdnDb,
                harness::bandPowerDb(idleLevels, rate, 20, 20000));
    std::fflush(stdout);
  }
}

} // namespace bench
