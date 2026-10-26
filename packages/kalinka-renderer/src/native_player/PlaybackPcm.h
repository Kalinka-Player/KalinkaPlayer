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
  bool iec958 =
      false; ///< Framing has 24 payload bits and caches channel status.

  /// The payload precision, which can be smaller than ALSA's sample container.
  int significantBits(const snd_pcm_hw_params_t *params) const;
};

/// @brief Open @p device for playback, with snd_pcm_open()'s @p mode flags.
///
/// Opens the selected name unchanged. HDMI framing is direct only when its
/// expanded ALSA configuration reaches hardware through transparent plugins.
/// @return the open PCM, or the negative ALSA error code.
std::expected<PlaybackPcm, int> openPlayback(const std::string &device,
                                             int mode);

/// Why @p device did not open, from openPlayback()'s negative @p error: in
/// snd_strerror()'s words, unless the error says more about an HDMI port.
std::string openFailure(const std::string &device, int error);

/// Inspect an HDMI definition without opening hardware; unknown plugins fail
/// closed.
/// @note The returned handle is null. The caller retains ownership of config.
PlaybackPcm inspectHdmiRoute(snd_config_t *config, const std::string &device);

/// Inspect the current ALSA HDMI definition without acquiring the audio device.
PlaybackPcm inspectHdmiRoute(const std::string &device);

/// Open against a supplied ALSA configuration, with the same checks as
/// openPlayback.
/// @note The caller retains ownership of config.
std::expected<PlaybackPcm, int> openPlayback(const std::string &device,
                                             int mode, snd_config_t *config);
