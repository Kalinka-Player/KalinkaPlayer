#include "AlsaAudioEmitter.h"
#include "AudioSampleFormat.h"
#include "DsdFormat.h"
#include "Log.h"
#include "PerfMon.h"
#include "StateMonitor.h"
#include "StreamState.h"
#include "Utils.h"

#include <condition_variable>
#include <iostream>
#include <mutex>
#include <pthread.h>
#include <sstream>
#include <stdexcept>

#define throw_on_error(func) throw_on_error_impl(func, #func " failed")
#define log_on_error(func) log_on_error_impl(func, #func " failed")

namespace {
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

// PCM retains its existing stereo route; DSD negotiates the source channels.
constexpr unsigned int kDeviceChannels = 2;

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
      reopenDeviceWithNewFormat(value_or(
          config, "fixups.alsa_reopen_device_with_new_format", false)) {
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

void AlsaAudioEmitter::setSoftwareVolume(float gain) {
  softwareGain.store(gain, std::memory_order_relaxed);
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
    setState(StreamState(AudioGraphNodeState::STOPPED));
  }
}

DeviceAccess AlsaAudioEmitter::deviceAccess() const {
  if (pcmHandle == nullptr) {
    return DeviceAccess::Unknown;
  }
  // Only a raw hw handle rules out a mixer, a resampler or a sound server:
  // every other PCM type has something of its own between us and the card.
  return snd_pcm_type(pcmHandle) == SND_PCM_TYPE_HW ? DeviceAccess::Exclusive
                                                    : DeviceAccess::Shared;
}

void AlsaAudioEmitter::setState(const StreamState &newState) {
  StreamState stamped = newState;
  stamped.deviceInfo = deviceInfo;
  AudioGraphNode::setState(stamped);
}

snd_pcm_sframes_t AlsaAudioEmitter::queuedFrames() {
  // Only a running stream is holding anything: a drop or a drain leaves it in
  // SETUP with nothing left to play, while the delay goes on reporting the
  // link latency (PipeWire) long after the buffer is gone.
  if (pcmHandle == nullptr ||
      snd_pcm_state(pcmHandle) != SND_PCM_STATE_RUNNING) {
    return 0;
  }
  snd_pcm_sframes_t queued = 0;
  if (snd_pcm_delay(pcmHandle, &queued) < 0) {
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
      pauseRequestSignal.respond(false);
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
  auto retVal = inputNode->seekTo(seekValue);
  if (retVal == -1U) {
    spdlog::warn("Seek request failed: requested={}", seekValue);
    seekRequestSignal.respond(-1);
    return false;
  }
  seekRequestSignal.respond(framesToTimeMs(retVal).count());
  seekHappened = true;
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

void AlsaAudioEmitter::waitForCommand(std::stop_token token) {
  std::mutex mutex;
  std::condition_variable_any woken;
  std::unique_lock lock(mutex);
  woken.wait(lock, token, [] { return false; });
}

long AlsaAudioEmitter::reachedPositionMs() {
  const auto current = getState();
  // Only a drain leaves a streaming state here, and it plays out all written.
  return current.state == AudioGraphNodeState::STREAMING
             ? framesToTimeMs(currentSourceTotalFramesWritten).count()
             : current.position;
}

bool AlsaAudioEmitter::handlePauseSignal(bool paused) {
  auto state = getState();
  auto stateToSet =
      paused ? AudioGraphNodeState::PAUSED : AudioGraphNodeState::STREAMING;

  this->paused = paused;
  StreamState newState(stateToSet, 0, state.streamInfo);
  newState.position = framesToTimeMs(currentSourceTotalFramesWritten).count();
  snd_pcm_sframes_t delay = 0;
  int err = snd_pcm_delay(pcmHandle, &delay);
  if (err < 0) {
    spdlog::error("Error when calling snd_pcm_delay: {}", snd_strerror(err));
    return false;
  }
  playedFramesCounter.callOnOrAfterFrame(
      delay,
      [this, newState](snd_pcm_sframes_t frames) { setState(newState); });

  return true;
}

void AlsaAudioEmitter::openDevice() {
  if (pcmHandle != nullptr) {
    return;
  }

  int err =
      snd_pcm_open(&pcmHandle, deviceName.c_str(), SND_PCM_STREAM_PLAYBACK, 0);

  /* Error check */
  if (err < 0) {
    std::stringstream stream;
    stream << "Cannot open audio device " << deviceName << " ("
           << snd_strerror(err) << ")";
    pcmHandle = nullptr;
    spdlog::error(stream.str());
    setState({AudioGraphNodeState::ERROR,
              StreamError{StreamErrorSource::AUDIO_OUTPUT, stream.str()}});

    throw std::runtime_error(stream.str());
  }

  currentSourceTotalFramesWritten = 0;
  playedFramesCounter.reset();
}

void AlsaAudioEmitter::closeDevice() {
  if (pcmHandle != nullptr) {
    log_on_error(snd_pcm_drop(pcmHandle));
    log_on_error(snd_pcm_hw_free(pcmHandle));
    log_on_error(snd_pcm_close(pcmHandle));
    pcmHandle = nullptr;
    currentStreamAudioFormat = StreamAudioFormat();
    deviceInfo.reset();
  }
}

snd_pcm_sframes_t AlsaAudioEmitter::waitForAlsaBufferSpace() {
  auto getAvailableFrames = [this]() -> snd_pcm_sframes_t {
    return throw_on_error(snd_pcm_avail_update(pcmHandle));
  };

  perfmon_end("fullPeriodProcessingTime");
  perfmon_begin("waitForAlsaBufferSpace");

  snd_pcm_sframes_t frames = getAvailableFrames();
  unsigned short revents = 0;

  while (!playbackThread.get_stop_token().stop_requested() &&
         static_cast<snd_pcm_uframes_t>(frames) < periodSize) {
    int ret = poll(ufds.data(), ufds.size(), pollTimeout.count());
    if (ret == 0) {
      continue;
    }
    frames = getAvailableFrames();
    throw_on_error(snd_pcm_poll_descriptors_revents(pcmHandle, ufds.data(),
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
    inputNode->acceptSourceChange();
    inputNodeState = inputNode->getState();
    // Unconditional, unlike the source loop: the queue holds the outgoing
    // track, so it says nothing about where the incoming one begins.
    beginSourceAt(inputNode->streamReadPosition());

    auto newStreamInfo = inputNodeState.streamInfo;
    if (inputNodeState.state != AudioGraphNodeState::STREAMING ||
        !newStreamInfo.has_value() ||
        newStreamInfo.value().format != currentStreamAudioFormat || paused) {
      spdlog::info("Source changed - not streaming, different format or paused "
                   "- draining");
      drainPcm();
      setStreamId(incomingStreamId);
      setState(StreamState(AudioGraphNodeState::SOURCE_CHANGED));
      return false;
    } else {
      snd_pcm_sframes_t framesDelay = 0;
      log_on_error(snd_pcm_delay(pcmHandle, &framesDelay));
      spdlog::debug("Reporting source change in {}ms, frames={}",
                    framesToTimeMs(framesDelay).count(), framesDelay);

      playedFramesCounter.callOnOrAfterFrame(
          framesDelay,
          [this, streamInfo = newStreamInfo, incomingStreamId,
           start = currentSourceStartFrames](snd_pcm_sframes_t frames) {
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
  int err = snd_pcm_mmap_begin(pcmHandle, &my_areas, &offset, &frames);
  if (err < 0) {
    if (isDsd(currentStreamAudioFormat.sampleFormat))
      throw std::runtime_error("DSD output underrun; restart playback");
    if (xrun_recovery(pcmHandle, err) < 0) {
      throw std::runtime_error("Error in mmap begin: " +
                               std::string(snd_strerror(err)));
    }
  }

  void *ptr = static_cast<uint8_t *>(my_areas[0].addr) +
              (my_areas[0].first / 8) +
              snd_pcm_frames_to_bytes(pcmHandle, offset);

  frames = func(ptr, frames, snd_pcm_frames_to_bytes(pcmHandle, frames));

  err = snd_pcm_mmap_commit(pcmHandle, offset, frames);
  if (static_cast<snd_pcm_uframes_t>(err) != frames || err < 0) {
    if (isDsd(currentStreamAudioFormat.sampleFormat))
      throw std::runtime_error("Incomplete DSD output write; restart playback");
    if (xrun_recovery(pcmHandle, err) < 0) {
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

  if (snd_pcm_state(pcmHandle) == SND_PCM_STATE_RUNNING) {
    playedFramesCounter.update(framesToRead);
  }

  while (framesRead < framesToRead && !stopToken.stop_requested()) {
    snd_pcm_uframes_t frames = framesToRead - framesRead;
    if (paused) {

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
      auto actualFrames = writeToAlsa(
          frames, [this](void *ptr, snd_pcm_uframes_t frames, size_t bytes) {
            return readAndConvertFrames(ptr, bytes);
          });

      framesRead += actualFrames;
      currentSourceTotalFramesWritten += actualFrames;

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
  if (snd_pcm_state(pcmHandle) == SND_PCM_STATE_RUNNING) {
    snd_pcm_sframes_t delayFrames = 0;
    log_on_error(snd_pcm_delay(pcmHandle, &delayFrames));
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
  throw_on_error(snd_pcm_start(pcmHandle));
  setState({AudioGraphNodeState::STREAMING, framesToTimeMs(position).count(),
            streamInfo});
}

void AlsaAudioEmitter::drainPcm() {
  if (snd_pcm_state(pcmHandle) == SND_PCM_STATE_RUNNING) {
    auto drainSequence = playedFramesCounter.drainSequence();
    if (!drainSequence.empty()) {
      snd_pcm_sframes_t prevFrames = 0;
      for (auto frames : drainSequence) {
        std::this_thread::sleep_for(framesToTimeMs(frames - prevFrames));
        playedFramesCounter.update(frames - prevFrames);
        prevFrames = frames;
      }
    }
    log_on_error(snd_pcm_drop(pcmHandle));
  }
  if (paused || seekHappened) {
    log_on_error(snd_pcm_drop(pcmHandle));
  } else {
    log_on_error(snd_pcm_drain(pcmHandle));
  }
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

      setupAudioFormat(streamInfo.value().format);
      streamInfo.value().format = currentStreamAudioFormat;

      bool started = false;
      seekHappened = false;
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
      setState({AudioGraphNodeState::PREPARING,
                framesToTimeMs(streamStartPosition).count(), streamInfo});
      paused = false;

      while (!token.stop_requested()) {
        auto combinedToken =
            combineStopTokens(token, seekRequestSignal.getStopToken(),
                              pauseRequestSignal.getStopToken());

        if (paused && !started) {
          // Silence written now would play before the audio it waits for.
          waitForCommand(combinedToken.get_token());
        } else {
          auto framesToRead = waitForAlsaBufferSpace();
          if (!framesToRead) {
            break;
          }
          auto framesRead =
              readIntoAlsaFromStream(combinedToken.get_token(), framesToRead);
          if (framesRead < 0) {
            break;
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
            const bool success = handlePauseSignal(requested);
            pauseRequestSignal.respond(success ? requested : paused);
          }
          continue;
        }

        if (seekRequestSignal.getValue()) {
          if (!handleSeekSignal()) {
            continue;
          }

          drainPcm();
          break;
        }

        if (!started) {
          startPcmStream(streamInfo.value(), streamStartPosition);
          started = true;
        }
      }

      if (token.stop_requested()) {
        log_on_error(snd_pcm_drop(pcmHandle));
      }
    }
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
  inputNode = nullptr;
  isWorkerRunning = false;
  seekRequestSignal.close(static_cast<size_t>(-1));
  pauseRequestSignal.close(false);
}

void AlsaAudioEmitter::setupAudioFormat(
    const StreamAudioFormat &streamAudioFormat) {

  spdlog::debug("Setting up audio format: {}", streamAudioFormat.toString());

  if (pcmHandle != nullptr && streamAudioFormat == currentStreamAudioFormat) {
    spdlog::debug("Audio format is already set up - ignoring");
    throw_on_error(snd_pcm_prepare(pcmHandle));
    return;
  }

  if (reopenDeviceWithNewFormat && pcmHandle != nullptr) {
    closeDevice();
  }

  if (pcmHandle == nullptr) {
    openDevice();
  }

  unsigned sampleRate = streamAudioFormat.sampleRate;
  if (sampleRate == 0) {
    throw std::runtime_error("Invalid stream sample rate: 0");
  }
  bufferSize = requestedBufferSize;
  periodSize = requestedPeriodSize;

  if (isDsd(streamAudioFormat.sampleFormat)) {
    if (!dsdAllowed.load())
      throw SettingsRefusal("DSD needs Volume control set to Fixed output");
    if (deviceAccess() != DeviceAccess::Exclusive)
      throw SettingsRefusal(DSD_SHARED_OUTPUT_ERROR);
  }
  outputChannels = isDsd(streamAudioFormat.sampleFormat)
                       ? streamAudioFormat.channels
                       : kDeviceChannels;
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

  int count = throw_on_error(snd_pcm_poll_descriptors_count(pcmHandle));
  ufds.resize(count);
  throw_on_error(snd_pcm_poll_descriptors(pcmHandle, ufds.data(), count));

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
    if (lastPlayedFrames >= it->first) {
      it->second(lastPlayedFrames - it->first);
      it = onFramesPlayedCallbacks.erase(it);
    } else {
      ++it;
    }
  }
}

std::vector<snd_pcm_sframes_t> PlayedFramesCounter::drainSequence() {
  std::vector<snd_pcm_sframes_t> sequence;
  for (auto &callback : onFramesPlayedCallbacks) {
    sequence.push_back(callback.first - lastPlayedFrames);
  }

  std::sort(sequence.begin(), sequence.end());
  return sequence;
}

void PlayedFramesCounter::callOnOrAfterFrame(
    snd_pcm_sframes_t frame,
    std::function<void(snd_pcm_sframes_t)> onFramesPlayed) {
  onFramesPlayedCallbacks.push_back({frame + lastPlayedFrames, onFramesPlayed});
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
  auto formatToProbe = requestedFormat;

  while (snd_pcm_hw_params_set_format(pcmHandle, params,
                                      alsaFormat(formatToProbe)) < 0) {
    const auto next = pcmFallback(formatToProbe, significantBits);
    if (!next) {
      throw std::runtime_error(
          std::string("Output device takes no format that carries ") +
          sampleFormatToString(requestedFormat));
    }
    spdlog::warn("{} not supported, trying {}",
                 sampleFormatToString(formatToProbe),
                 sampleFormatToString(*next));
    formatToProbe = *next;
  }

  if (requestedFormat != formatToProbe) {
    spdlog::warn("Using sample format {} instead of {}",
                 sampleFormatToString(formatToProbe),
                 sampleFormatToString(requestedFormat));
    sampleSubstitute[requestedFormat] = formatToProbe;
  }
}

void AlsaAudioEmitter::initHwParams(unsigned int &rate,
                                    AudioSampleFormat format,
                                    unsigned significantBits) {

  unsigned int rrate;
  int dir = 0;
  std::stringstream error;

  snd_pcm_drop(pcmHandle);
  snd_pcm_hw_free(pcmHandle);

  snd_pcm_hw_params_t *params;
  throw_on_error(snd_pcm_hw_params_malloc(&params));

  try {

    /* choose all parameters */
    throw_on_error(snd_pcm_hw_params_any(pcmHandle, params));

    /* set the interleaved read/write format */
    throw_on_error(snd_pcm_hw_params_set_access(
        pcmHandle, params, SND_PCM_ACCESS_MMAP_INTERLEAVED));

    /* set the count of channels */
    throw_on_error(
        snd_pcm_hw_params_set_channels(pcmHandle, params, outputChannels));

    setSampleFormat(format, params, significantBits);

    // DSD transport must never pass through ALSA resampling.
    throw_on_error(snd_pcm_hw_params_set_rate_resample(pcmHandle, params,
                                                       isDsd(format) ? 0 : 1));

    /* set the stream rate */
    rrate = rate;
    if (isDsd(format)) {
      throw_on_error(snd_pcm_hw_params_set_rate(pcmHandle, params, rate, 0));
    } else {
      throw_on_error(
          snd_pcm_hw_params_set_rate_near(pcmHandle, params, &rrate, 0));
    }

    if (rrate != rate) {
      throw std::runtime_error("Rate doesn't match");
    }

    rate = rrate;
    const int deviceBits = throw_on_error(snd_pcm_hw_params_get_sbits(params));
    if (isDop(format) && deviceBits < 24)
      throw std::runtime_error(
          "DoP requires at least 24 significant carrier bits");
    if (!isDsd(format) && deviceBits < static_cast<int>(significantBits))
      throw std::runtime_error(
          "Output device cannot preserve source PCM precision");
    if (requestedPeriodSize == 0 || requestedBufferSize == 0) {
      setLatencyBasedBufferSize(params);
    } else {
      throw_on_error(snd_pcm_hw_params_set_buffer_size_near(pcmHandle, params,
                                                            &bufferSize));
      throw_on_error(snd_pcm_hw_params_set_period_size_near(pcmHandle, params,
                                                            &periodSize, &dir));
    }

    /* write the parameters to device */
    throw_on_error(snd_pcm_hw_params(pcmHandle, params));

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
    throw_on_error(snd_pcm_sw_params_current(pcmHandle, sw_params));

    // Wake up when the buffer has this amount of space available
    throw_on_error(
        snd_pcm_sw_params_set_avail_min(pcmHandle, sw_params, periodSize));

    /* write the parameters to the playback device */
    throw_on_error(snd_pcm_sw_params(pcmHandle, sw_params));
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
  auto latencyMcs = requestedLatencyMs * 1000;
  auto periodMcs = requestedPeriodMs * 1000;
  int err = snd_pcm_hw_params_set_buffer_time_near(pcmHandle, params,
                                                   &latencyMcs, &dir);
  try {
    if (err < 0) {
      /* error path -> set period size as first */
      snd_pcm_hw_params_copy(params, paramsSaved);
      /* set the period time */
      throw_on_error(snd_pcm_hw_params_set_period_time_near(pcmHandle, params,
                                                            &periodMcs, 0));

      throw_on_error(snd_pcm_hw_params_get_period_size(params, &periodSize, 0));

      bufferSize = periodSize * (requestedLatencyMs / requestedPeriodMs);
      throw_on_error(snd_pcm_hw_params_set_buffer_size_near(pcmHandle, params,
                                                            &bufferSize));

      throw_on_error(snd_pcm_hw_params_get_buffer_size(params, &bufferSize));
    } else {
      /* standard configuration buffer_time -> periods */
      throw_on_error(snd_pcm_hw_params_get_buffer_size(params, &bufferSize));

      throw_on_error(snd_pcm_hw_params_get_buffer_time(params, &latencyMcs, 0));

      /* set the period time */
      periodMcs = latencyMcs / (requestedLatencyMs / requestedPeriodMs);
      throw_on_error(snd_pcm_hw_params_set_period_time_near(pcmHandle, params,
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
  const float gain = softwareGain.load(std::memory_order_relaxed);
  if (isDsd(currentStreamAudioFormat.sampleFormat)) {
    if (!dsdAllowed.load() || gain != 1.0f)
      throw std::runtime_error(
          "DSD requires fixed volume; software gain cannot alter DSD");
    const auto count = inputNode->read(dest, bytes);
    const auto frames = snd_pcm_bytes_to_frames(pcmHandle, count);
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
    // Unity gain (the common case) is a no-op, so playback stays bit-perfect.
    applyGainInPlace(dest, bytesRead, sourceFormat, gain);
    return snd_pcm_bytes_to_frames(pcmHandle, bytesRead);
  }

  const auto width = sampleSize(destFormat);
  const size_t sourceSamples =
      snd_pcm_bytes_to_samples(pcmHandle, bytes) / copies;
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
  // Gain is applied on the samples handed to ALSA, in the on-wire format.
  applyGainInPlace(out, converted * copies * width, destFormat, gain);
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
