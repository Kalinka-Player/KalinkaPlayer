#pragma once

#include <string>

/**
 * @brief The gain in dB that `dsd.gain_db` @p setting raises DSD converted to
 * PCM by: `0`, `6`, or `safe`, which lands DSD's specified peak, 71 %
 * modulation, 0.1 dB under full scale.
 * @throws std::invalid_argument for anything that is not one of its values.
 */
double dsdGainDb(const std::string &setting);
