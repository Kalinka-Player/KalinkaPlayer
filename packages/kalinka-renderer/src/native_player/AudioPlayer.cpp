#include "AudioPlayer.h"
#include "AlsaAudioEmitter.h"
#include "AudioGraphHttpStream.h"
#include "AudioStreamSwitcher.h"
#include "Config.h"
#include "FileInputNode.h"
#include "FlacStreamDecoder.h"
#include "Log.h"
#include "Mp3StreamDecoder.h"
#include "PerfMon.h"
#include "SineWaveNode.h"
#include "StateMonitor.h"
#include "VorbisStreamDecoder.h"

#include <algorithm>
#include <cstdlib>

namespace {
// These numbers can be reduced depending on audio bitness,
// sample rate and network throughput.
//
// 1.5MB = 1s for 192KHz / 24bit audio / stereo
const size_t FLAC_BUFFER_SIZE = 1536000;
const size_t MPEG_BUFFER_SIZE = 768000;
const size_t VORBIS_BUFFER_SIZE = 768000;
// 750KB, 50% of flac buffer size
// approx. flac compression ratio is 50%
const size_t HTTP_BUFFER_SIZE = 768000;

const size_t CHUNK_SIZE = HTTP_BUFFER_SIZE / 2;

bool isInvalidState(AudioGraphNodeState state) {
  return state == AudioGraphNodeState::FINISHED ||
         state == AudioGraphNodeState::STOPPED ||
         state == AudioGraphNodeState::ERROR;
}

// Map a 0..100 percent to a linear amplitude gain for software volume. Cubic
// gives a roughly perceptual taper (~ -18 dB at 50%) and lands exactly on 1.0
// at 100%, which AlsaAudioEmitter treats as a bit-perfect bypass.
float percentToGain(int percent) {
  const float p = std::clamp(percent, 0, 100) / 100.0f;
  return p * p * p;
}

// Pseudo-scheme for the speaker test:
//   tone://<left|right|both>?freq=<hz>&duration_ms=<ms>
// Generates audio in-process (SineWaveNode) instead of reading a stream, so
// no decoder is attached. Out-of-range values are clamped, unknown channel
// names fall back to both.
struct ToneSpec {
  int frequency = 440;
  int durationMs = 2000;
  ToneChannel channel = ToneChannel::Both;
};

ToneSpec parseToneUrl(const std::string &url) {
  ToneSpec spec;
  std::string rest = url.substr(7); // strip "tone://"
  std::string query;
  const auto qpos = rest.find('?');
  if (qpos != std::string::npos) {
    query = rest.substr(qpos + 1);
    rest = rest.substr(0, qpos);
  }
  if (rest == "left") {
    spec.channel = ToneChannel::Left;
  } else if (rest == "right") {
    spec.channel = ToneChannel::Right;
  }

  size_t start = 0;
  while (start < query.size()) {
    auto end = query.find('&', start);
    if (end == std::string::npos) {
      end = query.size();
    }
    const auto kv = query.substr(start, end - start);
    const auto eq = kv.find('=');
    if (eq != std::string::npos) {
      const auto key = kv.substr(0, eq);
      const int value = std::atoi(kv.substr(eq + 1).c_str());
      if (key == "freq") {
        spec.frequency = value;
      } else if (key == "duration_ms") {
        spec.durationMs = value;
      }
    }
    start = end + 1;
  }

  spec.frequency = std::clamp(spec.frequency, 20, 20000);
  spec.durationMs = std::clamp(spec.durationMs, 100, 10000);
  return spec;
}
} // namespace

struct StreamNodes {
  using NodeChain = std::list<std::shared_ptr<AudioGraphOutputNode>>;
  NodeChain nodeChain;
  const std::string url;
  const StreamId id;

  StreamNodes(StreamId id, const std::string &url, const Config &config,
              const AudioFormat format, size_t startOffsetMs = 0)
      : url(url), id(id) {
    if (url.substr(0, 7) == "tone://") {
      // Speaker-test tone, generated in-process — emits PCM frames directly,
      // so no decoder is attached and `format` is ignored.
      const auto spec = parseToneUrl(url);
      spdlog::debug("Creating SineWaveNode: freq={}Hz duration={}ms channel={}",
                    spec.frequency, spec.durationMs,
                    static_cast<int>(spec.channel));
      nodeChain.emplace_back(std::make_shared<SineWaveNode>(
          id, spec.frequency, spec.durationMs, 48000u, 16u, spec.channel));
      return;
    }

    if (url.substr(0, 7) == "file://") {
      // Use FileInputNode for local files
      std::string filePath = url.substr(7);
      spdlog::debug("Creating FileInputNode for local file: {}", filePath);
      nodeChain.emplace_back(std::make_shared<FileInputNode>(id, filePath));
    } else {
      // Use AudioGraphHttpStream for network streams
      spdlog::debug("Creating AudioGraphHttpStream for URL: {}", url);
      nodeChain.emplace_back(std::make_shared<AudioGraphHttpStream>(
          id, url, value_or(config, "input.http.buffer_size", HTTP_BUFFER_SIZE),
          value_or(config, "input.http.chunk_size", CHUNK_SIZE),
          std::chrono::seconds(value_or(
              config, "input.http.stall_timeout",
              AudioGraphHttpStream::DEFAULT_STALL_TIMEOUT.count()))));
    }

    auto decoder =
        connectDecoder(nodeChain.back(), config, format, startOffsetMs);
    if (nodeChain.back() != decoder) {
      nodeChain.emplace_back(std::move(decoder));
    }
  }

  std::shared_ptr<AudioGraphOutputNode>
  connectDecoder(std::shared_ptr<AudioGraphOutputNode> outputNode,
                 const Config &config, const AudioFormat format,
                 size_t startOffsetMs) {
    switch (format) {
    case AudioFormat::FormatFlac: {
      auto decoder = std::make_shared<FlacStreamDecoder>(
          id, value_or(config, "decoder.flac.buffer_size", FLAC_BUFFER_SIZE),
          startOffsetMs);
      decoder->connectTo(outputNode);
      return decoder;
    }
    case AudioFormat::FormatMpeg: {
      auto decoder = std::make_shared<Mp3StreamDecoder>(
          id, value_or(config, "decoder.mpeg.buffer_size", MPEG_BUFFER_SIZE),
          startOffsetMs);
      decoder->connectTo(outputNode);
      return decoder;
    }
    case AudioFormat::FormatVorbis: {
      auto decoder = std::make_shared<VorbisStreamDecoder>(
          id, value_or(config, "decoder.vorbis.buffer_size", VORBIS_BUFFER_SIZE),
          startOffsetMs);
      decoder->connectTo(outputNode);
      return decoder;
    }
    default:
      spdlog::warn(
          "Undefined format {}, assuming raw and not attaching any decoder",
          static_cast<int>(format));
      return outputNode;
    }
  }

  StreamNodes(StreamNodes &&other)
      : nodeChain(std::move(other.nodeChain)), url(std::move(other.url)),
        id(other.id) {}

  ~StreamNodes() {
    for (NodeChain::reverse_iterator rit = nodeChain.rbegin();
         rit != nodeChain.rend(); ++rit) {
      auto inputNode = std::dynamic_pointer_cast<AudioGraphInputNode>(*rit);
      if (inputNode) {
        ++rit;
        if (rit != nodeChain.rend()) {
          inputNode->disconnect(*rit);
        }
      }
    }
  }
};

AudioPlayer::AudioPlayer(const Config &config)
    : config(config), streamConfig(config),
      audioEmitter(std::make_shared<AlsaAudioEmitter>(config)),
      streamSwitcher(std::make_shared<AudioStreamSwitcher>()) {
  std::erase_if(streamConfig,
                [](const auto &entry) { return !isStreamKey(entry.first); });
  // Renderer delta: no initLogger() — the renderer owns the spdlog setup.
  perfmon_print_periodically(5);
  // Volume handling stays "fixed" (no processing) until the local output device
  // is set up and calls configureVolume(). Volume settings now live on that
  // device's module config, not in the global config read here.
}

bool AudioPlayer::configureVolume(const std::string &mode,
                                  const std::string &mixerControl,
                                  std::string *error) {
  std::lock_guard<std::mutex> lock(volumeMutex_);
  const VolumeMode requested = parseVolumeMode(mode);
  auto mixer = std::make_unique<AlsaVolumeControl>(
      value_or(config, "output.alsa.device", std::string("default")),
      mixerControl,
      [events = volumeEvents](int percent) { events->publish(percent); });

  // Software attenuation and fixed output both require the card-side stage at
  // unity. Without this, software mode attenuates twice and fixed does not mean
  // bit-perfect output when an ALSA mixer was previously left below 100%.
  if ((requested == VolumeMode::Software || requested == VolumeMode::Fixed) &&
      mixer->available()) {
    if (!mixer->setVolume(100) || mixer->getVolume() != 100) {
      if (error != nullptr) {
        *error = "ALSA mixer could not be set to 100%";
      }
      return false;
    }
  }

  volumeMode = requested;
  // Only an active hardware backend needs to stay open and report external
  // changes. Software/fixed used the mixer above solely to establish unity.
  volumeControl =
      (requested == VolumeMode::Hardware || requested == VolumeMode::Auto)
          ? std::move(mixer)
          : nullptr;
  // Keep the emitter's software gain consistent with the active backend so a
  // mode switch (or Auto resolving to hardware) can't leave stale attenuation
  // behind: only the software backend applies gain; everything else must stay
  // bit-perfect (unity).
  audioEmitter->setSoftwareVolume(activeBackend() == VolumeBackend::Software
                                      ? percentToGain(softwarePercent)
                                      : 1.0f);
  spdlog::info("AudioPlayer volume configured: mode={}, hardware mixer {}", mode,
               (volumeControl && volumeControl->available()) ? "available"
                                                             : "unavailable");
  return true;
}

AudioPlayer::~AudioPlayer() { stop(); }

void AudioPlayer::append(StreamId id, const std::string &url,
                         const AudioFormat format, size_t startOffsetMs) {
  cleanUpFinishedStreams();
  if (std::any_of(streamNodesList.begin(), streamNodesList.end(),
                  [id](const StreamNodes &s) { return s.id == id; })) {
    spdlog::warn("Stream id={} already queued; appending anyway", id);
  }
  spdlog::debug("Appending stream id={} url={} startOffset={}ms", id, url,
                startOffsetMs);
  StreamNodes newStream(id, url, currentStreamConfig(), format, startOffsetMs);
  streamSwitcher->connectTo(newStream.nodeChain.back());
  audioEmitter->connectTo(streamSwitcher);
  streamNodesList.emplace_back(std::move(newStream));
}

void AudioPlayer::remove(StreamId id) {
  auto stream = std::find_if(streamNodesList.begin(), streamNodesList.end(),
                             [id](const StreamNodes &s) { return s.id == id; });
  if (stream != streamNodesList.end()) {
    streamSwitcher->disconnect(stream->nodeChain.back());
    streamNodesList.erase(stream);
  } else {
    spdlog::warn("Stream id={} not found", id);
  }
}

void AudioPlayer::clearAll() { disconnectAllStreams(); }

void AudioPlayer::stop() {
  audioEmitter->disconnect(streamSwitcher);
  disconnectAllStreams();
}

void AudioPlayer::pause() { audioEmitter->pause(true); }
void AudioPlayer::resume() { audioEmitter->pause(false); }

size_t AudioPlayer::seek(size_t positionMs) {
  return audioEmitter->seek(positionMs);
}

StreamState AudioPlayer::getState() { return audioEmitter->getState(); }

std::unique_ptr<StateMonitor> AudioPlayer::monitor() {
  return std::make_unique<StateMonitor>(audioEmitter.get());
}

VolumeBackend AudioPlayer::activeBackend() const {
  switch (volumeMode) {
  case VolumeMode::Hardware:
    return (volumeControl && volumeControl->available())
               ? VolumeBackend::Hardware
               : VolumeBackend::None;
  case VolumeMode::Software:
    return VolumeBackend::Software;
  case VolumeMode::Fixed:
    return VolumeBackend::None;
  case VolumeMode::Auto:
  default:
    return (volumeControl && volumeControl->available())
               ? VolumeBackend::Hardware
               : VolumeBackend::Software;
  }
}

VolumeState AudioPlayer::getVolume() {
  std::lock_guard<std::mutex> lock(volumeMutex_);
  VolumeState state;
  state.max = 100;
  switch (activeBackend()) {
  case VolumeBackend::Hardware: {
    const int v = volumeControl->getVolume();
    state.supported = v >= 0;
    state.current = v >= 0 ? v : 0;
    state.backend = static_cast<int>(VolumeBackend::Hardware);
    break;
  }
  case VolumeBackend::Software:
    state.supported = true;
    state.current = softwarePercent;
    state.backend = static_cast<int>(VolumeBackend::Software);
    break;
  case VolumeBackend::None:
  default:
    state.supported = false;
    state.current = volumeMode == VolumeMode::Fixed ? 100 : 0;
    state.backend = static_cast<int>(VolumeBackend::None);
    break;
  }
  return state;
}

void AudioPlayer::setVolume(int percent) {
  std::lock_guard<std::mutex> lock(volumeMutex_);
  percent = std::clamp(percent, 0, 100);
  switch (activeBackend()) {
  case VolumeBackend::Hardware:
    volumeControl->setVolume(percent);
    break;
  case VolumeBackend::Software:
    softwarePercent = percent;
    audioEmitter->setSoftwareVolume(percentToGain(percent));
    break;
  case VolumeBackend::None:
  default:
    spdlog::debug("setVolume({}) ignored: no active volume backend "
                  "(mode=fixed or no hardware mixer)",
                  percent);
    break;
  }
}

std::unique_ptr<VolumeMonitor> AudioPlayer::volumeMonitor() {
  // No lock and no backend to ask: the monitor listens to the player, not to
  // whichever mixer happens to be open, so a later mode change reaches it too.
  return std::make_unique<VolumeMonitor>(volumeEvents);
}

bool AudioPlayer::isStreamKey(const std::string &key) {
  return key.starts_with("input.") || key.starts_with("decoder.");
}

bool AudioPlayer::setStreamConfig(const std::string &key,
                                  const std::string &value) {
  if (!isStreamKey(key)) {
    return false;
  }
  std::lock_guard<std::mutex> lock(streamConfigMutex_);
  streamConfig.insert_or_assign(key, value);
  return true;
}

Config AudioPlayer::currentStreamConfig() {
  std::lock_guard<std::mutex> lock(streamConfigMutex_);
  return streamConfig;
}

void AudioPlayer::disconnectAllStreams() {
  for (auto &streamNodes : streamNodesList) {
    streamSwitcher->disconnect(streamNodes.nodeChain.back());
  }
  streamNodesList.clear();
}

void AudioPlayer::cleanUpFinishedStreams() {
  for (auto it = streamNodesList.begin(); it != streamNodesList.end();) {
    if (isInvalidState(it->nodeChain.back()->getState().state)) {
      // Keep switcher state in sync: removing the stream from our bookkeeping
      // alone is not enough, it must be disconnected from the switcher too.
      streamSwitcher->disconnect(it->nodeChain.back());
      it = streamNodesList.erase(it);
    } else {
      ++it;
    }
  }
}
