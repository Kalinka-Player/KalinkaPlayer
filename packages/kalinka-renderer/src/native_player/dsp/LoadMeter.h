#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>

/**
 * @brief Processing time against the length of the audio it made.
 * @note add() belongs to the thread that processes; take() may come from any
 * thread.
 */
class LoadMeter {
public:
  /// Counts @p spent making @p frames frames at @p rate frames a second.
  void add(std::chrono::nanoseconds spent, uint64_t frames, unsigned rate) {
    if (rate == 0)
      return;
    spentNs.fetch_add(uint64_t(spent.count()), std::memory_order_relaxed);
    audioNs.fetch_add(frames * 1'000'000'000ull / rate,
                      std::memory_order_relaxed);
  }

  /// The time spent over the audio made since the last take(), 0 with none
  /// made; the count starts over.
  double take() {
    const uint64_t spent = spentNs.exchange(0, std::memory_order_relaxed);
    const uint64_t audio = audioNs.exchange(0, std::memory_order_relaxed);
    return audio ? double(spent) / double(audio) : 0.0;
  }

private:
  std::atomic<uint64_t> spentNs{0};
  std::atomic<uint64_t> audioNs{0};
};
