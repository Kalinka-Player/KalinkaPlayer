#include "AudioPlayer.h"
#include "StateMonitor.h"

#include "Analysis.h"
#include "SigmaDelta.h"
#include "Signals.h"

#include <gtest/gtest.h>

#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <optional>
#include <unistd.h>

#include "ScopedAlsaConfig.h"
#include "TestHelpers.h"

// DSF played in `pcm` mode, captured by ALSA's file plugin: the DSP stage
// converts it, and the output stage quantises it for the device.
namespace {
constexpr unsigned kDsd64 = 2822400;
constexpr unsigned kPcmRate = 176400;
constexpr size_t kBytes = kDsd64 / 8 / 2; // Half a second a channel.

std::filesystem::path freshDirectory() {
  static int count = 0;
  auto path = std::filesystem::temp_directory_path() /
              ("kalinka-dsd-pcm-" + std::to_string(getpid()) + "-" +
               std::to_string(++count));
  std::filesystem::create_directories(path);
  return path;
}

/// A 1 kHz tone at half modulation, one bit per entry.
std::vector<uint8_t> toneBits() {
  harness::SigmaDelta modulator(7, 64);
  return modulator.modulate(harness::sine(1000, kDsd64, kBytes * 8, 0.5));
}

class DsdPcmPlaybackTest : public testing::Test {
protected:
  const std::filesystem::path directory = freshDirectory();
  const std::filesystem::path capture = directory / "written.raw";
  ScopedAlsaConfig alsa{"pcm.null { type null }\n"
                        "pcm.capture { type file slave.pcm { type null } "
                        "file \"" +
                        capture.string() + "\" format raw }\n"};

  ~DsdPcmPlaybackTest() override {
    std::error_code error;
    std::filesystem::remove_all(directory, error);
  }

  std::string dsf(const std::string &name, std::span<const uint8_t> bits) {
    const auto path = directory / name;
    const std::vector<uint8_t> channel(bits.begin(), bits.end());
    const auto bytes = harness::dsfFile({channel, channel}, kDsd64);
    std::ofstream(path, std::ios::binary)
        .write(reinterpret_cast<const char *>(bytes.data()),
               std::streamsize(bytes.size()));
    return path.string();
  }

  /// What the device opened, the last a playing state named.
  std::optional<StreamAudioFormat> opened;
  /// What a playing state said was decoded.
  std::optional<StreamAudioFormat> decoded;

  /// The left channel ALSA was handed, full scale ±1, with @p settings
  /// besides the capture device and `pcm` mode.
  std::vector<double> play(const std::vector<std::string> &files,
                           Config settings = {}) {
    {
      settings.insert({{"output.alsa.device", "capture"},
                       {"output.dsd_mode", "pcm"}});
      AudioPlayer player(settings);
      auto monitor = player.monitor();
      StreamId id = 0;
      for (const auto &file : files)
        player.append(++id, "file://" + file, FormatDsd);
      const auto deadline =
          std::chrono::steady_clock::now() + std::chrono::seconds(30);
      while (std::chrono::steady_clock::now() < deadline) {
        const auto state = monitor->waitState();
        EXPECT_NE(state.state, AudioGraphNodeState::ERROR)
            << (state.error ? state.error->message : "");
        if (state.deviceInfo)
          opened = state.deviceInfo->format;
        if (state.state == AudioGraphNodeState::STREAMING && state.streamInfo)
          decoded = state.streamInfo->decodedFormat;
        if ((state.state == AudioGraphNodeState::FINISHED &&
             state.streamId == id) ||
            state.state == AudioGraphNodeState::ERROR)
          break;
      }
      player.stop();
    }
    std::ifstream in(capture, std::ios::binary);
    const std::vector<char> raw{std::istreambuf_iterator<char>(in), {}};
    EXPECT_TRUE(opened);
    return opened ? leftOf(raw, opened->sampleFormat) : std::vector<double>{};
  }

  template <class T>
  static std::vector<double> left(const std::vector<char> &raw, double scale) {
    std::vector<T> samples(raw.size() / sizeof(T));
    std::memcpy(samples.data(), raw.data(), samples.size() * sizeof(T));
    std::vector<double> out;
    for (size_t i = 0; i < samples.size(); i += 2)
      out.push_back(double(samples[i]) * scale);
    return out;
  }

  static std::vector<double> leftOf(const std::vector<char> &raw,
                                    AudioSampleFormat format) {
    switch (format) {
    case PCM_FLOAT64_LE:
      return left<double>(raw, 1.0);
    case PCM_FLOAT32_LE:
      return left<float>(raw, 1.0);
    case PCM32_LE:
      return left<int32_t>(raw, 1.0 / 2147483648.0);
    case PCM16_LE:
      return left<int16_t>(raw, 1.0 / 32768.0);
    default:
      ADD_FAILURE() << "no decoding of " << sampleFormatToString(format);
      return {};
    }
  }
};
} // namespace

TEST_F(DsdPcmPlaybackTest, ADsfTrackPlaysAsPcmAtFourTimesItsBase) {
  const auto left = play({dsf("tone.dsf", toneBits())});
  ASSERT_TRUE(opened);
  EXPECT_EQ(opened->sampleRate, kPcmRate);
  EXPECT_EQ(opened->channels, 2u);
  ASSERT_TRUE(decoded) << "the stream still says it was DSD";
  EXPECT_EQ(decoded->sampleFormat, DSD_RAW);
  EXPECT_EQ(decoded->dsdSampleRate, kDsd64);
  // Two DSD64 bytes a channel to each 176.4 kHz frame.
  ASSERT_EQ(left.size(), kBytes / 2);
  const size_t edge = left.size() / 10;
  const auto measured = harness::thdPlusN(
      std::span(left).subspan(edge, left.size() - 2 * edge), kPcmRate, 1000);
  // Half modulation, raised by the safe gain of 2.8 dB.
  EXPECT_NEAR(measured.fundamentalDbfs, -6.0206 + 2.8, 0.01);
  EXPECT_LT(measured.thdnDb, -110.0);
}

TEST_F(DsdPcmPlaybackTest, AutomaticConvertsATrackTheOutputCannotPlayAsDsd) {
  const auto left =
      play({dsf("tone.dsf", toneBits())}, {{"output.dsd_mode", "auto"}});
  ASSERT_TRUE(opened);
  EXPECT_EQ(opened->sampleRate, kPcmRate) << "a shared output takes no DSD";
  ASSERT_EQ(left.size(), kBytes / 2);
  const size_t edge = left.size() / 10;
  const auto measured = harness::thdPlusN(
      std::span(left).subspan(edge, left.size() - 2 * edge), kPcmRate, 1000);
  EXPECT_NEAR(measured.fundamentalDbfs, -6.0206 + 2.8, 0.01);
}

TEST_F(DsdPcmPlaybackTest, ATrackAskedForAtTheBaseRatePlaysAtIt) {
  const auto left =
      play({dsf("tone.dsf", toneBits())}, {{"dsd.pcm_rate", "1x"}});
  ASSERT_TRUE(opened);
  EXPECT_EQ(opened->sampleRate, 44100u);
  // Eight DSD64 bytes a channel to each 44.1 kHz frame.
  ASSERT_EQ(left.size(), kBytes / 8);
  const size_t edge = left.size() / 10;
  const auto measured = harness::thdPlusN(
      std::span(left).subspan(edge, left.size() - 2 * edge), 44100, 1000);
  EXPECT_NEAR(measured.fundamentalDbfs, -6.0206 + 2.8, 0.01);
  EXPECT_LT(measured.thdnDb, -110.0);
}

TEST_F(DsdPcmPlaybackTest, AGaplessJoinPlaysAsTheWholeTrackWould) {
  const auto bits = toneBits();
  // An odd number of bytes, so the join falls inside an output frame.
  const size_t split = 8 * 20001;
  const auto whole = play({dsf("whole.dsf", bits)});
  const auto joined =
      play({dsf("first.dsf", std::span(bits).first(split)),
            dsf("second.dsf", std::span(bits).subspan(split))});
  ASSERT_EQ(joined.size(), whole.size());
  double worst = 0;
  for (size_t i = 0; i < whole.size(); ++i)
    worst = std::max(worst, std::abs(joined[i] - whole[i]));
  // Two plays' dither apart, and no more: a filter restarted at the join
  // would leave an error near the signal's own level.
  EXPECT_LT(worst, std::ldexp(1.0, -20));
}
