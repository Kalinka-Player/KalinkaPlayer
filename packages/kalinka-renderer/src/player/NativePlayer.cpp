#include "NativePlayer.h"

#include <spdlog/spdlog.h>

#include <algorithm>
#include <charconv>
#include <chrono>
#include <span>

#include "../config/ConfigContributor.h"
#include "../config/SettingsPersistence.h"
#include "../native_player/AlsaDeviceEnumeration.h"
#include "../native_player/AudioGraphHttpStream.h"
#include "StateTranslator.h"

namespace asio = boost::asio;
namespace pb = kalinka::renderer::v1;

namespace {

int64_t nowUnixMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

AudioFormat formatOf(const pb::Source &source) {
  const std::string &mime = source.mime_type();
  if (mime.find("mpeg") != std::string::npos ||
      mime.find("mp3") != std::string::npos) {
    return AudioFormat::FormatMpeg;
  }
  if (mime.find("flac") != std::string::npos) {
    return AudioFormat::FormatFlac;
  }
  if (source.uri().ends_with(".mp3")) {
    return AudioFormat::FormatMpeg;
  }
  return AudioFormat::FormatFlac;
}

// The noisy virtual PCMs the server's device list also hides. "sysdefault:" is
// the same route as "default:" for a machine with no ~/.asoundrc, and offering
// both only asks the user to pick between two identical entries.
bool noisyPcm(const std::string &name) {
  static const char *prefixes[] = {"front:",   "surround",   "iec958:",
                                   "dmix:",    "dsnoop:",    "hdmi:",
                                   "usbstream:", "sysdefault"};
  return std::any_of(std::begin(prefixes), std::end(prefixes),
                     [&name](const char *p) { return name.starts_with(p); });
}

// What a numeric knob accepts. A range wide enough to be typed rather than
// dragged says so with `slider = false`.
struct Bounds {
  int64_t min = 0;
  int64_t max = 0;
  int64_t step = 0;
  bool slider = false;

  bool declared() const { return max > min; }
};

// Value and default live in the settings map; the graph key says how it applies.
struct Knob {
  const char *path;
  const char *title;
  const char *description;
  pb::ConfigFieldType type;
  const char *unit = "";
  Bounds bounds{};
};

const Knob kOutputKnobs[] = {
    {"output.latency_ms", "Output latency",
     "How much audio is kept ahead of the card, in milliseconds. Raise it if "
     "playback stutters.",
     pb::CONFIG_FIELD_TYPE_INT, "ms", {20, 1000, 10, true}},
    {"output.period_ms", "Period size",
     "How often audio is handed to the card, in milliseconds.",
     pb::CONFIG_FIELD_TYPE_INT, "ms", {5, 200, 5, true}},
    {"output.format_change_delay_ms", "Pause after a format change",
     "How long to wait once a new sample rate is set, in milliseconds, for a "
     "card that needs a moment before it will take audio.",
     pb::CONFIG_FIELD_TYPE_INT, "ms", {0, 2000, 50, true}},
    {"output.reopen_on_format_change", "Reopen on a format change",
     "Close and reopen the card when the next track has a different format. "
     "Some DACs need it.",
     pb::CONFIG_FIELD_TYPE_BOOL},
};

// Bytes, and the useful settings span orders of magnitude: bounded to keep a
// typo from starving or exhausting the machine, but typed rather than dragged.
const Knob kBufferKnobs[] = {
    {"buffers.network_stream", "Network buffer",
     "How much of a streamed track is held in memory, in bytes. Raise it on a "
     "slow or unreliable connection.",
     pb::CONFIG_FIELD_TYPE_INT, "bytes", {64000, 33554432}},
    {"buffers.network_request", "Network request size",
     "How much is fetched from the server at a time, in bytes.",
     pb::CONFIG_FIELD_TYPE_INT, "bytes", {16000, 8388608}},
    {"buffers.flac", "FLAC buffer",
     "Decoded audio held ahead for FLAC playback, in bytes.",
     pb::CONFIG_FIELD_TYPE_INT, "bytes", {64000, 33554432}},
    {"buffers.mpeg", "MP3 buffer",
     "Decoded audio held ahead for MP3 playback, in bytes.",
     pb::CONFIG_FIELD_TYPE_INT, "bytes", {64000, 33554432}},
};

const Knob kNetworkKnobs[] = {
    {"network.stall_timeout_s", "Stall timeout",
     "How long a track may go without anything arriving from the server, in "
     "seconds, before the renderer asks for the rest again. A track that keeps "
     "stalling fails. Raise it on a very slow connection.",
     pb::CONFIG_FIELD_TYPE_INT, "s", {5, 300, 5, true}},
};

// A section of the settings page that holds nothing but knobs.
struct Section {
  const char *path;
  const char *title;
  const char *description;
  std::span<const Knob> knobs;
};

const Section kBuffering{"buffers", "Buffering",
                         "How much of a track the renderer holds in memory "
                         "while it plays.",
                         kBufferKnobs};

const Section kNetwork{"network", "Network",
                       "How long the renderer waits on a server that has gone "
                       "quiet.",
                       kNetworkKnobs};

// Per-stream keys reach the next stream appended; the rest need a new graph.
const std::map<std::string, std::string> &graphKeys() {
  static const std::map<std::string, std::string> keys{
      {"output.device", "output.alsa.device"},
      {"output.latency_ms", "output.alsa.latency_ms"},
      {"output.period_ms", "output.alsa.period_ms"},
      {"output.format_change_delay_ms",
       "fixups.alsa_sleep_after_format_setup_ms"},
      {"output.reopen_on_format_change",
       "fixups.alsa_reopen_device_with_new_format"},
      {"buffers.network_stream", "input.http.buffer_size"},
      {"buffers.network_request", "input.http.chunk_size"},
      {"buffers.flac", "decoder.flac.buffer_size"},
      {"buffers.mpeg", "decoder.mpeg.buffer_size"},
      {"network.stall_timeout_s", "input.http.stall_timeout"},
  };
  return keys;
}

bool readPerStream(const std::string &path) {
  const auto key = graphKeys().find(path);
  return key != graphKeys().end() && AudioPlayer::isStreamKey(key->second);
}

bool holds(std::span<const Knob> knobs, const std::string &path) {
  return std::ranges::any_of(
      knobs, [&path](const Knob &knob) { return knob.path == path; });
}

const Knob *findKnob(const std::string &path) {
  for (std::span<const Knob> knobs : {std::span<const Knob>(kOutputKnobs),
                                      kBuffering.knobs, kNetwork.knobs}) {
    const auto knob = std::ranges::find_if(
        knobs, [&path](const Knob &knob) { return knob.path == path; });
    if (knob != knobs.end()) {
      return &*knob;
    }
  }
  return nullptr;
}

// Everything the settings page is told about a knob but its values.
void describe(pb::ConfigField &field, const Knob &knob) {
  field.set_path(knob.path);
  field.set_title(knob.title);
  field.set_description(knob.description);
  field.set_type(knob.type);
  field.set_apply(readPerStream(knob.path) ? pb::APPLY_COST_INSTANT
                                           : pb::APPLY_COST_INTERRUPTS_PLAYBACK);
  // Reached for when the card or the link misbehaves, which is not what a
  // settings page is for.
  field.set_importance(pb::CONFIG_IMPORTANCE_EXPERT);
  field.set_unit(knob.unit);
  if (!knob.bounds.declared()) {
    return;
  }
  pb::ConfigRange *range = field.mutable_range();
  range->set_min(knob.bounds.min);
  range->set_max(knob.bounds.max);
  range->set_step(knob.bounds.step);
  field.set_widget(knob.bounds.slider ? pb::CONFIG_WIDGET_SLIDER
                                      : pb::CONFIG_WIDGET_NUMBER);
}

void declare(pb::ConfigSection &out, const Knob &knob,
             const std::map<std::string, std::string> &settings,
             const std::map<std::string, std::string> &defaults) {
  pb::ConfigField *field = out.add_fields();
  describe(*field, knob);
  field->set_value(settings.at(knob.path));
  field->set_default_value(defaults.at(knob.path));
}

// A hand-edited stall timeout of 0 would switch stall detection off.
bool knobTakes(const std::string &path, const std::string &value,
               std::string &error) {
  const Knob *knob = findKnob(path);
  if (knob == nullptr) {
    return true;
  }
  pb::ConfigField field;
  describe(field, *knob);
  return fieldAccepts(field, value, error);
}

}  // namespace

const std::map<std::string, std::string> &NativePlayer::defaultSettings() {
  static const std::map<std::string, std::string> defaults{
      {"output.driver", "alsa"},
      {"output.device", "default"},
      {"output.volume_mode", "auto"},
      {"output.session_start_volume_ceiling_percent", "30"},
      // What the server shipped while it did the playing, rather than the
      // sink's own 100/25.
      {"output.latency_ms", "160"},
      {"output.period_ms", "40"},
      {"output.format_change_delay_ms", "0"},
      {"output.reopen_on_format_change", "false"},
      {"buffers.network_stream", "768000"},
      {"buffers.network_request", "384000"},
      {"buffers.flac", "1536000"},
      {"buffers.mpeg", "768000"},
      {"network.stall_timeout_s",
       std::to_string(AudioGraphHttpStream::DEFAULT_STALL_TIMEOUT.count())},
  };
  return defaults;
}

class NativePlayer::KnobSection : public ConfigContributor {
public:
  KnobSection(std::shared_ptr<NativePlayer> player, const Section &section)
      : player_(std::move(player)), section_(section) {}

  void fillConfig(pb::ConfigSection &out) const override {
    out.set_path(section_.path);
    out.set_title(section_.title);
    out.set_description(section_.description);
    for (const Knob &knob : section_.knobs) {
      declare(out, knob, player_->settings_, defaultSettings());
    }
  }

  bool applyConfig(const std::string &path, const std::string &value,
                   std::string &error) override {
    if (!holds(section_.knobs, path)) {
      error = "unknown setting";
      return false;
    }
    return player_->applySetting(path, value, error);
  }

private:
  const std::shared_ptr<NativePlayer> player_;
  const Section section_;
};

std::shared_ptr<ConfigContributor> NativePlayer::bufferSettings() {
  return std::make_shared<KnobSection>(shared_from_this(), kBuffering);
}

std::shared_ptr<ConfigContributor> NativePlayer::networkSettings() {
  return std::make_shared<KnobSection>(shared_from_this(), kNetwork);
}

void NativePlayer::persistOverrides() const {
  updateSettingsOverrides(settings_, defaultSettings());
}

Config NativePlayer::graphConfig() const {
  Config config;
  for (const auto &[path, key] : graphKeys()) {
    config[key] = settings_.at(path);
  }
  return config;
}

NativePlayer::NativePlayer(asio::io_context &ioc) : ioc_(ioc) {
  for (const auto &[key, value] : loadSettingsOverrides()) {
    const auto setting = settings_.find(key);
    if (setting == settings_.end()) {
      continue;
    }
    std::string error;
    if (!knobTakes(key, value, error)) {
      spdlog::warn("Ignoring config override {} = '{}': {}; using '{}'", key,
                   value, error, setting->second);
      continue;
    }
    setting->second = value;
    spdlog::info("Config override {} = '{}'", key, value);
  }
  ensurePlayer();
}

NativePlayer::~NativePlayer() { stopPumps(); }

void NativePlayer::setStateSink(StateSink sink) { sink_ = std::move(sink); }

bool NativePlayer::ensurePlayer() {
  if (player_) {
    return true;
  }
  try {
    player_ = std::make_unique<AudioPlayer>(graphConfig());
    std::string volumeError;
    if (!player_->configureVolume(effectiveVolumeMode(), "", &volumeError)) {
      spdlog::error("Cannot configure volume mode '{}': {}",
                    effectiveVolumeMode(), volumeError);
      player_.reset();
      return false;
    }
    startPumps();
    return true;
  } catch (const std::exception &e) {
    spdlog::error("Audio graph unavailable on device '{}': {}",
                  settings_.at("output.device"), e.what());
    player_.reset();
    return false;
  }
}

void NativePlayer::rebuildPlayer() {
  stopPumps();
  sources_.clear();
  currentId_.reset();
  player_.reset();
  ensurePlayer();
  // Whatever was playing is gone, as APPLY_COST_INTERRUPTS_PLAYBACK declared.
  pb::Envelope env;
  pb::PlaybackStateChanged *changed = env.mutable_playback_state_changed();
  changed->set_state(pb::PLAYBACK_STATE_STOPPED);
  changed->set_position_valid(false);
  changed->set_at_unix_ms(nowUnixMs());
  emit(env);
}

void NativePlayer::startPumps() {
  stateMonitor_ = std::shared_ptr<StateMonitor>(player_->monitor().release());
  volumeMonitor_ =
      std::shared_ptr<VolumeMonitor>(player_->volumeMonitor().release());

  statePump_ = std::thread([this, monitor = stateMonitor_] {
    while (monitor->isRunning()) {
      StreamState state = monitor->waitState();
      if (!monitor->isRunning()) {
        break;
      }
      asio::post(ioc_, [this, state] { onStreamState(state); });
    }
  });
  volumePump_ = std::thread([this, monitor = volumeMonitor_] {
    while (monitor->isRunning()) {
      VolumeState volume = monitor->wait();
      if (!monitor->isRunning()) {
        break;
      }
      asio::post(ioc_, [this, volume] { emitVolume(volume, true); });
    }
  });
}

void NativePlayer::stopPumps() {
  if (stateMonitor_) {
    stateMonitor_->stop();
  }
  if (volumeMonitor_) {
    volumeMonitor_->stop();
  }
  if (statePump_.joinable()) {
    statePump_.join();
  }
  if (volumePump_.joinable()) {
    volumePump_.join();
  }
  stateMonitor_.reset();
  volumeMonitor_.reset();
}

void NativePlayer::reportUnavailable(const char *command) {
  spdlog::warn("Command {}: audio graph unavailable", command);
  pb::Envelope env;
  pb::PlaybackStateChanged *changed = env.mutable_playback_state_changed();
  changed->set_state(pb::PLAYBACK_STATE_ERROR);
  changed->set_position_valid(false);
  changed->set_at_unix_ms(nowUnixMs());
  changed->mutable_error()->set_source(pb::ERROR_SOURCE_AUDIO_OUTPUT);
  changed->mutable_error()->set_message(
      "audio output is unavailable; check the configured device");
  emit(env);
}

StreamId NativePlayer::appendSource(const pb::Source &source) {
  const StreamId id = nextStreamId_++;
  sources_.push_back(TrackedSource{source, id});
  player_->append(id, source.uri(), formatOf(source),
                  source.start_offset_ms());
  return id;
}

void NativePlayer::setSource(const pb::Source &source) {
  if (!ensurePlayer()) {
    return reportUnavailable("set_source");
  }
  // Append before removing, or the graph reports the queue finished instead
  // of switching.
  std::vector<StreamId> displaced;
  for (const TrackedSource &tracked : sources_) {
    displaced.push_back(tracked.streamId);
  }
  appendSource(source);
  for (StreamId id : displaced) {
    player_->remove(id);
  }
  // Displaced entries stay until the graph confirms the switch: states still
  // in flight for them must keep resolving to a token.
}

void NativePlayer::enqueueSource(const pb::Source &source) {
  if (!ensurePlayer()) {
    return reportUnavailable("enqueue_source");
  }
  appendSource(source);
}

void NativePlayer::removeSource(const std::string &sourceToken) {
  if (!player_) {
    return;  // nothing to remove from
  }
  auto it = std::find_if(sources_.begin(), sources_.end(),
                         [&sourceToken](const TrackedSource &tracked) {
                           return tracked.source.source_token() == sourceToken;
                         });
  if (it == sources_.end()) {
    spdlog::debug("Ignoring removal of unknown source token {}", sourceToken);
    return;
  }
  // Removing the current source ends it as if it had finished.
  player_->remove(it->streamId);
  if (currentId_ != it->streamId) {
    sources_.erase(it);
  }
}

void NativePlayer::clearQueue() {
  if (!player_) {
    return;
  }
  player_->clearAll();
  sources_.clear();
  currentId_.reset();
}

void NativePlayer::pause() {
  if (!ensurePlayer()) {
    return reportUnavailable("pause");
  }
  player_->pause();
}

void NativePlayer::resume() {
  if (!ensurePlayer()) {
    return reportUnavailable("resume");
  }
  player_->resume();
}

void NativePlayer::stop() {
  if (!player_) {
    return;
  }
  player_->stop();
  sources_.clear();
  currentId_.reset();
}

void NativePlayer::setVolume(uint32_t percent) {
  if (!ensurePlayer()) {
    return reportUnavailable("set_volume");
  }
  player_->setVolume(static_cast<int>(std::min(percent, 100u)));
  emitVolume(player_->getVolume(), false);
}

void NativePlayer::seek(uint64_t positionMs) {
  if (!player_) {
    return;  // ignored when nothing is playing, per the contract
  }
  player_->seek(positionMs);
}

const pb::Source *
NativePlayer::sourceFor(const std::optional<StreamId> &streamId) const {
  if (!streamId) {
    return nullptr;
  }
  auto it = std::find_if(sources_.begin(), sources_.end(),
                         [&streamId](const TrackedSource &tracked) {
                           return tracked.streamId == *streamId;
                         });
  return it == sources_.end() ? nullptr : &it->source;
}

std::optional<std::string>
NativePlayer::tokenFor(const std::optional<StreamId> &streamId) const {
  const pb::Source *source = sourceFor(streamId);
  if (source == nullptr) {
    return std::nullopt;
  }
  return source->source_token();
}

std::optional<std::string> NativePlayer::currentToken() const {
  return tokenFor(currentId_);
}

void NativePlayer::forgetSourcesBefore(StreamId streamId) {
  std::erase_if(sources_, [streamId](const TrackedSource &tracked) {
    return tracked.streamId < streamId;
  });
}

void NativePlayer::onStreamState(const StreamState &state) {
  if (state.state == AudioGraphNodeState::SOURCE_CHANGED) {
    // A handover to something untracked is one already reported, or one
    // whose source was dropped.
    if (!state.streamId || sourceFor(state.streamId) == nullptr) {
      spdlog::debug("Ignoring a source change to an untracked stream");
      return;
    }
    if (currentId_ == state.streamId) {
      return;
    }
    std::optional<std::string> previous = currentToken();
    currentId_ = state.streamId;
    forgetSourcesBefore(*state.streamId);
    spdlog::info("Now playing source '{}' (stream {})", *currentToken(),
                 *currentId_);
    pb::Envelope env;
    pb::SourceChanged *changed = env.mutable_source_changed();
    changed->set_source_token(*currentToken());
    if (previous) {
      changed->set_previous_source_token(*previous);
    }
    changed->set_at_unix_ms(nowUnixMs());
    emit(env);
    return;
  }

  pb::Envelope env;
  state_translator::fillPlaybackStateChanged(
      state, tokenFor(state.streamId), nowUnixMs(),
      *env.mutable_playback_state_changed());
  emit(env);
}

void NativePlayer::emitVolume(const VolumeState &volume, bool external) {
  pb::Envelope env;
  pb::VolumeChanged *changed = env.mutable_volume_changed();
  state_translator::fillVolume(volume, *changed->mutable_volume());
  changed->set_external(external);
  emit(env);
}

void NativePlayer::emit(pb::Envelope &env) {
  if (sink_) {
    sink_(env);
  }
}

void NativePlayer::fillConfig(pb::ConfigSection &out) const {
  out.set_path("output");
  out.set_title("Output");
  out.set_description("Where this renderer sends audio and how it sets the "
                      "level.");

  pb::ConfigField *driver = out.add_fields();
  driver->set_path("output.driver");
  driver->set_title("Driver");
  driver->set_description("Audio backend used to reach the sound card.");
  driver->set_type(pb::CONFIG_FIELD_TYPE_ENUM);
  driver->set_value(settings_.at("output.driver"));
  driver->set_default_value("alsa");
  driver->set_apply(pb::APPLY_COST_RESTART_REQUIRED);
  // One backend to choose from, so far.
  driver->set_importance(pb::CONFIG_IMPORTANCE_EXPERT);
  pb::ConfigOption *alsa = driver->add_options();
  alsa->set_value("alsa");
  alsa->set_label("ALSA");

  pb::ConfigField *device = out.add_fields();
  device->set_path("output.device");
  device->set_title("Output device");
  device->set_description(
      "The sound card music plays through. Changing it stops what is "
      "playing.");
  device->set_type(pb::CONFIG_FIELD_TYPE_ENUM);
  device->set_value(settings_.at("output.device"));
  device->set_default_value("default");
  device->set_apply(pb::APPLY_COST_INTERRUPTS_PLAYBACK);
  device->set_importance(pb::CONFIG_IMPORTANCE_SIMPLE);
  bool currentOffered = false;
  for (const AlsaPcmDevice &pcm : listAlsaPcmDevices()) {
    if (!pcm.ioid.empty() && pcm.ioid != "Output") {
      continue;
    }
    if (noisyPcm(pcm.name)) {
      continue;
    }
    pb::ConfigOption *option = device->add_options();
    option->set_value(pcm.name);
    option->set_label(pcm.label.empty() ? pcm.name : pcm.label);
    option->set_description(pcm.description);
    currentOffered = currentOffered || pcm.name == device->value();
  }
  if (!currentOffered) {
    // The configured device may be unplugged right now; it must stay
    // selectable or the page could not even show what is set.
    const AlsaPcmDevice missing = describeAlsaPcm(device->value(), "");
    pb::ConfigOption *option = device->add_options();
    option->set_value(device->value());
    if (device->value() == "default") {
      // ALSA does not always hint "default", but it always resolves it.
      option->set_label(missing.label);
      option->set_description(missing.description);
    } else {
      option->set_label(missing.label + " (not detected)");
      option->set_description(
          "Configured, but ALSA is not reporting it right now — the card may "
          "be unplugged or renamed.");
    }
  }

  pb::ConfigField *mode = out.add_fields();
  mode->set_path("output.volume_mode");
  mode->set_title("Volume control");
  mode->set_description(
      "How the level is changed: in the card, in software, or not at all.");
  mode->set_type(pb::CONFIG_FIELD_TYPE_ENUM);
  mode->set_value(settings_.at("output.volume_mode"));
  mode->set_default_value("auto");
  mode->set_apply(pb::APPLY_COST_INTERRUPTS_PLAYBACK);
  mode->set_importance(pb::CONFIG_IMPORTANCE_SIMPLE);
  struct VolumeChoice {
    const char *value;
    const char *label;
    const char *description;
  };
  static const VolumeChoice choices[] = {
      {"auto", "Automatic",
       "Uses the card's mixer when it has one, otherwise scales in "
       "software."},
      {"hardware", "Card mixer",
       "Sets the level in the card, leaving the samples unchanged. Needs a "
       "card that has a mixer."},
      {"software", "Software",
       "Sets the card mixer to 100%, then scales samples before sending them. "
       "Works with any card; quiet levels lose some resolution."},
      {"fixed", "Fixed output",
       "Sets the card mixer and software gain to 100%, ignores volume commands, "
       "and bypasses the session-start ceiling. Use only when an amplifier or "
       "receiver controls the listening level."},
  };
  for (const VolumeChoice &choice : choices) {
    pb::ConfigOption *option = mode->add_options();
    option->set_value(choice.value);
    option->set_label(choice.label);
    option->set_description(choice.description);
  }

  pb::ConfigField *startCeiling = out.add_fields();
  startCeiling->set_path("output.session_start_volume_ceiling_percent");
  startCeiling->set_title("Session start volume ceiling");
  startCeiling->set_description(
      "Maximum volume when a renderer-controlled session starts. A quieter "
      "existing level is never raised. Fixed output bypasses this ceiling.");
  startCeiling->set_type(pb::CONFIG_FIELD_TYPE_INT);
  startCeiling->set_value(
      settings_.at("output.session_start_volume_ceiling_percent"));
  startCeiling->set_default_value("30");
  startCeiling->set_apply(pb::APPLY_COST_INSTANT);
  startCeiling->set_importance(pb::CONFIG_IMPORTANCE_SIMPLE);
  startCeiling->set_unit("%");
  startCeiling->mutable_range()->set_min(0);
  startCeiling->mutable_range()->set_max(100);
  startCeiling->mutable_range()->set_step(1);
  startCeiling->set_widget(pb::CONFIG_WIDGET_SLIDER);

  for (const Knob &knob : kOutputKnobs) {
    declare(out, knob, settings_, defaultSettings());
  }
}

bool NativePlayer::applyConfig(const std::string &path,
                               const std::string &value, std::string &error) {
  return applySetting(path, value, error);
}

bool NativePlayer::applySetting(const std::string &path,
                                const std::string &value, std::string &error) {
  auto setting = settings_.find(path);
  if (setting == settings_.end()) {
    error = "unknown setting";
    return false;
  }
  // Sizes and durations, which the graph reads as unsigned: a negative one
  // throws where it is read, which is halfway into the next track. The service
  // refuses what a knob's declared range excludes before it ever reaches here.
  if (value.starts_with("-") && findKnob(path) != nullptr) {
    error = "must not be negative";
    return false;
  }
  if (setting->second == value) {
    return true;  // nothing to do, and nothing to interrupt
  }

  if (path == "output.volume_mode" && player_ && !sessionForcedFixed_) {
    // Changing which stage owns volume can otherwise produce an abrupt level
    // jump in audio already on air. The field declares this interruption.
    stop();
    std::string configureError;
    if (!player_->configureVolume(value, "", &configureError)) {
      error = configureError;
      return false;
    }
  }

  setting->second = value;
  persistOverrides();
  spdlog::info("Config {} = '{}'", path, value);
  if (readPerStream(path)) {
    if (player_) {
      player_->setStreamConfig(graphKeys().at(path), value);
    }
  } else if (graphKeys().contains(path)) {
    rebuildPlayer();
  } else if (path == "output.volume_mode" && player_) {
    // A downstream session override outranks the configured value until it ends.
    if (!sessionForcedFixed_) {
      emitVolume(player_->getVolume(), false);
    }
  }
  return true;
}

const std::string &NativePlayer::effectiveVolumeMode() const {
  static const std::string fixed = "fixed";
  return sessionForcedFixed_ ? fixed : settings_.at("output.volume_mode");
}

bool NativePlayer::beginSessionVolume(const SessionVolumePolicy &policy,
                                      std::string &error) {
  error.clear();
  if (!ensurePlayer()) {
    error = "audio output is unavailable; session-start volume ceiling "
            "cannot be enforced";
    return false;
  }

  const std::string &configuredMode = settings_.at("output.volume_mode");
  if (policy.forceFixedOutput) {
    const VolumeState before = player_->getVolume();
    sessionRestoreMode_ = configuredMode;
    sessionRestoreVolume_ = before.supported
                                ? std::optional<int>(before.current)
                                : std::nullopt;
    if (!player_->configureVolume("fixed", "", &error)) {
      sessionRestoreMode_.reset();
      sessionRestoreVolume_.reset();
      return false;
    }
    sessionForcedFixed_ = true;
    spdlog::info("Session fixed output at unity (configured '{}' is kept)",
                 configuredMode);
    return true;
  }

  if (!player_->configureVolume(configuredMode, "", &error)) {
    return false;
  }

  // Persistent fixed mode is the explicit manual-amplifier configuration.
  if (configuredMode == "fixed") {
    return true;
  }

  const std::string &configuredCeiling =
      settings_.at("output.session_start_volume_ceiling_percent");
  uint32_t ceiling = 0;
  const char *end = configuredCeiling.data() + configuredCeiling.size();
  const auto [stop, parseError] =
      std::from_chars(configuredCeiling.data(), end, ceiling);
  if (parseError != std::errc{} || stop != end || ceiling > 100) {
    error = "session-start volume ceiling setting is invalid";
    return false;
  }
  VolumeState state = player_->getVolume();
  if (!state.supported) {
    error = "volume control is unavailable; session-start volume ceiling "
            "cannot be enforced";
    return false;
  }
  if (state.current > static_cast<int>(ceiling)) {
    player_->setVolume(static_cast<int>(ceiling));
    state = player_->getVolume();
  }
  // A coarse mixer may land below the requested level; only louder is unsafe.
  if (!state.supported || state.current > static_cast<int>(ceiling)) {
    error = "renderer failed to apply its session-start volume ceiling";
    return false;
  }
  spdlog::info("Session starts at {}% volume (ceiling {}%)", state.current,
               ceiling);
  return true;
}

void NativePlayer::endSessionVolume() {
  if (!sessionForcedFixed_) {
    return;
  }
  sessionForcedFixed_ = false;
  const std::string &configuredMode = settings_.at("output.volume_mode");
  spdlog::info("Session fixed output ended; back to '{}'", configuredMode);
  if (player_) {
    std::string error;
    if (!player_->configureVolume(configuredMode, "", &error)) {
      spdlog::error("Cannot restore configured volume mode '{}': {}",
                    configuredMode, error);
    } else if (sessionRestoreMode_ == configuredMode &&
               sessionRestoreVolume_ && configuredMode != "fixed") {
      player_->setVolume(*sessionRestoreVolume_);
    }
  }
  sessionRestoreMode_.reset();
  sessionRestoreVolume_.reset();
}

void NativePlayer::fillSnapshot(pb::StateSnapshot &out) const {
  out.set_captured_at_unix_ms(nowUnixMs());
  out.set_selected_device_id(settings_.at("output.device"));

  if (!player_) {
    out.set_playback_state(pb::PLAYBACK_STATE_ERROR);
    out.set_position_valid(false);
    out.mutable_volume()->set_supported(false);
    out.mutable_volume()->set_backend(pb::VOLUME_BACKEND_NONE);
    out.mutable_error()->set_source(pb::ERROR_SOURCE_AUDIO_OUTPUT);
    out.mutable_error()->set_message(
        "audio output is unavailable; check the configured device");
    return;
  }

  const StreamState state = player_->getState();
  const std::optional<StreamId> onAir =
      state.streamId ? state.streamId : currentId_;
  pb::PlaybackStateChanged translated;
  state_translator::fillPlaybackStateChanged(state, tokenFor(onAir),
                                             nowUnixMs(), translated);
  out.set_playback_state(translated.state());
  out.set_position_ms(translated.position_ms());
  out.set_position_valid(translated.position_valid());
  if (translated.has_error()) {
    *out.mutable_error() = translated.error();
  }
  if (const pb::Source *source = sourceFor(onAir)) {
    *out.mutable_current_source() = *source;
  }
  if (translated.has_format()) {
    *out.mutable_format() = translated.format();
  }
  if (translated.has_device_info()) {
    *out.mutable_device_info() = translated.device_info();
  }
  if (translated.has_duration_ms()) {
    out.set_duration_ms(translated.duration_ms());
  }
  state_translator::fillVolume(player_->getVolume(), *out.mutable_volume());
  for (const TrackedSource &tracked : sources_) {
    if (!onAir || tracked.streamId > *onAir) {
      out.add_queued_source_tokens(tracked.source.source_token());
    }
  }
}
