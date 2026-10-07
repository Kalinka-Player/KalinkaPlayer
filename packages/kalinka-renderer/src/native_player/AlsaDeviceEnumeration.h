#pragma once

#include <string>
#include <vector>

/// Stable identifier + display text for an ALSA PCM device.
///
/// `name` is what's passed to `snd_pcm_open` (e.g. "default" or
/// "hw:CARD=sofhdadsp,DEV=0"). The CARD=<id> form is stable across
/// reboots and kernel upgrades because it references the kernel
/// driver's text id rather than the dynamic card index — using it
/// avoids the "hw:0,0 became hw:1,0 after upgrade" failure mode.
///
/// `label` names the device the way a person would, with the access
/// mode in brackets, because one card shows up several times.
/// `description` says what that mode costs — bit-perfect, resampled,
/// or shared.
///
/// `ioid` is "Output", "Input", or empty (= both). The caller filters to
/// outputs.
struct AlsaPcmDevice {
  std::string name;
  std::string label;
  std::string description;
  std::string ioid;
};

/// Turn an ALSA PCM name and its raw `DESC` hint into display text.
///
/// Exposed for tests and for naming a configured device that ALSA is
/// not currently reporting.
AlsaPcmDevice describeAlsaPcm(const std::string &name,
                              const std::string &alsaDescription);

/// A playback PCM on a sound card, as the card's control interface reports it.
struct AlsaCardPcm {
  int device = 0;
  std::string name;      ///< "USB Audio"; some drivers leave it empty.
  bool capture = false;  ///< The same device also records.
};

/// A sound card and its playback PCMs.
struct AlsaCard {
  std::string id;    ///< Kernel text id ("AUDIO"), the CARD= of a PCM name.
  std::string name;  ///< "SMSL USB AUDIO".
  std::vector<AlsaCardPcm> playback;
};

/// Add the `hw:` and `plughw:` names of every card PCM that `hinted` lacks.
///
/// Whether ALSA hints those names at all is the distribution's choice
/// (`defaults.namehint.extended`; Fedora ships it off), so a list built from
/// hints alone can miss every card. Entries come out as ALSA would hint them:
/// per card, all `hw:` then all `plughw:`, appended after `hinted`.
std::vector<AlsaPcmDevice> withCardPcms(std::vector<AlsaPcmDevice> hinted,
                                        const std::vector<AlsaCard> &cards);

/// Enumerate every PCM ALSA hints, plus the direct and converted names of
/// each card's playback PCMs.
///
/// Returns an empty vector if libasound is unavailable. The caller is
/// responsible for filtering (e.g. dropping the noisy
/// `front:`/`surround*:`/`iec958:` virtual variants).
///
/// Fast — a few milliseconds typical — so safe to call per-request
/// when servicing /server/config.
std::vector<AlsaPcmDevice> listAlsaPcmDevices();
