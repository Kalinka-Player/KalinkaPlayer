#include "Measure.h"

#include "Analysis.h"
#include "ReferenceFilters.h"
#include "Signals.h"

#include <soxr.h>

#include <cmath>
#include <cstdio>
#include <memory>

namespace bench {
namespace {
constexpr double kRate = 44100;
// One step of a 24-bit integer at full scale ±1.
constexpr double kLsb24 = 1.0 / (1 << 23);

std::vector<double> concat(std::vector<double> a, const std::vector<double> &b) {
  a.insert(a.end(), b.begin(), b.end());
  return a;
}

std::vector<double> throughPeq(std::span<const double> in) {
  harness::BiquadCascade peq({harness::peaking(1000, 6, 2, kRate),
                              harness::lowShelf(100, 6, 1, kRate)});
  std::vector<double> out(in.size());
  peq.process(in, out);
  return out;
}

std::vector<double> throughFir(std::span<const double> in,
                               const std::vector<double> &fir) {
  const size_t latency = (fir.size() - 1) / 2;
  const auto full = harness::convolve(in, fir);
  return {full.begin() + long(latency), full.begin() + long(latency + in.size())};
}

std::vector<double> throughResampler(std::span<const double> in, double to) {
  const auto io = soxr_io_spec(SOXR_FLOAT64_I, SOXR_FLOAT64_I);
  const auto quality = soxr_quality_spec(SOXR_VHQ, 0);
  std::unique_ptr<soxr, decltype(&soxr_delete)> engine(
      soxr_create(kRate, to, 1, nullptr, &io, &quality, nullptr), soxr_delete);
  std::vector<double> out(size_t(double(in.size()) * to / kRate) + 4096);
  size_t used = 0, made = 0, total = 0;
  soxr_process(engine.get(), in.data(), in.size(), &used, out.data(),
               out.size(), &made);
  total += made;
  do {
    soxr_process(engine.get(), nullptr, 0, nullptr, out.data() + total,
                 out.size() - total, &made);
    total += made;
  } while (made > 0 && total < out.size());
  out.resize(total);
  return out;
}

void report(const char *stage, const std::vector<double> &continuous,
            const std::vector<double> &perStream, size_t join, double rate,
            double allowedStep) {
  const size_t length = std::min(continuous.size(), perStream.size());
  double peak = 0, jump = 0;
  size_t first = length, last = 0;
  for (size_t n = 0; n < length; ++n) {
    const double error = perStream[n] - continuous[n];
    peak = std::max(peak, std::abs(error));
    if (std::abs(error) > kLsb24) {
      first = std::min(first, n);
      last = n;
    }
    if (n > 0 && n + 1 >= join && n <= join + 1) {
      jump = std::max(jump,
                      std::abs(error - (perStream[n - 1] - continuous[n - 1])));
    }
  }
  const auto clicks = harness::findClicks(
      std::span(perStream).subspan(join - 2, 5), 0, rate, 0, allowedStep);
  const auto db = [](double x) { return x > 0 ? 20 * std::log10(x) : -400.0; };
  std::printf("| %s | %.1f dBFS | %.1f dBFS | %.1f ms, from %.1f ms before "
              "the join | %s |\n",
              stage, db(peak), db(jump),
              first < length ? 1000 * double(last - first + 1) / rate : 0.0,
              first < join ? 1000 * double(join - first) / rate : 0.0,
              clicks.at.empty() ? "no" : "yes");
}
} // namespace

void runJoin() {
  const size_t length = 6 * size_t(kRate), join = 3 * size_t(kRate) + 123;
  const auto signal = harness::multitone(std::vector<double>{55, 440, 3520},
                                         kRate, length, 0.5);
  const std::span all(signal);
  const auto first = all.first(join), second = all.subspan(join);
  const auto largestStep = [](const std::vector<double> &x) {
    double step = 0;
    for (size_t n = 1; n < x.size(); ++n) {
      step = std::max(step, std::abs(x[n] - x[n - 1]));
    }
    return step;
  };

  std::printf("One continuous 6 s multitone (55, 440, 3520 Hz, peak -6 dBFS) "
              "at 44.1 kHz, split in two.\nPer stream: each part through its "
              "own filter from silence, the first part's tail flushed with "
              "silence and cut at its end. Continuously: the whole signal "
              "through one filter. The error is per stream minus "
              "continuously; the jump is how far the error moves between two "
              "samples at the join; a click is a step at the join larger than "
              "any the continuous output makes.\n\n");
  std::printf("| Stage | Largest error | Jump at the join | Error above one "
              "24-bit step lasts | Click |\n|---|---|---|---|---|\n");

  const auto peqWhole = throughPeq(all);
  report("PEQ: 1 kHz +6 dB Q 2, 100 Hz low shelf +6 dB, split at 3 s + 123",
         peqWhole, concat(throughPeq(first), throughPeq(second)), join, kRate,
         largestStep(peqWhole));

  const auto fir = harness::linearPhaseFir(65535, kRate, [](double f) {
    if (f <= 0) {
      return 0.0;
    }
    const double octaves = std::log2(f / 50);
    return 6 * std::exp(-octaves * octaves / (2 * 0.25 * 0.25)) -
           3 / (1 + std::pow(8000 / f, 4));
  });
  const auto firWhole = throughFir(all, fir);
  report("FIR: 65535 taps, linear phase, +6 dB at 50 Hz over a quarter "
         "octave, split at 3 s + 123",
         firWhole, concat(throughFir(first, fir), throughFir(second, fir)),
         join, kRate, largestStep(firWhole));

  // 44.1 to 96 kHz is 147 in to 320 out: only a split on a multiple of 147
  // input frames starts the second part on the continuous output's grid.
  const double to = 96000;
  const auto soxrWhole = throughResampler(all, to);
  for (const size_t at : {join, size_t(3 * kRate)}) {
    const std::string stage =
        std::string("Resampler: libsoxr VHQ, 44.1 to 96 kHz, split at 3 s") +
        (at == join ? " + 123, off the 147-frame grid" : ", on the grid");
    report(stage.c_str(), soxrWhole,
           concat(throughResampler(all.first(at), to),
                  throughResampler(all.subspan(at), to)),
           size_t(double(at) * to / kRate), to, largestStep(soxrWhole));
  }
}

} // namespace bench
