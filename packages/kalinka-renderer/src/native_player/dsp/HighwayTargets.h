#pragma once

// SVE stays off until it measures faster on hardware we have (dsp-design.md).
// Include before <hwy/foreach_target.h> in every kernel source.
#ifndef HWY_DISABLED_TARGETS
#define HWY_DISABLED_TARGETS (HWY_SVE | HWY_SVE2 | HWY_SVE_256 | HWY_SVE2_128)
#endif
