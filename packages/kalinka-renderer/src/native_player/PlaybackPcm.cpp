#include "PlaybackPcm.h"

#include <algorithm>
#include <iterator>

namespace {

// What the renderer hands a card.
constexpr snd_pcm_format_t kPcmFormats[] = {
    SND_PCM_FORMAT_S16_LE, SND_PCM_FORMAT_S24_LE, SND_PCM_FORMAT_S24_3LE,
    SND_PCM_FORMAT_S32_LE};

bool framedOnly(snd_pcm_t *pcm) {
  snd_pcm_hw_params_t *params = nullptr;
  snd_pcm_hw_params_alloca(&params);
  if (snd_pcm_hw_params_any(pcm, params) < 0)
    return false;
  snd_pcm_format_mask_t *formats = nullptr;
  snd_pcm_format_mask_alloca(&formats);
  snd_pcm_hw_params_get_format_mask(params, formats);
  return onlyIec958Subframes(formats);
}

} // namespace

bool onlyIec958Subframes(const snd_pcm_format_mask_t *formats) {
  return snd_pcm_format_mask_test(formats, SND_PCM_FORMAT_IEC958_SUBFRAME_LE) &&
         std::none_of(std::begin(kPcmFormats), std::end(kPcmFormats),
                      [formats](snd_pcm_format_t format) {
                        return snd_pcm_format_mask_test(formats, format);
                      });
}

std::string iec958FramingOf(int card, int device) {
  return "hdmi:CARD=" + std::to_string(card) +
         ",DEV=" + std::to_string(device) + ",AES3=0x01";
}

std::expected<PlaybackPcm, int> openPlayback(const std::string &device,
                                             int mode) {
  snd_pcm_t *pcm = nullptr;
  int err = snd_pcm_open(&pcm, device.c_str(), SND_PCM_STREAM_PLAYBACK, mode);
  if (err < 0)
    return std::unexpected(err);
  // Anything but a raw hw handle has a plugin of its own before the card.
  if (snd_pcm_type(pcm) != SND_PCM_TYPE_HW)
    return PlaybackPcm{pcm, DeviceAccess::Shared};
  if (!framedOnly(pcm))
    return PlaybackPcm{pcm, DeviceAccess::Exclusive};

  snd_pcm_info_t *info = nullptr;
  snd_pcm_info_alloca(&info);
  err = snd_pcm_info(pcm, info);
  const std::string framing =
      err < 0
          ? std::string()
          : iec958FramingOf(snd_pcm_info_get_card(info),
                            static_cast<int>(snd_pcm_info_get_device(info)));
  snd_pcm_close(pcm);
  if (err < 0)
    return std::unexpected(err);
  err = snd_pcm_open(&pcm, framing.c_str(), SND_PCM_STREAM_PLAYBACK, mode);
  if (err < 0)
    return std::unexpected(err);
  return PlaybackPcm{pcm, DeviceAccess::Exclusive};
}
