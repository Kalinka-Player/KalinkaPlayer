#pragma once

#include "AudioInfo.h"

#include <optional>
#include <span>
#include <string>

/// Whether @p setting is one of `dsd.pcm_rate`'s values.
bool isDsdPcmRate(const std::string &setting);

/**
 * @brief The multiple of the DSD family's base rate that `dsd.pcm_rate`
 * @p setting asks for; none for `auto`.
 * @throws std::invalid_argument for anything that is not one of its values.
 */
std::optional<unsigned> dsdPcmMultiple(const std::string &setting);

/**
 * @brief The PCM rate DSD at @p dsdRate converts to on an output that takes
 * @p device: @p wanted times the family's base, 44.1 or 48 kHz, when the
 * output takes it, else the first it takes of 4, 8, 2 and 1 times. With
 * nothing known of the output, @p wanted or 4 times, which opening it then
 * checks.
 * @return Nothing when the output takes no rate of the family at all.
 */
std::optional<unsigned>
dsdPcmRate(unsigned dsdRate, std::span<const StreamAudioFormat> device,
           std::optional<unsigned> wanted = std::nullopt);

/// The rates DSD at @p dsdRate can convert to, for a message: "44.1, 88.2,
/// 176.4 or 352.8 kHz".
std::string dsdPcmRatesText(unsigned dsdRate);
