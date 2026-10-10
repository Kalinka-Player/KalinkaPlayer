#ifndef ALSA_AUDIO_EMITTER_H
#define ALSA_AUDIO_EMITTER_H

#include "AudioGraphNode.h"
#include "Config.h"
#include "Utils.h"

#include <alsa/asoundlib.h>

#include <atomic>
#include <functional>
#include <list>
#include <thread>
#include <unordered_map>
#include <vector>

class PlayedFramesCounter {
public:
  PlayedFramesCounter() = default;
  void update(snd_pcm_sframes_t frames);

  snd_pcm_sframes_t getPlayedFrames() const { return lastPlayedFrames; }

  std::vector<snd_pcm_sframes_t> drainSequence();

  void
  callOnOrAfterFrame(snd_pcm_sframes_t frame,
                     std::function<void(snd_pcm_sframes_t)> onFramesPlayed);

  void reset();

private:
  snd_pcm_sframes_t lastPlayedFrames = 0;

  std::list<
      std::pair<snd_pcm_sframes_t, std::function<void(snd_pcm_sframes_t)>>>
      onFramesPlayedCallbacks;
};

class AlsaAudioEmitter : public AudioGraphEmitterNode {
public:
  AlsaAudioEmitter(const Config &config);

  virtual void
  connectTo(std::shared_ptr<AudioGraphOutputNode> outputNode) override;
  virtual void
  disconnect(std::shared_ptr<AudioGraphOutputNode> outputNode) override;

  virtual void pause(bool paused) override;
  virtual size_t seek(size_t positionMs) override;

  // Software volume: linear amplitude gain in [0, 1] applied to outgoing PCM
  // frames. 1.0 (the default) is a bit-perfect bypass. Thread-safe; the value
  // is read on the playback worker thread for each conversion.
  void setSoftwareVolume(float gain);
  void setDsdAllowed(bool allowed) { dsdAllowed.store(allowed); }

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
  std::atomic<float> softwareGain = 1.0f;
  std::atomic<bool> dsdAllowed = true; // AudioPlayer starts in fixed mode.
  uint64_t dopFrame = 0;
  unsigned outputChannels = 2;

  StreamAudioFormat currentStreamAudioFormat;
  // What the open device took, which is not always what was asked for.
  std::optional<DeviceInfo> deviceInfo;
  PlayedFramesCounter playedFramesCounter;
  // Absolute in the source's timeline, which need not begin at zero, and
  // ahead of what has been heard by whatever ALSA still has queued.
  snd_pcm_sframes_t currentSourceTotalFramesWritten = 0;
  snd_pcm_sframes_t currentSourceStartFrames = 0;
  std::chrono::milliseconds pollTimeout = std::chrono::milliseconds(100);
  snd_pcm_t *pcmHandle = nullptr;
  std::vector<pollfd> ufds;
  std::unordered_map<AudioSampleFormat, AudioSampleFormat> sampleSubstitute;

  Signal<size_t> seekRequestSignal;
  Signal<bool> pauseRequestSignal;
  bool paused = false;
  bool seekHappened = false;

  void workerThread(std::stop_token token);
  bool handleSeekSignal();
  bool handlePauseSignal(bool paused);
  /// Applies a pause or resume that lands before the device started: nothing
  /// is playing out, so the new state holds at once.
  void pauseBeforeStart(bool paused, const StreamInfo &streamInfo,
                        snd_pcm_uframes_t position);
  /// Blocks until `token` stops: a pause, a seek or the worker's end.
  void waitForCommand(std::stop_token token);
  /// Where playback stands with nothing queued on the device.
  long reachedPositionMs();

  void openDevice();
  void closeDevice();
  /// Whether the open device is ours alone, as ALSA reports its PCM type.
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

  void setupAudioFormat(const StreamAudioFormat &streamAudioFormat);
  snd_pcm_sframes_t queuedFrames();
  void beginSourceAt(std::optional<long> position);
  StreamState waitForInputToBeReady(std::stop_token token);
  snd_pcm_sframes_t waitForAlsaBufferSpace();
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
  void stop();

  /// Stamps the open device onto every state this node reports.
  void setState(const StreamState &newState) override;
};

#endif
