#include "AlsaAudioEmitter.h"
#include "AudioSampleFormat.h"
#include "Log.h"
#include "PerfMon.h"
#include "StateMonitor.h"
#include "StreamState.h"
#include "Utils.h"

#include <iostream>
#include <pthread.h>
#include <sstream>
#include <stdexcept>

#define throw_on_error(func) throw_on_error_impl(func, #func " failed")
#define log_on_error(func) log_on_error_impl(func, #func " failed")

namespace {
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

// Every device is opened for stereo; nothing in the graph converts channels.
constexpr unsigned int kDeviceChannels = 2;

std::unordered_map<AudioSampleFormat, snd_pcm_format_t> ALSA_FORMAT_MAP = {
    {AudioSampleFormat::PCM16_LE, SND_PCM_FORMAT_S16_LE},
    {AudioSampleFormat::PCM24_LE, SND_PCM_FORMAT_S24_LE},
    {AudioSampleFormat::PCM32_LE, SND_PCM_FORMAT_S32_LE},
    {AudioSampleFormat::PCM24_3LE, SND_PCM_FORMAT_S24_3LE}};

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
      setState(StreamState(AudioGraphNodeState::PREPARING));
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
          frames, [](void *ptr, snd_pcm_uframes_t frames, size_t bytes) {
            memset(ptr, 0, bytes);
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
          // A pause/seek interrupts the wait even though this is not an
          // underrun. Return to the worker so it can acknowledge the command;
          // draining/restarting here leaves that request pending forever.
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
  if (snd_pcm_state(pcmHandle) == SND_PCM_STATE_RUNNING) {
    snd_pcm_sframes_t delayFrames = 0;
    log_on_error(snd_pcm_delay(pcmHandle, &delayFrames));
    auto timeout = framesToTimeMs(std::max(
        0l, delayFrames - static_cast<snd_pcm_sframes_t>(2 * periodSize)));
    return inputNode->waitForDataFor(
        stopToken, timeout, snd_pcm_frames_to_bytes(pcmHandle, frames));
  }

  return inputNode->waitForData(stopToken,
                                snd_pcm_frames_to_bytes(pcmHandle, frames));
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
      // A refill is still the same timeline. Reporting zero here rewinds a
      // live producer's progress/credit when it has already played this prefix.
      setState({AudioGraphNodeState::PREPARING,
                framesToTimeMs(streamStartPosition).count(), streamInfo});
      paused = false;

      while (!token.stop_requested()) {
        auto framesToRead = waitForAlsaBufferSpace();
        if (!framesToRead) {
          break;
        }

        auto combinedToken =
            combineStopTokens(token, seekRequestSignal.getStopToken(),
                              pauseRequestSignal.getStopToken());

        auto framesRead =
            readIntoAlsaFromStream(combinedToken.get_token(), framesToRead);
        if (framesRead < 0 || token.stop_requested()) {
          break;
        }

        if (pauseRequestSignal.getValue()) {
          if (paused != *pauseRequestSignal.getValue()) {
            bool success = handlePauseSignal(*pauseRequestSignal.getValue());
            pauseRequestSignal.respond(success ? *pauseRequestSignal.getValue()
                                               : paused);
          } else {
            pauseRequestSignal.respond(paused);
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
    setState({AudioGraphNodeState::ERROR,
              StreamError{StreamErrorSource::AUDIO_OUTPUT,
                          "Internal error: " + std::string(ex.what())}});
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

  initHwParams(sampleRate, streamAudioFormat.sampleFormat);
  setSwParams();

  currentStreamAudioFormat = streamAudioFormat;
  // initHwParams settles the rate; setSampleFormat any substitution.
  const AudioSampleFormat deviceSampleFormat =
      sampleSubstitute.count(streamAudioFormat.sampleFormat)
          ? sampleSubstitute[streamAudioFormat.sampleFormat]
          : streamAudioFormat.sampleFormat;
  const StreamAudioFormat opened{
      sampleRate, kDeviceChannels,
      static_cast<unsigned int>(sampleBits(deviceSampleFormat)),
      deviceSampleFormat};
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
                                       snd_pcm_hw_params_t *params) {
  auto formatToProbe = sampleSubstitute.count(requestedFormat)
                           ? sampleSubstitute[requestedFormat]
                           : requestedFormat;

  while (true) {
    auto alsaFormat = ALSA_FORMAT_MAP.at(formatToProbe);
    int err = snd_pcm_hw_params_set_format(pcmHandle, params, alsaFormat);
    if (err >= 0) {
      break;
    }

    switch (formatToProbe) {
    case AudioSampleFormat::PCM24_LE:
      spdlog::warn("PCM24_LE not supported, trying PCM32_LE");
      formatToProbe = AudioSampleFormat::PCM32_LE;
      break;
    case AudioSampleFormat::PCM32_LE:
      spdlog::warn("PCM32_LE not supported, trying PCM24_3LE");
      formatToProbe = AudioSampleFormat::PCM24_3LE;
      break;
    default:
      throw std::runtime_error("Unsupported sample format, format=" +
                               std::to_string(static_cast<int>(formatToProbe)));
    }
  }

  if (requestedFormat != formatToProbe) {
    spdlog::warn("Using sample format {} instead of {}",
                 sampleFormatToString(formatToProbe),
                 sampleFormatToString(requestedFormat));
    sampleSubstitute[requestedFormat] = formatToProbe;
  }
}

void AlsaAudioEmitter::initHwParams(unsigned int &rate,
                                    AudioSampleFormat format) {

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
        snd_pcm_hw_params_set_channels(pcmHandle, params, kDeviceChannels));

    setSampleFormat(format, params);

    // Enabled resampling
    throw_on_error(snd_pcm_hw_params_set_rate_resample(pcmHandle, params, 1));

    /* set the stream rate */
    rrate = rate;
    throw_on_error(
        snd_pcm_hw_params_set_rate_near(pcmHandle, params, &rrate, 0));

    if (rrate != rate) {
      throw std::runtime_error("Rate doesn't match");
    }

    rate = rrate;
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

  if (!sampleSubstitute.count(currentStreamAudioFormat.sampleFormat)) {
    size_t bytesRead = inputNode->read(dest, bytes);
    // Apply software volume in the on-wire format ALSA will play. Unity (the
    // common case) is a no-op, so bit-perfect playback is preserved.
    applyGainInPlace(dest, bytesRead, currentStreamAudioFormat.sampleFormat,
                     gain);
    return snd_pcm_bytes_to_frames(pcmHandle, bytesRead);
  }

  auto sourceFormat = currentStreamAudioFormat.sampleFormat;
  auto destFormat = sampleSubstitute[currentStreamAudioFormat.sampleFormat];
  size_t destSampleCount = snd_pcm_bytes_to_samples(pcmHandle, bytes);
  size_t sourceBytesToRead = destSampleCount * sampleSize(sourceFormat);

  std::vector<uint8_t> sampleBuffer(sourceBytesToRead);
  size_t bytesRead = inputNode->read(sampleBuffer.data(), sourceBytesToRead);
  size_t sampleCount = bytesRead / sampleSize(sourceFormat);

  size_t convertedSamples = convertSampleFormat(
      sampleBuffer.data(), sourceFormat, sampleCount, dest, destFormat, bytes);
  assert(convertedSamples == sampleCount);

  // Gain is applied after format conversion, on the samples handed to ALSA.
  applyGainInPlace(dest, convertedSamples * sampleSize(destFormat), destFormat,
                   gain);

  auto frames = snd_pcm_bytes_to_frames(
      pcmHandle, snd_pcm_samples_to_bytes(pcmHandle, convertedSamples));

  return frames;
}
