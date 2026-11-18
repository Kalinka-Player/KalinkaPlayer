#include "AlsaAudioEmitter.h"
#include "AudioSampleFormat.h"
#include "DsdFormat.h"
#include "Log.h"
#include "PerfMon.h"
#include "PlaybackPcm.h"
#include "StateMonitor.h"
#include "StreamState.h"
#include "Utils.h"
#include "dsp/output/PackedFade.h"

#include <cstring>
#include <iostream>
#include <mutex>
#include <pthread.h>
#include <sstream>
#include <stdexcept>
#include <utility>

#define throw_on_error(func) throw_on_error_impl(func, #func " failed")
#define log_on_error(func) log_on_error_impl(func, #func " failed")

namespace {
// What ALSA calls safe to take back may already be on its way to the DAC.
constexpr std::chrono::milliseconds kRewindMargin{5};

/// A setting refuses the stream; reported as is, never as an internal error.
struct SettingsRefusal : std::runtime_error {
  using std::runtime_error::runtime_error;
};

inline int throw_on_error_impl(int err, const std::string &message) {
  if (err < 0) {
    throw std::runtime_error(message + ": " + snd_strerror(err));
  }
  return err;
}

inline int log_on_error_impl(int err, const std::string &message) {
  if (err < 0) {
    spdlog::warn("{}: {}", message, snd_strerror(err));
  }
  return err;
}

int xrun_recovery(snd_pcm_t *handle, int err) {
  if (err == -EPIPE) { /* under-run */
    err = snd_pcm_prepare(handle);
    if (err < 0)
      spdlog::error("Can't recovery from underrun, prepare failed: %s\n",
                    snd_strerror(err));
    return 0;
  } else if (err == -ESTRPIPE) {
    while ((err = snd_pcm_resume(handle)) == -EAGAIN)
      sleep(1); /* wait until the suspend flag is released */
    if (err < 0) {
      err = snd_pcm_prepare(handle);
      if (err < 0) {
        spdlog::error("Can't recovery from suspend, prepare failed: %s\n",
                      snd_strerror(err));
      }
    }
    return 0;
  }
  return err;
}
} // namespace

AlsaAudioEmitter::AlsaAudioEmitter(const Config &config)
    : deviceName(
          value_or(config, "output.alsa.device", std::string("default"))),
      requestedLatencyMs(value_or(config, "output.alsa.latency_ms", 100)),
      requestedPeriodMs(value_or(config, "output.alsa.period_ms", 25)),
      sleepAfterFormatSetupMs(
          value_or(config, "fixups.alsa_sleep_after_format_setup_ms", 0)),
      reopenDeviceWithNewFormat(
          value_or(config, "fixups.alsa_reopen_device_with_new_format", false)),
      packedFade(std::make_unique<PackedFade>()) {
  auto configBufferSize =
      value<snd_pcm_uframes_t>(config, "output.alsa.buffer_size");
  auto configPeriodSize =
      value<snd_pcm_uframes_t>(config, "output.alsa.period_size");

  if (configBufferSize.has_value() && configPeriodSize.has_value()) {
    requestedBufferSize = configBufferSize.value();
    requestedPeriodSize = configPeriodSize.value();
  }
}

AlsaAudioEmitter::~AlsaAudioEmitter() { stop(); }

void AlsaAudioEmitter::connectTo(
    std::shared_ptr<AudioGraphOutputNode> outputNode) {
  if (outputNode == nullptr) {
    throw std::runtime_error("Input node cannot be nullptr");
  }
  if (inputNode == outputNode) {
    return;
  }

  if (inputNode != nullptr) {
    throw std::runtime_error("AlsaAudioEmitter is already connected to an "
                             "AudioGraphOutputNode node");
  }

  inputNode = outputNode;
  start();
}

void AlsaAudioEmitter::disconnect(
    std::shared_ptr<AudioGraphOutputNode> outputNode) {
  if (inputNode == outputNode) {
    stop();
    inputNode = nullptr;
  }
}

void AlsaAudioEmitter::pause(bool paused) {
  if (playbackThread.joinable() && isWorkerRunning) {
    pauseRequestSignal.sendValue(paused);
    pauseRequestSignal.getResponse(playbackThread.get_stop_token());
  }
}

size_t AlsaAudioEmitter::seek(size_t positionMs) {
  if (playbackThread.joinable() && isWorkerRunning &&
      !seekRequestSignal.getValue()) {
    seekRequestSignal.sendValue(positionMs);
    return seekRequestSignal.getResponse(playbackThread.get_stop_token());
  }

  return -1;
}

void AlsaAudioEmitter::start() {
  if (inputNode == nullptr) {
    throw std::runtime_error("AlsaAudioEmitter must be connected to an "
                             "AudioGraphOutputNode node");
  }

  if (playbackThread.joinable()) {
    playbackThread.request_stop();
    playbackThread.join();
  }
  seekRequestSignal.reopen();
  pauseRequestSignal.reopen();
  playbackThread =
      std::jthread(std::bind_front(&AlsaAudioEmitter::workerThread, this));
}

void AlsaAudioEmitter::stop() {
  // Renderer delta: stopped means no stream, worker to wind down or not.
  setStreamId(std::nullopt);
  if (playbackThread.joinable()) {
    playbackThread.request_stop();
    playbackThread.join();
    playbackThread = std::jthread();
    // The delay is the device's own claim; a wrong one must not hang a stop.
    std::this_thread::sleep_until(std::min(
        silentAt, std::chrono::steady_clock::now() + std::chrono::seconds(1)));
    setState(StreamState(AudioGraphNodeState::STOPPED));
  }
}

DeviceAccess AlsaAudioEmitter::deviceAccess() const {
  return pcm.handle == nullptr ? DeviceAccess::Unknown : pcm.access;
}

void AlsaAudioEmitter::setState(const StreamState &newState) {
  StreamState stamped = newState;
  stamped.deviceInfo = deviceInfo;
  AudioGraphNode::setState(stamped);
}

snd_pcm_sframes_t framesBeyondBuffer(snd_pcm_sframes_t delay,
                                     snd_pcm_uframes_t bufferSize,
                                     snd_pcm_sframes_t avail) {
  const snd_pcm_sframes_t buffered = std::max<snd_pcm_sframes_t>(
      0, static_cast<snd_pcm_sframes_t>(bufferSize) - avail);
  return std::max<snd_pcm_sframes_t>(0, delay - buffered);
}

snd_pcm_sframes_t AlsaAudioEmitter::framesHeardAfterDrop() {
  if (pcm.handle == nullptr ||
      snd_pcm_state(pcm.handle) != SND_PCM_STATE_RUNNING) {
    return 0;
  }
  snd_pcm_sframes_t avail = 0;
  snd_pcm_sframes_t delay = 0;
  if (snd_pcm_avail_delay(pcm.handle, &avail, &delay) < 0) {
    return 0;
  }
  return framesBeyondBuffer(delay, bufferSize, avail);
}

snd_pcm_sframes_t AlsaAudioEmitter::queuedFrames() {
  // Only a running stream is holding anything: a drop or a drain leaves it in
  // SETUP with nothing left to play, while the delay goes on reporting the
  // link latency (PipeWire) long after the buffer is gone.
  if (pcm.handle == nullptr ||
      snd_pcm_state(pcm.handle) != SND_PCM_STATE_RUNNING) {
    return 0;
  }
  snd_pcm_sframes_t queued = 0;
  if (snd_pcm_delay(pcm.handle, &queued) < 0) {
    return 0;
  }
  return std::max<snd_pcm_sframes_t>(0, queued);
}

void AlsaAudioEmitter::beginSourceAt(std::optional<long> position) {
  currentSourceStartFrames =
      std::max<snd_pcm_sframes_t>(0, position.value_or(0));
  currentSourceTotalFramesWritten = currentSourceStartFrames;
}

StreamState AlsaAudioEmitter::waitForInputToBeReady(std::stop_token token) {
  StreamState inputNodeState = inputNode->getState();
  while (!token.stop_requested()) {
    switch (inputNodeState.state) {
    case AudioGraphNodeState::PREPARING:
      setState({AudioGraphNodeState::PREPARING, reachedPositionMs(),
                getState().streamInfo});
      break;
    case AudioGraphNodeState::STREAMING:
      return inputNodeState;
    case AudioGraphNodeState::FINISHED:
      setState(
          StreamState(AudioGraphNodeState::FINISHED,
                      framesToTimeMs(currentSourceTotalFramesWritten).count(),
                      inputNodeState.streamInfo));
      break;
    case AudioGraphNodeState::ERROR:
      setState({AudioGraphNodeState::ERROR, *inputNodeState.error});
      break;
    case AudioGraphNodeState::SOURCE_CHANGED:
      setStreamId(inputNodeState.streamId);
      setState(StreamState(AudioGraphNodeState::SOURCE_CHANGED));
      inputNode->acceptSourceChange();
      beginSourceAt(inputNode->streamReadPosition());
      break;
    default:
      setState(StreamState(AudioGraphNodeState::STOPPED));
      break;
    }
    if (seekRequestSignal.getValue()) {
      handleSeekSignal();
    }

    if (pauseRequestSignal.getValue()) {
      // Between runs a request can only lift a pause held for the next one.
      if (!*pauseRequestSignal.getValue())
        pauseCarried = false;
      pauseRequestSignal.respond(pauseCarried);
    }

    auto combinedToken =
        combineStopTokens(token, seekRequestSignal.getStopToken(),
                          pauseRequestSignal.getStopToken());
    StateChangeWaitLock lock(combinedToken.get_token(), *inputNode,
                             inputNodeState.timestamp);
    inputNodeState = lock.state();
  };

  return inputNodeState;
}

bool AlsaAudioEmitter::handleSeekSignal() {
  auto positionMs = *seekRequestSignal.getValue();
  auto seekValue = positionMs * currentStreamAudioFormat.sampleRate / 1000;

  // Calculate the quantized position (what we'll actually achieve)
  auto quantizedPositionMs = framesToTimeMs(seekValue).count();

  // Report PREPARING state with the quantized target position and stream info
  auto state = getState();
  setState(
      {AudioGraphNodeState::PREPARING, quantizedPositionMs, state.streamInfo});

  spdlog::info("Request seek to {}ms ({} frames), quantized to {}ms",
               positionMs, seekValue, quantizedPositionMs);
  // The seek means the stream being heard, which upstream may have left.
  auto retVal = state.streamId
                    ? inputNode->seekStream(*state.streamId, seekValue)
                    : inputNode->seekTo(seekValue);
  if (retVal == size_t(-1)) {
    spdlog::warn("Seek request failed: requested={}", seekValue);
    // As before the attempt, stamped now so nothing reported goes back in time.
    StreamState resumed = state;
    resumed.timestamp = getTimestampNs();
    resumed.position = state.positionAt(resumed.timestamp);
    setState(resumed);
    seekRequestSignal.respond(-1);
    return false;
  }
  forgetQueuedReports();
  seekRequestSignal.respond(framesToTimeMs(retVal).count());
  queueAbandoned = true;
  fadeInNext = true;
  // Played from where it lands, as a seek under any pause is.
  pauseCarried = false;
  return true;
}

void AlsaAudioEmitter::pauseBeforeStart(bool paused,
                                        const StreamInfo &streamInfo,
                                        snd_pcm_uframes_t position) {
  this->paused = paused;
  setState({paused ? AudioGraphNodeState::PAUSED
                   : AudioGraphNodeState::PREPARING,
            framesToTimeMs(position).count(), streamInfo});
}

long AlsaAudioEmitter::reachedPositionMs() {
  const auto current = getState();
  // Only a drain leaves a streaming state here, and it plays out all written.
  return current.state == AudioGraphNodeState::STREAMING
             ? framesToTimeMs(currentSourceTotalFramesWritten).count()
             : current.position;
}

void AlsaAudioEmitter::handlePauseSignal(bool paused) {
  this->paused = paused;
  pauseFadeLeft = 0;
  holdsSilence = false;
  if (fades() && paused) {
    fade->fadeOut();
    // Paused once the fade, still to be written, has been heard.
    if (!fade->silent()) {
      pauseFadeLeft = fadeFrames(currentStreamAudioFormat.sampleRate);
      return;
    }
  } else if (fades()) {
    fade->fadeIn();
  }
  reportOnceHeard(paused ? AudioGraphNodeState::PAUSED
                         : AudioGraphNodeState::STREAMING);
}

void AlsaAudioEmitter::reportOnceHeard(AudioGraphNodeState state) {
  const long position = framesToTimeMs(currentSourceTotalFramesWritten).count();
  const unsigned report = ++pauseReports;
  auto heard = [this, state, position, report](snd_pcm_sframes_t) {
    // A pause a later resume overtook: the device holds what followed.
    if (report == pauseReports)
      holdsSilence = state == AudioGraphNodeState::PAUSED;
    // What the device plays by then, which a gapless join may have changed.
    setState({state, position, getState().streamInfo});
  };
  snd_pcm_sframes_t delay = 0;
  if (const int err = snd_pcm_delay(pcm.handle, &delay); err < 0) {
    spdlog::warn("Reporting {} at once: snd_pcm_delay failed: {}",
                 stateToString(state), snd_strerror(err));
    heard(0);
    return;
  }
  playedFramesCounter.callOnOrAfterFrame(delay, std::move(heard));
}

bool AlsaAudioEmitter::fades() const {
  return fade && !isDsd(currentStreamAudioFormat.sampleFormat);
}

void AlsaAudioEmitter::fadeQueuedOut(bool keepQueue) {
  if (!fades() || holdsSilence || pcm.handle == nullptr ||
      snd_pcm_state(pcm.handle) != SND_PCM_STATE_RUNNING)
    return;
  const unsigned rate = currentStreamAudioFormat.sampleRate;
  const auto length = snd_pcm_sframes_t(fadeFrames(rate));
  const snd_pcm_sframes_t margin = rate * kRewindMargin.count() / 1000;
  const snd_pcm_sframes_t rewindable = snd_pcm_rewindable(pcm.handle) - margin;
  if (rewindable < length)
    return;
  const snd_pcm_sframes_t rewound =
      snd_pcm_rewind(pcm.handle, keepQueue ? length : rewindable);
  if (rewound < length) {
    if (rewound > 0)
      snd_pcm_forward(pcm.handle, rewound);
    return;
  }
  const auto format = deviceFormatFor(currentStreamAudioFormat.sampleFormat);
  const unsigned bits = currentStreamAudioFormat.bitsPerSample
                            ? currentStreamAudioFormat.bitsPerSample
                            : unsigned(sampleBits(format));
  packedFade->begin(rate);
  for (snd_pcm_sframes_t at = 0; at < rewound;) {
    const auto written = writeToAlsa(
        rewound - at, [&](void *ptr, snd_pcm_uframes_t frames, size_t) {
          packedFade->out(static_cast<uint8_t *>(ptr), format, outputChannels,
                          bits, frames);
          return snd_pcm_sframes_t(frames);
        });
    if (written <= 0)
      return;
    at += written;
  }
  snd_pcm_sframes_t delay = 0;
  if (snd_pcm_delay(pcm.handle, &delay) == 0 && delay > rewound - length)
    std::this_thread::sleep_for(std::chrono::microseconds(
        1'000'000 * (delay - (rewound - length)) / rate));
}

void AlsaAudioEmitter::cut(StreamId id) {
  std::lock_guard lock(readingMutex);
  if (readingStream == id)
    readingCut = true;
}

void AlsaAudioEmitter::reading(std::optional<StreamId> id) {
  std::lock_guard lock(readingMutex);
  if (readingStream != id)
    readingCut = false;
  readingStream = id;
}

bool AlsaAudioEmitter::abandonIfCut() {
  {
    std::lock_guard lock(readingMutex);
    if (!std::exchange(readingCut, false))
      return false;
  }
  forgetQueuedReports();
  // A seek abandoning the queue at the same time has faded it already.
  if (!queueAbandoned)
    fadeQueuedOut();
  queueAbandoned = true;
  return true;
}

void AlsaAudioEmitter::forgetQueuedReports() {
  playedFramesCounter.reset();
  pendingSourceReport.reset();
}

void AlsaAudioEmitter::openDevice() {
  if (pcm.handle != nullptr) {
    return;
  }

  const auto opened = openPlayback(deviceName, 0);
  if (!opened) {
    std::stringstream stream;
    stream << "Cannot open audio device " << deviceName << " ("
           << openFailure(deviceName, opened.error()) << ")";
    spdlog::error(stream.str());
    setState({AudioGraphNodeState::ERROR,
              StreamError{StreamErrorSource::AUDIO_OUTPUT, stream.str()}});

    throw std::runtime_error(stream.str());
  }
  pcm = *opened;

  currentSourceTotalFramesWritten = 0;
  forgetQueuedReports();
}

void AlsaAudioEmitter::closeDevice() {
  if (pcm.handle != nullptr) {
    if (const auto stillHeard = framesHeardAfterDrop(); stillHeard > 0) {
      const auto stillHeardMs = framesToTimeMs(stillHeard);
      spdlog::debug("Closing with {}ms still to be heard",
                    stillHeardMs.count());
      silentAt = std::chrono::steady_clock::now() + stillHeardMs;
    }
    log_on_error(snd_pcm_drop(pcm.handle));
    log_on_error(snd_pcm_hw_free(pcm.handle));
    log_on_error(snd_pcm_close(pcm.handle));
    pcm = {};
    currentStreamAudioFormat = StreamAudioFormat();
    deviceInfo.reset();
  }
}

void AlsaAudioEmitter::recoverFromUnderrun() {
  if (isDsd(currentStreamAudioFormat.sampleFormat))
    throw std::runtime_error("DSD output underrun; restart playback");
  spdlog::warn("Output underrun; playing on");
  playedFramesCounter.playOut();
  throw_on_error(snd_pcm_prepare(pcm.handle));
}

snd_pcm_sframes_t
AlsaAudioEmitter::waitForAlsaBufferSpace(std::stop_token token) {
  // A card's writes go through in an underrun: it shows here first.
  bool recovered = false;
  auto getAvailableFrames = [this, &recovered]() -> snd_pcm_sframes_t {
    const snd_pcm_sframes_t frames = snd_pcm_avail_update(pcm.handle);
    if (frames != -EPIPE)
      return throw_on_error(frames);
    recoverFromUnderrun();
    recovered = true;
    return throw_on_error(snd_pcm_avail_update(pcm.handle));
  };

  perfmon_end("fullPeriodProcessingTime");
  perfmon_begin("waitForAlsaBufferSpace");

  snd_pcm_sframes_t frames = getAvailableFrames();
  unsigned short revents = 0;

  while (!token.stop_requested() &&
         static_cast<snd_pcm_uframes_t>(frames) < periodSize) {
    int ret = poll(ufds.data(), ufds.size(), pollTimeout.count());
    if (ret == 0) {
      continue;
    }
    frames = getAvailableFrames();
    // The poll's error was the underrun, which is behind us.
    if (recovered)
      break;
    throw_on_error(snd_pcm_poll_descriptors_revents(pcm.handle, ufds.data(),
                                                    ufds.size(), &revents));
    if (revents & POLLERR) {
      throw std::runtime_error("Poll error");
    }
    if (revents & POLLOUT) {
      break;
    }
  }

  perfmon_end("waitForAlsaBufferSpace");
  perfmon_begin("fullPeriodProcessingTime");
  return frames;
}

bool AlsaAudioEmitter::handleInputNodeStateChange() {
  auto inputNodeState = inputNode->getState();
  if (inputNodeState.state == AudioGraphNodeState::SOURCE_CHANGED) {
    const std::optional<StreamId> incomingStreamId = inputNodeState.streamId;
    const bool cut = abandonIfCut();
    inputNode->acceptSourceChange();
    reading(incomingStreamId);
    inputNodeState = inputNode->getState();
    // Unconditional, unlike the source loop: the queue holds the outgoing
    // track, so it says nothing about where the incoming one begins.
    beginSourceAt(inputNode->streamReadPosition());

    auto newStreamInfo = inputNodeState.streamInfo;
    if (cut || inputNodeState.state != AudioGraphNodeState::STREAMING ||
        !newStreamInfo.has_value() ||
        newStreamInfo.value().format != currentStreamAudioFormat ||
        pausedOut()) {
      spdlog::info("Source changed - cut, not streaming, different format or "
                   "paused - ending the run");
      pauseCarried = paused && !cut;
      drainPcm();
      setStreamId(incomingStreamId);
      setState(StreamState(AudioGraphNodeState::SOURCE_CHANGED));
      return false;
    } else {
      snd_pcm_sframes_t framesDelay = 0;
      log_on_error(snd_pcm_delay(pcm.handle, &framesDelay));
      spdlog::debug("Reporting source change in {}ms, frames={}",
                    framesToTimeMs(framesDelay).count(), framesDelay);

      pendingSourceReport = playedFramesCounter.callOnOrAfterFrame(
          framesDelay,
          [this, streamInfo = newStreamInfo, incomingStreamId,
           start = currentSourceStartFrames](snd_pcm_sframes_t frames) {
            pendingSourceReport.reset();
            setStreamId(incomingStreamId);
            setState(StreamState(AudioGraphNodeState::SOURCE_CHANGED));
            setState(StreamState{AudioGraphNodeState::STREAMING,
                                 framesToTimeMs(start + frames).count(),
                                 streamInfo});
          });
    }
  } else if (inputNodeState.state == AudioGraphNodeState::FINISHED ||
             inputNodeState.state == AudioGraphNodeState::ERROR) {
    spdlog::info("Source finished, state={}",
                 stateToString(inputNodeState.state));
    drainPcm();
    return false;
  }

  return true;
}

snd_pcm_sframes_t AlsaAudioEmitter::writeToAlsa(
    snd_pcm_uframes_t framesToWrite,
    std::function<snd_pcm_sframes_t(void *ptr, snd_pcm_uframes_t frames,
                                    size_t bytes)>
        func) {
  perfmon_begin("writeToAlsa");
  const snd_pcm_channel_area_t *my_areas = nullptr;
  snd_pcm_uframes_t offset = 0, frames = framesToWrite;
  int err = snd_pcm_mmap_begin(pcm.handle, &my_areas, &offset, &frames);
  if (err == -EPIPE) {
    recoverFromUnderrun();
    perfmon_end("writeToAlsa");
    return 0;
  }
  if (err < 0) {
    if (isDsd(currentStreamAudioFormat.sampleFormat))
      throw std::runtime_error("DSD output underrun; restart playback");
    if (xrun_recovery(pcm.handle, err) < 0) {
      throw std::runtime_error("Error in mmap begin: " +
                               std::string(snd_strerror(err)));
    }
    perfmon_end("writeToAlsa");
    return 0;
  }

  void *ptr = static_cast<uint8_t *>(my_areas[0].addr) +
              (my_areas[0].first / 8) +
              snd_pcm_frames_to_bytes(pcm.handle, offset);

  frames = func(ptr, frames, snd_pcm_frames_to_bytes(pcm.handle, frames));

  err = snd_pcm_mmap_commit(pcm.handle, offset, frames);
  if (err == -EPIPE) {
    recoverFromUnderrun();
  } else if (static_cast<snd_pcm_uframes_t>(err) != frames || err < 0) {
    if (isDsd(currentStreamAudioFormat.sampleFormat))
      throw std::runtime_error("Incomplete DSD output write; restart playback");
    if (xrun_recovery(pcm.handle, err) < 0) {
      throw std::runtime_error("Error in mmap commit: " +
                               std::string(snd_strerror(err)));
    }
  }

  perfmon_end("writeToAlsa");

  return frames;
}

snd_pcm_sframes_t
AlsaAudioEmitter::readIntoAlsaFromStream(std::stop_token stopToken,
                                         snd_pcm_sframes_t framesToRead) {
  snd_pcm_sframes_t framesRead = 0;

  perfmon_begin("readIntoAlsaFromStream");

  if (snd_pcm_state(pcm.handle) == SND_PCM_STATE_RUNNING) {
    playedFramesCounter.update(framesToRead);
  }

  while (framesRead < framesToRead && !stopToken.stop_requested()) {
    snd_pcm_uframes_t frames = framesToRead - framesRead;
    if (paused && pauseFadeLeft == 0) {
      framesRead += writeToAlsa(
          frames, [this](void *ptr, snd_pcm_uframes_t frames, size_t bytes) {
            memset(ptr, isDsd(currentStreamAudioFormat.sampleFormat) ? 0x69 : 0,
                   bytes);
            stampDop(ptr, frames);
            return frames;
          });

      if (!handleInputNodeStateChange()) {
        return -1;
      }

    } else {
      if (pauseFadeLeft > 0)
        frames = std::min(frames, pauseFadeLeft);
      auto actualFrames = writeToAlsa(
          frames, [this](void *ptr, snd_pcm_uframes_t frames, size_t bytes) {
            return readAndConvertFrames(ptr, bytes);
          });

      framesRead += actualFrames;
      currentSourceTotalFramesWritten += actualFrames;
      if (pauseFadeLeft > 0) {
        pauseFadeLeft -=
            std::min<snd_pcm_uframes_t>(pauseFadeLeft, actualFrames);
        if (pauseFadeLeft == 0)
          reportOnceHeard(AudioGraphNodeState::PAUSED);
      }

      if (!handleInputNodeStateChange()) {
        return -1;
      }

      if (static_cast<snd_pcm_uframes_t>(actualFrames) < frames) {
        perfmon_begin("waitForMoreInputData");
        auto bytesAvailable =
            waitForInputData(stopToken, frames - actualFrames);
        perfmon_end("waitForMoreInputData");
        if (bytesAvailable == 0) {
          // A command cut the wait short: the worker has to answer it.
          if (stopToken.stop_requested()) {
            break;
          }
          const bool cut = abandonIfCut();
          const auto input = inputNode->getState().state;
          // Dry inside a pause's fade, not ended: the queue's end fades out.
          if (paused && !cut &&
              (input == AudioGraphNodeState::STREAMING ||
               input == AudioGraphNodeState::PREPARING)) {
            fadeQueuedOut(true);
            pauseCarried = true;
            fadeInNext = true;
          }
          drainPcm();
          return -1;
        }
      }
    }
  }
  perfmon_end("readIntoAlsaFromStream");
  return framesRead;
}

size_t AlsaAudioEmitter::waitForInputData(std::stop_token stopToken,
                                          snd_pcm_uframes_t frames) {
  const auto sourceBytes = frames * currentStreamAudioFormat.channels *
                           sampleSize(currentStreamAudioFormat.sampleFormat);
  if (snd_pcm_state(pcm.handle) == SND_PCM_STATE_RUNNING) {
    snd_pcm_sframes_t delayFrames = 0;
    log_on_error(snd_pcm_delay(pcm.handle, &delayFrames));
    auto timeout = framesToTimeMs(std::max(
        0l, delayFrames - static_cast<snd_pcm_sframes_t>(2 * periodSize)));
    return inputNode->waitForDataFor(stopToken, timeout, sourceBytes);
  }

  return inputNode->waitForData(stopToken, sourceBytes);
}

std::chrono::milliseconds
AlsaAudioEmitter::framesToTimeMs(snd_pcm_sframes_t frames) {
  const auto sampleRate = currentStreamAudioFormat.sampleRate;
  if (sampleRate == 0) {
    spdlog::warn("framesToTimeMs called with sampleRate=0; returning 0ms");
    return std::chrono::milliseconds(0);
  }
  return std::chrono::milliseconds(1000 * frames / sampleRate);
}

void AlsaAudioEmitter::startPcmStream(const StreamInfo &streamInfo,
                                      snd_pcm_uframes_t position) {
  spdlog::info("Starting playback");
  throw_on_error(snd_pcm_start(pcm.handle));
  setState({AudioGraphNodeState::STREAMING, framesToTimeMs(position).count(),
            streamInfo});
}

void AlsaAudioEmitter::drainPcm() {
  abandonIfCut();
  if (snd_pcm_state(pcm.handle) == SND_PCM_STATE_RUNNING) {
    playedFramesCounter.playOut([this](snd_pcm_sframes_t frames) {
      std::this_thread::sleep_for(framesToTimeMs(frames));
    });
    log_on_error(snd_pcm_drop(pcm.handle));
  }
  if (pausedOut() || queueAbandoned) {
    log_on_error(snd_pcm_drop(pcm.handle));
  } else {
    log_on_error(snd_pcm_drain(pcm.handle));
  }
  // Left when the device was not started: it has now played or dropped it all.
  playedFramesCounter.playOut();
}

void AlsaAudioEmitter::workerThread(std::stop_token token) {
  if (inputNode == nullptr) {
    return;
  }
  pthread_setname_np(pthread_self(), "AlsaAudio");
  isWorkerRunning = true;
  try {
    while (!token.stop_requested()) {
      auto inputNodeState = waitForInputToBeReady(token);

      if (token.stop_requested()) {
        break;
      }

      auto streamInfo = inputNodeState.streamInfo;
      if (!streamInfo.has_value()) {
        throw std::runtime_error("No stream information available");
      }
      if (streamInfo.value().streamType != StreamType::FRAMES) {
        throw std::runtime_error("Unsupported stream type");
      }

      setupAudioFormat(streamInfo.value().format, streamInfo->live);
      streamInfo.value().format = currentStreamAudioFormat;
      if (fades()) {
        // Mid-waveform after a seek or a paused stall; else as its source does.
        fade->settle(fadeInNext ? 0.0 : 1.0);
        if (fadeInNext)
          fade->fadeIn();
      }
      fadeInNext = false;

      bool started = false;
      queueAbandoned = false;
      reading(inputNodeState.streamId);
      const auto queued = queuedFrames();

      // Only with nothing queued do the two agree, and there the source is
      // the authority: it is the end that begins partway in, or seeks itself.
      if (queued == 0) {
        if (auto position = inputNode->streamReadPosition()) {
          beginSourceAt(position);
        }
      }

      // What has been heard, rather than what has been handed over, and never
      // further back than this run began however much the device claims.
      snd_pcm_uframes_t streamStartPosition = std::max(
          currentSourceStartFrames, currentSourceTotalFramesWritten - queued);
      // Zero would rewind a live producer's credit for what already played.
      StreamState prepared{AudioGraphNodeState::PREPARING,
                           framesToTimeMs(streamStartPosition).count(), streamInfo};
      prepared.liveReady = streamInfo->live.has_value();
      setState(prepared);
      paused = false;
      pauseFadeLeft = 0;
      holdsSilence = false;
      if (std::exchange(pauseCarried, false)) {
        pauseBeforeStart(true, streamInfo.value(), streamStartPosition);
      }
      auto lastLiveReport = std::chrono::steady_clock::now();

      while (!token.stop_requested()) {
        auto combinedToken =
            combineStopTokens(token, seekRequestSignal.getStopToken(),
                              pauseRequestSignal.getStopToken());

        if (paused && !started) {
          // Silence written now would play before the audio it waits for,
          // and a skip in the meantime still ends the run.
          const auto seen = inputNode->getState().timestamp;
          if (!handleInputNodeStateChange())
            break;
          StateChangeWaitLock changed(combinedToken.get_token(), *inputNode,
                                      seen);
          if (!combinedToken.get_token().stop_requested())
            continue;
        } else {
          if (!started && streamInfo->prebufferMs && !streamInfo->live) {
            const auto bytes =
                uint64_t(streamInfo->prebufferMs) *
                currentStreamAudioFormat.sampleRate / 1000 *
                currentStreamAudioFormat.channels *
                sampleSize(currentStreamAudioFormat.sampleFormat);
            inputNode->waitForData(combinedToken.get_token(), bytes);
          }
          // A pause or seek must still be answered while filling the reserve.
          if (!combinedToken.get_token().stop_requested()) {
            auto framesToRead = waitForAlsaBufferSpace(token);
            if (!framesToRead) {
              break;
            }
            auto framesRead =
                readIntoAlsaFromStream(combinedToken.get_token(), framesToRead);
            if (framesRead < 0) {
              break;
            }
            // Refilled after an underrun: writes alone do not start a device.
            if (started && framesRead > 0 &&
                snd_pcm_state(pcm.handle) == SND_PCM_STATE_PREPARED) {
              throw_on_error(snd_pcm_start(pcm.handle));
            }
          }
        }
        if (token.stop_requested()) {
          break;
        }

        if (pauseRequestSignal.getValue()) {
          const bool requested = *pauseRequestSignal.getValue();
          if (paused == requested) {
            pauseRequestSignal.respond(paused);
          } else if (!started) {
            pauseBeforeStart(requested, streamInfo.value(), streamStartPosition);
            pauseRequestSignal.respond(requested);
          } else {
            handlePauseSignal(requested);
            pauseRequestSignal.respond(requested);
          }
          continue;
        }

        if (seekRequestSignal.getValue()) {
          if (!handleSeekSignal()) {
            continue;
          }

          fadeQueuedOut();
          drainPcm();
          break;
        }

        if (!started) {
          startPcmStream(streamInfo.value(), streamStartPosition);
          started = true;
        }
        // Feedback uses the device's audible frame position. Comparing clocks
        // from the two hosts would conflate network delay and clock offsets.
        const auto now = std::chrono::steady_clock::now();
        if (streamInfo->live && !paused &&
            now - lastLiveReport >= std::chrono::milliseconds(250)) {
          const auto audible = std::max<snd_pcm_sframes_t>(
              0, currentSourceTotalFramesWritten - queuedFrames());
          StreamState report{AudioGraphNodeState::STREAMING,
                             framesToTimeMs(audible).count(), streamInfo};
          if (auto frame = inputNode->liveSourceFrame()) {
            const auto queued = uint64_t(std::max<snd_pcm_sframes_t>(0, queuedFrames()));
            report.liveSourceFrame = *frame > queued ? *frame - queued : 0;
          }
          setState(report);
          lastLiveReport = now;
        }
      }
    }
    // Stopped: what the device holds fades out rather than cut.
    fadeQueuedOut();
  } catch (const std::exception &ex) {
    spdlog::error("Error in AlsaAudioEmitter::workerThread: {}", ex.what());
    // Nothing is emitted after this, so the device has to go first or the
    // failure is the last state anyone sees and it names an open device.
    closeDevice();
    const std::string message = ex.what();
    const bool refused = dynamic_cast<const SettingsRefusal *>(&ex);
    setState({AudioGraphNodeState::ERROR,
              StreamError{StreamErrorSource::AUDIO_OUTPUT,
                          refused ? message : "Internal error: " + message}});
  }

  closeDevice();
  reading(std::nullopt);
  pauseCarried = false;
  fadeInNext = false;
  inputNode = nullptr;
  isWorkerRunning = false;
  seekRequestSignal.close(static_cast<size_t>(-1));
  pauseRequestSignal.close(false);
}

void AlsaAudioEmitter::setupAudioFormat(
    const StreamAudioFormat &streamAudioFormat,
    std::optional<LiveAudioPolicy> live) {

  spdlog::debug("Setting up audio format: {}", streamAudioFormat.toString());

  if (pcm.handle != nullptr && streamAudioFormat == currentStreamAudioFormat &&
      live == currentLivePolicy) {
    spdlog::debug("Audio format is already set up - ignoring");
    throw_on_error(snd_pcm_prepare(pcm.handle));
    return;
  }

  currentLivePolicy = live;

  if ((reopenDeviceWithNewFormat || pcm.iec958) && pcm.handle != nullptr) {
    closeDevice();
  }

  if (pcm.handle == nullptr) {
    openDevice();
  }

  unsigned sampleRate = streamAudioFormat.sampleRate;
  if (sampleRate == 0) {
    throw std::runtime_error("Invalid stream sample rate: 0");
  }
  bufferSize = live ? 0 : requestedBufferSize;
  periodSize = live ? 0 : requestedPeriodSize;

  if (isDsd(streamAudioFormat.sampleFormat)) {
    if (!dsdAllowed.load())
      throw SettingsRefusal("DSD needs Volume control set to Fixed output");
    if (deviceAccess() != DeviceAccess::Exclusive)
      throw SettingsRefusal(DSD_SHARED_OUTPUT_ERROR);
  }
  outputChannels = isDsd(streamAudioFormat.sampleFormat)
                       ? streamAudioFormat.channels
                       : kPcmDeviceChannels;
  dopFrame = 0;
  initHwParams(sampleRate, streamAudioFormat.sampleFormat,
               streamAudioFormat.bitsPerSample);
  setSwParams();

  currentStreamAudioFormat = streamAudioFormat;
  // initHwParams settles the rate; setSampleFormat any substitution.
  // A fallback container only pads, so the device gets the stream's bits.
  const StreamAudioFormat opened{
      sampleRate, outputChannels, streamAudioFormat.bitsPerSample,
      deviceFormatFor(streamAudioFormat.sampleFormat)};
  deviceInfo = DeviceInfo{opened, deviceAccess()};

  int count = throw_on_error(snd_pcm_poll_descriptors_count(pcm.handle));
  ufds.resize(count);
  throw_on_error(snd_pcm_poll_descriptors(pcm.handle, ufds.data(), count));

  pollTimeout = std::chrono::milliseconds(bufferSize * 1000 / sampleRate);

  spdlog::info(
      "Audio format set up: {}, bufferSize={}, periodSize={}, latency={}ms",
      streamAudioFormat.toString(), bufferSize, periodSize,
      pollTimeout.count());

  // Hack for HiFiBerry boards on Raspberry Pi
  // Sleep to make sure RPi is ready to play.
  // I2S sync mechanism doesn't work properly
  // wich results in the first ~500 ms of the track being cut off.
  if (sleepAfterFormatSetupMs) {
    std::this_thread::sleep_for(
        std::chrono::milliseconds(sleepAfterFormatSetupMs));
  }
}

void PlayedFramesCounter::update(snd_pcm_sframes_t frames) {
  lastPlayedFrames += frames;
  for (auto it = onFramesPlayedCallbacks.begin();
       it != onFramesPlayedCallbacks.end();) {
    if (lastPlayedFrames >= it->frame) {
      const auto onFramesPlayed = std::move(it->onFramesPlayed);
      const auto late = lastPlayedFrames - it->frame;
      it = onFramesPlayedCallbacks.erase(it);
      onFramesPlayed(late);
    } else {
      ++it;
    }
  }
}

std::vector<snd_pcm_sframes_t> PlayedFramesCounter::drainSequence() {
  std::vector<snd_pcm_sframes_t> sequence;
  for (auto &callback : onFramesPlayedCallbacks) {
    sequence.push_back(callback.frame - lastPlayedFrames);
  }

  std::sort(sequence.begin(), sequence.end());
  return sequence;
}

void PlayedFramesCounter::playOut(
    const std::function<void(snd_pcm_sframes_t)> &untilPlayed) {
  snd_pcm_sframes_t played = 0;
  for (const auto frame : drainSequence()) {
    if (untilPlayed)
      untilPlayed(frame - played);
    update(frame - played);
    played = frame;
  }
}

int PlayedFramesCounter::callOnOrAfterFrame(
    snd_pcm_sframes_t frame,
    std::function<void(snd_pcm_sframes_t)> onFramesPlayed) {
  const int id = nextId++;
  onFramesPlayedCallbacks.push_back(
      {id, frame + lastPlayedFrames, std::move(onFramesPlayed)});
  return id;
}

void PlayedFramesCounter::cancel(int id) {
  onFramesPlayedCallbacks.remove_if(
      [id](const Pending &pending) { return pending.id == id; });
}

void PlayedFramesCounter::reset() {
  onFramesPlayedCallbacks.clear();
  lastPlayedFrames = 0;
}

void AlsaAudioEmitter::setSampleFormat(AudioSampleFormat requestedFormat,
                                       snd_pcm_hw_params_t *params,
                                       unsigned significantBits) {
  // A substitution used by a previous 24-in-32 stream may discard real bits
  // from a later 32-bit stream. Negotiate anew for each source format/rate.
  sampleSubstitute.erase(requestedFormat);
  for (const auto format : pcmOutputFormats(requestedFormat, significantBits)) {
    if (snd_pcm_hw_params_set_format(pcm.handle, params, alsaFormat(format)) < 0)
      continue;
    if (requestedFormat != format) {
      spdlog::info("Using sample format {} instead of {}",
                   sampleFormatToString(format),
                   sampleFormatToString(requestedFormat));
      sampleSubstitute[requestedFormat] = format;
    }
    return;
  }
  throw std::runtime_error(
      std::string("Output device takes no format that carries ") +
      sampleFormatToString(requestedFormat));
}

void AlsaAudioEmitter::initHwParams(unsigned int &rate,
                                    AudioSampleFormat format,
                                    unsigned significantBits) {

  unsigned int rrate;
  int dir = 0;
  std::stringstream error;

  snd_pcm_drop(pcm.handle);
  snd_pcm_hw_free(pcm.handle);

  snd_pcm_hw_params_t *params;
  throw_on_error(snd_pcm_hw_params_malloc(&params));

  try {

    /* choose all parameters */
    throw_on_error(snd_pcm_hw_params_any(pcm.handle, params));

    /* set the interleaved read/write format */
    throw_on_error(snd_pcm_hw_params_set_access(
        pcm.handle, params, SND_PCM_ACCESS_MMAP_INTERLEAVED));

    /* set the count of channels */
    throw_on_error(
        snd_pcm_hw_params_set_channels(pcm.handle, params, outputChannels));

    setSampleFormat(format, params, significantBits);

    // DSD transport must never pass through ALSA resampling.
    throw_on_error(snd_pcm_hw_params_set_rate_resample(pcm.handle, params,
                                                       isDsd(format) ? 0 : 1));

    /* set the stream rate */
    rrate = rate;
    if (isDsd(format)) {
      throw_on_error(snd_pcm_hw_params_set_rate(pcm.handle, params, rate, 0));
    } else {
      throw_on_error(
          snd_pcm_hw_params_set_rate_near(pcm.handle, params, &rrate, 0));
    }

    if (rrate != rate) {
      throw std::runtime_error("Rate doesn't match");
    }

    rate = rrate;
    const int deviceBits = throw_on_error(pcm.significantBits(params));
    if (isDop(format) && deviceBits < 24)
      throw std::runtime_error(
          "DoP requires at least 24 significant carrier bits");
    if (!isDsd(format) && deviceBits < static_cast<int>(significantBits))
      throw std::runtime_error(
          "Output device cannot preserve source PCM precision");
    if (currentLivePolicy || requestedPeriodSize == 0 || requestedBufferSize == 0) {
      setLatencyBasedBufferSize(params);
    } else {
      throw_on_error(snd_pcm_hw_params_set_buffer_size_near(pcm.handle, params,
                                                            &bufferSize));
      throw_on_error(snd_pcm_hw_params_set_period_size_near(pcm.handle, params,
                                                            &periodSize, &dir));
    }

    /* write the parameters to device */
    throw_on_error(snd_pcm_hw_params(pcm.handle, params));

  } catch (const std::exception &ex) {
    snd_pcm_hw_params_free(params);
    throw;
  }

  snd_pcm_hw_params_free(params);
}

void AlsaAudioEmitter::setSwParams() {

  snd_pcm_sw_params_t *sw_params;
  throw_on_error(snd_pcm_sw_params_malloc(&sw_params));
  try {
    /* get the current swparams */
    throw_on_error(snd_pcm_sw_params_current(pcm.handle, sw_params));

    // Wake up when the buffer has this amount of space available
    throw_on_error(
        snd_pcm_sw_params_set_avail_min(pcm.handle, sw_params, periodSize));

    /* write the parameters to the playback device */
    throw_on_error(snd_pcm_sw_params(pcm.handle, sw_params));
  } catch (const std::exception &ex) {
    snd_pcm_sw_params_free(sw_params);
    throw;
  }

  snd_pcm_sw_params_free(sw_params);
}

void AlsaAudioEmitter::setLatencyBasedBufferSize(snd_pcm_hw_params_t *params) {
  snd_pcm_hw_params_t *paramsSaved;
  throw_on_error(snd_pcm_hw_params_malloc(&paramsSaved));
  snd_pcm_hw_params_copy(paramsSaved, params);

  int dir = 0;
  const auto latencyMs = currentLivePolicy
      ? std::clamp(currentLivePolicy->targetLatencyMs / 2, 10u, 60u)
      : requestedLatencyMs;
  const auto periodMs = currentLivePolicy ? 5u : requestedPeriodMs;
  auto latencyMcs = latencyMs * 1000;
  auto periodMcs = periodMs * 1000;
  int err = snd_pcm_hw_params_set_buffer_time_near(pcm.handle, params,
                                                   &latencyMcs, &dir);
  try {
    if (err < 0) {
      /* error path -> set period size as first */
      snd_pcm_hw_params_copy(params, paramsSaved);
      /* set the period time */
      throw_on_error(snd_pcm_hw_params_set_period_time_near(pcm.handle, params,
                                                            &periodMcs, 0));

      throw_on_error(snd_pcm_hw_params_get_period_size(params, &periodSize, 0));

      bufferSize = periodSize * (latencyMs / periodMs);
      throw_on_error(snd_pcm_hw_params_set_buffer_size_near(pcm.handle, params,
                                                            &bufferSize));

      throw_on_error(snd_pcm_hw_params_get_buffer_size(params, &bufferSize));
    } else {
      /* standard configuration buffer_time -> periods */
      throw_on_error(snd_pcm_hw_params_get_buffer_size(params, &bufferSize));

      throw_on_error(snd_pcm_hw_params_get_buffer_time(params, &latencyMcs, 0));

      /* set the period time */
      periodMcs = latencyMcs / (latencyMs / periodMs);
      throw_on_error(snd_pcm_hw_params_set_period_time_near(pcm.handle, params,
                                                            &periodMcs, 0));

      throw_on_error(snd_pcm_hw_params_get_period_size(params, &periodSize, 0));
    }
  } catch (const std::exception &ex) {
    snd_pcm_hw_params_free(paramsSaved);
    throw;
  }

  snd_pcm_hw_params_free(paramsSaved);
}

size_t AlsaAudioEmitter::readAndConvertFrames(void *dest, size_t bytes) {
  if (isDsd(currentStreamAudioFormat.sampleFormat)) {
    if (!dsdAllowed.load())
      throw std::runtime_error(
          "DSD requires fixed volume; software gain cannot alter DSD");
    const auto count = inputNode->read(dest, bytes);
    const auto frames = snd_pcm_bytes_to_frames(pcm.handle, count);
    stampDop(dest, frames);
    return frames;
  }

  const auto sourceFormat = currentStreamAudioFormat.sampleFormat;
  const auto destFormat = deviceFormatFor(sourceFormat);
  // A mono stream plays each sample in every output channel.
  const unsigned copies =
      currentStreamAudioFormat.channels == 1 ? outputChannels : 1;
  if (sourceFormat == destFormat && copies == 1) {
    const size_t bytesRead = inputNode->read(dest, bytes);
    return snd_pcm_bytes_to_frames(pcm.handle, bytesRead);
  }

  const auto width = sampleSize(destFormat);
  const size_t sourceSamples =
      snd_pcm_bytes_to_samples(pcm.handle, bytes) / copies;
  auto &source = conversionScratch;
  source.resize(sourceSamples * sampleSize(sourceFormat));
  const auto count =
      inputNode->read(source.data(), source.size()) / sampleSize(sourceFormat);
  auto *out = static_cast<uint8_t *>(dest);
  const auto converted = convertSampleFormat(source.data(), sourceFormat, count,
                                             out, destFormat, count * width);
  if (copies > 1) {
    // Work backwards so expansion cannot overwrite unexpanded samples.
    for (size_t i = converted; i > 0; --i) {
      auto *first = out + (i - 1) * copies * width;
      std::memmove(first, out + (i - 1) * width, width);
      for (unsigned c = 1; c < copies; ++c)
        std::memcpy(first + c * width, first, width);
    }
  }
  return converted * copies / outputChannels;
}

AudioSampleFormat
AlsaAudioEmitter::deviceFormatFor(AudioSampleFormat streamFormat) const {
  const auto substitute = sampleSubstitute.find(streamFormat);
  return substitute == sampleSubstitute.end() ? streamFormat
                                              : substitute->second;
}

void AlsaAudioEmitter::stampDop(void *dest, size_t frames) {
  const auto format = currentStreamAudioFormat.sampleFormat;
  if (!isDop(format))
    return;
  stampDopMarkers({static_cast<uint8_t *>(dest),
                   frames * outputChannels * sampleSize(format)},
                  outputChannels, format, dopFrame);
}
