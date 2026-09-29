#ifndef STREAM_STATE_H
#define STREAM_STATE_H

#include <chrono>
#include <cstddef>
#include <optional>
#include <string>

#include "AudioInfo.h"

// Renderer delta: repeated alias (also in AudioPlayer.h), legal for the same
// type, so a state can name its stream without depending on the player.
using StreamId = size_t;

// enum class State {
//   IDLE = 0,
//   READY,
//   BUFFERING,
//   PLAYING,
//   PAUSED,
//   FINISHED,
//   STOPPED,
//   ERROR
// };

enum class AudioGraphNodeState {
  ERROR = -1,
  STOPPED,
  PREPARING,
  STREAMING,
  PAUSED,
  FINISHED,
  SOURCE_CHANGED
};

enum class StreamErrorSource { NONE, HTTP_STREAM, AUDIO_OUTPUT, DECODER };

struct StreamError {
  StreamErrorSource source;
  std::string message;

  bool operator==(const StreamError &other) const = default;
  bool operator!=(const StreamError &other) const = default;
};

extern std::ostream &operator<<(std::ostream &os, AudioGraphNodeState state);
extern std::string stateToString(AudioGraphNodeState state);

inline long long getTimestampNs() {
  return std::chrono::steady_clock::now().time_since_epoch().count();
}

struct StreamState {
  AudioGraphNodeState state;
  long position;
  std::optional<StreamInfo> streamInfo;
  /// The output side, when the emitter knows it. Not the decoded format: a
  /// device may widen the sample format or resample.
  std::optional<DeviceInfo> deviceInfo;
  std::optional<StreamError> error;
  std::optional<StreamId> streamId;
  unsigned long long timestamp;

  StreamState(AudioGraphNodeState state, long position,
              std::optional<StreamInfo> streamInfo)
      : state(state), position(position), streamInfo(streamInfo),
        timestamp(getTimestampNs()) {}

  StreamState(AudioGraphNodeState state, StreamError error)
      : state(state), position(0), error(error), timestamp(getTimestampNs()) {}

  explicit StreamState(AudioGraphNodeState state)
      : state(state), position(0), timestamp(getTimestampNs()) {}

  // position is anchored when the state changes. A later snapshot must move
  // the running clock forward, without advancing a paused or buffering source.
  long positionAt(unsigned long long now) const {
    return position +
           (state == AudioGraphNodeState::STREAMING && now > timestamp
                ? static_cast<long>((now - timestamp) / 1000000)
                : 0);
  }

  StreamState(AudioGraphNodeState state, long position)
      : state(state), position(position), timestamp(getTimestampNs()) {}

  bool operator==(const StreamState &other) const = default;
  bool operator!=(const StreamState &other) const = default;

  std::string toString() const {
    std::string errorStr = "null";
    if (error.has_value()) {
      errorStr = "{source=" + std::to_string(static_cast<int>(error->source)) +
                 ", message=" + error->message + "}";
    }
    return "<StreamState state=" + stateToString(state) +
           ", position=" + std::to_string(position) + ", error=" + errorStr +
           ", streamInfo=" +
           (streamInfo.has_value() ? streamInfo.value().toString() : "null") +
           ", deviceInfo=" +
           (deviceInfo.has_value() ? deviceInfo.value().toString() : "null") +
           ", streamId=" +
           (streamId.has_value() ? std::to_string(*streamId) : "null") +
           ", timestamp=" + std::to_string(timestamp) + ">";
  }

  friend std::ostream &operator<<(std::ostream &os, const StreamState &state) {
    os << state.toString();
    return os;
  }
};

#endif
