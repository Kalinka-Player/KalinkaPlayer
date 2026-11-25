#pragma once

#include "FirImpulse.h"
#include "FirRoomCorrection.h"

#include <list>
#include <map>
#include <mutex>

struct FirVariant {
  unsigned sourceRate;
  double automaticPreampDb;
  std::shared_ptr<FirRoomCorrection> filter;
  // 257 log-spaced magnitudes from 20 Hz to min(20 kHz, Nyquist).
  // Mono or identical stereo uses the left response for both channels.
  std::array<std::vector<float>, 2> responseDb;
};

class FirFilterBank {
public:
  std::string name;
  void add(FirImpulse impulse);
  std::vector<unsigned> suppliedRates() const;
  std::shared_ptr<const FirVariant> at(unsigned rate) const;

private:
  std::map<unsigned, FirImpulse> originals;
  // Only used at setup or when reading settings, never in the audio sample
  // loop.
  mutable std::mutex mutex;
  mutable std::list<std::pair<unsigned, std::shared_ptr<const FirVariant>>>
      recent;
};
