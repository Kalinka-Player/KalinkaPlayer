#include "AudioStreamSwitcher.h"
#include "CountingNode.h"
#include "dsp/DspStage.h"
#include "dsp/fir/FirRoomCorrection.h"

#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <random>

namespace {
constexpr StreamAudioFormat format{48000, 2, 64, PCM_FLOAT64_LE};
using Stereo = std::array<double, 2>;

void process(FirRoomCorrection &fir, const Stereo *in, std::span<Stereo> out) {
  ASSERT_EQ(fir.process(reinterpret_cast<const uint8_t *>(in),
                        reinterpret_cast<uint8_t *>(out.data()), out.size()),
            out.size());
}

TEST(FirRoomCorrectionTest,
     IrregularReadsAndDrainMatchDirectStereoConvolution) {
  std::mt19937 gen(7210);
  std::uniform_real_distribution<double> noise(-0.5, 0.5);
  std::array<std::vector<double>, 2> ir{std::vector<double>(1113),
                                        std::vector<double>(750)};
  for (auto &response : ir)
    for (auto &tap : response)
      tap = noise(gen) * 0.01;
  std::vector<Stereo> input(2401);
  for (auto &pair : input)
    pair = {noise(gen), noise(gen)};
  for (FirRoomCorrection::Partitions sizes :
       {FirRoomCorrection::Partitions{32, 192}, {64, 64}, {64, 320}}) {
    FirRoomCorrection fir(format.sampleRate, ir[0], ir[1], sizes);
    const size_t lag = fir.start(format);
    EXPECT_EQ(lag, sizes.head);
    std::vector<Stereo> actual(input.size() + fir.history() + lag + 41);
    const std::vector<size_t> reads{1, 93, 2, 17, 511, 317, 1053};
    for (size_t at = 0, i = 0; at < input.size(); ++i) {
      const auto count = std::min(reads[i % reads.size()], input.size() - at);
      process(fir, input.data() + at, std::span(actual).subspan(at, count));
      at += count;
    }
    process(fir, nullptr, std::span(actual).subspan(input.size()));
    double maxError = 0;
    for (size_t i = 0; i < actual.size(); ++i) {
      for (unsigned ch = 0; ch < 2; ++ch) {
        double reference = 0;
        if (i >= lag) {
          const size_t pos = i - lag;
          for (size_t j = pos >= input.size() ? pos - input.size() + 1 : 0;
               j < ir[ch].size() && j <= pos; ++j)
            reference += ir[ch][j] * input[pos - j][ch];
        }
        ASSERT_TRUE(std::isfinite(actual[i][ch]));
        maxError = std::max(maxError, std::abs(reference - actual[i][ch]));
      }
    }
    EXPECT_LT(maxError, 1e-12);
  }
}

TEST(FirRoomCorrectionTest, LongImpulseAndRestartCoverEveryPartition) {
  std::vector<double> ir(FirRoomCorrection::kMaxTaps);
  for (size_t i = 0; i < ir.size(); ++i)
    ir[i] = std::sin(0.19 * i) / (1 + i);
  const std::array<double, 1> identity{1};
  FirRoomCorrection fir(format.sampleRate, ir, identity);
  const size_t lag = fir.start(format);
  EXPECT_EQ(fir.history(), ir.size() - 1);
  std::vector<Stereo> actual(lag + ir.size());
  const Stereo impulse{1, -1};
  process(fir, &impulse, std::span(actual).first(1));
  process(fir, nullptr, std::span(actual).subspan(1));
  for (size_t i = 0; i < actual.size(); ++i) {
    ASSERT_NEAR(actual[i][0], i < lag ? 0 : ir[i - lag], 1e-12) << i;
    ASSERT_NEAR(actual[i][1], i == lag ? -1 : 0, 1e-12) << i;
  }
  process(fir, &impulse, std::span(actual).first(1));
  fir.start(format);
  process(fir, nullptr, actual);
  for (const auto &pair : actual)
    ASSERT_EQ(pair, Stereo{});
}

TEST(FirRoomCorrectionTest, NonFiniteFloatSamplesLeaveTheNextSamplesPlayable) {
  const std::array<double, 2> ir{1, 0.5};
  FirRoomCorrection fir(48000, ir, ir, {32, 64});
  const size_t lag = fir.start({48000, 2, 32, PCM_FLOAT32_LE});
  std::vector<std::array<float, 2>> input(lag + 4);
  input[0] = {std::numeric_limits<float>::quiet_NaN(),
              std::numeric_limits<float>::infinity()};
  input[1] = {0.5f, -0.25f};
  std::vector<Stereo> output(input.size());
  fir.process(reinterpret_cast<const uint8_t *>(input.data()),
              reinterpret_cast<uint8_t *>(output.data()), input.size());
  for (size_t i = 0; i < output.size(); ++i) {
    const double expected = i == lag + 1 ? 0.5 : i == lag + 2 ? 0.25 : 0;
    ASSERT_NEAR(output[i][0], expected, 1e-12);
    ASSERT_NEAR(output[i][1], -expected / 2, 1e-12);
  }
}

TEST(FirRoomCorrectionTest, ValidatesImpulseAndPartitionSizesBeforePlayback) {
  std::vector<double> ir{1};
  EXPECT_THROW(FirRoomCorrection(0, ir, ir), std::invalid_argument);
  EXPECT_THROW(FirRoomCorrection(48000, {}, ir), std::invalid_argument);
  EXPECT_THROW(FirRoomCorrection(48000, ir, ir, {63, 512}),
               std::invalid_argument);
  EXPECT_THROW(FirRoomCorrection(48000, ir, ir, {256, 128}),
               std::invalid_argument);
  EXPECT_THROW(FirRoomCorrection(48000, ir, ir, {0, 0}), std::invalid_argument);
  EXPECT_THROW(FirRoomCorrection(48000, ir, ir, {64, 96}),
               std::invalid_argument);
  ir.resize(FirRoomCorrection::kMaxTaps + 1);
  EXPECT_THROW(FirRoomCorrection(48000, ir, ir), std::invalid_argument);
  ir = {std::numeric_limits<double>::infinity()};
  EXPECT_THROW(FirRoomCorrection(48000, ir, ir), std::invalid_argument);
}

TEST(FirRoomCorrectionTest, DescribesOnlyStereoPcmAtTheImpulseRate) {
  const std::array<double, 1> ir{1};
  FirRoomCorrection fir(48000, ir, ir);
  EXPECT_TRUE(fir.handles(format));
  const auto path = fir.describe(format);
  ASSERT_EQ(path.steps.size(), 1u);
  EXPECT_EQ(path.steps.front().kind, "fir");
  EXPECT_DOUBLE_EQ(path.latencyMs,
                   1000.0 * fir.start(format) / format.sampleRate);
  for (auto f : {StreamAudioFormat{44100, 2, 16, PCM16_LE},
                 StreamAudioFormat{48000, 1, 32, PCM32_LE},
                 StreamAudioFormat{48000, 2, 1, DSD_RAW},
                 StreamAudioFormat{48000, 2, 24, DOP32_LE}}) {
    EXPECT_FALSE(fir.handles(f));
    EXPECT_TRUE(fir.describe(f).steps.empty());
  }
}

struct FirGraph : testing::Test {
  std::vector<double> ir = std::vector<double>(701);
  std::shared_ptr<AudioStreamSwitcher> switcher =
      std::make_shared<AudioStreamSwitcher>();
  std::shared_ptr<DspStage> stage;
  std::vector<size_t> joins;

  void SetUp() override {
    ir[0] = 0.5;
    ir[319] = -0.25;
    ir.back() = 0.125;
    stage = std::make_shared<DspStage>(std::make_unique<FirRoomCorrection>(
        48000, ir, ir, FirRoomCorrection::Partitions{32, 192}));
    stage->connectTo(switcher);
  }

  std::vector<double> queue(StreamId id, int32_t base, size_t frames) {
    auto node = std::make_shared<CountingNode>(id, base, frames);
    switcher->connectTo(node);
    std::vector<double> input;
    for (int32_t x : node->values(0, frames))
      input.push_back(x / 2147483648.);
    return input;
  }

  std::vector<double> read(size_t limit, size_t block = 53) {
    std::vector<double> left;
    std::vector<Stereo> buf(block);
    for (size_t tries = 0; left.size() < limit && tries < 1000; ++tries) {
      const auto state = stage->getState().state;
      if (state == AudioGraphNodeState::SOURCE_CHANGED) {
        joins.push_back(left.size());
        stage->acceptSourceChange();
      } else if (state == AudioGraphNodeState::FINISHED) {
        break;
      } else {
        const auto count = std::min(buf.size(), limit - left.size());
        const auto got =
            stage->read(buf.data(), count * sizeof(Stereo)) / sizeof(Stereo);
        for (const auto &pair : std::span(buf).first(got)) {
          EXPECT_NEAR(pair[1], -pair[0], 1e-12);
          left.push_back(pair[0]);
        }
      }
    }
    return left;
  }

  double expected(const std::vector<double> &input, size_t i) const {
    return 0.5 * input[i] - (i >= 319 ? 0.25 * input[i - 319] : 0) +
           (i >= 700 ? 0.125 * input[i - 700] : 0);
  }
};

TEST_F(FirGraph, CompatibleJoinPreservesTailAndSourceBoundary) {
  auto input = queue(1, 500000000, 997);
  auto second = queue(2, -900000000, 849);
  input.insert(input.end(), second.begin(), second.end());
  auto out = read(input.size() + 1);
  ASSERT_EQ(out.size(), input.size());
  EXPECT_EQ(joins, (std::vector<size_t>{0, 997}));
  for (size_t i = 0; i < out.size(); ++i)
    ASSERT_NEAR(out[i], expected(input, i), 1e-12) << i;
}

class FloatCountingNode final : public CountingNode {
public:
  FloatCountingNode(StreamId id, int32_t base, size_t frames)
      : AudioGraphNode(id),
        CountingNode(id, base, frames, {.format = PCM_FLOAT64_LE}) {}

  size_t read(void *data, size_t bytes) override {
    std::vector<int32_t> integers(2 * (bytes / sizeof(Stereo)));
    const size_t count =
        CountingNode::read(integers.data(), integers.size() * sizeof(int32_t)) /
        sizeof(int32_t);
    for (size_t i = 0; i < count; ++i) {
      const double value = integers[i] / 2147483648.;
      std::memcpy(static_cast<uint8_t *>(data) + i * sizeof(value), &value,
                  sizeof(value));
    }
    return count * sizeof(double);
  }
};

TEST_F(FirGraph, AChangeFromIntegerToFloatKeepsTheTailAndReadsTheNewWidth) {
  auto input = queue(1, 500000000, 997);
  auto second = std::make_shared<FloatCountingNode>(2, -900000000, 5303);
  switcher->connectTo(second);
  for (const auto sample : second->values(0, 5303))
    input.push_back(sample / 2147483648.);
  auto out = read(input.size() + 1, 5000);
  ASSERT_EQ(out.size(), input.size());
  EXPECT_EQ(joins, (std::vector<size_t>{0, 997}));
  for (size_t i = 0; i < out.size(); ++i)
    ASSERT_NEAR(out[i], expected(input, i), 1e-12) << i;
}

TEST_F(FirGraph, SeekBeforeFirstReadUsesThePreparedImpulse) {
  auto input = queue(1, 700000000, 2000);
  stage->acceptSourceChange();
  ASSERT_EQ(stage->seekStream(1, 1200), 1200u);
  const auto out = read(97);
  ASSERT_EQ(out.size(), 97u);
  for (size_t i = 0; i < out.size(); ++i)
    ASSERT_NEAR(out[i], expected(input, 1200 + i), 1e-12) << i;
}

TEST_F(FirGraph, SeekPrerollReconstructsTheTargetSample) {
  auto input = queue(1, 700000000, 3503);
  ASSERT_EQ(read(1601).size(), 1601u);
  for (size_t target : {2103u, 97u}) {
    ASSERT_EQ(stage->seekTo(target), target);
    EXPECT_EQ(stage->streamReadPosition(), long(target));
    auto out = read(807);
    ASSERT_EQ(out.size(), 807u);
    for (size_t i = 0; i < out.size(); ++i)
      ASSERT_NEAR(out[i], expected(input, target + i), 1e-12) << i;
  }
}
} // namespace
