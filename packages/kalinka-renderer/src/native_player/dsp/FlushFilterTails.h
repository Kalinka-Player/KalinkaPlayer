#pragma once

#include <cstdint>
#if defined(__SSE2__)
#include <xmmintrin.h>
#endif

namespace dsp {
// Confine flush-to-zero to the filter; callers keep their floating-point mode.
class FlushFilterTails {
#if defined(__SSE2__)
  unsigned saved = _mm_getcsr();
  static constexpr unsigned flush = (1u << 15) | (1u << 6); // FTZ and DAZ
public:
  FlushFilterTails() { _mm_setcsr(saved | flush); }
  ~FlushFilterTails() { _mm_setcsr(saved); }
#elif defined(__aarch64__)
  uint64_t saved;

public:
  FlushFilterTails() {
    asm volatile("mrs %0, fpcr" : "=r"(saved));
    asm volatile("msr fpcr, %0" ::"r"(saved | (1ull << 24)) : "memory");
  }
  ~FlushFilterTails() { asm volatile("msr fpcr, %0" ::"r"(saved) : "memory"); }
#elif defined(__arm__) && defined(__ARM_FP)
  // A VFP core such as the Pi 1's traps every subnormal to kernel support code.
  uint32_t saved;

public:
  FlushFilterTails() {
    asm volatile("vmrs %0, fpscr" : "=r"(saved));
    asm volatile("vmsr fpscr, %0" ::"r"(saved | (1u << 24)) : "memory");
  }
  ~FlushFilterTails() { asm volatile("vmsr fpscr, %0" ::"r"(saved) : "memory"); }
#endif
};
} // namespace dsp
