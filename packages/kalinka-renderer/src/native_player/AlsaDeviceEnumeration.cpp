#include "AlsaDeviceEnumeration.h"

#include <alsa/asoundlib.h>

#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <memory>
#include <optional>
#include <string>
#include <unordered_set>
#include <vector>

namespace {

// snd_device_name_get_hint() returns malloc'd strings. RAII so we
// don't leak when filtering early.
struct CFree {
  void operator()(char *p) const noexcept {
    if (p) std::free(p);
  }
};
using CString = std::unique_ptr<char, CFree>;

CString getHint(const void *hint, const char *key) {
  return CString(snd_device_name_get_hint(hint, key));
}

struct CtlClose {
  void operator()(snd_ctl_t *ctl) const noexcept { snd_ctl_close(ctl); }
};
using Ctl = std::unique_ptr<snd_ctl_t, CtlClose>;

std::vector<std::string> splitLines(const char *raw) {
  std::vector<std::string> lines;
  if (!raw) return lines;
  std::string current;
  for (const char *p = raw; *p; ++p) {
    if (*p == '\n' || *p == '\r') {
      if (!current.empty()) lines.push_back(current);
      current.clear();
    } else {
      current += *p;
    }
  }
  if (!current.empty()) lines.push_back(current);
  return lines;
}

std::vector<std::string> words(const std::string &text) {
  std::vector<std::string> out;
  std::string current;
  for (char c : text) {
    if (c == ' ') {
      if (!current.empty()) out.push_back(current);
      current.clear();
    } else {
      current += c;
    }
  }
  if (!current.empty()) out.push_back(current);
  return out;
}

std::string join(const std::vector<std::string> &parts) {
  std::string out;
  for (const std::string &part : parts) {
    if (!out.empty()) out += ' ';
    out += part;
  }
  return out;
}

bool hasDigit(const std::string &word) {
  return std::any_of(word.begin(), word.end(), [](unsigned char c) {
    return std::isdigit(c) != 0;
  });
}

bool equalsIgnoringCase(const std::string &word, const char *other) {
  return word.size() == std::char_traits<char>::length(other) &&
         std::equal(word.begin(), word.end(), other,
                    [](unsigned char a, unsigned char b) {
                      return std::tolower(a) == std::tolower(b);
                    });
}

// Kernel plumbing a person has no use for: widget ids ("wm8804-spdif-0",
// "i2s-hifi-0") and the "HiFi" every ASoC card carries.
bool plumbing(const std::string &word) {
  return equalsIgnoringCase(word, "HiFi") ||
         (word.find('-') != std::string::npos &&
          std::isdigit(static_cast<unsigned char>(word.back())));
}

// Chip ids drivers put in front of the connector — "bcm2835 Headphones".
bool chipId(const std::string &word) {
  return hasDigit(word) && std::islower(static_cast<unsigned char>(word[0]));
}

std::string tidy(std::string name) {
  if (name.starts_with("vc4-hdmi-")) {
    // The Pi's HDMI ports, counted from zero by the driver and from one
    // by everything printed on the board.
    return "HDMI " + std::to_string(std::atoi(name.c_str() + 9) + 1);
  }
  std::vector<std::string> parts = words(name);
  while (parts.size() > 1 && plumbing(parts.back())) {
    parts.pop_back();
  }
  if (parts.size() > 1 && chipId(parts.front())) {
    parts.erase(parts.begin());
  }
  name = join(parts);
  if (!name.empty() && !hasDigit(words(name).front())) {
    name[0] = static_cast<char>(
        std::toupper(static_cast<unsigned char>(name.front())));
  }
  return name;
}

// ALSA's first DESC line reads "<card>, <pcm>". The card half is usually the
// name a person would recognise, but some drivers put their module id there
// ("snd_rpi_hifiberry_digi") and the readable name in the pcm half.
std::string deviceName(const std::string &descFirstLine) {
  const size_t split = descFirstLine.find(", ");
  if (split == std::string::npos) {
    return tidy(descFirstLine);
  }
  const std::string card = descFirstLine.substr(0, split);
  const std::string pcm = descFirstLine.substr(split + 2);
  const bool moduleId = card.find('_') != std::string::npos;
  if (moduleId && !pcm.starts_with(card) &&
      pcm.find(' ') != std::string::npos) {
    return tidy(pcm);
  }
  return tidy(card);
}

// "Default ALSA Output (currently PipeWire Media Server)" — the tail says what
// the device happens to be right now, which belongs in the description.
std::string takeAside(std::string &text) {
  if (text.empty() || text.back() != ')') return {};
  const size_t open = text.rfind('(');
  if (open == std::string::npos || open == 0) return {};
  std::string aside = text.substr(open + 1, text.size() - open - 2);
  text.erase(open);
  while (!text.empty() && text.back() == ' ') {
    text.pop_back();
  }
  return aside;
}

std::string sentence(std::string text) {
  if (text.empty()) return text;
  text[0] =
      static_cast<char>(std::toupper(static_cast<unsigned char>(text[0])));
  if (text.back() != '.' && text.back() != '!') {
    text += '.';
  }
  return text;
}

std::string subdevice(const std::string &name) {
  const size_t at = name.find("DEV=");
  if (at == std::string::npos) return {};
  const std::string dev = name.substr(at + 4);
  return dev == "0" ? std::string() : " #" + dev;
}

std::string prefixOf(const std::string &name) {
  const size_t colon = name.find(':');
  return colon == std::string::npos ? name : name.substr(0, colon);
}

struct Wording {
  std::string label;  // empty = name it after the card
  std::string suffix;
  std::string description;
};

Wording wordingFor(const std::string &name) {
  if (name == "default") {
    return {"System default", "",
            "Whatever ALSA is set to use, with mixing and resampling as "
            "needed."};
  }
  if (name == "pipewire" || name == "pulse" || name == "jack") {
    return {"", "",
            "Hands the audio to the sound server, which mixes and resamples "
            "it."};
  }
  const std::string prefix = prefixOf(name);
  if (prefix == "hw") {
    return {"", " (direct)",
            "Sends samples to the card unchanged. The card must support the "
            "track's sample rate and format, and nothing else can play "
            "meanwhile."};
  }
  if (prefix == "plughw") {
    return {"", " (converted)",
            "Same card, with software conversion: resamples and reformats "
            "whatever the card cannot take as it is."};
  }
  if (prefix == "default" || prefix == "sysdefault") {
    return {"", " (shared)",
            "The card's default route: mixed with other applications, "
            "resampled when needed."};
  }
  return {};
}

std::vector<AlsaPcmDevice> hintedPcms() {
  std::vector<AlsaPcmDevice> out;
  void **hints = nullptr;
  // -1 == all cards; "pcm" == interface family.
  if (snd_device_name_hint(-1, "pcm", &hints) < 0 || !hints) {
    return out;
  }
  for (void **h = hints; *h; ++h) {
    CString name = getHint(*h, "NAME");
    CString desc = getHint(*h, "DESC");
    CString ioid = getHint(*h, "IOID");
    if (!name) continue;
    AlsaPcmDevice device =
        describeAlsaPcm(std::string(name.get()),
                        desc ? std::string(desc.get()) : std::string());
    device.ioid = ioid ? std::string(ioid.get()) : std::string();
    out.push_back(std::move(device));
  }
  snd_device_name_free_hint(hints);
  return out;
}

// Mirrors what ALSA's own name hints accept: subdevice 0, and no modem or
// digitiser PCMs.
std::optional<std::string> pcmName(snd_ctl_t *ctl, int device,
                                   snd_pcm_stream_t stream) {
  snd_pcm_info_t *info = nullptr;
  snd_pcm_info_alloca(&info);
  snd_pcm_info_set_device(info, device);
  snd_pcm_info_set_subdevice(info, 0);
  snd_pcm_info_set_stream(info, stream);
  if (snd_ctl_pcm_info(ctl, info) < 0) return std::nullopt;
  const snd_pcm_class_t pcmClass = snd_pcm_info_get_class(info);
  if (pcmClass == SND_PCM_CLASS_MODEM || pcmClass == SND_PCM_CLASS_DIGITIZER) {
    return std::nullopt;
  }
  return std::string(snd_pcm_info_get_name(info));
}

std::optional<AlsaCard> readCard(int index) {
  snd_ctl_t *raw = nullptr;
  if (snd_ctl_open(&raw, ("hw:" + std::to_string(index)).c_str(), 0) < 0) {
    return std::nullopt;
  }
  Ctl ctl(raw);
  snd_ctl_card_info_t *info = nullptr;
  snd_ctl_card_info_alloca(&info);
  if (snd_ctl_card_info(ctl.get(), info) < 0) return std::nullopt;

  AlsaCard card{snd_ctl_card_info_get_id(info),
                snd_ctl_card_info_get_name(info), {}};
  int device = -1;
  while (snd_ctl_pcm_next_device(ctl.get(), &device) >= 0 && device >= 0) {
    std::optional<std::string> name =
        pcmName(ctl.get(), device, SND_PCM_STREAM_PLAYBACK);
    if (!name) continue;
    card.playback.push_back(
        {device, std::move(*name),
         pcmName(ctl.get(), device, SND_PCM_STREAM_CAPTURE).has_value()});
  }
  return card;
}

std::vector<AlsaCard> listAlsaCards() {
  std::vector<AlsaCard> cards;
  int index = -1;
  while (snd_card_next(&index) >= 0 && index >= 0) {
    if (std::optional<AlsaCard> card = readCard(index)) {
      cards.push_back(std::move(*card));
    }
  }
  return cards;
}

}  // namespace

AlsaPcmDevice describeAlsaPcm(const std::string &name,
                              const std::string &alsaDescription) {
  AlsaPcmDevice out;
  out.name = name;
  if (name == "null") {
    // The only hint whose own text is worth nothing to a listener.
    out.label = "No output";
    out.description = "Discards the audio. Nothing is heard.";
    return out;
  }

  const std::vector<std::string> lines = splitLines(alsaDescription.c_str());
  std::string first = lines.empty() ? std::string() : lines.front();
  const std::string aside = takeAside(first);
  const Wording wording = wordingFor(name);
  const std::string base = !wording.label.empty() ? wording.label
                           : first.empty()        ? name
                                                  : deviceName(first);

  out.label = base + subdevice(name) + wording.suffix;
  out.description = wording.description;
  if (out.description.empty()) {
    for (size_t i = 1; i < lines.size(); ++i) {
      if (!out.description.empty()) out.description += ' ';
      out.description += sentence(lines[i]);
    }
  }
  if (!aside.empty()) {
    if (!out.description.empty()) out.description += ' ';
    out.description += sentence(aside);
  }
  return out;
}

std::vector<AlsaPcmDevice> withCardPcms(std::vector<AlsaPcmDevice> hinted,
                                        const std::vector<AlsaCard> &cards) {
  std::unordered_set<std::string> known;
  for (const AlsaPcmDevice &device : hinted) {
    known.insert(device.name);
  }
  for (const AlsaCard &card : cards) {
    for (const char *access : {"hw", "plughw"}) {
      for (const AlsaCardPcm &pcm : card.playback) {
        std::string name = std::string(access) + ":CARD=" + card.id +
                           ",DEV=" + std::to_string(pcm.device);
        if (!known.insert(name).second) continue;
        AlsaPcmDevice device =
            describeAlsaPcm(name, card.name + ", " + pcm.name);
        device.ioid = pcm.capture ? "" : "Output";
        hinted.push_back(std::move(device));
      }
    }
  }
  return hinted;
}

std::vector<AlsaPcmDevice> listAlsaPcmDevices() {
  return withCardPcms(hintedPcms(), listAlsaCards());
}
