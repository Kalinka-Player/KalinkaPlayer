#pragma once

#include "dsp/dsd/DsdOptions.h"

#include <optional>
#include <string>

/**
 * @brief The filter `dsd.filter` @p setting asks for; none for `auto`.
 * @throws std::invalid_argument for anything that is not one of its values.
 */
std::optional<DsdFilter> dsdFilterSetting(const std::string &setting);

/// @p filter for a listener: "30 kHz filter".
std::string dsdFilterText(DsdFilter filter);
