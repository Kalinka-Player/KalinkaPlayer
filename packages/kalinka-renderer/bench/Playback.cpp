#include "Measure.h"

#include "AudioPlayer.h"
#include "StateMonitor.h"

#include <atomic>
#include <cmath>
#include <numbers>
#include <cstdio>
#include <dirent.h>
#include <fstream>
#include <map>
#include <sstream>
#include <sys/resource.h>
#include <thread>
#include <unistd.h>

namespace bench {
namespace {
struct ThreadTime {
  std::string name;
  double seconds = 0;
};

// CPU seconds of every live thread of this process, by thread id.
std::map<int, ThreadTime> threadTimes() {
  std::map<int, ThreadTime> out;
  const double tick = double(sysconf(_SC_CLK_TCK));
  DIR *dir = opendir("/proc/self/task");
  if (dir == nullptr) {
    return out;
  }
  while (const dirent *entry = readdir(dir)) {
    const int tid = std::atoi(entry->d_name);
    if (tid <= 0) {
      continue;
    }
    std::ifstream in("/proc/self/task/" + std::string(entry->d_name) +
                     "/stat");
    std::string stat((std::istreambuf_iterator<char>(in)), {});
    const auto open = stat.find('('), close = stat.rfind(')');
    if (open == std::string::npos || close == std::string::npos) {
      continue;
    }
    std::istringstream fields(stat.substr(close + 2));
    std::string field;
    double ticks = 0;
    for (int i = 0; i <= 12 && fields >> field; ++i) {
      if (i == 11 || i == 12) {
        ticks += std::stod(field);
      }
    }
    out[tid] = {stat.substr(open + 1, close - open - 1), ticks / tick};
  }
  closedir(dir);
  return out;
}

double processSeconds() {
  rusage usage{};
  getrusage(RUSAGE_SELF, &usage);
  return double(usage.ru_utime.tv_sec + usage.ru_stime.tv_sec) +
         double(usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) / 1e6;
}
} // namespace

void runPlayback(const std::vector<std::string> &files, int delayMs) {
  std::printf("Played to the end into the ALSA null device, volume fixed%s.\n\n",
              delayMs ? (", through a " + std::to_string(delayMs) +
                         " ms DelayLine")
                            .c_str()
                      : "");
  std::printf("| File | Format | Audio | Wall | CPU | Of one core in real "
              "time | CPU by thread |\n|---|---|---|---|---|---|---|\n");
  for (const auto &file : files) {
    Config config{{"output.alsa.device", "null"}};
    if (delayMs) {
      config["dsp.delay_ms"] = std::to_string(delayMs);
    }
    AudioPlayer player(config);
    auto monitor = player.monitor();
    std::map<int, ThreadTime> seen;
    std::atomic<bool> done = false;
    std::thread sampler([&seen, &done] {
      while (!done) {
        for (auto &[tid, time] : threadTimes()) {
          seen[tid] = time;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
      }
    });
    const double cpuBefore = processSeconds();
    const auto start = std::chrono::steady_clock::now();
    player.append(1, "file://" + file);
    std::optional<StreamInfo> info;
    while (true) {
      const auto state = monitor->waitState();
      if (state.state == AudioGraphNodeState::STREAMING && state.streamInfo) {
        info = state.streamInfo;
      }
      if (state.state == AudioGraphNodeState::FINISHED ||
          state.state == AudioGraphNodeState::ERROR) {
        break;
      }
    }
    const std::chrono::duration<double> wall =
        std::chrono::steady_clock::now() - start;
    const double cpu = processSeconds() - cpuBefore;
    done = true;
    sampler.join();
    player.stop();
    const double audio =
        info ? double(info->streamSize) / info->format.sampleRate : 0.0;
    std::map<std::string, double> byName;
    for (const auto &[tid, time] : seen) {
      byName[time.name] += time.seconds;
    }
    std::string threads;
    for (const auto &[name, seconds] : byName) {
      char part[96];
      std::snprintf(part, sizeof part, "%s%s %.2f s", threads.empty() ? "" : ", ",
                    name.c_str(), seconds);
      threads += part;
    }
    const auto slash = file.rfind('/');
    std::printf("| %s | %u-bit %.1f kHz | %.1f s | %.2f s | %.2f s | %.2f %% | "
                "%s |\n",
                file.substr(slash == std::string::npos ? 0 : slash + 1).c_str(),
                info ? info->format.bitsPerSample : 0,
                info ? info->format.sampleRate / 1000.0 : 0.0, audio,
                wall.count(), cpu, audio > 0 ? 100 * cpu / audio : 0.0,
                threads.c_str());
    std::fflush(stdout);
  }
}

void writeWav(const std::string &path, unsigned rate, unsigned bits,
              unsigned seconds) {
  // Pink noise under three tones, so a FLAC encoder works as hard as on dense
  // music rather than on silence or a pure tone.
  const uint64_t frames = uint64_t(rate) * seconds;
  const unsigned width = bits / 8;
  std::ofstream out(path, std::ios::binary);
  const auto put = [&out](uint64_t value, unsigned bytes) {
    for (unsigned i = 0; i < bytes; ++i) {
      out.put(char(value >> (8 * i)));
    }
  };
  const uint64_t data = frames * 2 * width;
  out.write("RIFF", 4);
  put(36 + data, 4);
  out.write("WAVEfmt ", 8);
  put(16, 4);
  put(1, 2);
  put(2, 2);
  put(rate, 4);
  put(rate * 2 * width, 4);
  put(2 * width, 2);
  put(bits, 2);
  out.write("data", 4);
  put(data, 4);
  uint64_t seed = 0x9e3779b97f4a7c15u;
  double b[2][7] = {};
  const double scale = double((1u << (bits - 1)) - 1);
  for (uint64_t n = 0; n < frames; ++n) {
    for (int c = 0; c < 2; ++c) {
      seed ^= seed << 13;
      seed ^= seed >> 7;
      seed ^= seed << 17;
      const double white = double(int64_t(seed)) / 9.2233720368547758e18;
      // Paul Kellet's pink filter.
      double *p = b[c];
      p[0] = 0.99886 * p[0] + white * 0.0555179;
      p[1] = 0.99332 * p[1] + white * 0.0750759;
      p[2] = 0.96900 * p[2] + white * 0.1538520;
      p[3] = 0.86650 * p[3] + white * 0.3104856;
      p[4] = 0.55000 * p[4] + white * 0.5329522;
      p[5] = -0.7616 * p[5] - white * 0.0168980;
      const double pink =
          (p[0] + p[1] + p[2] + p[3] + p[4] + p[5] + p[6] + white * 0.5362) *
          0.02;
      p[6] = white * 0.115926;
      const double t = double(n) / rate;
      const double w = 2 * std::numbers::pi * t;
      const double tones = 0.1 * std::sin(w * 110) +
                           0.05 * std::sin(w * (440 + c)) +
                           0.02 * std::sin(w * 3520);
      put(uint64_t(int64_t(std::lround((pink + tones) * scale))), width);
    }
  }
}

} // namespace bench
