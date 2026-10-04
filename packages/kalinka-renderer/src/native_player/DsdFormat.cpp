#include "DsdFormat.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <map>
#include <mutex>
#include <stdexcept>

snd_pcm_format_t alsaFormat(AudioSampleFormat f) {
  switch (f) {
  case PCM16_LE:
    return SND_PCM_FORMAT_S16_LE;
  case PCM24_LE:
  case DOP24_LE:
    return SND_PCM_FORMAT_S24_LE;
  case PCM24_3LE:
  case DOP24_3LE:
    return SND_PCM_FORMAT_S24_3LE;
  case PCM32_LE:
  case DOP32_LE:
    return SND_PCM_FORMAT_S32_LE;
  case DSD_U8:
    return SND_PCM_FORMAT_DSD_U8;
  case DSD_U16_LE:
    return SND_PCM_FORMAT_DSD_U16_LE;
  case DSD_U32_LE:
    return SND_PCM_FORMAT_DSD_U32_LE;
  case DSD_U16_BE:
    return SND_PCM_FORMAT_DSD_U16_BE;
  case DSD_U32_BE:
    return SND_PCM_FORMAT_DSD_U32_BE;
  }
  return SND_PCM_FORMAT_UNKNOWN;
}

namespace {
std::string dsdRateName(unsigned rate) {
  for (unsigned base : {44100u, 48000u})
    if (rate % base == 0)
      return "DSD" + std::to_string(rate / base);
  return "DSD at " + std::to_string(rate) + " Hz";
}
} // namespace

bool isDsdMode(const std::string &mode) {
  return mode == "disabled" || mode == "auto" || mode == "native" ||
         mode == "dop";
}

unsigned dsdBitsPerFrame(AudioSampleFormat format) {
  if (!isDsd(format) || !sampleSize(format))
    throw std::invalid_argument("Not a DSD transport");
  return isDop(format) ? 16 : sampleSize(format) * 8;
}

OutputCapabilities probeOutput(const std::string &device) {
  static std::mutex mutex;
  static std::map<std::string, OutputCapabilities> cache;
  std::lock_guard lock(mutex);
  snd_pcm_t *pcm = nullptr;
  const int error =
      snd_pcm_open(&pcm, device.c_str(), SND_PCM_STREAM_PLAYBACK,
                   SND_PCM_NONBLOCK | SND_PCM_NO_AUTO_RESAMPLE |
                       SND_PCM_NO_AUTO_FORMAT | SND_PCM_NO_AUTO_CHANNELS);
  if (error < 0) {
    if (error == -EBUSY && cache.contains(device))
      return cache.at(device);
    return {DeviceAccess::Unknown, snd_strerror(error), {}};
  }
  OutputCapabilities result;
  result.access = snd_pcm_type(pcm) == SND_PCM_TYPE_HW ? DeviceAccess::Exclusive
                                                       : DeviceAccess::Shared;
  constexpr std::array rates{8000u,   11025u,   16000u,   22050u,   32000u,
                             44100u,  48000u,   64000u,   88200u,   96000u,
                             176400u, 192000u,  352800u,  384000u,  705600u,
                             768000u, 1411200u, 1536000u, 2822400u, 3072000u};
  snd_pcm_hw_params_t *params;
  snd_pcm_hw_params_alloca(&params);
  for (auto format : {PCM16_LE, PCM24_LE, PCM24_3LE, PCM32_LE, DSD_U8,
                      DSD_U16_LE, DSD_U32_LE, DSD_U16_BE, DSD_U32_BE}) {
    if (isDsd(format) && result.access != DeviceAccess::Exclusive)
      continue;
    for (unsigned channels : {1u, 2u}) {
      for (unsigned rate : rates) {
        if (snd_pcm_hw_params_any(pcm, params) < 0 ||
            snd_pcm_hw_params_set_rate_resample(pcm, params, 0) < 0 ||
            snd_pcm_hw_params_set_access(pcm, params,
                                         SND_PCM_ACCESS_MMAP_INTERLEAVED) < 0 ||
            snd_pcm_hw_params_set_channels(pcm, params, channels) < 0 ||
            snd_pcm_hw_params_set_format(pcm, params, alsaFormat(format)) < 0 ||
            snd_pcm_hw_params_set_rate(pcm, params, rate, 0) < 0)
          continue;
        const int significant = snd_pcm_hw_params_get_sbits(params);
        result.formats.push_back(
            {rate, channels,
             isDsd(format) ? 1u
                           : static_cast<unsigned>(std::max(0, significant)),
             format, isDsd(format) ? rate * dsdBitsPerFrame(format) : 0});
      }
    }
  }
  snd_pcm_close(pcm);
  cache[device] = result;
  return result;
}

void stampDopMarkers(std::span<uint8_t> output, unsigned channels,
                     AudioSampleFormat format, uint64_t &frame) {
  if (!isDop(format))
    return;
  const auto width = sampleSize(format);
  if (!channels || output.size() % (channels * width))
    throw std::invalid_argument("Incomplete DoP output frame");
  const size_t marker = format == DOP32_LE ? 3 : 2;
  for (size_t at = 0; at < output.size(); at += channels * width) {
    for (unsigned c = 0; c < channels; ++c) {
      auto *sample = output.data() + at + c * width;
      sample[marker] = (frame & 1) ? 0xfa : 0x05;
      if (format == DOP32_LE)
        sample[0] = 0;
      if (format == DOP24_LE)
        sample[3] = 0;
    }
    ++frame;
  }
}

StreamAudioFormat chooseDsdOutput(const OutputCapabilities &caps,
                                  const std::string &mode, unsigned rate,
                                  unsigned channels) {
  if (mode == "disabled")
    throw std::runtime_error(DSD_DISABLED_ERROR);
  if (!isDsdMode(mode))
    throw std::runtime_error("Invalid DSD output mode");
  if (caps.access == DeviceAccess::Shared)
    throw std::runtime_error(DSD_SHARED_OUTPUT_ERROR);
  if (caps.access != DeviceAccess::Exclusive)
    throw std::runtime_error("Output device unavailable: " + caps.error);
  for (const auto &f : caps.formats) {
    if (f.channels != channels)
      continue;
    if (mode != "dop" && isDsd(f.sampleFormat) && f.dsdSampleRate == rate)
      return f;
    if (mode == "dop" && !isDsd(f.sampleFormat) && f.bitsPerSample >= 24 &&
        rate % 16 == 0 && f.sampleRate == rate / 16) {
      auto format = f;
      switch (f.sampleFormat) {
      case PCM24_LE:
        format.sampleFormat = DOP24_LE;
        break;
      case PCM24_3LE:
        format.sampleFormat = DOP24_3LE;
        break;
      case PCM32_LE:
        format.sampleFormat = DOP32_LE;
        break;
      default:
        continue;
      }
      format.dsdSampleRate = rate;
      return format;
    }
  }
  throw std::runtime_error("This output cannot play " + dsdRateName(rate) +
                           (channels == 1 ? " mono" : " stereo") + " as " +
                           (mode == "dop" ? "DoP" : "native DSD"));
}

std::vector<uint8_t> packDsd(std::span<const uint8_t> input, unsigned channels,
                             AudioSampleFormat format, uint64_t firstFrame) {
  if (!channels || input.size() % channels)
    throw std::invalid_argument("Incomplete DSD channel frame");
  const unsigned count = dsdBitsPerFrame(format) / 8;
  const size_t frames = (input.size() / channels + count - 1) / count;
  const size_t width = sampleSize(format);
  std::vector<uint8_t> result(frames * channels * width, 0);
  for (size_t f = 0; f < frames; ++f) {
    for (unsigned c = 0; c < channels; ++c) {
      auto *dst = result.data() + (f * channels + c) * width;
      const unsigned offset = format == DOP32_LE ? 1 : 0;
      for (unsigned b = 0; b < count; ++b) {
        const auto index = (f * count + b) * channels + c;
        const bool bigEndian = format == DSD_U16_BE || format == DSD_U32_BE;
        dst[offset + (bigEndian ? b : count - 1 - b)] =
            index < input.size() ? input[index] : 0x69;
      }
      if (isDop(format))
        dst[offset + 2] = ((firstFrame + f) & 1) ? 0xfa : 0x05;
    }
  }
  return result;
}
