#include "DsdBlockKernels.h"

#include <algorithm>

#include "dsp/HighwayTargets.h"

#undef HWY_TARGET_INCLUDE
#define HWY_TARGET_INCLUDE "DsdBlockKernels.cpp"
#include <hwy/foreach_target.h> // IWYU pragma: keep
#include <hwy/highway.h>

HWY_BEFORE_NAMESPACE();
namespace dsd_block_kernels {
namespace HWY_NAMESPACE {
namespace hn = hwy::HWY_NAMESPACE;

using D = hn::ScalableTag<uint8_t>;
using V = hn::Vec<D>;

template <bool kReverse> HWY_INLINE V Load(D d, const uint8_t *from) {
  const V v = hn::LoadU(d, from);
  if constexpr (kReverse)
    return hn::ReverseBits(v);
  else
    return v;
}

/// A vector's worth of frames from blocks @p stride apart.
template <unsigned kChannels, bool kReverse>
HWY_INLINE void Frames(D d, const uint8_t *from, size_t stride, uint8_t *out) {
  if constexpr (kChannels == 1)
    hn::StoreU(Load<kReverse>(d, from), d, out);
  else
    hn::StoreInterleaved2(Load<kReverse>(d, from),
                          Load<kReverse>(d, from + stride), d, out);
}

template <unsigned kChannels, bool kReverse>
HWY_INLINE void Blocks(const uint8_t *blocks, size_t stride, size_t bytes,
                       uint8_t *out) {
  const D d;
  const size_t lanes = hn::Lanes(d);
  size_t b = 0;
  for (; b + lanes <= bytes; b += lanes)
    Frames<kChannels, kReverse>(d, blocks + b, stride, out + kChannels * b);
  if (b == bytes)
    return;
  // Padded to a whole vector, the tail takes the same path as the rest.
  constexpr size_t kLanes = hn::MaxLanes(D());
  HWY_ALIGN uint8_t in[kChannels * kLanes] = {};
  HWY_ALIGN uint8_t to[kChannels * kLanes];
  const size_t left = bytes - b;
  for (unsigned c = 0; c < kChannels; ++c)
    std::copy_n(blocks + c * stride + b, left, in + c * kLanes);
  Frames<kChannels, kReverse>(d, in, kLanes, to);
  std::copy_n(to, kChannels * left, out + kChannels * b);
}

void Interleave(const uint8_t *blocks, size_t stride, unsigned channels,
                size_t bytes, bool reverseBits, uint8_t *out) {
  if (channels == 1 && reverseBits)
    Blocks<1, true>(blocks, stride, bytes, out);
  else if (channels == 1)
    Blocks<1, false>(blocks, stride, bytes, out);
  else if (reverseBits)
    Blocks<2, true>(blocks, stride, bytes, out);
  else
    Blocks<2, false>(blocks, stride, bytes, out);
}

} // namespace HWY_NAMESPACE
} // namespace dsd_block_kernels
HWY_AFTER_NAMESPACE();

#if HWY_ONCE
namespace dsd_block_kernels {
HWY_EXPORT(Interleave);

void interleave(const uint8_t *blocks, size_t stride, unsigned channels,
                size_t bytes, bool reverseBits, uint8_t *out) {
  HWY_DYNAMIC_DISPATCH(Interleave)(blocks, stride, channels, bytes, reverseBits,
                                   out);
}
} // namespace dsd_block_kernels
#endif
