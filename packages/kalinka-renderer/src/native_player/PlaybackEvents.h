#pragma once

#include "StreamState.h"

#include <array>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <optional>
#include <stop_token>
#include <variant>
#include <vector>

/// @brief The DSP clamped samples to the output's range.
struct Clipping {
  /// Samples clamped since the source began.
  uint64_t samples = 0;

  bool operator==(const Clipping &) const = default;
};

/// @brief Something that happened in playback, as opposed to a state it is in.
/// @note Trivially copyable, so the audio threads post it without allocating.
struct PlaybackEvent {
  using Kind = std::variant<Clipping>;

  /// The stream it happened to, when it concerns one.
  std::optional<StreamId> stream;
  Kind kind;

  bool operator==(const PlaybackEvent &) const = default;
};

/**
 * @brief Where playback posts what happens in it, for one listener to report.
 *
 * Each kind keeps only the event posted last, so a run of one kind reaches the
 * listener as one event carrying the latest of it, however often it was
 * posted in between.
 * @note post() may come from any thread, the audio threads included: it never
 * allocates and holds the lock only to store the event. take() belongs to the
 * one listener.
 */
class PlaybackEvents {
public:
  void post(const PlaybackEvent &event);

  /// Waits until an event was posted or @p token stops, and hands over the
  /// latest of each kind posted since the last take, in kind order; nothing
  /// once stopped.
  std::vector<PlaybackEvent> take(std::stop_token token);

private:
  std::mutex mutex;
  std::condition_variable_any posted;
  std::array<std::optional<PlaybackEvent>,
             std::variant_size_v<PlaybackEvent::Kind>>
      latest;
};
