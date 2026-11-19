#include "dsp/dsd/DsdGainSetting.h"

#include <stdexcept>

double dsdGainDb(const std::string &setting) {
  if (setting == "safe")
    return 2.8;
  if (setting == "0")
    return 0.0;
  if (setting == "6")
    return 6.0;
  throw std::invalid_argument("unknown DSD gain '" + setting + "'");
}
