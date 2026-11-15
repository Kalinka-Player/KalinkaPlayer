#include "HeapProbe.h"
#include "RealFft.h"
#include "ReferenceFft.h"

#include <sched.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <complex>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <limits>
#include <memory>
#include <new>
#include <optional>
#include <random>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

using Clock = std::chrono::steady_clock;
using Spectrum = std::vector<std::complex<long double>>;

constexpr size_t kSteadyCalls = 8;

struct Options {
  size_t minSize = 256;
  size_t maxSize = 131072;
  int rounds = 21;
  double batchMs = 5;
  bool accuracyOnly = false;
};

/// What one library did at one size.
struct Result {
  std::string failure;
  std::string simd;
  double planMicros = 0;
  HeapUse plan;
  double spectrumErrorDb = 0;
  double roundTripErrorDb = 0;
  HeapUse firstForward;
  HeapUse firstInverse;
  HeapUse steadyForward;
  HeapUse steadyInverse;
  double forwardNs = 0;
  double inverseNs = 0;
  double forwardSpread = 0;
  double inverseSpread = 0;
};

template <typename... Args>
std::string format(const char *pattern, Args... args) {
  const int length = std::snprintf(nullptr, 0, pattern, args...);
  std::string text(static_cast<size_t>(length), '\0');
  std::snprintf(text.data(), text.size() + 1, pattern, args...);
  return text;
}

/// Zeroed storage aligned for the widest vector load any library makes.
template <typename T> class AlignedBuffer {
public:
  explicit AlignedBuffer(size_t count)
      : storage(static_cast<T *>(
            std::aligned_alloc(kAlignment, roundUp(count * sizeof(T))))) {
    if (!storage) {
      throw std::bad_alloc();
    }
    std::fill_n(storage.get(), count, T(0));
  }

  T *data() { return storage.get(); }
  const T *data() const { return storage.get(); }

private:
  static constexpr size_t kAlignment = 64;

  static size_t roundUp(size_t bytes) {
    return (bytes + kAlignment - 1) / kAlignment * kAlignment;
  }

  struct Free {
    void operator()(T *buffer) const { std::free(buffer); }
  };

  std::unique_ptr<T, Free> storage;
};

/// One library's plan at one size, with the buffers its calls write.
template <typename T> struct Plan {
  std::unique_ptr<RealFft<T>> fft;
  AlignedBuffer<T> spectrum;
  AlignedBuffer<T> out;
};

/// Uniform in [-1, 1), rounded to T: the same on every platform.
template <typename T> std::vector<T> randomSignal(size_t n) {
  std::mt19937_64 engine(n);
  std::vector<T> x(n);
  for (T &sample : x) {
    sample = static_cast<T>(static_cast<double>(engine() >> 11) * 0x1p-52 -
                            1.0);
  }
  return x;
}

double decibels(long double errorEnergy, long double signalEnergy) {
  return 10.0 * std::log10(static_cast<double>(errorEnergy / signalEnergy));
}

template <typename Bin>
double spectrumErrorDb(const Bin &bin, const Spectrum &reference) {
  long double error = 0;
  long double signal = 0;
  for (size_t k = 0; k < reference.size(); ++k) {
    error += std::norm(bin(k) - reference[k]);
    signal += std::norm(reference[k]);
  }
  return decibels(error, signal);
}

template <typename T>
double roundTripErrorDb(const T *out, const std::vector<T> &x, double gain) {
  const long double scale =
      1.0L / (static_cast<long double>(x.size()) * gain);
  long double error = 0;
  long double signal = 0;
  for (size_t i = 0; i < x.size(); ++i) {
    const long double difference = out[i] * scale - x[i];
    error += difference * difference;
    signal += static_cast<long double>(x[i]) * x[i];
  }
  return decibels(error, signal);
}

template <typename T>
void requireUnchanged(const T *buffer, const std::vector<T> &original,
                      const std::string &what) {
  if (std::memcmp(buffer, original.data(), original.size() * sizeof(T)) != 0) {
    throw std::logic_error(what + " changed its input");
  }
}

template <typename T>
std::optional<Plan<T>> planAndCheck(const FftLibrary<T> &library, size_t n,
                                    const std::vector<T> &x,
                                    const AlignedBuffer<T> &input,
                                    const Spectrum &reference,
                                    Result &result) {
  std::unique_ptr<RealFft<T>> fft;
  const HeapProbe planning;
  const auto start = Clock::now();
  try {
    fft = library.plan(n);
  } catch (const std::exception &error) {
    result.failure = error.what();
    return std::nullopt;
  }
  result.planMicros =
      std::chrono::duration<double, std::micro>(Clock::now() - start).count();
  result.plan = planning.use();

  Plan<T> plan{std::move(fft), AlignedBuffer<T>(n + 2), AlignedBuffer<T>(n)};
  RealFft<T> &transform = *plan.fft;

  const HeapProbe firstForward;
  transform.forward(input.data(), plan.spectrum.data());
  result.firstForward = firstForward.use();
  const std::vector<T> spectrum(plan.spectrum.data(),
                                plan.spectrum.data() + n + 2);
  const HeapProbe firstInverse;
  transform.inverse(plan.spectrum.data(), plan.out.data());
  result.firstInverse = firstInverse.use();
  requireUnchanged(input.data(), x, library.name + " forward");
  requireUnchanged(plan.spectrum.data(), spectrum, library.name + " inverse");

  AlignedBuffer<T> bins(n + 2);
  transform.toBins(plan.spectrum.data(), bins.data());
  result.spectrumErrorDb = spectrumErrorDb(
      [&](size_t k) {
        return std::complex<long double>(bins.data()[2 * k],
                                         bins.data()[2 * k + 1]);
      },
      reference);
  result.roundTripErrorDb =
      roundTripErrorDb(plan.out.data(), x, library.roundTripScale);

  const HeapProbe forwards;
  for (size_t i = 0; i < kSteadyCalls; ++i) {
    transform.forward(input.data(), plan.spectrum.data());
  }
  result.steadyForward = forwards.use();
  const HeapProbe inverses;
  for (size_t i = 0; i < kSteadyCalls; ++i) {
    transform.inverse(plan.spectrum.data(), plan.out.data());
  }
  result.steadyInverse = inverses.use();
  result.simd = transform.simd();
  return plan;
}

enum class Direction { Forward, Inverse };

template <typename T>
double nsPerCall(RealFft<T> &fft, Direction direction, const T *in, T *out,
                 size_t calls) {
  const auto start = Clock::now();
  if (direction == Direction::Forward) {
    for (size_t i = 0; i < calls; ++i) {
      fft.forward(in, out);
    }
  } else {
    for (size_t i = 0; i < calls; ++i) {
      fft.inverse(in, out);
    }
  }
  return std::chrono::duration<double, std::nano>(Clock::now() - start)
             .count() /
         static_cast<double>(calls);
}

struct Summary {
  double median;
  /// Interquartile range over the median.
  double spread;
};

Summary summarise(std::vector<double> samples) {
  std::sort(samples.begin(), samples.end());
  const auto quantile = [&](double q) {
    return samples[static_cast<size_t>(
        q * static_cast<double>(samples.size() - 1) + 0.5)];
  };
  const double median = quantile(0.5);
  return {median, (quantile(0.75) - quantile(0.25)) / median};
}

// One batch per library per round: a change of clock speed hits all alike.
template <typename T>
void timeDirection(std::vector<std::optional<Plan<T>>> &plans,
                   std::vector<Result> &results, Direction direction,
                   const AlignedBuffer<T> &input, const Options &options) {
  struct Series {
    const T *in = nullptr;
    T *out = nullptr;
    size_t calls = 1;
    std::vector<double> ns;
  };
  std::vector<Series> series(plans.size());
  const double batchNs = options.batchMs * 1e6;
  for (size_t i = 0; i < plans.size(); ++i) {
    if (!plans[i]) {
      continue;
    }
    Plan<T> &plan = *plans[i];
    Series &s = series[i];
    s.in = direction == Direction::Forward ? input.data()
                                           : plan.spectrum.data();
    s.out = direction == Direction::Forward ? plan.spectrum.data()
                                            : plan.out.data();
    while (nsPerCall(*plan.fft, direction, s.in, s.out, s.calls) *
               static_cast<double>(s.calls) <
           batchNs) {
      s.calls *= 2;
    }
  }
  for (int round = 0; round < options.rounds; ++round) {
    for (size_t i = 0; i < plans.size(); ++i) {
      if (plans[i]) {
        Series &s = series[i];
        s.ns.push_back(
            nsPerCall(*plans[i]->fft, direction, s.in, s.out, s.calls));
      }
    }
  }
  for (size_t i = 0; i < plans.size(); ++i) {
    if (!plans[i]) {
      continue;
    }
    const Summary summary = summarise(series[i].ns);
    if (direction == Direction::Forward) {
      results[i].forwardNs = summary.median;
      results[i].forwardSpread = summary.spread;
    } else {
      results[i].inverseNs = summary.median;
      results[i].inverseSpread = summary.spread;
    }
  }
}

class Table {
public:
  explicit Table(std::vector<std::string> header) : header(std::move(header)) {}

  void add(std::vector<std::string> row) { rows.push_back(std::move(row)); }

  void print() const {
    printRow(header);
    std::string rule = "|---|";
    for (size_t i = 1; i < header.size(); ++i) {
      rule += "---:|";
    }
    std::printf("%s\n", rule.c_str());
    for (const auto &row : rows) {
      printRow(row);
    }
    std::printf("\n");
  }

private:
  static void printRow(const std::vector<std::string> &cells) {
    std::string line = "|";
    for (const std::string &cell : cells) {
      line += " " + cell + " |";
    }
    std::printf("%s\n", line.c_str());
  }

  std::vector<std::string> header;
  std::vector<std::vector<std::string>> rows;
};

std::string kib(double bytes) { return format("%.1f KiB", bytes / 1024.0); }

std::string decibelCell(double db) {
  return std::isinf(db) ? std::string("exact") : format("%.1f", db);
}

std::string allocations(const HeapUse &use, double calls) {
  if (use.allocations == 0) {
    return "0";
  }
  return format("%.3g (%s)", static_cast<double>(use.allocations) / calls,
                kib(static_cast<double>(use.allocatedBytes) / calls).c_str());
}

std::string heapCell(const Result &r) {
  return allocations(r.firstForward, 1) + ", " +
         allocations(r.steadyForward, kSteadyCalls) + " / " +
         allocations(r.firstInverse, 1) + ", " +
         allocations(r.steadyInverse, kSteadyCalls);
}

template <typename T>
void printTables(const char *precision,
                 const std::vector<FftLibrary<T>> &libraries,
                 const std::vector<size_t> &sizes,
                 const std::vector<std::vector<Result>> &results,
                 const Options &options) {
  std::vector<std::string> header{"n"};
  for (const auto &library : libraries) {
    header.push_back(library.name);
  }
  const auto table = [&](const char *title, auto cell) {
    std::printf("### %s: %s\n\n", precision, title);
    Table t(header);
    for (size_t s = 0; s < sizes.size(); ++s) {
      std::vector<std::string> row{std::to_string(sizes[s])};
      for (size_t l = 0; l < libraries.size(); ++l) {
        const Result &r = results[s][l];
        row.push_back(r.failure.empty() ? cell(r, sizes[s]) : "n/a");
      }
      t.add(row);
    }
    t.print();
  };
  const auto spreadNote = [&](auto spreadOf) {
    std::vector<double> spreads;
    double worst = 0;
    std::string worstAt;
    for (size_t s = 0; s < sizes.size(); ++s) {
      for (size_t l = 0; l < libraries.size(); ++l) {
        if (!results[s][l].failure.empty()) {
          continue;
        }
        const double spread = spreadOf(results[s][l]);
        spreads.push_back(spread);
        if (spread > worst) {
          worst = spread;
          worstAt = format("%s, n = %zu", libraries[l].name.c_str(), sizes[s]);
        }
      }
    }
    if (spreads.empty()) {
      return;
    }
    std::sort(spreads.begin(), spreads.end());
    std::printf("Interquartile range over the median, across %d rounds: "
                "typically %.1f%%, at worst %.1f%% (%s).\n\n",
                options.rounds, 100 * spreads[spreads.size() / 2], 100 * worst,
                worstAt.c_str());
  };

  if (!options.accuracyOnly) {
    table("forward, median ns per transform (ns per sample)",
          [](const Result &r, size_t n) {
            return format("%.0f (%.3f)", r.forwardNs,
                          r.forwardNs / static_cast<double>(n));
          });
    spreadNote([](const Result &r) { return r.forwardSpread; });
    table("inverse, median ns per transform (ns per sample)",
          [](const Result &r, size_t n) {
            return format("%.0f (%.3f)", r.inverseNs,
                          r.inverseNs / static_cast<double>(n));
          });
    spreadNote([](const Result &r) { return r.inverseSpread; });
  }
  table("error in dB relative to the signal, forward spectrum / round trip",
        [](const Result &r, size_t) {
          return decibelCell(r.spectrumErrorDb) + " / " +
                 decibelCell(r.roundTripErrorDb);
        });
  if (options.accuracyOnly) {
    table("heap held by the plan", [](const Result &r, size_t) {
      return kib(static_cast<double>(r.plan.heldBytes));
    });
  } else {
    table("planning time, and heap held by the plan",
          [](const Result &r, size_t) {
            return format("%.0f µs, %s", r.planMicros,
                          kib(static_cast<double>(r.plan.heldBytes)).c_str());
          });
  }
  table("heap allocations per call, forward (first call, then each) / "
        "inverse (first call, then each), with KiB per call",
        [](const Result &r, size_t) { return heapCell(r); });

  std::printf("### %s: instruction set in use\n\n", precision);
  Table paths({"library", "SIMD path reported"});
  for (size_t l = 0; l < libraries.size(); ++l) {
    std::set<std::string> seen;
    std::string joined;
    for (size_t s = 0; s < sizes.size(); ++s) {
      const Result &r = results[s][l];
      if (r.failure.empty() && seen.insert(r.simd).second) {
        joined += (joined.empty() ? "" : "; ") + r.simd;
      }
    }
    paths.add({libraries[l].name, joined});
  }
  paths.print();
}

template <typename T> std::vector<FftLibrary<T>> libraries() {
  std::vector<FftLibrary<T>> list;
#ifdef FFT_BENCH_WITH_FFTW
  list.push_back(fftwLibrary<T>());
  list.push_back(fftwPreserveInputLibrary<T>());
#endif
  list.push_back(pffftLibrary<T>());
  list.push_back(pffftUnorderedLibrary<T>());
  list.push_back(pocketfftLibrary<T>());
#ifdef FFT_BENCH_WITH_KFR
  list.push_back(kfrLibrary<T>());
#endif
#ifdef FFT_BENCH_WITH_ZLDSP
  list.push_back(zldspLibrary<T>());
#endif
  return list;
}

template <typename T> void run(const char *precision, const Options &options) {
  const std::vector<FftLibrary<T>> libs = libraries<T>();
  std::vector<size_t> sizes;
  for (size_t n = options.minSize; n <= options.maxSize; n *= 2) {
    sizes.push_back(n);
  }
  std::vector<std::vector<Result>> results(sizes.size(),
                                           std::vector<Result>(libs.size()));
  for (size_t s = 0; s < sizes.size(); ++s) {
    const size_t n = sizes[s];
    std::fprintf(stderr, "%s n = %zu\n", precision, n);
    const std::vector<T> x = randomSignal<T>(n);
    AlignedBuffer<T> input(n);
    std::copy(x.begin(), x.end(), input.data());
    const Spectrum reference =
        referenceSpectrum(std::vector<long double>(x.begin(), x.end()));

    std::vector<std::optional<Plan<T>>> plans;
    for (size_t l = 0; l < libs.size(); ++l) {
      plans.push_back(
          planAndCheck(libs[l], n, x, input, reference, results[s][l]));
    }
    if (!options.accuracyOnly) {
      timeDirection(plans, results[s], Direction::Forward, input, options);
      timeDirection(plans, results[s], Direction::Inverse, input, options);
    }
  }
  printTables(precision, libs, sizes, results, options);
}

std::string cpuModel() {
  std::ifstream cpuinfo("/proc/cpuinfo");
  for (std::string line; std::getline(cpuinfo, line);) {
    if (line.starts_with("model name")) {
      return line.substr(line.find(':') + 2);
    }
  }
  std::ifstream tree("/proc/device-tree/model");
  std::string model;
  std::getline(tree, model, '\0');
  return model.empty() ? "unknown" : model;
}

std::string compiledIsa() {
  std::string isa;
#ifdef __SSE2__
  isa += " SSE2";
#endif
#ifdef __SSE4_2__
  isa += " SSE4.2";
#endif
#ifdef __AVX__
  isa += " AVX";
#endif
#ifdef __AVX2__
  isa += " AVX2";
#endif
#ifdef __FMA__
  isa += " FMA";
#endif
#ifdef __AVX512F__
  isa += " AVX-512F";
#endif
#ifdef __ARM_NEON
  isa += " NEON";
#endif
  return isa.empty() ? " none" : isa;
}

/// The worst disagreement between the radix-2 reference and direct summation.
double referenceSelfCheckDb() {
  double worst = -std::numeric_limits<double>::infinity();
  for (size_t n = 16; n <= 1024; n *= 2) {
    const std::vector<long double> x = randomSignal<long double>(n);
    const Spectrum fast = referenceSpectrum(x);
    const Spectrum slow = naiveSpectrum(x);
    worst = std::max(
        worst, spectrumErrorDb([&](size_t k) { return fast[k]; }, slow));
  }
  return worst;
}

void *volatile heapProbeSink;

void requireHeapProbeWorks() {
  const HeapProbe probe;
  heapProbeSink = ::operator new(64);
  ::operator delete(heapProbeSink);
  if (probe.use().allocations != 1) {
    throw std::runtime_error("the allocation counter does not see operator "
                             "new: malloc is not interposed");
  }
}

void printHeader(const Options &options) {
  cpu_set_t affinity;
  CPU_ZERO(&affinity);
  sched_getaffinity(0, sizeof(affinity), &affinity);
  std::printf("## Run\n\n");
  std::printf("- CPU: %s; running on CPU %d, allowed %d CPU(s)\n",
              cpuModel().c_str(), sched_getcpu(), CPU_COUNT(&affinity));
  std::printf("- Compiler: %s\n", FFT_BENCH_COMPILER);
  std::printf("- Flags: `%s`; target macros:%s\n", FFT_BENCH_FLAGS,
              compiledIsa().c_str());
  std::printf("- Libraries:");
  for (const auto &library : libraries<float>()) {
    std::printf(" %s %s;", library.name.c_str(), library.version.c_str());
  }
  std::printf("\n");
  if (options.accuracyOnly) {
    std::printf("- Accuracy and heap only: nothing was timed\n");
  } else {
    std::printf("- Timing: %d rounds of one batch (at least %.1f ms) per "
                "library, median shown\n",
                options.rounds, options.batchMs);
  }
  std::printf("- Reference: long double radix-2, %zu-bit mantissa; against "
              "direct summation at n = 16..1024 it differs by at most %.1f "
              "dB\n\n",
              static_cast<size_t>(std::numeric_limits<long double>::digits),
              referenceSelfCheckDb());
}

size_t parsePowerOfTwo(const char *text) {
  const unsigned long long value = std::strtoull(text, nullptr, 10);
  if (value < 32 || (value & (value - 1)) != 0) {
    throw std::invalid_argument(std::string("not a power of two >= 32: ") +
                                text);
  }
  return static_cast<size_t>(value);
}

Options parse(int argc, char **argv) {
  Options options;
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    const auto value = [&]() -> const char * {
      if (i + 1 >= argc) {
        throw std::invalid_argument(arg + " needs a value");
      }
      return argv[++i];
    };
    if (arg == "--min") {
      options.minSize = parsePowerOfTwo(value());
    } else if (arg == "--max") {
      options.maxSize = parsePowerOfTwo(value());
    } else if (arg == "--rounds") {
      options.rounds = std::max(1, std::atoi(value()));
    } else if (arg == "--batch-ms") {
      options.batchMs = std::atof(value());
    } else if (arg == "--accuracy-only") {
      options.accuracyOnly = true;
    } else {
      throw std::invalid_argument(
          "usage: kalinka-fft-bench [--min N] [--max N] [--rounds R] "
          "[--batch-ms MS] [--accuracy-only]");
    }
  }
  if (options.minSize > options.maxSize) {
    throw std::invalid_argument("--min is above --max");
  }
  return options;
}

} // namespace

int main(int argc, char **argv) {
  try {
    const Options options = parse(argc, argv);
    requireHeapProbeWorks();
    printHeader(options);
    run<float>("float", options);
    run<double>("double", options);
  } catch (const std::exception &error) {
    std::fprintf(stderr, "kalinka-fft-bench: %s\n", error.what());
    return 1;
  }
  return 0;
}
