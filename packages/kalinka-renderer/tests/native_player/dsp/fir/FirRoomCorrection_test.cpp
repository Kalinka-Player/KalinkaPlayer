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
  struct Case {
    unsigned rate;
    FirRoomCorrection::Partitions sizes;
  };
  for (const auto &[rate, sizes] : std::array<Case, 4>{{{48000, {32, 192}},
                                                        {48000, {64, 64}},
                                                        {96000, {64, 320}},
                                                        {192000, {32, 192}}}}) {
    auto stream = format;
    stream.sampleRate = rate;
    FirRoomCorrection fir(rate, ir[0], ir[1], sizes);
    const size_t lag = fir.start(stream);
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

class WideUniformFirTest : public ::testing::TestWithParam<size_t> {};

TEST_P(WideUniformFirTest, PreservesStereoHistoryAcrossWrapAndRestart) {
  const size_t hop = GetParam();
  std::vector<double> left(4 * hop + 3);
  left[0] = 0.5;
  left[hop - 1] = -0.25;
  left.back() = 0.125;
  for (size_t length : {hop + 3, 3 * hop + 1}) {
    std::vector<double> right(length);
    right[1] = -0.25;
    right[hop + 1] = 0.5;
    right.back() = -0.125;
    FirRoomCorrection fir(format.sampleRate, left, right, {hop, hop});
    ASSERT_EQ(fir.start(format), hop);
    std::vector<Stereo> input(7 * hop + 13),
        out(input.size() + left.size() + hop);
    std::mt19937 gen(3349);
    std::uniform_real_distribution<double> noise(-0.25, 0.25);
    for (size_t i = 0; i < input.size(); ++i)
      input[i] = {0.25 + noise(gen), (i % 2 ? -0.5 : 0.5) + noise(gen)};
    process(fir, input.data(), std::span(out).first(2 * hop + 7));
    ASSERT_EQ(fir.start(format), hop);
    process(fir, input.data(), std::span(out).first(input.size()));
    process(fir, nullptr, std::span(out).subspan(input.size()));
    for (size_t i = 0; i < out.size(); ++i) {
      const auto sample = [&](size_t ch, size_t tap) {
        return i >= hop + tap && i - hop - tap < input.size()
                   ? input[i - hop - tap][ch]
                   : 0.0;
      };
      ASSERT_NEAR(out[i][0],
                  0.5 * sample(0, 0) - 0.25 * sample(0, hop - 1) +
                      0.125 * sample(0, left.size() - 1),
                  1e-12)
          << i;
      ASSERT_NEAR(out[i][1],
                  -0.25 * sample(1, 1) + 0.5 * sample(1, hop + 1) -
                      0.125 * sample(1, right.size() - 1),
                  1e-12)
          << i;
    }
  }
}

INSTANTIATE_TEST_SUITE_P(HopSize, WideUniformFirTest,
                         ::testing::Values(4096, 8192, 16384));

TEST(FirRoomCorrectionTest, PairedHopsMatchBothResponsesAcrossRestartAndDrain) {
  constexpr size_t hop = 4096;
  const StreamAudioFormat stream{192000, 2, 64, PCM_FLOAT64_LE};
  std::mt19937 gen(173);
  std::uniform_real_distribution<double> noise(-0.25, 0.25);
  std::vector<double> left(32 * hop + 7), right(32 * hop - 11);
  for (auto *response : {&left, &right})
    std::ranges::generate(*response, [&] { return noise(gen); });
  FirRoomCorrection fir(stream.sampleRate, left, right, {hop, hop});

  const std::array<size_t, 6> positions{0, hop - 257, hop, 2 * hop + 1,
                                        31 * hop + 17, 62 * hop - 1};
  std::vector<Stereo> input(positions.back() + 733);
  for (auto pos : positions)
    input[pos] = {noise(gen), noise(gen)};
  std::vector<Stereo> out(input.size() + left.size() + hop);
  ASSERT_EQ(fir.start(stream), hop);
  for (size_t stop : {8 * hop + 1500, 9 * hop + 1500}) {
    process(fir, input.data(), std::span(out).first(stop));
    ASSERT_EQ(fir.start(stream), hop);
  }
  constexpr std::array<size_t, 5> reads{257, 31, 1023, 510, 4101};
  for (size_t at = 0, i = 0; at < input.size(); ++i) {
    const size_t n = std::min(reads[i % reads.size()], input.size() - at);
    process(fir, input.data() + at, std::span(out).subspan(at, n));
    at += n;
  }
  process(fir, nullptr, std::span(out).subspan(input.size()));
  for (size_t i = 0; i < out.size(); ++i) {
    Stereo expected{};
    for (auto pos : positions) {
      if (i < pos + hop)
        break;
      const size_t tap = i - pos - hop;
      if (tap < left.size())
        expected[0] += left[tap] * input[pos][0];
      if (tap < right.size())
        expected[1] += right[tap] * input[pos][1];
    }
    ASSERT_NEAR(out[i][0], expected[0], 1e-12) << i;
    ASSERT_NEAR(out[i][1], expected[1], 1e-12) << i;
  }
}

TEST(FirRoomCorrectionTest, RestartClearsPartiallyAccumulatedHighRateTails) {
  auto stream = format;
  stream.sampleRate = 192000;
  std::vector<double> ir(1501, 0.001);
  FirRoomCorrection fir(stream.sampleRate, ir, ir, {32, 192});
  const size_t lag = fir.start(stream);
  std::vector<Stereo> music(2003, Stereo{0.25, -0.5}), out(music.size());
  process(fir, music.data(), out);
  fir.start(stream);
  const Stereo pulse{1, -1};
  process(fir, &pulse, std::span(out).first(1));
  process(fir, nullptr, std::span(out).subspan(1));
  for (size_t i = 0; i < out.size(); ++i) {
    const double expected = i >= lag && i - lag < ir.size() ? ir[i - lag] : 0;
    ASSERT_NEAR(out[i][0], expected, 1e-12) << i;
    ASSERT_NEAR(out[i][1], -expected, 1e-12) << i;
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

TEST(FirRoomCorrectionTest,
     ExactTrailingZerosReducePrerollWithoutMovingTheImpulse) {
  std::vector<double> left(65536), right(32768);
  right[23] = -0.25;
  left[4095] = 1e-15;
  FirRoomCorrection fir(format.sampleRate, left, right);
  EXPECT_EQ(fir.preparedHistory(), 4095u);
  const auto lag = fir.start(format);
  std::vector<Stereo> out(lag + left.size());
  const Stereo impulse{1, 1};
  process(fir, &impulse, std::span(out).first(1));
  process(fir, nullptr, std::span(out).subspan(1));
  EXPECT_NEAR(out[lag + 4095][0], 1e-15, 1e-28);
  EXPECT_NEAR(out[lag + 23][1], -0.25, 1e-14);
  EXPECT_EQ(out.back(), Stereo{});

  std::fill(left.begin(), left.end(), 0.0);
  FirRoomCorrection muted(format.sampleRate, left, left);
  EXPECT_EQ(muted.preparedHistory(), 0u);
  muted.start(format);
  process(muted, &impulse, std::span(out).first(1));
  process(muted, nullptr, std::span(out).subspan(1));
  for (const auto &pair : out)
    ASSERT_EQ(pair, Stereo{});
}

TEST(FirRoomCorrectionTest,
     SilencePreservesTheLastTapAndAudioResumesAtTheSameDelay) {
  std::vector<double> ir(713);
  ir.front() = 0.5;
  ir.back() = -0.25;
  FirRoomCorrection fir(format.sampleRate, ir, ir, {32, 192});
  const auto lag = fir.start(format);
  const size_t next = 2873;
  std::vector<Stereo> input(next + ir.size() + lag + 31), actual(input.size());
  input[0][0] = 1;
  input[next][1] = -0.5;
  process(fir, input.data(), std::span(actual).first(1));
  // Resume inside an unaligned PCM block after draining part of the tail.
  process(fir, nullptr, std::span(actual).subspan(1, 417));
  process(fir, input.data() + 418, std::span(actual).subspan(418));
  for (size_t i = 0; i < actual.size(); ++i) {
    const double l = i < lag || i - lag >= ir.size() ? 0 : ir[i - lag];
    const double r = i < next + lag || i - next - lag >= ir.size()
                         ? 0
                         : -0.5 * ir[i - next - lag];
    ASSERT_NEAR(actual[i][0], l, 1e-13) << i;
    ASSERT_NEAR(actual[i][1], r, 1e-13) << i;
  }
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
    return samples(*node, frames);
  }

  static std::vector<double> samples(const CountingNode &node, size_t frames) {
    std::vector<double> input;
    for (int32_t x : node.values(0, frames))
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

class UnseekableFloatNode final : public CountingNode {
public:
  UnseekableFloatNode(StreamId id, int32_t base, size_t frames)
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

  size_t seekTo(size_t) override {
    ADD_FAILURE()
        << "A gapless join or discard must work on a nonseekable source";
    return size_t(-1);
  }
};

TEST_F(FirGraph, AChangeFromIntegerToFloatKeepsTheTailAndReadsTheNewWidth) {
  auto input = queue(1, 500000000, 997);
  auto second = std::make_shared<UnseekableFloatNode>(2, -900000000, 5303);
  switcher->connectTo(second);
  for (const auto sample : second->values(0, 5303))
    input.push_back(sample / 2147483648.);
  auto out = read(input.size() + 1, 5000);
  ASSERT_EQ(out.size(), input.size());
  EXPECT_EQ(joins, (std::vector<size_t>{0, 997}));
  for (size_t i = 0; i < out.size(); ++i)
    ASSERT_NEAR(out[i], expected(input, i), 1e-12) << i;
}

TEST_F(FirGraph, CuttingTheOutgoingTrackRebuildsAllReadAheadFromItsSuccessors) {
  queue(1, 700000000, 1000);
  auto second = std::make_shared<UnseekableFloatNode>(2, -900000000, 7);
  switcher->connectTo(second);
  auto third = queue(3, 500000000, 1253);
  ASSERT_EQ(read(980).size(), 980u);
  ASSERT_EQ(switcher->streamId(), 3);
  stage->discard(1);

  std::vector<double> input;
  for (const auto x : second->values(0, 7))
    input.push_back(x / 2147483648.);
  input.insert(input.end(), third.begin(), third.end());
  const auto out = read(input.size() + 1);
  ASSERT_EQ(out.size(), input.size());
  EXPECT_EQ(joins, (std::vector<size_t>{0, 0, 7}));
  for (size_t i = 0; i < out.size(); ++i)
    ASSERT_NEAR(out[i], expected(input, i), 1e-12) << i;
}

TEST_F(FirGraph,
       CuttingABufferedMiddleTrackFinishesTheHeadAndRestartsTheThird) {
  const auto first = queue(1, 700000000, 1000);
  auto second = std::make_shared<UnseekableFloatNode>(2, -900000000, 7);
  switcher->connectTo(second);
  const auto third = queue(3, 500000000, 1253);
  ASSERT_EQ(read(980).size(), 980u);
  ASSERT_EQ(switcher->streamId(), 3);
  switcher->disconnect(second);
  stage->discard(2);

  const auto out = read(20 + third.size() + 1);
  ASSERT_EQ(out.size(), 20 + third.size());
  EXPECT_EQ(joins, (std::vector<size_t>{0, 20}));
  for (size_t i = 0; i < out.size(); ++i)
    ASSERT_NEAR(out[i],
                i < 20 ? expected(first, 980 + i) : expected(third, i - 20),
                1e-12)
        << i;
}

TEST_F(FirGraph, RemovingAFullyPlayedTrackKeepsItsNaturalTail) {
  auto input = queue(1, 700000000, 1000);
  const auto second = queue(2, -900000000, 850);
  input.insert(input.end(), second.begin(), second.end());
  ASSERT_EQ(read(1000).size(), 1000u);
  ASSERT_EQ(stage->getState().state, AudioGraphNodeState::SOURCE_CHANGED);
  stage->discard(1);
  const auto out = read(second.size() + 1);
  ASSERT_EQ(out.size(), second.size());
  for (size_t i = 0; i < out.size(); ++i)
    ASSERT_NEAR(out[i], expected(input, 1000 + i), 1e-12) << i;
}

class RestoredDecoder : public CountingNode {
public:
  RestoredDecoder(std::optional<StreamId> id, int32_t base, size_t frames)
      : AudioGraphNode(id), CountingNode(id, base, frames) {}
  std::vector<size_t> seeks;
  bool preparing = false;
  std::optional<size_t> refused;
  std::optional<StreamState> ready;

  size_t seekTo(size_t frame) override {
    seeks.push_back(frame);
    if (refused == frame) {
      setState({AudioGraphNodeState::ERROR,
                StreamError{StreamErrorSource::DECODER, "seek failed"}});
      return size_t(-1);
    }
    const size_t reached = CountingNode::seekTo(frame);
    if (preparing) {
      ready = getState();
      setState(StreamState(AudioGraphNodeState::PREPARING));
    }
    return reached;
  }
  std::optional<long> streamReadPosition() const override {
    return ready ? std::nullopt : CountingNode::streamReadPosition();
  }
  std::optional<StreamInfo> seekStreamInfo(std::optional<StreamId> id,
                                           bool waitForHeader) override {
    if (ready && waitForHeader) {
      ADD_FAILURE() << "The decoder is still filling its buffer after a seek "
                       "in a format the stage already knows";
      return std::nullopt;
    }
    return CountingNode::seekStreamInfo(id, waitForHeader);
  }

  void finishPreparing() {
    setState(ready.value());
    ready.reset();
  }
};

TEST_F(FirGraph, RetiredDecoderCanAcknowledgeThePrerollWhileStillPreparing) {
  auto first = std::make_shared<RestoredDecoder>(1, 700000000, 2100);
  switcher->connectTo(first);
  queue(2, -600000000, 1700);
  ASSERT_EQ(read(3801).size(), 3800u);
  first->preparing = true;
  ASSERT_EQ(stage->seekStream(1, 1100), 1100u);
  EXPECT_EQ(first->seeks, (std::vector<size_t>{400}));
  EXPECT_EQ(stage->streamReadPosition(), 1100);
  ASSERT_FALSE(first->streamReadPosition());
  const auto buffered = read(7);
  ASSERT_EQ(buffered.size(), 7u);
  EXPECT_EQ(stage->streamReadPosition(), 1107);
  first->finishPreparing();
  std::vector<double> input;
  for (auto x : first->values(0, 2100))
    input.push_back(x / 2147483648.);
  auto out = buffered;
  const auto rest = read(84);
  out.insert(out.end(), rest.begin(), rest.end());
  ASSERT_EQ(out.size(), 91u);
  for (size_t i = 0; i < out.size(); ++i)
    ASSERT_NEAR(out[i], expected(input, 1100 + i), 1e-12) << i;
}

TEST_F(FirGraph, FailedRetiredPrerollReturnsFailureAndExposesTheDecoderError) {
  auto first = std::make_shared<RestoredDecoder>(1, 700000000, 2100);
  switcher->connectTo(first);
  queue(2, -600000000, 1700);
  ASSERT_EQ(read(3801).size(), 3800u);
  first->refused = 400;
  EXPECT_EQ(stage->seekStream(1, 1100), size_t(-1));
  EXPECT_EQ(first->seeks, (std::vector<size_t>{400}));
  EXPECT_EQ(stage->getState().state, AudioGraphNodeState::ERROR);
}

TEST_F(FirGraph, SeekingBeyondKnownEndReportsTheLastReachablePosition) {
  queue(1, 700000000, 1000);
  stage->acceptSourceChange();
  EXPECT_EQ(stage->seekTo(1100), 1000u);
  EXPECT_EQ(stage->streamReadPosition(), 1000);
  EXPECT_TRUE(read(1).empty());
  EXPECT_EQ(stage->getState().state, AudioGraphNodeState::FINISHED);
}

TEST_F(FirGraph, DirectDecoderKeepsItsFIRFormatAfterAnEarlyAcknowledgement) {
  auto node = std::make_shared<RestoredDecoder>(1, 700000000, 2000);
  stage->disconnect(switcher);
  stage->connectTo(node);
  node->preparing = true;
  ASSERT_EQ(stage->seekStream(1, 1000), 1000u);
  const auto out = read(1);
  ASSERT_EQ(out.size(), 1u);
  EXPECT_NEAR(out[0], expected(samples(*node, 2000), 1000), 1e-12);
  EXPECT_EQ(stage->streamReadPosition(), 1001);
}

TEST_F(FirGraph, NamedSeekOnAnUnboundSourceDiscardsThePreroll) {
  auto node = std::make_shared<RestoredDecoder>(std::nullopt, 500000000, 1500);
  stage->disconnect(switcher);
  stage->connectTo(node);
  node->preparing = true;
  const auto input = samples(*node, 1500);
  for (size_t frame : {400, 900}) {
    ASSERT_EQ(stage->seekStream(42, frame), frame);
    const auto out = read(1);
    ASSERT_EQ(out.size(), 1u);
    EXPECT_NEAR(out[0], expected(input, frame), 1e-12);
    EXPECT_EQ(stage->streamReadPosition(), frame + 1);
  }
}

TEST_F(FirGraph, RefusedSeekOnAlreadyFailedDecoderPreservesPlayableTail) {
  struct FailedDecoder final : CountingNode {
    FailedDecoder()
        : AudioGraphNode(1),
          CountingNode(1, 500000000, 2000, {.errorAt = 1000}) {}
    size_t seekTo(size_t) override { return size_t(-1); }
  };
  auto node = std::make_shared<FailedDecoder>();
  switcher->connectTo(node);
  ASSERT_EQ(read(990).size(), 990u);
  ASSERT_EQ(node->getState().state, AudioGraphNodeState::ERROR);
  EXPECT_EQ(stage->seekStream(1, 400), size_t(-1));
  ASSERT_EQ(stage->streamReadPosition(), 990);
  const auto rest = read(10);
  ASSERT_EQ(rest.size(), 10u);
  const auto input = samples(*node, 1000);
  for (size_t i = 0; i < rest.size(); ++i)
    EXPECT_NEAR(rest[i], expected(input, 990 + i), 1e-12);
  EXPECT_EQ(stage->getState().state, AudioGraphNodeState::ERROR);
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

TEST_F(FirGraph, SeekingTheBypassedSuccessorDoesNotUseTheOutgoingImpulse) {
  queue(1, 700000000, 1000);
  auto bypassed = std::make_shared<CountingNode>(
      2, 1000000, 2000, CountingNode::Options{.rate = 44100});
  switcher->connectTo(bypassed);
  ASSERT_EQ(read(980).size(), 980u);
  ASSERT_EQ(switcher->streamId(), 2);
  ASSERT_EQ(stage->seekStream(2, 850), 850u);
  EXPECT_EQ(bypassed->streamReadPosition(), 850);
  std::array<int32_t, 2> frame{};
  ASSERT_EQ(stage->read(frame.data(), sizeof(frame)), sizeof(frame));
  EXPECT_EQ(frame[0], bypassed->value(850));
}

TEST_F(FirGraph, SeekingARetiredStreamReconstructsItsOwnHistory) {
  const auto first = queue(1, 700000000, 2100);
  queue(2, -600000000, 1700);
  ASSERT_EQ(read(3801).size(), 3800u);
  ASSERT_EQ(stage->seekStream(1, 1100), 1100u);
  const auto out = read(91);
  ASSERT_EQ(out.size(), 91u);
  for (size_t i = 0; i < out.size(); ++i)
    ASSERT_NEAR(out[i], expected(first, 1100 + i), 1e-12) << i;
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
