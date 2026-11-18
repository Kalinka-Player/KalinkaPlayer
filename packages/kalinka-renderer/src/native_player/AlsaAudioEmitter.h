#ifndef ALSA_AUDIO_EMITTER_H
#define ALSA_AUDIO_EMITTER_H

#include "AudioGraphNode.h"
#include "Config.h"
#include "PlaybackPcm.h"
#include "Utils.h"
#include "dsp/output/OutputFade.h"

#include <alsa/asoundlib.h>

#include <atomic>
#include <chrono>
#include <functional>
#include <list>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <unordered_map>
#include <vector>

class PackedFade;

class PlayedFramesCounter {
public:
  PlayedFramesCounter() = default;
  void update(snd_pcm_sframes_t frames);

  snd_pcm_sframes_t getPlayedFrames() const { return lastPlayedFrames; }

  std::vector<snd_pcm_sframes_t> drainSequence();

  /// @return an id that cancel() takes, until the callback has run.
  int callOnOrAfterFrame(snd_pcm_sframes_t frame,
                         std::function<void(snd_pcm_sframes_t)> onFramesPlayed);

  /// Forgets callback @p id; one that has run or been cancelled is ignored.
  void cancel(int id);

  void reset();

private:
  struct Pending {
    int id;
    snd_pcm_sframes_t frame;
    std::function<void(snd_pcm_sframes_t)> onFramesPlayed;
  };

  snd_pcm_sframes_t lastPlayedFrames = 0;
  int nextId = 0;
  std::list<Pending> onFramesPlayedCallbacks;
};

/// Frames a drop cannot take back, as they are already past the buffer: a
/// sound server's own latency, or a USB card's frames in flight.
snd_pcm_sframes_t framesBeyondBuffer(snd_pcm_sframes_t delay,
                                     snd_pcm_uframes_t bufferSize,
                                     snd_pcm_sframes_t avail);

class AlsaAudioEmitter : public AudioGraphEmitterNode {
public:
  AlsaAudioEmitter(const Config &config);

  virtual void
  connectTo(std::shared_ptr<AudioGraphOutputNode> outputNode) override;
  virtual void
  disconnect(std::shared_ptr<AudioGraphOutputNode> outputNode) override;

  virtual void pause(bool paused) override;
  virtual size_t seek(size_t positionMs) override;

  void setDsdAllowed(bool allowed) { dsdAllowed.store(allowed); }
  /// What fades a pause, a resume and the audio after a seek: the node it
  /// reads from, set before connectTo(). Without one, nothing fades.
  void fadeWith(std::shared_ptr<OutputFade> fade) {
    this->fade = std::move(fade);
  }
  /// Stream @p id is about to leave the graph. If it is the one being
  /// written, what the device holds of it fades out and is dropped rather
  /// than played to its end. Thread-safe; call it before the removal.
  void cut(StreamId id);

  virtual ~AlsaAudioEmitter();

private:
  std::string deviceName;
  snd_pcm_uframes_t requestedBufferSize = 0;
  snd_pcm_uframes_t requestedPeriodSize = 0;

  uint32_t requestedLatencyMs;
  uint32_t requestedPeriodMs;

  snd_pcm_uframes_t bufferSize;
  snd_pcm_uframes_t periodSize;

  // Fixups
  size_t sleepAfterFormatSetupMs;
  bool reopenDeviceWithNewFormat;

  std::shared_ptr<AudioGraphOutputNode> inputNode;
  std::jthread playbackThread;
  std::atomic<bool> isWorkerRunning = false;
  std::atomic<bool> dsdAllowed = true; // AudioPlayer starts in fixed mode.
  uint64_t dopFrame = 0;
  unsigned outputChannels = 2;

  StreamAudioFormat currentStreamAudioFormat;
  std::optional<LiveAudioPolicy> currentLivePolicy;
  // What the open device took, which is not always what was asked for.
  std::optional<DeviceInfo> deviceInfo;
  PlayedFramesCounter playedFramesCounter;
  // Absolute in the source's timeline, which need not begin at zero, and
  // ahead of what has been heard by whatever ALSA still has queued.
  snd_pcm_sframes_t currentSourceTotalFramesWritten = 0;
  snd_pcm_sframes_t currentSourceStartFrames = 0;
  std::chrono::milliseconds pollTimeout = std::chrono::milliseconds(100);
  PlaybackPcm pcm;
  // When the device has played out what it held as it was last closed.
  std::chrono::steady_clock::time_point silentAt;
  std::vector<pollfd> ufds;
  std::unordered_map<AudioSampleFormat, AudioSampleFormat> sampleSubstitute;
  std::vector<uint8_t> conversionScratch;

  Signal<size_t> seekRequestSignal;
  Signal<bool> pauseRequestSignal;
  bool paused = false;
  /// What the device holds is not to be heard out: the next drain drops it.
  bool queueAbandoned = false;
  std::shared_ptr<OutputFade> fade;
  const std::unique_ptr<PackedFade> packedFade;
  /// Frames a pause still reads, fading, before it writes silence.
  snd_pcm_uframes_t pauseFadeLeft = 0;
  /// The next run starts mid-waveform: where a seek landed, or where an input
  /// that ran dry under a pause resumes.
  bool fadeInNext = false;
  /// A pause the run ended under, a track's end or a stall: the next run
  /// starts paused.
  bool pauseCarried = false;
  /// All the device holds is a pause's silence: its fade has been heard.
  bool holdsSilence = false;
  /// Pause and resume reports made; only the newest decides holdsSilence.
  unsigned pauseReports = 0;
  /// The report of a gapless source change, waiting for its first frame to
  /// be heard.
  std::optional<int> pendingSourceReport;
  std::mutex readingMutex;
  /// The stream whose audio is being written, guarded by readingMutex.
  std::optional<StreamId> readingStream;
  /// readingStream was cut(), guarded by readingMutex.
  bool readingCut = false;

  void workerThread(std::stop_token token);
  bool handleSeekSignal();
  void handlePauseSignal(bool paused);
  /// Reports @p state once what the device holds now has been heard, or at
  /// once if the device cannot say how much that is.
  void reportOnceHeard(AudioGraphNodeState state);
  /// Whether what plays can be faded: PCM, with something to fade it.
  bool fades() const;
  /// Paused, and its fade out written: nothing more is read.
  bool pausedOut() const { return paused && pauseFadeLeft == 0; }
  /// Takes back what the device holds unplayed, fades its start out and
  /// silences the rest, and waits until the fade has been heard. Leaves the
  /// queue alone on a device that cannot take it back.
  /// @param keepQueue Takes back only the fade's length at the queue's end,
  /// so that all of it is still heard.
  void fadeQueuedOut(bool keepQueue = false);
  /// Forgets the reports waiting on what the device holds, which is dropped.
  void forgetQueuedReports();
  /// Now writing @p id; a cut of another stream no longer applies.
  void reading(std::optional<StreamId> id);
  /// If the stream being written was cut, fades the device's queue out and
  /// has the next drain drop it. @return whether it was.
  bool abandonIfCut();
  /// Applies a pause or resume that lands before the device started: nothing
  /// is playing out, so the new state holds at once.
  void pauseBeforeStart(bool paused, const StreamInfo &streamInfo,
                        snd_pcm_uframes_t position);
  /// Where playback stands with nothing queued on the device.
  long reachedPositionMs();

  void openDevice();
  void closeDevice();
  /// Whether the open device is ours alone, as openPlayback() found it.
  DeviceAccess deviceAccess() const;

  void initHwParams(unsigned int &rate, AudioSampleFormat format,
                    unsigned significantBits);
  void setSampleFormat(AudioSampleFormat requestedFormat,
                       snd_pcm_hw_params_t *params, unsigned significantBits);
  void setSwParams();
  void setLatencyBasedBufferSize(snd_pcm_hw_params_t *params);
  size_t readAndConvertFrames(void *dest, size_t bytes);
  /// The on-wire format the open device plays a stream's samples in.
  AudioSampleFormat deviceFormatFor(AudioSampleFormat streamFormat) const;
  void stampDop(void *dest, size_t frames);

  void setupAudioFormat(const StreamAudioFormat &streamAudioFormat,
                        std::optional<LiveAudioPolicy> live = std::nullopt);
  snd_pcm_sframes_t queuedFrames();
  /// Frames the running device will still play after a drop.
  snd_pcm_sframes_t framesHeardAfterDrop();
  void beginSourceAt(std::optional<long> position);
  StreamState waitForInputToBeReady(std::stop_token token);
  /// Waits for a period of room on the device; @p token is the worker's own,
  /// as its thread object may still be being assigned.
  snd_pcm_sframes_t waitForAlsaBufferSpace(std::stop_token token);
  bool handleInputNodeStateChange();
  snd_pcm_sframes_t writeToAlsa(
      snd_pcm_uframes_t framesToWrite,
      std::function<snd_pcm_sframes_t(void *ptr, snd_pcm_uframes_t frames,
                                      size_t bytes)>
          func);
  snd_pcm_sframes_t readIntoAlsaFromStream(std::stop_token stopToken,
                                           snd_pcm_sframes_t framesToRead);
  size_t waitForInputData(std::stop_token stopToken, snd_pcm_uframes_t frames);
  void startPcmStream(const StreamInfo &streamInfo, snd_pcm_uframes_t position);
  void drainPcm();

  inline std::chrono::milliseconds framesToTimeMs(snd_pcm_sframes_t frames);

  void start();
  /// Returns once the device is silent.
  void stop();

  /// Stamps the open device onto every state this node reports.
  void setState(const StreamState &newState) override;
};

#endif
