#include "dsp/fir/SymmetricFir.h"

#include "dsp/fir/FirKernels.h"

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
