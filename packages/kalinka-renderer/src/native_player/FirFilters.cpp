#include "FirFilters.h"

#include "FirKernels.h"

#include <algorithm>

SymmetricFir::SymmetricFir(const double *half, size_t halfSize, size_t capacity,
                           FirPath path)
    : taps(half, half + halfSize), history(2 * (halfSize - 1)),
      capacity_(capacity), path(path),
      buffer(history + capacity + fir_kernels::kSlack) {}

void SymmetricFir::filter(size_t count, double *out) {
  const double *x = input();
  const size_t centre = taps.size() - 1;
  if (path == FirPath::Vector) {
    fir_kernels::symmetric(taps.data(), taps.size(), x, count, out);
  } else {
    for (size_t t = 0; t < count; ++t) {
      const double *newest = x + t;
      double sum = taps[centre] * newest[-ptrdiff_t(centre)];
      for (size_t j = 0; j < centre; ++j)
        sum += taps[j] *
               (newest[-ptrdiff_t(j)] + newest[-ptrdiff_t(2 * centre - j)]);
      out[t] = sum;
    }
  }
  std::copy(buffer.begin() + count, buffer.begin() + count + history,
            buffer.begin());
}

void SymmetricFir::reset() { std::ranges::fill(buffer, 0.0); }

HalfBandDecimator::HalfBandDecimator(const double *side, size_t sides,
                                     size_t capacity, FirPath path)
    : side(side, side + sides), history(4 * sides - 2), capacity_(capacity),
      path(path), buffer(history + capacity + fir_kernels::kSlack),
      centres(maxOutputs(capacity) + fir_kernels::kSlack),
      others(maxOutputs(capacity) + 2 * sides + fir_kernels::kSlack) {}

size_t HalfBandDecimator::decimate(size_t count, double *out) {
  const size_t sides = side.size();
  const size_t centre = history / 2;
  const size_t first = pending ? 0 : 1;
  const size_t outputs = count > first ? (count - first + 1) / 2 : 0;
  // Output m's centre sample is x[2m]; its side samples sit at odd offsets.
  const double *x = input() + first - centre;
  if (path == FirPath::Vector) {
    for (size_t m = 0; m < outputs; ++m)
      centres[m] = x[2 * m];
    for (size_t j = 0; j < outputs + 2 * sides - 1; ++j)
      others[j] = x[2 * ptrdiff_t(j) - 2 * ptrdiff_t(sides) + 1];
    fir_kernels::halfBand(side.data(), sides, centres.data(),
                          others.data() + sides - 1, outputs, out);
  } else {
    for (size_t m = 0; m < outputs; ++m) {
      const double *at = x + 2 * m;
      double sum = 0.5 * at[0];
      for (size_t i = 0; i < sides; ++i)
        sum += side[i] * (at[-ptrdiff_t(2 * i + 1)] + at[2 * i + 1]);
      out[m] = sum;
    }
  }
  std::copy(buffer.begin() + count, buffer.begin() + count + history,
            buffer.begin());
  pending = (pending + count) % 2;
  return outputs;
}

void HalfBandDecimator::reset() {
  std::ranges::fill(buffer, 0.0);
  pending = false;
}
