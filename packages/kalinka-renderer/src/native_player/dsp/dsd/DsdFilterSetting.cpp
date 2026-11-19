#include "dsp/dsd/DsdFilterSetting.h"

#include <stdexcept>

std::optional<DsdFilter> dsdFilterSetting(const std::string &setting) {
  if (setting == "auto")
    return std::nullopt;
  if (setting == "30k")
    return DsdFilter::Cut30k;
  if (setting == "50k")
    return DsdFilter::Cut50k;
  if (setting == "wide")
    return DsdFilter::Wide;
  throw std::invalid_argument("unknown DSD filter '" + setting + "'");
}

std::string dsdFilterText(DsdFilter filter) {
  switch (filter) {
  case DsdFilter::Cut30k:
    return "30 kHz filter";
  case DsdFilter::Cut50k:
    return "50 kHz filter";
  case DsdFilter::Wide:
    break;
  }
  return "wide filter";
}
