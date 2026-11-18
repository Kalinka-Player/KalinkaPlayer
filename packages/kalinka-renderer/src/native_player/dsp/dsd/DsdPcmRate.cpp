#include "dsp/dsd/DsdPcmRate.h"

#include "AudioSampleFormat.h"

#include <algorithm>
#include <array>
#include <stdexcept>

namespace {
constexpr std::array<unsigned, 4> kMultiples{1, 2, 4, 8};
// Automatic's order: the most common DAC rate first, then the higher one.
constexpr std::array<unsigned, 4> kAutomatic{4, 8, 2, 1};

std::string settingFor(unsigned multiple) {
  return std::to_string(multiple) + "x";
}

std::string kiloHertz(unsigned rate) {
  const unsigned tenths = rate / 100;
  std::string text = std::to_string(tenths / 10);
  if (tenths % 10)
    text += "." + std::to_string(tenths % 10);
  return text;
}
} // namespace

unsigned dsdBaseRate(unsigned dsdRate) {
  return dsdRate % 44100 == 0 ? 44100 : 48000;
}

std::optional<unsigned> dsdPcmMultiple(const std::string &setting) {
  if (setting == "auto")
    return std::nullopt;
  for (const unsigned multiple : kMultiples)
    if (setting == settingFor(multiple))
      return multiple;
  throw std::invalid_argument("unknown DSD PCM rate '" + setting + "'");
}

std::optional<unsigned>
dsdPcmRate(unsigned dsdRate, std::span<const StreamAudioFormat> device,
           std::optional<unsigned> wanted) {
  const unsigned base = dsdBaseRate(dsdRate);
  if (device.empty())
    return base * wanted.value_or(4);
  const auto taken = [&device, base](unsigned multiple) {
    return std::ranges::any_of(device, [rate = base * multiple](
                                           const StreamAudioFormat &format) {
      return !isDsd(format.sampleFormat) && format.sampleRate == rate &&
             format.channels == kPcmDeviceChannels;
    });
  };
  if (wanted && taken(*wanted))
    return base * *wanted;
  for (const unsigned multiple : kAutomatic)
    if (taken(multiple))
      return base * multiple;
  return std::nullopt;
}

std::string dsdPcmRatesText(unsigned dsdRate) {
  const unsigned base = dsdBaseRate(dsdRate);
  std::string text;
  for (size_t i = 0; i < kMultiples.size(); ++i) {
    if (i)
      text += i + 1 == kMultiples.size() ? " or " : ", ";
    text += kiloHertz(base * kMultiples[i]);
  }
  return text + " kHz";
}
