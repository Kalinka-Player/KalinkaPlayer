#include "dsp/peq/ParallelPeq.h"
#include "dsp/peq/RoomEq.h"

#if defined(__aarch64__)
#include <algorithm>
#include <cmath>
#include <complex>

namespace peq {
namespace {
using Complex = std::complex<long double>;
using Mode = ParallelPlan::Mode;
using Channel = ParallelPlan::Channel;

static_assert(sizeof(Mode) == 32 && sizeof(State) == 32);
static_assert(offsetof(State, z2) == 16);
using FilterGroup = void(const Mode *, double *, const double *, const double *,
                         double *, size_t, double);
extern "C" FilterGroup kalinka_peq_five_modes, kalinka_peq_four_modes;

// Evaluating about z = 1 retains precision for bass filters at high rates.
Complex polynomial(long double a, long double b, long double c, Complex t) {
  return (a * t + (2 * a + b)) * t + (a + b + c);
}

bool prepareChannel(Channel &channel, const Cascade &cascade, size_t lane) {
  const auto &sections = cascade.sections;
  size_t count = cascade.count;
  while (count && sections[count - 1].b0[lane] == 1 &&
         sections[count - 1].b1[lane] == sections[count - 1].a1[lane] &&
         sections[count - 1].b2[lane] == sections[count - 1].a2[lane])
    --count;
  channel.count = count;
  long double direct = 1, bound = 0;
  for (size_t i = 0; i < count; ++i) {
    const auto &s = sections[i];
    const long double a = -s.a1[lane], b = -s.a2[lane];
    const long double real = a / 2, imag2 = -b - real * real;
    if (imag2 <= 1e-10L)
      return false;
    const Complex pole(real, std::sqrt(imag2)), t = pole - 1.0L;
    const long double margin = 1 - std::abs(pole);
    if (margin < 1e-5L)
      return false;

    // H(z) = direct + sum(R / (z-p) + conjugate(R) / (z-conjugate(p))).
    Complex residue = polynomial(s.b0[lane], s.b1[lane], s.b2[lane], t) /
                      Complex(0, 2 * pole.imag());
    for (size_t j = 0; j < count; ++j) {
      if (i == j)
        continue;
      const auto &v = sections[j];
      const Complex den = polynomial(1, v.a1[lane], v.a2[lane], t);
      if (std::abs(den) < 1e-8L * std::norm(t))
        return false;
      residue *= polynomial(v.b0[lane], v.b1[lane], v.b2[lane], t) / den;
    }
    // Sum of absolute impulse-response bounds. Large cancelling modes are
    // better conditioned in the original second-order-section realization.
    bound += 2 * std::abs(residue) / margin;
    if (!(bound <= 32))
      return false;

    // R/(z-p) plus its conjugate has numerator c*z + d.
    channel.modes[i] = {double(b), double(a), double(2 * residue.real()),
                        double(-2 * (residue * std::conj(pole)).real())};
    direct *= s.b0[lane];
  }
  channel.direct = double(direct);
  return std::isfinite(channel.direct);
}

} // namespace

std::optional<ParallelPlan> prepareParallel(const Cascade &cascade,
                                            bool sharedStereo) {
  ParallelPlan plan;
  if (!prepareChannel(plan.channels[0], cascade, 0))
    return std::nullopt;
  if (sharedStereo)
    plan.channels[1] = plan.channels[0];
  else if (!prepareChannel(plan.channels[1], cascade, 1))
    return std::nullopt;
  return plan;
}

namespace {
inline Stereo &coordinates(State &state, size_t lane) {
  return lane == 0 ? state.z1 : state.z2;
}

void runChannel(const Channel &channel, State *state, size_t lane, double *out,
                size_t frames) {
  if (!channel.count)
    return;
  alignas(64) double partial[kPlanarFrames];
  while (frames) {
    const size_t n = std::min(frames, kPlanarFrames);
    const size_t bulk = n - n % 10;
    // Unused modes have zero residues, including the end of the final group.
    static_assert(kBands % 5 == 0);
    if (bulk)
      for (size_t m = 0; m < channel.count; m += 5) {
        double *dest = m + 5 >= channel.count ? out : partial;
        auto filter = channel.count - m == 4 ? kalinka_peq_four_modes
                                             : kalinka_peq_five_modes;
        filter(&channel.modes[m], coordinates(state[m], lane).data(), out,
               m ? partial : nullptr, dest, bulk, channel.direct);
      }
    for (size_t i = bulk; i < n; ++i) {
      const double x = out[i];
      double sum = channel.direct * x;
      for (size_t m = 0; m < channel.count; ++m) {
        const auto &c = channel.modes[m];
        auto &h = coordinates(state[m], lane);
        sum = std::fma(c.c, h[0], std::fma(c.d, h[1], sum));
        h = {std::fma(c.a, h[0], std::fma(c.b, h[1], x)), h[0]};
      }
      out[i] = sum;
    }
    frames -= n;
    out += n;
  }
}
} // namespace

void filterParallel(const ParallelPlan &plan, State *history, double *left,
                    double *right, size_t frames) {
  runChannel(plan.channels[0], history, 0, left, frames);
  if (right)
    runChannel(plan.channels[1], history, 1, right, frames);
}
} // namespace peq
#endif
