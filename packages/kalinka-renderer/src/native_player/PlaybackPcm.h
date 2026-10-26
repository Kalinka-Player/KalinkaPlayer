#pragma once

#include "AudioInfo.h"

#include <alsa/asoundlib.h>

#include <expected>
#include <string>

/// @brief An ALSA playback PCM and how directly it reaches the card.
/// @note The caller owns @c handle and closes it with snd_pcm_close().
struct PlaybackPcm {
  snd_pcm_t *handle = nullptr;
  DeviceAccess access = DeviceAccess::Unknown;
};

/// @brief Open @p device for playback, with snd_pcm_open()'s @p mode flags.
///
/// A card device that takes only IEC958 subframes, as the Pi's HDMI ports
/// do, is opened through the card's IEC958 framing instead. The framing
/// carries the top 24 bits of every sample unchanged and mixes nothing in, so
/// the device stays exclusive.
/// @return the open PCM, or the negative ALSA error code.
std::expected<PlaybackPcm, int> openPlayback(const std::string &device,
                                             int mode);

/// Whether a card device taking @p formats plays only through IEC958 framing.
bool onlyIec958Subframes(const snd_pcm_format_mask_t *formats);

/// The ALSA name of the HDMI framing of @p device on @p card, which fills in
/// the sample rate of its channel status itself.
std::string iec958FramingOf(int card, int device);
