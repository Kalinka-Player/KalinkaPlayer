#include "dsp/output/OutputFade.h"

#include "dsp/output/GainRamp.h"

size_t fadeFrames(unsigned rate) {
  return GainRamp::framesFor(kFadeLength, rate);
}
