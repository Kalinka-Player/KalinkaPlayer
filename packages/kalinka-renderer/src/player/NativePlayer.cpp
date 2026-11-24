#include "NativePlayer.h"

#include <spdlog/spdlog.h>

#include <algorithm>
#include <charconv>
#include <cctype>
#include <chrono>
#include <condition_variable>
#include <mutex>
#include <span>
#include <sstream>
#include <string_view>
#include <unordered_map>
#include <utility>

#include "../config/ConfigContributor.h"
#include "../config/DspProfileCache.h"
#include "../config/SettingsPersistence.h"
#include "../native_player/AlsaDeviceEnumeration.h"
#include "../native_player/AudioGraphHttpStream.h"
#include "../native_player/DsdFormat.h"
#include "../native_player/dsp/fir/FirImpulse.h"
#include "../native_player/dsp/fir/LiveFir.h"
#include "StateTranslator.h"

namespace asio = boost::asio;
namespace renderer_pb = kalinka::renderer::v1;

namespace {

// A run of one kind is reported at most twice a second, as the protocol says.
constexpr std::chrono::milliseconds kEventInterval{500};

int64_t nowUnixMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

AudioFormat formatOf(const renderer_pb::Source &source) {
  auto lower = [](std::string value) {
    std::transform(value.begin(), value.end(), value.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    return value;
  };
  const auto mimeValue = lower(source.mime_type());
  std::string_view mime(mimeValue);
  mime = mime.substr(0, mime.find(';'));
  const auto first = mime.find_first_not_of(" \t\r\n");
  mime = first == std::string_view::npos
             ? std::string_view{}
             : mime.substr(first, mime.find_last_not_of(" \t\r\n") - first + 1);
  static const std::unordered_map<std::string_view, AudioFormat> declared = {
      {"application/vnd.kalinka.pcm", AudioFormat::FormatLivePcm},
      {"application/dash+xml", AudioFormat::FormatDash},
      {"audio/mp4", AudioFormat::FormatMp4},
      {"audio/m4a", AudioFormat::FormatMp4},
      {"audio/x-m4a", AudioFormat::FormatMp4},
      {"mp4", AudioFormat::FormatMp4},
      {"m4a", AudioFormat::FormatMp4},
      {"audio/aac", AudioFormat::FormatAdts},
      {"audio/x-aac", AudioFormat::FormatAdts},
      {"audio/aacp", AudioFormat::FormatAdts},
      {"aac", AudioFormat::FormatAdts},
      {"audio/dsf", AudioFormat::FormatDsd},
      {"audio/x-dsf", AudioFormat::FormatDsd},
      {"audio/dff", AudioFormat::FormatDsd},
      {"audio/x-dff", AudioFormat::FormatDsd},
      {"audio/dsd", AudioFormat::FormatDsd},
      {"audio/mpeg", AudioFormat::FormatMpeg},
      {"audio/mp3", AudioFormat::FormatMpeg},
      {"audio/x-mp3", AudioFormat::FormatMpeg},
      {"audio/x-mpeg", AudioFormat::FormatMpeg},
      {"audio/mpeg3", AudioFormat::FormatMpeg},
      {"audio/x-mpeg-3", AudioFormat::FormatMpeg},
      {"mpeg", AudioFormat::FormatMpeg},
      {"mp3", AudioFormat::FormatMpeg},
      {"audio/flac", AudioFormat::FormatFlac},
      {"audio/x-flac", AudioFormat::FormatFlac},
      {"application/x-flac", AudioFormat::FormatFlac},
      {"flac", AudioFormat::FormatFlac},
      {"audio/wav", AudioFormat::FormatWav},
      {"audio/x-wav", AudioFormat::FormatWav},
      {"audio/wave", AudioFormat::FormatWav},
      {"audio/vnd.wave", AudioFormat::FormatWav},
      {"wav", AudioFormat::FormatWav},
      {"audio/ogg", AudioFormat::FormatVorbis},
      {"audio/x-ogg", AudioFormat::FormatVorbis},
      {"application/ogg", AudioFormat::FormatVorbis},
      {"application/x-ogg", AudioFormat::FormatVorbis},
      {"audio/vorbis", AudioFormat::FormatVorbis},
      {"audio/x-vorbis", AudioFormat::FormatVorbis},
      {"audio/x-vorbis+ogg", AudioFormat::FormatVorbis},
      {"ogg", AudioFormat::FormatVorbis},
      {"vorbis", AudioFormat::FormatVorbis},
  };
  if (const auto found = declared.find(mime); found != declared.end()) {
    return found->second;
  }
  // A declared type outranks the URL suffix, even one no decoder handles.
  if (!mime.empty() && mime != "application/octet-stream") {
    return AudioFormat::FormatUnsupported;
  }
  const auto path = lower(source.uri().substr(0, source.uri().find_first_of("?#")));
  if (path.ends_with(".mpd")) {
    return AudioFormat::FormatDash;
  }
  if (path.ends_with(".m4a") || path.ends_with(".mp4")) {
    return AudioFormat::FormatMp4;
  }
  if (path.ends_with(".aac")) {
    return AudioFormat::FormatAdts;
  }
  if (path.ends_with(".dsf") || path.ends_with(".dff"))
    return AudioFormat::FormatDsd;
  if (path.ends_with(".flac")) {
    return AudioFormat::FormatFlac;
  }
  if (path.ends_with(".mp3")) {
    return AudioFormat::FormatMpeg;
  }
  if (path.ends_with(".wav") || path.ends_with(".wave")) {
    return AudioFormat::FormatWav;
  }
  if (path.ends_with(".ogg") || path.ends_with(".oga")) {
    return AudioFormat::FormatVorbis;
  }
  return AudioFormat::FormatUnsupported;
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

// One option of an enum setting; the page lists them in declaration order.
struct Choice {
  const char *value;
  const char *label;
  const char *description;
};

// Value and default live in the settings map; the graph key says how it applies.
struct Knob {
  const char *path;
  const char *title;
  const char *description;
  renderer_pb::ConfigFieldType type;
  const char *unit = "";
  Bounds bounds{};
  const char *helpDetails = "";
  std::span<const Choice> choices = {};
  // Most are for a misbehaving card or link, not the settings page proper.
  renderer_pb::ConfigImportance importance =
      renderer_pb::CONFIG_IMPORTANCE_EXPERT;
  // Opens the help, where knobs share it.
  const char *helpLead = "";
};

constexpr const char *kConvertedDsdHelp =
    "Used when DSD is converted to PCM: with Convert to PCM, and with "
    "Automatic for tracks that do not play as native DSD.\n\n";

constexpr const char *kFormatChangeHelp =
    "Some DACs need extra time or must be reopened after a format change. Adjust "
    "these settings if tracks fail to start correctly.";

const char *const kFirProfile = "output.rew_fir_profile";
const char *const kFirOff = "off";

std::vector<std::string> firProfiles() {
  const auto dir = dsp_profiles::directory() / "fir";
  std::vector<std::string> names;
  std::error_code error;
  for (std::filesystem::directory_iterator it(dir, error), end;
       !error && it != end; it.increment(error)) {
    const auto name = it->path().filename();
    auto extension = name.extension().string();
    std::ranges::transform(extension, extension.begin(),
                           [](unsigned char c) { return std::tolower(c); });
    if (extension == ".wav" &&
        name.string().find_first_of("\r\n") == std::string::npos &&
        std::filesystem::is_regular_file(it->symlink_status(error)))
      names.push_back(name.string());
  }
  std::ranges::sort(names);
  return names;
}

std::shared_ptr<FirFilterBank> loadFirProfile(const std::string &name) {
  if (name == kFirOff)
    return {};
  auto bank = std::make_shared<FirFilterBank>();
  if (dsp_profiles::validId(name)) {
    const auto profile = dsp_profiles::loadFir(name);
    bank->name = profile.name();
    for (const auto &wav : profile.impulse_wav())
      bank->add(FirImpulse::decode(wav));
  } else {
    if (!std::ranges::binary_search(firProfiles(), name))
      throw std::runtime_error("Unknown filter impulse WAV");
    bank->name = name;
    bank->add(FirImpulse::load(dsp_profiles::directory() / "fir" / name));
  }
  return bank;
}

std::vector<unsigned>
installedFirRates(const std::shared_ptr<FirFilterBank> &selected) {
  std::vector<unsigned> rates;
  if (selected)
    rates = selected->suppliedRates();
  for (const auto &file : firProfiles()) {
    try {
      const auto rate =
          FirImpulse::sampleRate(dsp_profiles::directory() / "fir" / file);
      if (std::ranges::find(rates, rate) == rates.end())
        rates.push_back(rate);
    } catch (const std::exception &e) {
      spdlog::warn("Ignoring FIR header {}: {}", file, e.what());
    }
  }
  return rates;
}

const Knob kOutputKnobs[] = {
    {"output.latency_ms",
     "Output latency",
     "Audio buffered ahead of playback. Increase if playback stutters.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "ms",
     {20, 1000, 10, true}},
    {"output.period_ms",
     "Period size",
     "How often audio is sent to the sound card.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "ms",
     {5, 200, 5, true}},
    {"output.format_change_delay_ms",
     "Pause after a format change",
     "Wait after the audio format changes. Increase if track beginnings are "
     "cut "
     "off.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "ms",
     {0, 2000, 50, true},
     kFormatChangeHelp},
    {"output.reopen_on_format_change",
     "Reopen on a format change",
     "Close and reopen the sound card when the audio format changes. Some DACs "
     "need this.",
     renderer_pb::CONFIG_FIELD_TYPE_BOOL,
     "",
     {},
     kFormatChangeHelp},
};

// Bytes, and the useful settings span orders of magnitude: bounded to keep a
// typo from starving or exhausting the machine, but typed rather than dragged.
const Knob kBufferKnobs[] = {
    {"buffers.network_stream",
     "Network buffer",
     "Memory used to buffer streamed audio.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "bytes",
     {64000, 33554432},
     "A larger buffer can help with an unreliable connection, but uses more "
     "memory."},
    {"buffers.network_request",
     "Network request size",
     "How much is downloaded from the server at a time.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "bytes",
     {16000, 8388608}},
    {"buffers.network_dsd",
     "DSD network buffer",
     "Memory used to buffer streamed DSD. Each download asks for half of it.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "bytes",
     {64000, 33554432},
     "DSD carries several times the data of PCM, DSD512 5.6 MB a second, so it "
     "has a buffer of its own: a larger one asks the server less often."},
    {"buffers.flac",
     "FLAC buffer",
     "Memory for audio kept ready when playing FLAC.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "bytes",
     {64000, 33554432}},
    {"buffers.mpeg",
     "MP3 buffer",
     "Memory for audio kept ready when playing MP3.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "bytes",
     {64000, 33554432}},
    {"buffers.vorbis",
     "Ogg Vorbis buffer",
     "Memory for audio kept ready when playing Ogg Vorbis.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "bytes",
     {64000, 33554432}},
    {"buffers.wav",
     "WAV buffer",
     "Memory for audio kept ready when playing WAV.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "bytes",
     {64000, 33554432}},
    {"buffers.dsd",
     "DSD buffer",
     "Memory for audio kept ready when playing DSD.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "bytes",
     {64000, 33554432}},
    {"buffers.aac",
     "AAC and MP4 buffer",
     "Memory for audio kept ready when playing AAC and MP4.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "bytes",
     {64000, 33554432}},
};

const Knob kNetworkKnobs[] = {
    {"network.stall_timeout_s",
     "Stall timeout",
     "Seconds without data before a download is retried. Increase on a very "
     "slow "
     "connection.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "s",
     {5, 300, 5, true}},
    {"network.max_redirects",
     "Maximum redirects",
     "Maximum redirects when loading a track. Set to 0 to disable redirects.",
     renderer_pb::CONFIG_FIELD_TYPE_INT,
     "",
     {0, 10, 1}},
};

const Choice kDsdPcmRates[] = {
    {"auto", "Automatic",
     "176.4 or 192 kHz when the output supports it, otherwise the highest rate "
     "it supports."},
    {"1x", "44.1 or 48 kHz", "For outputs that support nothing higher."},
    {"2x", "88.2 or 96 kHz", ""},
    {"4x", "176.4 or 192 kHz", ""},
    {"8x", "352.8 or 384 kHz", ""},
};

const Choice kDsdChoices[] = {
    {"disabled", "Disabled", "DSD tracks do not play."},
    {"auto", "Automatic",
     "Send native DSD when the DAC supports the track’s rate and the volume is "
     "Fixed output. Otherwise convert to PCM. Never DoP."},
    {"native", "Native DSD",
     "Send DSD directly to a compatible DAC."},
    {"dop", "DoP",
     "Send DSD wrapped in PCM. Choose only if the DAC supports DoP, as other DACs "
     "play it as noise."},
    {"pcm", "Convert to PCM",
     "Convert DSD to PCM in the renderer. Plays on any output that supports a "
     "rate it converts to, with any volume control."},
};

const Choice kDsdFilters[] = {
    {"auto", "Automatic", "30 kHz for DSD64, 50 kHz for DSD128 and higher."},
    {"30k", "30 kHz", "Flat to 20 kHz. Removes the most ultrasonic noise."},
    {"50k", "50 kHz", "Flat to 40 kHz."},
    {"wide", "Wide", "Keeps as much as the PCM rate allows, noise included."},
};

const Choice kDsdGains[] = {
    {"safe", "Safe, +2.8 dB",
     "DSD’s highest specified peak lands just under full scale."},
    {"0", "0 dB", "The level as converted."},
    {"6", "+6 dB",
     "DSD’s 0 dB lands at full scale, as many DACs play it. Loud recordings "
     "clip."},
};

const Knob kDsdKnobs[] = {
    {"output.dsd_mode",
     "DSD output",
     "Native DSD and DoP require an output device marked (direct), and either "
     "Fixed output volume or a connected device. Convert to PCM plays on any "
     "output that supports a rate it converts to.",
     renderer_pb::CONFIG_FIELD_TYPE_ENUM,
     "",
     {},
     "Plays DSF and DSDIFF files, either as DSD, which the DAC must support in "
     "the selected mode and at the track’s DSD rate, or converted to PCM. "
     "Automatic chooses for each track. Unsupported tracks fail to play.",
     kDsdChoices,
     renderer_pb::CONFIG_IMPORTANCE_SIMPLE},
    {"output.dsd_pcm_rate",
     "DSD to PCM rate",
     "Overrides the PCM sample rate DSD is converted to.",
     renderer_pb::CONFIG_FIELD_TYPE_ENUM,
     "",
     {},
     "Each choice names two rates. Almost all DSD, DSD64 at 2.8 MHz and its "
     "multiples, converts to the first. The second is for DSD at 3.072 MHz "
     "and its multiples, which is rare.\n\n"
     "**Automatic** converts to 176.4 kHz. If the output does not support it, "
     "Automatic tries 352.8, 88.2 and 44.1 kHz, in that order.\n\n"
     "**176.4 or 192 kHz** and **352.8 or 384 kHz** have room for every DSD "
     "to PCM filter. With the 30 or 50 kHz filter, 352.8 kHz holds nothing "
     "more than 176.4 kHz.\n\n"
     "**88.2 or 96 kHz** has room for the 30 kHz filter. The 50 kHz filter "
     "works as Wide there, flat to about 40 kHz.\n\n"
     "**44.1 or 48 kHz** keeps the audio band up to 20 kHz, whatever the "
     "filter. Use it for outputs that support nothing higher, such as HDMI or "
     "simple USB DACs.\n\n"
     "If the output does not support the chosen rate, Automatic is used "
     "instead. If it supports no rate for the track at all, the track does "
     "not play: an output limited to 48, 96 and 192 kHz cannot play 2.8 MHz "
     "DSD.",
     kDsdPcmRates,
     renderer_pb::CONFIG_IMPORTANCE_SIMPLE,
     kConvertedDsdHelp},
    {"output.dsd_filter",
     "DSD to PCM filter",
     "Overrides the low-pass filter DSD is converted with.",
     renderer_pb::CONFIG_FIELD_TYPE_ENUM,
     "",
     {},
     "DSD carries strong ultrasonic noise, rising with frequency above the "
     "audio band. The filter decides how much of it, and of any music above "
     "20 kHz, reaches the DAC.\n\n"
     "**Automatic** uses 30 kHz for DSD64 and 50 kHz for DSD128 and higher, "
     "whose noise starts higher up.\n\n"
     "**30 kHz** is flat to 20 kHz and removes everything from 40 kHz.\n\n"
     "**50 kHz** is flat to 40 kHz and removes everything from 60 kHz.\n\n"
     "**Wide** keeps everything up to about 79 kHz at 176.4 kHz, and more at "
     "352.8 kHz, ultrasonic noise included. Use it only when the DAC or "
     "amplifier filters ultrasonics itself.\n\n"
     "At 88.2 kHz and below, the PCM rate limits the filter: see DSD to PCM "
     "rate.",
     kDsdFilters,
     renderer_pb::CONFIG_IMPORTANCE_SIMPLE,
     kConvertedDsdHelp},
    {"output.dsd_gain_db",
     "DSD to PCM level",
     "Raises DSD converted to PCM, which is recorded below PCM’s full scale.",
     renderer_pb::CONFIG_FIELD_TYPE_ENUM,
     "",
     {},
     "DSD’s 0 dB is half its full modulation, and the SACD specification "
     "lets peaks reach 3.1 dB above it. Converted as it is, DSD plays about "
     "6 dB quieter than PCM.\n\n"
     "**Safe** raises it by 2.8 dB, so that the highest specified peak stays "
     "just under full scale.\n\n"
     "**+6 dB** brings DSD’s 0 dB to full scale, as many DACs do. Peaks above "
     "it clip; the track’s stream information counts clipped samples.\n\n"
     "**0 dB** leaves the level as converted.\n\n"
     "A change applies at once, to the track playing too.",
     kDsdGains,
     renderer_pb::CONFIG_IMPORTANCE_EXPERT,
     kConvertedDsdHelp},
};

// A section of the settings page that holds nothing but knobs.
struct Section {
  const char *path;
  const char *title;
  const char *description;
  std::span<const Knob> knobs;
};

const Section kBuffering{"buffers", "Buffering",
                         "Audio held in memory during playback.",
                         kBufferKnobs};

const Section kNetwork{"network", "Network",
                       "How the renderer downloads tracks.",
                       kNetworkKnobs};

const Section kDsd{"dsd", "DSD",
                   "How DSD tracks play, as DSD or converted to PCM.",
                   kDsdKnobs};

const Choice kVolumeChoices[] = {
    {"auto", "Automatic",
     "Use the sound card’s volume control when available, otherwise software."},
    {"hardware", "Card mixer",
     "Use the sound card’s volume control. Needs a card that has one."},
    {"software", "Software",
     "Adjust volume in the renderer."},
    {"fixed", "Fixed output",
     "Play at full volume and control volume on your amplifier or receiver."},
};

void addChoices(renderer_pb::ConfigField &field,
                std::span<const Choice> choices) {
  for (const Choice &choice : choices) {
    renderer_pb::ConfigOption *option = field.add_options();
    option->set_value(choice.value);
    option->set_label(choice.label);
    option->set_description(choice.description);
  }
}

// Per-stream keys reach the next stream appended; the rest need a new graph.
const std::map<std::string, std::string> &graphKeys() {
  static const std::map<std::string, std::string> keys{
      {"output.device", "output.alsa.device"},
      {"output.dsd_mode", "output.dsd_mode"},
      {"output.dsd_pcm_rate", "dsd.pcm_rate"},
      {"output.dsd_filter", "dsd.filter"},
      {"output.dsd_gain_db", "dsd.gain_db"},
      {"output.latency_ms", "output.alsa.latency_ms"},
      {"output.period_ms", "output.alsa.period_ms"},
      {"output.format_change_delay_ms",
       "fixups.alsa_sleep_after_format_setup_ms"},
      {"output.reopen_on_format_change",
       "fixups.alsa_reopen_device_with_new_format"},
      {"buffers.network_stream", "input.http.buffer_size"},
      {"buffers.network_request", "input.http.chunk_size"},
      {"buffers.network_dsd", "input.http.dsd_buffer_size"},
      {"buffers.flac", "decoder.flac.buffer_size"},
      {"buffers.mpeg", "decoder.mpeg.buffer_size"},
      {"buffers.vorbis", "decoder.vorbis.buffer_size"},
      {"buffers.wav", "decoder.wav.buffer_size"},
      {"buffers.dsd", "decoder.dsd.buffer_size"},
      {"buffers.aac", "decoder.aac.buffer_size"},
      {"network.stall_timeout_s", "input.http.stall_timeout"},
      {"network.max_redirects", "input.http.max_redirects"},
  };
  return keys;
}

bool readPerStream(const std::string &path) {
  const auto key = graphKeys().find(path);
  return key != graphKeys().end() && AudioPlayer::isStreamKey(key->second);
}

// Graph keys the built graph takes as they change, what plays included.
bool appliesLive(const std::string &path) {
  return path == "output.dsd_gain_db";
}

// Card mixer volume on a card without one leaves nothing to set the level, and
// every session is refused for want of its start ceiling.
bool cardHasVolume(const std::string &device) {
  return AlsaVolumeControl(device, "").available();
}

bool holds(std::span<const Knob> knobs, const std::string &path) {
  return std::ranges::any_of(
      knobs, [&path](const Knob &knob) { return knob.path == path; });
}

const Knob *findKnob(const std::string &path) {
  for (std::span<const Knob> knobs :
       {std::span<const Knob>(kOutputKnobs), kDsd.knobs, kBuffering.knobs,
        kNetwork.knobs}) {
    const auto knob = std::ranges::find_if(
        knobs, [&path](const Knob &knob) { return knob.path == path; });
    if (knob != knobs.end()) {
      return &*knob;
    }
  }
  return nullptr;
}

// Everything the settings page is told about a knob but its values.
void describe(renderer_pb::ConfigField &field, const Knob &knob) {
  field.set_path(knob.path);
  field.set_title(knob.title);
  field.set_description(knob.description);
  field.set_help_details(std::string(knob.helpLead) + knob.helpDetails);
  field.set_type(knob.type);
  field.set_apply(readPerStream(knob.path) || appliesLive(knob.path)
                      ? renderer_pb::APPLY_COST_INSTANT
                      : renderer_pb::APPLY_COST_INTERRUPTS_PLAYBACK);
  field.set_importance(knob.importance);
  field.set_unit(knob.unit);
  addChoices(field, knob.choices);
  if (!knob.bounds.declared()) {
    return;
  }
  renderer_pb::ConfigRange *range = field.mutable_range();
  range->set_min(knob.bounds.min);
  range->set_max(knob.bounds.max);
  range->set_step(knob.bounds.step);
  field.set_widget(knob.bounds.slider ? renderer_pb::CONFIG_WIDGET_SLIDER
                                      : renderer_pb::CONFIG_WIDGET_NUMBER);
}

void declare(renderer_pb::ConfigSection &out, const Knob &knob,
             const std::map<std::string, std::string> &settings,
             const std::map<std::string, std::string> &defaults) {
  renderer_pb::ConfigField *field = out.add_fields();
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
  renderer_pb::ConfigField field;
  describe(field, *knob);
  return fieldAccepts(field, value, error);
}

}  // namespace

const std::map<std::string, std::string> &NativePlayer::defaultSettings() {
  static const std::map<std::string, std::string> defaults{
      {"output.driver", "alsa"},
      {kFirProfile, kFirOff},
      {"output.device", "default"},
      {"output.volume_mode", "auto"},
      {"output.dsd_mode", "auto"},
      {"output.dsd_pcm_rate", "auto"},
      {"output.dsd_filter", "auto"},
      {"output.dsd_gain_db", "safe"},
      {"output.session_start_volume_ceiling_percent", "30"},
      // What the server shipped while it did the playing, rather than the
      // sink's own 100/25.
      {"output.latency_ms", "160"},
      {"output.period_ms", "40"},
      // A HiFiBerry on a Pi can stay silent when audio starts before I2S syncs.
      {"output.format_change_delay_ms", "500"},
      {"output.reopen_on_format_change", "false"},
      {"buffers.network_stream", "768000"},
      {"buffers.network_request", "384000"},
      {"buffers.network_dsd", "4096000"},
      {"buffers.flac", "1536000"},
      {"buffers.mpeg", "768000"},
      {"buffers.vorbis", "768000"},
      {"buffers.wav", "1536000"},
      {"buffers.dsd", "1536000"},
      {"buffers.aac", "1536000"},
      {"network.stall_timeout_s",
       std::to_string(AudioGraphHttpStream::DEFAULT_STALL_TIMEOUT.count())},
      {"network.max_redirects",
       std::to_string(AudioGraphHttpStream::DEFAULT_MAX_REDIRECTS)},
  };
  return defaults;
}

class NativePlayer::KnobSection : public ConfigContributor {
public:
  KnobSection(std::shared_ptr<NativePlayer> player, const Section &section)
      : player_(std::move(player)), section_(section) {}

  void fillConfig(renderer_pb::ConfigSection &out) const override {
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

std::shared_ptr<ConfigContributor> NativePlayer::dsdSettings() {
  return std::make_shared<KnobSection>(shared_from_this(), kDsd);
}

// The schema section controls placement in released clients. Keep the FIR key
// stable for saved settings, profile imports and clients that edit it by path.
class NativePlayer::RoomCorrectionSection final : public ConfigContributor {
public:
  explicit RoomCorrectionSection(std::shared_ptr<NativePlayer> player)
      : player_(std::move(player)) {}

  void fillConfig(renderer_pb::ConfigSection &out) const override;

  bool applyConfig(const std::string &path, const std::string &value,
                   std::string &error) override {
    if (path == kFirProfile)
      return player_->applySetting(path, value, error);
    return player_->dsp_->applyConfig(path, value, error);
  }

private:
  const std::shared_ptr<NativePlayer> player_;
};

void NativePlayer::RoomCorrectionSection::fillConfig(
    renderer_pb::ConfigSection &out) const {
  player_->dsp_->fillConfig(out);
  auto *fir = out.add_fields();
  fir->set_path(kFirProfile);
  fir->set_title("FIR Convolution");
  fir->set_supports_fir_import(true);
  fir->set_matches_fir_rates(true);
  if (player_->firFilter_) {
    const unsigned lastRate =
        player_->liveFir_ ? player_->liveFir_->lastRate() : 0;
    unsigned rate = lastRate ? lastRate : 48000;
    if (player_->player_) {
      const auto info = player_->player_->getState().streamInfo;
      if (info && info->format.channels == 2 &&
          !isDsd(info->format.sampleFormat) && info->format.sampleRate &&
          (!info->decodedFormat || info->decodedFormat->channels == 2))
        rate = info->format.sampleRate;
    }
    const auto plan = player_->firFilter_->at(rate);
    if (plan && !plan->responseDb[0].empty()) {
      auto &response = *fir->mutable_fir_response();
      response.set_sample_rate(rate);
      response.set_source_rate(plan->sourceRate);
      response.mutable_left_db()->Assign(plan->responseDb[0].begin(),
                                         plan->responseDb[0].end());
      response.mutable_right_db()->Assign(plan->responseDb[1].begin(),
                                          plan->responseDb[1].end());
    } else if (!plan) {
      fir->set_help_details(
          std::format("The renderer could not prepare this filter for {:g} "
                      "kHz. Supply a WAV at that rate.",
                      rate / 1000.));
    }
  }
  fir->set_description(
      "Uses the WAV supplied for the stereo PCM rate, including converted DSD. "
      "For other rates, converts the closest supplied impulse and reuses it. "
      "Switch between this filter and Off during playback.");
  fir->set_type(renderer_pb::CONFIG_FIELD_TYPE_ENUM);
  fir->set_importance(renderer_pb::CONFIG_IMPORTANCE_SIMPLE);
  fir->set_apply(renderer_pb::APPLY_COST_INSTANT);
  fir->set_default_value(kFirOff);
  fir->set_value(player_->settings_.at(kFirProfile));
  auto *off = fir->add_options();
  off->set_value(kFirOff);
  off->set_label("Off");
  if (player_->firFilter_ &&
      dsp_profiles::validId(player_->settings_.at(kFirProfile))) {
    auto *option = fir->add_options();
    option->set_value(player_->settings_.at(kFirProfile));
    option->set_label(player_->firFilter_->name);
    std::string rates;
    for (const unsigned rate : player_->firFilter_->suppliedRates()) {
      if (!rates.empty())
        rates += ", ";
      rates += std::format("{:g}", rate / 1000.0);
    }
    option->set_description(rates + " kHz");
  }
  for (const auto &name : firProfiles()) {
    auto *option = fir->add_options();
    option->set_value(name);
    option->set_label(name);
  }
}

std::shared_ptr<ConfigContributor> NativePlayer::dspSettings() {
  return std::make_shared<RoomCorrectionSection>(shared_from_this());
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
  dsp_ = std::make_unique<DspSettings>([this] {
    if (player_) {
      updateRoomCorrection();
      restatePlayback();
    }
  });
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
    if (key == kFirProfile) {
      try {
        firFilter_ = loadFirProfile(value);
      } catch (const std::exception &e) {
        spdlog::warn("Cannot restore REW FIR profile {}: {}", value, e.what());
        continue;
      }
    }
    setting->second = value;
    spdlog::info("Config override {} = '{}'", key, value);
  }
  auto &device = settings_.at("output.device");
  const auto playback = playbackDeviceName(device);
  if (playback != device) {
    device = playback;
    persistOverrides();
  }
  ensurePlayer();
}

NativePlayer::~NativePlayer() { stopPumps(); }

void NativePlayer::setStateSink(StateSink sink) { sink_ = std::move(sink); }

void NativePlayer::updateRoomCorrection() {
  liveFir_->select(dsp_->bypassed() ? nullptr : firFilter_);
  player_->setRoomEq(dsp_->current());
}

bool NativePlayer::ensurePlayer() {
  if (player_) {
    return true;
  }
  try {
    auto rates = installedFirRates(firFilter_);
    liveFir_ = std::make_shared<LiveFir>();
    player_ = std::make_unique<AudioPlayer>(graphConfig(), liveFir_,
                                            std::move(rates));
    updateRoomCorrection();
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
  // Whatever was playing is gone, as APPLY_COST_INTERRUPTS_PLAYBACK declared.
  interruptPending_ = interruptPending_ || onAir();
  stopPumps();
  sources_.clear();
  currentId_.reset();
  player_.reset();
  // Without a graph to report its resting STOPPED, post one as it would.
  if (!ensurePlayer()) {
    asio::post(ioc_, [this, generation = graphGeneration_] {
      if (generation == graphGeneration_) {
        onStreamState(StreamState(AudioGraphNodeState::STOPPED));
      }
    });
  }
  // The new device may set the level elsewhere: a mixer, software, or none.
  emitVolume(player_ ? player_->getVolume() : VolumeState{}, false);
}

bool NativePlayer::onAir() const {
  if (!player_) {
    return false;
  }
  switch (player_->getState().state) {
  case AudioGraphNodeState::PREPARING:
  case AudioGraphNodeState::STREAMING:
  case AudioGraphNodeState::PAUSED:
  case AudioGraphNodeState::SOURCE_CHANGED:
    return true;
  default:
    return false;
  }
}

void NativePlayer::startPumps() {
  stateMonitor_ = std::shared_ptr<StateMonitor>(player_->monitor().release());
  volumeMonitor_ =
      std::shared_ptr<VolumeMonitor>(player_->volumeMonitor().release());
  const uint64_t generation = graphGeneration_;

  statePump_ = std::thread([this, monitor = stateMonitor_, generation] {
    while (monitor->isRunning()) {
      StreamState state = monitor->waitState();
      if (!monitor->isRunning()) {
        break;
      }
      asio::post(ioc_, [this, state, generation] {
        if (generation == graphGeneration_) {
          onStreamState(state);
        }
      });
    }
  });
  eventPump_ = std::jthread([this, events = player_->playbackEvents(),
                             generation](std::stop_token token) {
    std::mutex mutex;
    std::condition_variable_any held;
    while (!token.stop_requested()) {
      auto taken = events->take(token);
      if (taken.empty()) {
        continue;
      }
      asio::post(ioc_, [this, taken = std::move(taken), generation] {
        if (generation == graphGeneration_) {
          for (const PlaybackEvent &event : taken) {
            emitEvent(event);
          }
        }
      });
      std::unique_lock lock(mutex);
      held.wait_for(lock, token, kEventInterval,
                    [&token] { return token.stop_requested(); });
    }
  });
  volumePump_ = std::thread([this, monitor = volumeMonitor_, generation] {
    while (monitor->isRunning()) {
      monitor->wait();
      if (!monitor->isRunning()) {
        break;
      }
      asio::post(ioc_, [this, generation] {
        if (generation == graphGeneration_ && player_) {
          emitVolume(player_->getVolume(), true);
        }
      });
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
  if (eventPump_.joinable()) {
    eventPump_.request_stop();
    eventPump_.join();
  }
  stateMonitor_.reset();
  volumeMonitor_.reset();
  ++graphGeneration_;
}

void NativePlayer::reportUnavailable(const char *command) {
  spdlog::warn("Command {}: audio graph unavailable", command);
  renderer_pb::Envelope env;
  renderer_pb::PlaybackStateChanged *changed =
      env.mutable_playback_state_changed();
  changed->set_state(renderer_pb::PLAYBACK_STATE_ERROR);
  changed->set_position_valid(false);
  changed->set_at_unix_ms(nowUnixMs());
  changed->mutable_error()->set_source(renderer_pb::ERROR_SOURCE_AUDIO_OUTPUT);
  changed->mutable_error()->set_message(
      "audio output is unavailable; check the configured device");
  emit(env);
}

StreamId NativePlayer::appendSource(const renderer_pb::Source &source) {
  const StreamId id = nextStreamId_++;
  sources_.push_back(TrackedSource{source, id});
  player_->append(id, source.uri(), formatOf(source),
                  source.start_offset_ms());
  return id;
}

void NativePlayer::setSource(const renderer_pb::Source &source) {
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

void NativePlayer::enqueueSource(const renderer_pb::Source &source) {
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

const renderer_pb::Source *
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
  const renderer_pb::Source *source = sourceFor(streamId);
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
    renderer_pb::Envelope env;
    renderer_pb::SourceChanged *changed = env.mutable_source_changed();
    changed->set_source_token(*currentToken());
    if (previous) {
      changed->set_previous_source_token(*previous);
    }
    changed->set_at_unix_ms(nowUnixMs());
    emit(env);
    for (const PlaybackEvent &event : std::exchange(unheardEvents_, {})) {
      emitEvent(event);
    }
    return;
  }

  renderer_pb::Envelope env;
  renderer_pb::PlaybackStateChanged *changed =
      env.mutable_playback_state_changed();
  state_translator::fillPlaybackStateChanged(state, tokenFor(state.streamId),
                                             nowUnixMs(), *changed);
  if (const auto path = signalPathOf(state)) {
    state_translator::fillSignalPath(*path, *changed->mutable_signal_path());
  }
  if (state.state == AudioGraphNodeState::STOPPED && interruptPending_) {
    changed->set_interrupted(true);
    interruptPending_ = false;
  }
  emit(env);
}

std::optional<SignalPath>
NativePlayer::signalPathOf(const StreamState &state) const {
  if (!player_ || !state.streamInfo) {
    return std::nullopt;
  }
  SignalPath path = player_->signalPath(*state.streamInfo);
  if (path.steps.empty()) {
    return std::nullopt;
  }
  return path;
}

void NativePlayer::restatePlayback() {
  if (!player_) {
    return;
  }
  StreamState state = player_->getState();
  if (state.state != AudioGraphNodeState::PREPARING &&
      state.state != AudioGraphNodeState::STREAMING &&
      state.state != AudioGraphNodeState::PAUSED) {
    return;
  }
  state.position = state.positionAt(getTimestampNs());
  onStreamState(state);
}

void NativePlayer::emitEvent(const PlaybackEvent &event) {
  if (event.stream && currentId_ && *event.stream > *currentId_) {
    std::erase_if(unheardEvents_, [&event](const PlaybackEvent &held) {
      return held.kind.index() == event.kind.index();
    });
    unheardEvents_.push_back(event);
    return;
  }
  const std::optional<std::string> token = tokenFor(event.stream);
  if (event.stream && !token) {
    return;  // about a source already let go of
  }
  renderer_pb::Envelope env;
  state_translator::fillPlaybackEvent(event, token, nowUnixMs(),
                                      *env.mutable_playback_event());
  emit(env);
}

void NativePlayer::emitVolume(const VolumeState &volume, bool external) {
  renderer_pb::Envelope env;
  renderer_pb::VolumeChanged *changed = env.mutable_volume_changed();
  state_translator::fillVolume(volume, *changed->mutable_volume());
  changed->set_external(external);
  emit(env);
}

void NativePlayer::emit(renderer_pb::Envelope &env) {
  if (sink_) {
    sink_(env);
  }
}

void NativePlayer::fillConfig(renderer_pb::ConfigSection &out) const {
  out.set_path("output");
  out.set_title("Output");

  renderer_pb::ConfigField *driver = out.add_fields();
  driver->set_path("output.driver");
  driver->set_title("Driver");
  driver->set_description("Audio backend used to reach the sound card.");
  driver->set_type(renderer_pb::CONFIG_FIELD_TYPE_ENUM);
  driver->set_value(settings_.at("output.driver"));
  driver->set_default_value("alsa");
  driver->set_apply(renderer_pb::APPLY_COST_RESTART_REQUIRED);
  // One backend to choose from, so far.
  driver->set_importance(renderer_pb::CONFIG_IMPORTANCE_EXPERT);
  renderer_pb::ConfigOption *alsa = driver->add_options();
  alsa->set_value("alsa");
  alsa->set_label("ALSA");

  renderer_pb::ConfigField *device = out.add_fields();
  device->set_path("output.device");
  device->set_title("Output device");
  device->set_help_details(
      "Direct output requires the sound card to support the track’s sample rate "
      "and format. Converted output adapts unsupported formats. Shared output "
      "allows other apps to play audio too. Changing the device sets Native DSD "
      "and DoP back to Automatic, as the new card may not support them, and "
      "switches Card mixer volume to Automatic when the new card has no volume "
      "control.");
  device->set_description(
      "Changing the device interrupts playback and sets Native DSD and DoP back "
      "to Automatic.");
  device->set_type(renderer_pb::CONFIG_FIELD_TYPE_ENUM);
  device->set_value(settings_.at("output.device"));
  device->set_default_value("default");
  device->set_apply(renderer_pb::APPLY_COST_INTERRUPTS_PLAYBACK);
  device->set_importance(renderer_pb::CONFIG_IMPORTANCE_SIMPLE);
  bool currentOffered = false;
  for (const AlsaPcmDevice &pcm : listAlsaPcmDevices()) {
    if (!isPlaybackPcm(pcm)) {
      continue;
    }
    renderer_pb::ConfigOption *option = device->add_options();
    option->set_value(pcm.name);
    option->set_label(pcm.label.empty() ? pcm.name : pcm.label);
    option->set_description(pcm.description);
    currentOffered = currentOffered || pcm.name == device->value();
  }
  if (!currentOffered) {
    // The configured device may be unplugged right now; it must stay
    // selectable or the page could not even show what is set.
    const AlsaPcmDevice missing = describeAlsaPcm(device->value(), "");
    renderer_pb::ConfigOption *option = device->add_options();
    option->set_value(device->value());
    if (device->value() == "default") {
      // ALSA does not always hint "default", but it always resolves it.
      option->set_label(missing.label);
      option->set_description(missing.description);
    } else {
      option->set_label(missing.label + " (not detected)");
      option->set_description(
          "Device not found. It may be disconnected or renamed.");
    }
  }

  unsigned maxBits = 0;
  unsigned maxRate = 0;
  // The graph has already probed this device. Opening it again to draw the
  // settings page can compete with playback for an exclusive PCM handle.
  const std::span<const StreamAudioFormat> formats =
      player_ ? player_->outputFormats() : std::span<const StreamAudioFormat>{};
  for (const auto &format : formats) {
    if (isDsd(format.sampleFormat))
      continue;
    // A DAC's resolution: float's container says nothing about it.
    if (!isFloat(format.sampleFormat))
      maxBits = std::max(maxBits, format.bitsPerSample);
    maxRate = std::max(maxRate, format.sampleRate);
  }
  auto addCapability = [&](const char *path, const char *title,
                           const std::string &value) {
    auto *field = out.add_fields();
    field->set_path(path);
    field->set_title(title);
    field->set_type(renderer_pb::CONFIG_FIELD_TYPE_STRING);
    field->set_read_only(true);
    field->set_importance(renderer_pb::CONFIG_IMPORTANCE_SIMPLE);
    field->set_value(value);
  };
  addCapability("output.bit_depth", "Bit depth",
                maxBits ? "Up to " + std::to_string(maxBits) + " bit"
                        : "Unavailable");
  std::ostringstream sampleRate;
  sampleRate << "Up to " << maxRate / 1000.0 << " kHz";
  addCapability("output.sample_rate", "Sample rate",
                maxRate ? sampleRate.str() : "Unavailable");

  renderer_pb::ConfigField *mode = out.add_fields();
  mode->set_path("output.volume_mode");
  mode->set_title("Volume control");
  mode->set_help_details(
      "Automatic uses the sound card’s volume control when it has one, otherwise "
      "software. Card mixer uses the card’s volume control and leaves the audio "
      "samples unchanged. Software adjusts the samples and leaves the card at full "
      "volume. It works with any card, but quiet levels lose a little quality. "
      "Fixed output leaves volume control to an amplifier or receiver.");
  mode->set_type(renderer_pb::CONFIG_FIELD_TYPE_ENUM);
  mode->set_value(settings_.at("output.volume_mode"));
  mode->set_default_value("auto");
  mode->set_apply(renderer_pb::APPLY_COST_INTERRUPTS_PLAYBACK);
  mode->set_importance(renderer_pb::CONFIG_IMPORTANCE_SIMPLE);
  addChoices(*mode, kVolumeChoices);

  renderer_pb::ConfigField *startCeiling = out.add_fields();
  startCeiling->set_path("output.session_start_volume_ceiling_percent");
  startCeiling->set_title("Session start volume ceiling");
  startCeiling->set_help_details(
      "When playback starts, a volume above the ceiling is lowered to it and a "
      "lower volume stays as it is. The check runs when playback starts from a "
      "stop or a long pause, not between tracks.");
  startCeiling->set_description(
      "Highest volume playback can start at. A louder setting is lowered to it. "
      "Not used with Fixed output or a connected device.");
  startCeiling->set_type(renderer_pb::CONFIG_FIELD_TYPE_INT);
  startCeiling->set_value(
      settings_.at("output.session_start_volume_ceiling_percent"));
  startCeiling->set_default_value("30");
  startCeiling->set_apply(renderer_pb::APPLY_COST_INSTANT);
  startCeiling->set_importance(renderer_pb::CONFIG_IMPORTANCE_SIMPLE);
  startCeiling->set_unit("%");
  startCeiling->mutable_range()->set_min(0);
  startCeiling->mutable_range()->set_max(100);
  startCeiling->mutable_range()->set_step(1);
  startCeiling->set_widget(renderer_pb::CONFIG_WIDGET_SLIDER);

  for (const Knob &knob : kOutputKnobs) {
    declare(out, knob, settings_, defaultSettings());
  }
}

bool NativePlayer::applyConfig(const std::string &path,
                               const std::string &value, std::string &error) {
  return applySetting(path, value, error);
}

bool NativePlayer::applySetting(const std::string &path,
                                const std::string &requested, std::string &error) {
  const std::string value = path == "output.device"
                                ? playbackDeviceName(requested) : requested;
  auto setting = settings_.find(path);
  if (setting == settings_.end()) {
    error = "unknown setting";
    return false;
  }
  if (path == "output.dsd_mode" && !isDsdMode(value)) {
    error = "unknown DSD output mode";
    return false;
  }
  // Sizes and durations, which the graph reads as unsigned: a negative one
  // throws where it is read, which is halfway into the next track. The service
  // refuses what a knob's declared range excludes before it ever reaches here.
  if (value.starts_with("-") && findKnob(path) != nullptr) {
    error = "must not be negative";
    return false;
  }
  // The playing graph takes a live key at once, and only the values it has.
  if (appliesLive(path) && !knobTakes(path, value, error)) {
    return false;
  }
  if (setting->second == value) {
    return true;  // nothing to do, and nothing to interrupt
  }
  if (path == "output.volume_mode" && value == "hardware" &&
      !cardHasVolume(settings_.at("output.device"))) {
    error = "this sound card has no volume control of its own; choose "
            "Automatic or Software";
    return false;
  }

  if (path == "output.volume_mode" && player_ && !sessionForcedFixed_) {
    // Changing which stage owns volume can otherwise produce an abrupt level
    // jump in audio already on air. The field declares this interruption.
    stop();
    // A plain stop, even after a rebuild: replayed, the track would jump too.
    interruptPending_ = false;
    std::string configureError;
    if (!player_->configureVolume(value, "", &configureError)) {
      error = configureError;
      return false;
    }
  }

  if (path == kFirProfile) {
    try {
      firFilter_ = loadFirProfile(value);
    } catch (const std::exception &e) {
      error = e.what();
      return false;
    }
  }
  setting->second = value;
  if (path == "output.device") {
    // Native DSD and DoP were chosen for the old card; Automatic probes each.
    std::string &dsdMode = settings_.at("output.dsd_mode");
    if (dsdMode == "native" || dsdMode == "dop") {
      dsdMode = "auto";
    }
    std::string &volumeMode = settings_.at("output.volume_mode");
    if (volumeMode == "hardware" && !cardHasVolume(value)) {
      volumeMode = "auto";
      spdlog::info("'{}' has no mixer; volume control is back to 'auto'",
                   value);
    }
  }
  persistOverrides();
  spdlog::info("Config {} = '{}'", path, value);
  if (readPerStream(path)) {
    if (player_) {
      player_->setStreamConfig(graphKeys().at(path), value);
    }
  } else if (appliesLive(path)) {
    if (player_) {
      player_->setDsdGain(value);
      restatePlayback();
    }
  } else if (path == kFirProfile) {
    if (player_) {
      updateRoomCorrection();
      restatePlayback();
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
    error = configuredMode == "hardware"
                ? "this output's sound card has no volume control of its "
                  "own; set its volume control to Automatic or Software"
                : "volume control is unavailable; session-start volume "
                  "ceiling cannot be enforced";
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

void NativePlayer::fillSnapshot(renderer_pb::StateSnapshot &out) const {
  out.set_captured_at_unix_ms(nowUnixMs());
  out.set_selected_device_id(settings_.at("output.device"));

  if (!player_) {
    out.set_playback_state(renderer_pb::PLAYBACK_STATE_ERROR);
    out.set_position_valid(false);
    out.mutable_volume()->set_supported(false);
    out.mutable_volume()->set_backend(renderer_pb::VOLUME_BACKEND_NONE);
    out.mutable_error()->set_source(renderer_pb::ERROR_SOURCE_AUDIO_OUTPUT);
    out.mutable_error()->set_message(
        "audio output is unavailable; check the configured device");
    return;
  }

  StreamState state = player_->getState();
  state.position = state.positionAt(getTimestampNs());
  const std::optional<StreamId> onAir =
      state.streamId ? state.streamId : currentId_;
  renderer_pb::PlaybackStateChanged translated;
  state_translator::fillPlaybackStateChanged(state, tokenFor(onAir),
                                             nowUnixMs(), translated);
  out.set_playback_state(translated.state());
  out.set_position_ms(translated.position_ms());
  out.set_position_valid(translated.position_valid());
  if (translated.has_error()) {
    *out.mutable_error() = translated.error();
  }
  if (const renderer_pb::Source *source = sourceFor(onAir)) {
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
  if (const auto path = signalPathOf(state)) {
    state_translator::fillSignalPath(*path, *out.mutable_signal_path());
  }
  state_translator::fillVolume(player_->getVolume(), *out.mutable_volume());
  for (const TrackedSource &tracked : sources_) {
    if (!onAir || tracked.streamId > *onAir) {
      out.add_queued_source_tokens(tracked.source.source_token());
    }
  }
}

void NativePlayer::fillCapabilities(renderer_pb::Capabilities &out) const {
  out.set_dsd(settings_.at("output.dsd_mode") != "disabled");
  out.set_live_pcm(1);
  renderer_pb::DspCapabilities *dsp = out.mutable_dsp();
  dsp->set_dsd_to_pcm(true);
  for (const char *kind : signal_step::kAll) {
    dsp->add_steps(kind);
  }
  dsp->set_max_output_channels(kPcmDeviceChannels);
}
