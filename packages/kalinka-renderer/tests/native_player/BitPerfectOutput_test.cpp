#include "AudioPlayer.h"
#include "FileInputNode.h"
#include "FlacStreamDecoder.h"
#include "StateMonitor.h"

#include <gtest/gtest.h>

#include <filesystem>
#include <fstream>
#include <unistd.h>

#include "ScopedAlsaConfig.h"
#include "TestHelpers.h"

// What AudioPlayer hands to ALSA, captured by its file plugin, against what
// the decoder makes of the same file. With nothing processing and the volume
// fixed they are the same bytes, across a gapless join too, whatever latency
// the DSP stage hides.
namespace {

std::string ladder() {
  return std::string(KALINKA_TEST_DATA_DIR) + "/ladder.flac";
}

std::vector<uint8_t> decoded(const std::string &path) {
  auto input = std::make_shared<FileInputNode>(1, path);
  auto decoder = std::make_shared<FlacStreamDecoder>(1, 1 << 20);
  decoder->connectTo(input);
  std::vector<uint8_t> out, chunk(4096);
  while (decoder->waitForData(std::stop_token(), chunk.size())) {
    const size_t bytes = decoder->read(chunk.data(), chunk.size());
    out.insert(out.end(), chunk.begin(), chunk.begin() + long(bytes));
  }
  decoder->disconnect(input);
  return out;
}

std::filesystem::path freshDirectory() {
  static int count = 0;
  auto path = std::filesystem::temp_directory_path() /
              ("kalinka-bit-perfect-" + std::to_string(getpid()) + "-" +
               std::to_string(++count));
  std::filesystem::create_directories(path);
  return path;
}

class BitPerfectOutputTest : public testing::TestWithParam<int> {
protected:
  const std::filesystem::path directory = freshDirectory();
  const std::filesystem::path capture = directory / "written.raw";
  ScopedAlsaConfig alsa{"pcm.null { type null }\n"
                        "pcm.capture { type file slave.pcm { type null } "
                        "file \"" +
                        capture.string() + "\" format raw }\n"};

  ~BitPerfectOutputTest() override {
    std::error_code error;
    std::filesystem::remove_all(directory, error);
  }

  std::vector<uint8_t> play(const std::vector<std::string> &files) {
    {
      AudioPlayer player({{"output.alsa.device", "capture"},
                          {"dsp.delay_ms", std::to_string(GetParam())}});
      auto monitor = player.monitor();
      StreamId id = 0;
      for (const auto &file : files) {
        player.append(++id, "file://" + file);
      }
      const auto deadline =
          std::chrono::steady_clock::now() + std::chrono::seconds(20);
      while (std::chrono::steady_clock::now() < deadline) {
        const auto state = monitor->waitState();
        if ((state.state == AudioGraphNodeState::FINISHED &&
             state.streamId == id) ||
            state.state == AudioGraphNodeState::ERROR) {
          break;
        }
      }
      player.stop();
    }
    std::ifstream in(capture, std::ios::binary);
    return {std::istreambuf_iterator<char>(in), {}};
  }
};

TEST_P(BitPerfectOutputTest, OneTrackReachesAlsaUnchanged) {
  const auto expected = decoded(ladder());
  ASSERT_GT(expected.size(), 0u);
  EXPECT_TRUE(play({ladder()}) == expected);
}

TEST_P(BitPerfectOutputTest, AGaplessJoinReachesAlsaUnchanged) {
  auto expected = decoded(ladder());
  expected.insert(expected.end(), expected.begin(), expected.end());
  const auto written = play({ladder(), ladder()});
  ASSERT_EQ(written.size(), expected.size());
  EXPECT_TRUE(written == expected);
}

INSTANTIATE_TEST_SUITE_P(, BitPerfectOutputTest, testing::Values(0, 300),
                         [](const testing::TestParamInfo<int> &info) {
                           return "DelayMs" + std::to_string(info.param);
                         });

} // namespace
