#include "dsp/fir/HalfBandDecimator.h"

#include "dsp/fir/FirKernels.h"

#include <algorithm>

namespace {
constexpr size_t roundedUpToEven(size_t count) { return count + count % 2; }
} // namespace

HalfBandDecimator::HalfBandDecimator(const double *side, size_t sides,
                                     size_t capacity, FirPath path)
    : side(side, side + sides), capacity_(capacity), path(path),
      evenLead(roundedUpToEven(sides - 1)),
      oddLead(roundedUpToEven(2 * sides - 1)),
      buffer(kLead + capacity + fir_kernels::kSlack),
      evens(evenLead + maxOutputs(capacity) + fir_kernels::kSlack),
      odds(oddLead + maxOutputs(capacity) + fir_kernels::kSlack) {}

size_t HalfBandDecimator::decimate(size_t count, double *out) {
  const size_t sides = side.size();
  const double *x = input() - pending;
  const size_t total = count + pending;
  const size_t pairs = total / 2;
  double *newEvens = evens.data() + evenLead;
  double *newOdds = odds.data() + oddLead;
  for (size_t i = 0; i < pairs; ++i) {
    newEvens[i] = x[2 * i];
    newOdds[i] = x[2 * i + 1];
  }
  const double *centre = newEvens - (sides - 1);
  const double *other = newOdds - sides;
  if (path == FirPath::Vector) {
    fir_kernels::halfBand(side.data(), sides, centre, other, pairs, out);
  } else {
    for (size_t m = 0; m < pairs; ++m) {
      double sum = 0.5 * centre[m];
      for (size_t i = 0; i < sides; ++i)
        sum +=
            side[i] * (other[ptrdiff_t(m) - ptrdiff_t(i)] + other[m + i + 1]);
      out[m] = sum;
    }
  }
  if (pairs > 0) {
    std::copy(evens.begin() + pairs, evens.begin() + pairs + evenLead,
              evens.begin());
    std::copy(odds.begin() + pairs, odds.begin() + pairs + oddLead,
              odds.begin());
  }
  pending = total % 2;
  if (pending)
    input()[-1] = x[2 * pairs];
  return pairs;
}

void HalfBandDecimator::reset() {
  std::ranges::fill(buffer, 0.0);
  std::ranges::fill(evens, 0.0);
  std::ranges::fill(odds, 0.0);
  pending = false;
}
