#include "dsp/peq/ParallelPeq.h"
#include "dsp/peq/RoomEq.h"

#if defined(__aarch64__)
#include <algorithm>
#include <boost/container/static_vector.hpp>
#include <cmath>
#include <limits>
#include <span>

namespace peq {
namespace {
using Mode = ParallelPlan::Mode;
using Bank = ParallelPlan::Bank;
using Channel = ParallelPlan::Channel;
using Indices = boost::container::static_vector<size_t, kBands>;

static_assert(sizeof(Mode) == 32 && sizeof(State) == 32);
static_assert(offsetof(State, z2) == 16);
using FilterGroup = void(const Mode *, double *, const double *, const double *,
                         double *, size_t, double);
extern "C" FilterGroup kalinka_peq_five_modes, kalinka_peq_four_modes;

constexpr size_t kGroup = 5;

/// One channel of a section as N(t) / D(t) with t = z - 1, which retains
/// precision for bass filters at high rates: N = n2 t² + n1 t + n0 and
/// D = d2 t² + e1 t + e0. A first-order section keeps its one true pole, with
/// d2 = 0, where the same section as a biquad would add a pole at z = 0 that
/// every first-order section shares.
struct Shifted {
  long double n2, n1, n0, d2, e1, e0;
  bool firstOrder() const { return d2 == 0; }
};

bool firstOrder(const Section &s, size_t lane) {
  return s.b2[lane] == 0 && s.a2[lane] == 0;
}

Shifted shifted(const Section &s, size_t lane) {
  const long double b0 = s.b0[lane], b1 = s.b1[lane], a1 = s.a1[lane];
  if (firstOrder(s, lane))
    return {0, b0, b0 + b1, 0, 1, 1 + a1};
  return {b0, 2 * b0 + b1, b0 + b1 + s.b2[lane],
          1,  2 + a1,      1 + a1 + s.a2[lane]};
}

/// u1 t + u0: a polynomial reduced by one section's denominator, which is as
/// much as its value at that section's poles, whether they are complex, real
/// or one pole twice. By a first-order denominator u1 is zero and u0 is the
/// value at its pole.
struct Reduced {
  long double u1, u0;
};

Reduced reduce(long double q2, long double q1, long double q0,
               const Shifted &by) {
  if (by.firstOrder())
    return {0, (q1 - q2 * by.e0) * -by.e0 + q0};
  return {q1 - by.e1 * q2, q0 - by.e0 * q2};
}

Reduced multiply(Reduced u, Reduced v, const Shifted &by) {
  const long double square = u.u1 * v.u1;
  return {u.u1 * v.u0 + u.u0 * v.u1 - by.e1 * square,
          u.u0 * v.u0 - by.e0 * square};
}

Reduced numerator(const Shifted &of, const Shifted &by) {
  return reduce(of.n2, of.n1, of.n0, by);
}

/// The whole of section @p of, its numerator over its denominator, reduced by
/// @p by. Not finite where the two share a pole.
Reduced response(const Shifted &of, const Shifted &by) {
  const Reduced d = reduce(of.d2, of.e1, of.e0, by);
  const long double norm =
      d.u0 * d.u0 - by.e1 * d.u0 * d.u1 + by.e0 * d.u1 * d.u1;
  return multiply(numerator(of, by),
                  {-d.u1 / norm, (d.u0 - by.e1 * d.u1) / norm}, by);
}

/// A section's poles about z = 1: a conjugate pair at `centre` ± i `spread`,
/// or two real ones at `centre` ± `spread`. A first-order section has its one
/// pole twice over, the second copy as far inside the unit circle as z = 0.
struct Poles {
  bool conjugate;
  long double centre, spread;
  /// How far inside the unit circle each lies.
  std::array<long double, 2> margin;
};

Poles poles(const Shifted &s) {
  if (s.firstOrder())
    return {false, -s.e0, 0, {1 - std::abs(1 - s.e0), 1}};
  const long double disc = s.e1 * s.e1 - 4 * s.e0, centre = -s.e1 / 2;
  const long double spread = std::sqrt(std::abs(disc)) / 2;
  if (disc < 0) {
    const long double margin = 1 - std::hypot(1 + centre, spread);
    return {true, centre, spread, {margin, margin}};
  }
  return {
      false,
      centre,
      spread,
      {1 - std::abs(1 + centre + spread), 1 - std::abs(1 + centre - spread)}};
}

/// A bound on the absolute sum of the impulse response of @p mode over the
/// denominator with poles @p at.
long double weight(Reduced mode, const Poles &at) {
  const long double lead = std::abs(mode.u1);
  std::array<long double, 2> value;
  if (at.conjugate)
    value.fill(std::hypot(mode.u1 * at.centre + mode.u0, mode.u1 * at.spread));
  else
    value = {std::abs(mode.u1 * (at.centre + at.spread) + mode.u0),
             std::abs(mode.u1 * (at.centre - at.spread) + mode.u0)};
  const long double both = at.margin[0] * at.margin[1];
  // Taking one pole first bounds close and repeated poles, where partial
  // fractions cancel. Partial fractions bound poles that stand apart.
  const long double chained = std::min(lead / at.margin[0] + value[1] / both,
                                       lead / at.margin[1] + value[0] / both);
  const long double apart =
      (value[0] / at.margin[0] + value[1] / at.margin[1]) / (2 * at.spread);
  return at.spread > 0 ? std::min(apart, chained) : chained;
}

void copyLane(Section &to, const Section &from, size_t lane) {
  to.b0[lane] = from.b0[lane];
  to.b1[lane] = from.b1[lane];
  to.b2[lane] = from.b2[lane];
  to.a1[lane] = from.a1[lane];
  to.a2[lane] = from.a2[lane];
}

/// Sections kept as the modes of one bank, with each one's numerator by its
/// place in the cascade, and the candidates that had to be left out.
struct Split {
  Indices modes, rest;
  std::array<Reduced, kBands> numerator{};
};

/// One channel of a cascade, ready to be divided between modes and series.
class Lane {
public:
  Lane(const Cascade &cascade, size_t lane);

  /// Sections that can be modes, and those too close to the unit circle.
  Indices expandable, fixed;

  /// Keeps as modes those of @p candidates that expand accurately together,
  /// giving up the heaviest until they do and number at most @p mostModes.
  Split split(std::span<const size_t> candidates,
              size_t mostModes = kBands) const;

private:
  std::array<Shifted, kBands> sections;
  std::array<Poles, kBands> roots;
  /// across[i][j] is section j reduced by section i.
  std::array<std::array<Reduced, kBands>, kBands> across;
};

Lane::Lane(const Cascade &cascade, size_t lane) {
  size_t count = cascade.count;
  const auto &all = cascade.sections;
  while (count && all[count - 1].b0[lane] == 1 &&
         all[count - 1].b1[lane] == all[count - 1].a1[lane] &&
         all[count - 1].b2[lane] == all[count - 1].a2[lane])
    --count;
  for (size_t i = 0; i < count; ++i) {
    sections[i] = shifted(all[i], lane);
    roots[i] = poles(sections[i]);
    const bool mode = std::min(roots[i].margin[0], roots[i].margin[1]) >= 1e-5L;
    (mode ? expandable : fixed).push_back(i);
  }
  for (const size_t i : expandable)
    for (const size_t j : expandable)
      if (j != i)
        across[i][j] = response(sections[j], sections[i]);
}

Split Lane::split(std::span<const size_t> candidates, size_t mostModes) const {
  Split out;
  out.modes.assign(candidates.begin(), candidates.end());
  while (!out.modes.empty()) {
    // H(z) = direct + the sum of each mode's numerator over its denominator,
    // and the bound is the sum of their absolute impulse responses.
    long double bound = 0, heaviest = 0;
    auto worst = out.modes.begin();
    for (auto mode = out.modes.begin(); mode != out.modes.end(); ++mode) {
      const size_t i = *mode;
      Reduced product = numerator(sections[i], sections[i]);
      for (const size_t j : out.modes)
        if (j != i)
          product = multiply(product, across[i][j], sections[i]);
      out.numerator[i] = product;
      long double share = weight(product, roots[i]);
      if (!std::isfinite(share))
        share = std::numeric_limits<long double>::infinity();
      bound += share;
      if (share >= heaviest) {
        heaviest = share;
        worst = mode;
      }
    }
    if (bound <= 32 && out.modes.size() <= mostModes)
      break;
    // Large cancelling modes are better conditioned as second-order sections.
    out.rest.push_back(*worst);
    out.modes.erase(worst);
  }
  return out;
}

/// One channel divided into banks of modes and sections left in series.
struct Arrangement {
  boost::container::static_vector<Split, 2> banks;
  Indices serial;

  size_t modes() const {
    size_t total = 0;
    for (const Split &bank : banks)
      total += bank.modes.size();
    return total;
  }

  /// In tenths of a group of five modes. A pair of sections in series costs
  /// about a group on a Pi 4 and two thirds of one on a Pi 5, and the first of
  /// a pair most of that.
  unsigned cost() const {
    unsigned total = 10 * (serial.size() / 2) + 9 * (serial.size() % 2);
    for (const Split &bank : banks)
      total += 10 * ((bank.modes.size() + kGroup - 1) / kGroup);
    return total;
  }
};

/// The modes of @p first as one bank, then @p pool as a second where three or
/// more of it expand together, and in series where they do not.
Arrangement arrangement(const Lane &lane, const Split &first,
                        std::span<const size_t> pool) {
  Arrangement out;
  out.serial = lane.fixed;
  out.banks.push_back(first);
  const Split second = lane.split(pool);
  if (second.modes.size() >= 3) {
    out.banks.push_back(second);
    pool = second.rest;
  }
  out.serial.insert(out.serial.end(), pool.begin(), pool.end());
  return out;
}

/// @p regroup also tries whole groups of five in the first bank, whose
/// stragglers then join what it could not hold, and keeps the cheaper.
Arrangement arrange(const Lane &lane, bool regroup) {
  const Split whole = lane.split(lane.expandable);
  const Arrangement natural = arrangement(lane, whole, whole.rest);
  const size_t stragglers = whole.modes.size() % kGroup;
  if (!regroup || whole.modes.size() < kGroup || !stragglers)
    return natural;
  // With nothing left out, only a lone straggler costs less in series.
  if (whole.rest.empty() && stragglers > 1)
    return natural;

  Indices pool = whole.rest, kept = whole.modes;
  const size_t unresolved = lane.split(pool).rest.size();
  for (size_t at = kept.size(), moved = 0; at-- > 0 && moved < stragglers;) {
    pool.push_back(kept[at]);
    if (lane.split(pool).rest.size() == unresolved) {
      kept.erase(kept.begin() + at);
      ++moved;
    } else {
      pool.pop_back();
    }
  }
  if (kept.size() % kGroup)
    return natural;
  const Split first = lane.split(kept);
  pool.insert(pool.end(), first.rest.begin(), first.rest.end());
  const Arrangement regrouped = arrangement(lane, first, pool);
  return regrouped.cost() < natural.cost() ? regrouped : natural;
}

} // namespace

std::optional<ParallelPlan> prepareParallel(Cascade &cascade,
                                            bool sharedStereo) {
  const Lane left(cascade, 0);
  const std::optional<Lane> right =
      sharedStereo ? std::nullopt
                   : std::optional<Lane>(std::in_place, cascade, 1);
  std::array<Arrangement, 2> lanes;
  size_t modes = 0, serial = 0;
  // Both channels keep their modes in the same history slots, ahead of the
  // sections in series, and regrouping can ask for more slots than there are.
  for (const bool regroup : {true, false}) {
    lanes[0] = arrange(left, regroup);
    lanes[1] = right ? arrange(*right, regroup) : lanes[0];
    modes = std::max(lanes[0].modes(), lanes[1].modes());
    serial = std::max(lanes[0].serial.size(), lanes[1].serial.size());
    if (modes + serial <= kBands)
      break;
  }
  // Fewer than three modes cost more than their sections.
  if (modes < 3 || modes + serial > kBands)
    return std::nullopt;

  ParallelPlan plan;
  plan.replaced = modes;
  std::array<Section, kBands> arranged{};
  for (size_t lane = 0; lane < lanes.size(); ++lane) {
    Channel &channel = plan.channels[lane];
    size_t at = 0, filled = 0;
    for (const Split &chosen : lanes[lane].banks) {
      if (chosen.modes.empty())
        continue;
      Bank &bank = channel.banks[filled++];
      long double direct = 1;
      bank.first = at;
      bank.count = chosen.modes.size();
      for (const size_t i : chosen.modes) {
        const Section &section = cascade.sections[i];
        const Reduced top = chosen.numerator[i];
        // (u1 t + u0) / D is (c z + d) / D with t = z - 1, and a first-order
        // u0 / (z - p) is u0 z over z (z - p).
        const bool one = firstOrder(section, lane);
        channel.modes[at] = {-section.a2[lane], -section.a1[lane],
                             double(one ? top.u0 : top.u1),
                             double(one ? 0 : top.u0 - top.u1)};
        direct *= section.b0[lane];
        copyLane(arranged[at++], section, lane);
      }
      bank.direct = double(direct);
      if (!std::isfinite(bank.direct))
        return std::nullopt;
    }
    size_t behind = modes;
    for (const size_t i : lanes[lane].serial)
      copyLane(arranged[behind++], cascade.sections[i], lane);
  }
  cascade.sections = arranged;
  cascade.count = modes + serial;
  return plan;
}

namespace {
inline Stereo &coordinates(State &state, size_t lane) {
  return lane == 0 ? state.z1 : state.z2;
}

/// @p frames of @p out through one bank, at most kPlanarFrames of them.
void runBank(const Mode *modes, const Bank &bank, State *state, size_t lane,
             double *out, double *partial, size_t frames) {
  const size_t bulk = frames - frames % 10;
  if (bulk)
    for (size_t m = 0; m < bank.count; m += kGroup) {
      const size_t live = std::min(bank.count - m, kGroup);
      double *dest = m + kGroup >= bank.count ? out : partial;
      auto filter = live == 4 ? kalinka_peq_four_modes : kalinka_peq_five_modes;
      // A short group runs its padding too. On copies it reads no mode of the
      // next bank and leaves alone the history slots behind its own, which
      // belong to that bank or to the sections in series.
      alignas(64) std::array<Mode, kGroup> padded;
      std::array<State, kGroup> scratch;
      const Mode *group = modes + m;
      State *history = state + m;
      if (live < 4) {
        padded = {};
        scratch = {};
        std::copy_n(group, live, padded.begin());
        std::copy_n(history, live, scratch.begin());
        group = padded.data();
        history = scratch.data();
      }
      filter(group, coordinates(*history, lane).data(), out,
             m ? partial : nullptr, dest, bulk, bank.direct);
      if (history == scratch.data())
        for (size_t k = 0; k < live; ++k)
          coordinates(state[m + k], lane) = coordinates(scratch[k], lane);
    }
  for (size_t i = bulk; i < frames; ++i) {
    const double x = out[i];
    double sum = bank.direct * x;
    for (size_t m = 0; m < bank.count; ++m) {
      const auto &c = modes[m];
      auto &h = coordinates(state[m], lane);
      sum = std::fma(c.c, h[0], std::fma(c.d, h[1], sum));
      h = {std::fma(c.a, h[0], std::fma(c.b, h[1], x)), h[0]};
    }
    out[i] = sum;
  }
}

void runChannel(const Channel &channel, State *state, size_t lane, double *out,
                size_t frames) {
  if (!channel.banks.front().count)
    return;
  alignas(64) double partial[kPlanarFrames];
  while (frames) {
    const size_t n = std::min(frames, kPlanarFrames);
    for (const Bank &bank : channel.banks)
      if (bank.count)
        runBank(&channel.modes[bank.first], bank, state + bank.first, lane, out,
                partial, n);
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
