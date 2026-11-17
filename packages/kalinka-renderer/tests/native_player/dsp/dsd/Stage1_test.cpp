#include "dsp/dsd/Stage1.h"

#include "dsp/dsd/DsdFilterTaps.h"
#include "dsp/dsd/DsdKernels.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <random>
#include <set>
#include <string>
#include <vector>

namespace {
constexpr size_t kCapacity = 64;
constexpr double kScale = 1.0 / double(1ull << dsd_filters::kStage1Shift);
constexpr uint8_t kIdle = 0x69;

/// The first stage 1 taps with @p groups above or at most 16, the most the
/// whole tables hold.
const dsd_filters::TapsInt &tapsWith(bool past16) {
  return *std::ranges::find_if(dsd_filters::kStage1, [past16](const auto &t) {
    return (t.size / 8 > 16) == past16;
  });
}

std::vector<uint8_t> randomBytes(size_t count, unsigned seed) {
  std::mt19937 random(seed);
  std::vector<uint8_t> bytes(count);
  for (auto &byte : bytes)
    byte = uint8_t(random());
  return bytes;
}

using PerChannel = std::vector<std::vector<double>>;

/**
 * Stage 1 by its definition: out[m] = scale × Σ taps[n] x[8m + 7 − n], the
 * levels x ±1 in time order, the idle pattern before the stream.
 */
PerChannel direct(const dsd_filters::TapsInt &taps,
                  const std::vector<uint8_t> &interleaved, unsigned channels,
                  DsdBitOrder order) {
  const size_t bytes = interleaved.size() / channels;
  const size_t before = taps.size;
  PerChannel out(channels);
  for (unsigned c = 0; c < channels; ++c) {
    std::vector<int> x;
    const auto push = [&x](uint8_t byte, bool msbFirst) {
      for (int i = 0; i < 8; ++i)
        x.push_back((byte >> (msbFirst ? 7 - i : i)) & 1 ? 1 : -1);
    };
    for (size_t i = 0; i < before / 8; ++i)
      push(kIdle, true);
    for (size_t m = 0; m < bytes; ++m)
      push(interleaved[m * channels + c], order == DsdBitOrder::MsbFirst);
    for (size_t m = 0; m < bytes; ++m) {
      int64_t sum = 0;
      for (size_t n = 0; n < taps.size; ++n)
        sum += taps.taps[n] * x[before + 8 * m + 7 - n];
      out[c].push_back(double(sum) * kScale);
    }
  }
  return out;
}

/// What @p stage makes of @p interleaved, fed in blocks of @p sizes, cycled.
PerChannel run(Stage1 &stage, const std::vector<uint8_t> &interleaved,
               unsigned channels, const std::vector<size_t> &sizes) {
  const size_t bytes = interleaved.size() / channels;
  PerChannel out(channels, std::vector<double>(bytes));
  std::vector<double *> targets(channels);
  size_t done = 0;
  for (size_t i = 0; done < bytes; ++i) {
    const size_t block = std::min(sizes[i % sizes.size()], bytes - done);
    for (unsigned c = 0; c < channels; ++c)
      targets[c] = out[c].data() + done;
    stage.process(interleaved.data() + done * channels, block, targets.data());
    done += block;
  }
  return out;
}

constexpr DsdKernel kKernels[] = {DsdKernel::Reference, DsdKernel::Sums,
                                  DsdKernel::ChannelPair, DsdKernel::Nibbles,
                                  DsdKernel::Rows};

std::string describe(DsdKernel kernel, bool past16, DsdBitOrder order,
                     unsigned channels) {
  static const char *names[] = {"reference", "sums", "channel pairs", "nibbles",
                                "rows"};
  return std::string(names[int(kernel)]) + (past16 ? ", half" : ", whole") +
         " tables, " + (order == DsdBitOrder::MsbFirst ? "MSB" : "LSB") +
         " first, " + std::to_string(channels) + " channels";
}
} // namespace

TEST(Stage1, EveryKernelIsTheSumOfItsTapsOverTheBits) {
  for (const bool past16 : {false, true}) {
    const auto &taps = tapsWith(past16);
    for (const auto order : {DsdBitOrder::MsbFirst, DsdBitOrder::LsbFirst}) {
      for (const unsigned channels : {1u, 2u, 3u}) {
        const auto in = randomBytes(300 * channels, channels);
        const auto expected = direct(taps, in, channels, order);
        for (const auto kernel : kKernels) {
          auto stage = makeStage1(kernel, {taps.taps, taps.size, kScale,
                                           channels, order, kCapacity});
          EXPECT_EQ(run(*stage, in, channels, {kCapacity, 1, 37, 7, 13}),
                    expected)
              << describe(kernel, past16, order, channels);
        }
      }
    }
  }
}

TEST(Stage1, RowsAreTheSumOfTheTapsForEveryFilterTheyAreBuiltFor) {
  // A round takes up to 7 pairs, so 1 to 15 bytes end a block at every point.
  std::vector<size_t> sizes{kCapacity, 37};
  for (size_t size = 1; size <= 15; ++size)
    sizes.push_back(size);
  std::set<size_t> tested;
  for (const auto &taps : dsd_filters::kStage1) {
    const size_t groups = taps.size / 8;
    if (!dsd_kernels::stage1RowsHandles(groups))
      continue;
    tested.insert(groups);
    for (const auto order : {DsdBitOrder::MsbFirst, DsdBitOrder::LsbFirst}) {
      for (const unsigned channels : {1u, 2u, 3u}) {
        const auto in =
            randomBytes(400 * channels, unsigned(groups) * channels);
        auto stage = makeStage1(DsdKernel::Rows, {taps.taps, taps.size, kScale,
                                                  channels, order, kCapacity});
        EXPECT_EQ(run(*stage, in, channels, sizes),
                  direct(taps, in, channels, order))
            << describe(DsdKernel::Rows, false, order, channels) << ", "
            << groups << " groups";
      }
    }
  }
  EXPECT_EQ(tested, (std::set<size_t>{5, 6, 7, 8, 10, 12}));
}

TEST(Stage1, RowsAreBuiltForEvery30kAnd50kChain) {
  for (const auto &chain : dsd_filters::kChains) {
    if (chain.filter != int(DsdFilter::Cut30k) &&
        chain.filter != int(DsdFilter::Cut50k))
      continue;
    const size_t groups = dsd_filters::kStage1[chain.stage1].size / 8;
    EXPECT_TRUE(dsd_kernels::stage1RowsHandles(groups))
        << chain.base * chain.dsdMultiple << " Hz to "
        << chain.base * chain.pcmMultiple << " Hz, filter " << chain.filter
        << ": " << groups << " groups";
  }
}

TEST(Stage1, ResetForgetsTheHistory) {
  const auto &taps = tapsWith(false);
  const auto first = randomBytes(200, 1), second = randomBytes(200, 2);
  for (const auto kernel : kKernels) {
    const Stage1Spec spec{
        taps.taps, taps.size, kScale, 1, DsdBitOrder::MsbFirst, kCapacity};
    auto used = makeStage1(kernel, spec);
    run(*used, first, 1, {kCapacity});
    used->reset();
    auto fresh = makeStage1(kernel, spec);
    EXPECT_EQ(run(*used, second, 1, {kCapacity}),
              run(*fresh, second, 1, {kCapacity}))
        << describe(kernel, false, DsdBitOrder::MsbFirst, 1);
  }
}
