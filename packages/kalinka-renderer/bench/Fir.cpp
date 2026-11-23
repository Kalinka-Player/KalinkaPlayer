#include "Measure.h"
#include "dsp/fir/FirRoomCorrection.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <random>
#include <span>
#include <time.h>
#include <vector>

namespace bench {
namespace {
constexpr size_t taps = 65536, callback = 256;
constexpr size_t callbacksPerRun = 4 * taps / callback;
constexpr unsigned runs = 5;

double cpuTime() {
  timespec time{};
  clock_gettime(CLOCK_THREAD_CPUTIME_ID, &time);
  return double(time.tv_sec) + double(time.tv_nsec) * 1e-9;
}

void measure(const char *signal, unsigned rate,
             FirRoomCorrection::Partitions sizes, std::span<const double> left,
             std::span<const double> right, std::span<const double> pcm,
             bool measureSilence = false) {
  FirRoomCorrection fir(rate, left, right, sizes);
  fir.start({rate, 2, 64, PCM_FLOAT64_LE});
  std::array<double, callback * 2> out;
  const std::array<double, callback * 2> silence{};
  auto process = [&](const double *input) {
    fir.process(reinterpret_cast<const uint8_t *>(input),
                reinterpret_cast<uint8_t *>(out.data()), callback);
  };
  auto input = [&](size_t i) {
    return pcm.data() + 2 * ((i * callback) % taps);
  };
  const auto impulseFrames = std::max(left.size(), right.size());
  for (size_t i = 0; i * callback < impulseFrames + sizes.head; ++i)
    process(input(i));
  if (measureSilence)
    for (size_t at = 0; at < impulseFrames + sizes.head; at += callback)
      process(silence.data());

  std::array<double, runs> cpu;
  std::vector<double> calls(runs * callbacksPerRun);
  size_t at = 0;
  for (auto &percent : cpu) {
    const auto start = cpuTime();
    for (size_t i = 0; i < callbacksPerRun; ++i) {
      const auto begin = std::chrono::steady_clock::now();
      process(measureSilence ? silence.data() : input(i));
      calls[at++] = std::chrono::duration<double, std::micro>(
                        std::chrono::steady_clock::now() - begin)
                        .count();
    }
    percent = 100 * (cpuTime() - start) * rate / (callbacksPerRun * callback);
  }
  std::ranges::sort(cpu);
  std::ranges::sort(calls);
  std::printf("| %s | %u | %zu | %zu/%zu | %.2f | %.3f | %.0f | %.0f |\n",
              signal, rate, impulseFrames, sizes.head, sizes.tail,
              1000.0 * sizes.head / rate, cpu[runs / 2],
              calls[calls.size() * 99 / 100], calls.back());
  std::fflush(stdout);
}
} // namespace

void runFir() {
  std::mt19937 random(431);
  std::uniform_real_distribution<double> noise(-1, 1);
  std::vector<double> left(taps), right(taps), input(taps * 2);
  for (auto *ir : {&left, &right})
    for (auto &tap : *ir)
      tap = 0.001 * noise(random);
  for (auto &sample : input)
    sample = 0.5 * noise(random);
  std::puts("| Signal / impulse | Hz | Taps | FIR partitions (frames) | Buffer "
            "(ms) | CPU (% of "
            "one core) | "
            "p99 callback (us) | Max callback (us) "
            "|\n|---|---:|---:|---|---:|---:|---:|---:|");
  for (FirRoomCorrection::Partitions sizes :
       {FirRoomCorrection::Partitions{640, 640},
        {4096, 4096},
        {256, 8192},
        {512, 5120},
        FirRoomCorrection::partitionsFor(taps),
        FirRoomCorrection::kPartitions,
        {1024, 8192}})
    measure("Audio / dense", 44100, sizes, left, right, input);
  measure("Silence after tail / dense", 44100,
          FirRoomCorrection::partitionsFor(taps), left, right, input, true);
  measure("Audio / dense", 192000, FirRoomCorrection::partitionsFor(taps), left,
          right, input);
  std::fill(left.begin() + 4096, left.end(), 0.0);
  std::fill(right.begin() + 4096, right.end(), 0.0);
  measure("Audio / 4096 taps padded to 65536", 44100,
          FirRoomCorrection::partitionsFor(4096), left, right, input);
  for (auto *ir : {&left, &right}) {
    ir->resize(FirRoomCorrection::kMaxTaps);
    for (auto &tap : *ir)
      tap = 0.001 * noise(random);
  }
  measure("Audio / dense", 192000,
          FirRoomCorrection::partitionsFor(left.size()), left, right, input);
  measure("Audio / dense", 192000, {4096, 4096}, left, right, input);
  measure("Audio / dense", 192000, {8192, 8192}, left, right, input);
  measure("Audio / dense", 192000, {16384, 16384}, left, right, input);
}
} // namespace bench
