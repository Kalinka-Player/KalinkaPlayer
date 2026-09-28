#ifndef AUDIO_PLAYER_H
#define AUDIO_PLAYER_H

#include <list>
#include <memory>
#include <mutex>
#include <string>

#include "AlsaAudioEmitter.h"
#include "AlsaVolumeControl.h"
#include "Config.h"

struct StreamState;
class AudioStreamSwitcher;
struct StreamNodes;
class StateMonitor;

enum AudioFormat { FormatFlac = 0, FormatMpeg, FormatVorbis };

using StreamId = size_t;

class AudioPlayer {
public:
  AudioPlayer(const Config &config);
  ~AudioPlayer();

  // Append a stream under a caller-assigned id (the play queue owns id
  // allocation; ids must be unique among live streams). Playback starts
  // automatically if no non-finished stream is currently active (e.g. after
  // stop() or clearAll()). startOffsetMs begins the stream partway in, so no
  // seek follows the append.
  void append(StreamId id, const std::string &url,
              const AudioFormat format = AudioFormat::FormatFlac,
              size_t startOffsetMs = 0);

  // Remove a stream from the playback queue by StreamId.
  void remove(StreamId id);

  // Remove all streams from the queue but keep the stream switcher connected
  // to the emitter. Unlike stop(), the audio device stays open and playback
  // resumes automatically when a new stream is appended.
  void clearAll();

  // Stop playback and close the device.
  void stop();

  // Pause the playback.
  // Note that if paused for too long, the http stream may be closed by the
  // server. The API supports reconnection but depending on the URL, it might
  // expire by the time the player tries to reconnect.
  void pause();

  // Resume the playback after pause().
  void resume();

  size_t seek(size_t positionMs);

  // Retrieves the current state of the node (non-blocking)
  StreamState getState();

  // Returns all state changes one by one as they occur.
  // Waits for the state change if no new state has been set yet.
  std::unique_ptr<StateMonitor> monitor();

  // (Re)configure volume handling for the currently selected ALSA output.
  // `mode` is auto/hardware/software/fixed; `mixerControl` overrides the mixer
  // element (empty = auto-detect). Called when the local output device is set
  // up so its settings drive the native player; can be called again to change
  // mode at runtime. Until configured, the player is in "fixed" (no volume
  // control, bit-perfect) mode.
  /// Select a volume mode and put every inactive gain stage at unity. Returns
  /// false when a detected ALSA mixer cannot be set as the mode requires.
  bool configureVolume(const std::string &mode, const std::string &mixerControl,
                       std::string *error = nullptr);

  // Volume control for the currently selected ALSA output. Values are 0..100
  // percent. getVolume() reports supported=false when no backend applies (e.g.
  // fixed mode, or hardware mode on a card with no mixer).
  VolumeState getVolume();
  void setVolume(int percent);

  // Monitor external hardware-mixer changes so the UI can track a knob / amixer
  // / another app. Reports for as long as it is held, whatever the mode does to
  // the mixer behind it. Mirrors monitor() for stream state.
  std::unique_ptr<VolumeMonitor> volumeMonitor();

  /// Whether @p key is read as each stream is appended (those under `input.`
  /// and `decoder.`), rather than once, when the output is built.
  static bool isStreamKey(const std::string &key);

  /**
   * @brief Change a stream key for every stream appended from now on.
   *
   * Streams already appended, the one playing among them, keep what they were
   * built with. Safe to call from any thread.
   *
   * @return false, changing nothing, when @p key is not a stream key: anything
   * else needs a new AudioPlayer.
   */
  bool setStreamConfig(const std::string &key, const std::string &value);

private:
  const Config config;
  std::mutex streamConfigMutex_;
  Config streamConfig;
  std::shared_ptr<AlsaAudioEmitter> audioEmitter;
  std::shared_ptr<AudioStreamSwitcher> streamSwitcher;
  std::list<StreamNodes> streamNodesList;

  // Outlives every mixer handed to it, and is shared with whoever listens: the
  // one thing here a mode change does not replace, so it needs no guarding.
  const std::shared_ptr<VolumeEvents> volumeEvents =
      std::make_shared<VolumeEvents>();

  // Guards the volume fields below. The volume API is called off the playback
  // serial executor (so it stays responsive) and may be reconfigured at
  // runtime, so volumeMode/softwarePercent/volumeControl can be touched
  // concurrently — without this lock that is a data race / use-after-free.
  std::mutex volumeMutex_;
  std::unique_ptr<AlsaVolumeControl> volumeControl;
  // Fixed until configureVolume() runs (i.e. until the local output device is
  // set up) — so by default the player does no volume processing.
  VolumeMode volumeMode = VolumeMode::Fixed;
  int softwarePercent = 100;

  // Requires volumeMutex_ held by the caller.
  VolumeBackend activeBackend() const;

  Config currentStreamConfig();

  void disconnectAllStreams();
  void cleanUpFinishedStreams();
};

#endif
