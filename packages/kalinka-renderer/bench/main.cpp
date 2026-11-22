#include "Measure.h"

#include <spdlog/spdlog.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <sys/utsname.h>

namespace bench {
namespace {
std::string firstLine(const std::string &path) {
  std::ifstream in(path);
  std::string line;
  std::getline(in, line);
  return line;
}

// Arm's /proc/cpuinfo names no model, only the part number of the core.
std::string cpuModel() {
  std::ifstream in("/proc/cpuinfo");
  std::string line, part;
  while (std::getline(in, line)) {
    if (line.rfind("model name", 0) == 0) {
      return line.substr(line.find(':') + 2);
    }
    if (line.rfind("CPU part", 0) == 0 && part.empty()) {
      part = line.substr(line.find(':') + 2);
    }
  }
  if (part == "0xd08") {
    return "Arm Cortex-A72";
  }
  if (part == "0xd0b") {
    return "Arm Cortex-A76";
  }
  return part.empty() ? "unknown" : "Arm core, part " + part;
}

int usage() {
  std::fputs("usage: kalinka-dsp-bench kernels [NAME [RATE]] | peq-settings | "
             "simd | dsd | fixtures | join\n"
             "       kalinka-dsp-bench playback [--delay-ms N] FILE...\n"
             "       kalinka-dsp-bench make-wav FILE RATE BITS SECONDS\n",
             stderr);
  return 2;
}
} // namespace

std::string machine() {
  utsname name{};
  uname(&name);
  std::ostringstream out;
  out << "- CPU: " << cpuModel() << "\n";
  if (const auto board = firstLine("/proc/device-tree/model"); !board.empty()) {
    out << "- Board: " << board.c_str() << "\n";
  }
  out << "- Kernel: " << name.sysname << " " << name.release << " "
      << name.machine << "\n";
  out << "- Compiler: " << __VERSION__ << ", flags: " << KALINKA_BENCH_FLAGS
      << "\n";
  out << "- cpu0 governor: "
      << firstLine("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
      << ", clock now: "
      << firstLine("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
      << " kHz";
  if (const auto temp = firstLine("/sys/class/thermal/thermal_zone0/temp");
      !temp.empty()) {
    out << ", zone0: " << std::atoi(temp.c_str()) / 1000.0 << " C";
  }
  out << "\n";
  return out.str();
}

std::string cost(double ns, unsigned rate) {
  char text[64];
  std::snprintf(text, sizeof text, "%.2f ns (%.3f %%)", ns,
                percentOfCore(ns, rate));
  return text;
}

} // namespace bench

int main(int argc, char **argv) {
  spdlog::set_level(spdlog::level::warn);
  if (argc < 2) {
    return bench::usage();
  }
  const std::string command = argv[1];
  if (command == "make-wav") {
    if (argc != 6) {
      return bench::usage();
    }
    bench::writeWav(argv[2], std::atoi(argv[3]), std::atoi(argv[4]),
                    std::atoi(argv[5]));
    return 0;
  }
  std::printf("%s\n", bench::machine().c_str());
  if (command == "kernels") {
    if (argc > 4)
      return bench::usage();
    std::span<const unsigned> rates = bench::kRates;
    if (argc == 4) {
      const auto rate = std::ranges::find_if(rates, [&](unsigned hz) {
        return std::to_string(hz) == argv[3];
      });
      if (rate == rates.end())
        return bench::usage();
      rates = rates.subspan(rate - rates.begin(), 1);
    }
    bench::runKernels(argc >= 3 ? argv[2] : "", rates);
  } else if (command == "peq-settings" && argc == 2) {
    bench::runPeqSettings();
  } else if (command == "simd") {
    bench::runSimd();
  } else if (command == "dsd") {
    bench::runDsd();
  } else if (command == "fixtures") {
    bench::runFixtures();
  } else if (command == "join") {
    bench::runJoin();
  } else if (command == "playback") {
    int delayMs = 0;
    std::vector<std::string> files;
    for (int i = 2; i < argc; ++i) {
      if (std::strcmp(argv[i], "--delay-ms") == 0 && i + 1 < argc) {
        delayMs = std::atoi(argv[++i]);
      } else {
        files.emplace_back(argv[i]);
      }
    }
    if (files.empty()) {
      return bench::usage();
    }
    bench::runPlayback(files, delayMs);
  } else {
    return bench::usage();
  }
  return 0;
}
