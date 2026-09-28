#include <gtest/gtest.h>

#include <alsa/asoundlib.h>
#include <chrono>
#include <cstdlib>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "TestHelpers.h"
#include "native_player/AudioPlayer.h"
#include "native_player/StateMonitor.h"
#include "native_player/StreamState.h"

// Playback end to end, sink included: what the sink reports is what reaches
// the Core, and it is the sink that decides when frames count as played.
// Silent by default (testDevice()), and silent on a real device too, since
// software volume is set to zero.
namespace {

constexpr long kTrackMs = 6000;

std::string fixture(const char *file) {
  return std::string("file://") + KALINKA_TEST_DATA_DIR + "/" + file;
}

bool deviceOpens() {
  snd_pcm_t *handle = nullptr;
  if (snd_pcm_open(&handle, testDevice().c_str(), SND_PCM_STREAM_PLAYBACK, 0) <
      0) {
    return false;
  }
  snd_pcm_close(handle);
  return true;
}

class Playing {
public:
  Playing()
      : config_({{"output.alsa.device", testDevice()},
                 {"output.alsa.latency_ms", "100"},
                 {"output.alsa.period_ms", "25"}}),
        player_(config_) {
    player_.configureVolume("software", "");
    player_.setVolume(0);
    monitor_ = player_.monitor();
    collector_ = std::thread([this] {
      while (monitor_->isRunning()) {
        auto state = monitor_->waitState();
        if (!monitor_->isRunning()) {
          break;
        }
        std::lock_guard<std::mutex> lock(mutex_);
        seen_.push_back(state);
      }
    });
  }

  ~Playing() {
    monitor_->stop();
    collector_.join();
  }

  AudioPlayer &player() { return player_; }

  // Everything reported from here on, for an assertion about what a command
  // did rather than about what happened to precede it.
  size_t mark() {
    std::lock_guard<std::mutex> lock(mutex_);
    return seen_.size();
  }

  std::optional<StreamState>
  nextOf(size_t from, AudioGraphNodeState wanted,
         std::chrono::milliseconds timeout = std::chrono::milliseconds(5000)) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    do {
      {
        std::lock_guard<std::mutex> lock(mutex_);
        for (size_t i = from; i < seen_.size(); ++i) {
          if (seen_[i].state == wanted) {
            return seen_[i];
          }
        }
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    } while (std::chrono::steady_clock::now() < deadline);
    return std::nullopt;
  }

private:
  Config config_;
  AudioPlayer player_;
  std::unique_ptr<StateMonitor> monitor_;
  std::thread collector_;
  std::mutex mutex_;
  std::vector<StreamState> seen_;
};

class PlaybackPositionTest : public testing::Test {
protected:
  void SetUp() override {
    if (!deviceOpens()) {
      GTEST_SKIP() << "no ALSA device '" << testDevice() << "'";
    }
  }
};

void playFor(std::chrono::milliseconds howLong) {
  std::this_thread::sleep_for(howLong);
}

TEST_F(PlaybackPositionTest, PlaysWhatItIsGiven) {
  Playing playing;

  playing.player().append(1, fixture("ladder.flac"));

  auto streaming = playing.nextOf(0, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(streaming.has_value()) << "never started";
  EXPECT_EQ(streaming->position, 0);
  ASSERT_TRUE(streaming->streamInfo.has_value());
  EXPECT_EQ(streaming->streamInfo->format.sampleRate, 22050u);
}

TEST_F(PlaybackPositionTest, PlaysOggVorbis) {
  Playing playing;
  playing.player().append(1, fixture("ladder.ogg"), AudioFormat::FormatVorbis);

  const auto streaming = playing.nextOf(0, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(streaming.has_value()) << "never started";
  ASSERT_TRUE(streaming->streamInfo.has_value());
  EXPECT_EQ(streaming->streamInfo->format.sampleRate, 22050u);
  EXPECT_EQ(streaming->streamInfo->format.bitsPerSample, 16u);
  EXPECT_TRUE(playing.nextOf(0, AudioGraphNodeState::FINISHED).has_value());
}

TEST_F(PlaybackPositionTest, SeekingForwardReportsWhereItLanded) {
  Playing playing;
  playing.player().append(1, fixture("ladder.flac"));
  ASSERT_TRUE(playing.nextOf(0, AudioGraphNodeState::STREAMING).has_value());
  playFor(std::chrono::milliseconds(500));

  const auto mark = playing.mark();
  EXPECT_NEAR(playing.player().seek(4000), 4000, 25);

  auto streaming = playing.nextOf(mark, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(streaming.has_value()) << "never resumed";
  EXPECT_NEAR(streaming->position, 4000, 100);
}

TEST_F(PlaybackPositionTest, SeekingBackwardReportsWhereItLanded) {
  Playing playing;
  playing.player().append(1, fixture("ladder.flac"));
  ASSERT_TRUE(playing.nextOf(0, AudioGraphNodeState::STREAMING).has_value());
  playing.player().seek(4000);
  playFor(std::chrono::milliseconds(500));

  const auto mark = playing.mark();
  playing.player().seek(1000);

  auto streaming = playing.nextOf(mark, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(streaming.has_value()) << "never resumed";
  EXPECT_NEAR(streaming->position, 1000, 100);
}

TEST_F(PlaybackPositionTest, SeekAfterSeekLandsOnTheLastOne) {
  Playing playing;
  playing.player().append(1, fixture("ladder.flac"));
  ASSERT_TRUE(playing.nextOf(0, AudioGraphNodeState::STREAMING).has_value());

  playing.player().seek(1000);
  playing.player().seek(3000);
  const auto mark = playing.mark();
  playing.player().seek(2000);

  auto streaming = playing.nextOf(mark, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(streaming.has_value()) << "never resumed";
  EXPECT_NEAR(streaming->position, 2000, 100);
}

TEST_F(PlaybackPositionTest, SeekingPastTheEndFinishes) {
  Playing playing;
  playing.player().append(1, fixture("ladder.flac"));
  ASSERT_TRUE(playing.nextOf(0, AudioGraphNodeState::STREAMING).has_value());

  EXPECT_NEAR(playing.player().seek(kTrackMs + 1000), kTrackMs, 25)
      << "answered with where it landed, not what was asked for";

  EXPECT_TRUE(playing.nextOf(0, AudioGraphNodeState::FINISHED).has_value());
}

TEST_F(PlaybackPositionTest, AStartOffsetIsReportedAsThePosition) {
  Playing playing;

  playing.player().append(1, fixture("ladder.flac"), AudioFormat::FormatFlac,
                          3000);

  auto streaming = playing.nextOf(0, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(streaming.has_value()) << "never started";
  EXPECT_NEAR(streaming->position, 3000, 100);
}

TEST_F(PlaybackPositionTest, SeekingAfterAStartOffsetIsStillAbsolute) {
  Playing playing;
  playing.player().append(1, fixture("ladder.flac"), AudioFormat::FormatFlac,
                          3000);
  ASSERT_TRUE(playing.nextOf(0, AudioGraphNodeState::STREAMING).has_value());
  playFor(std::chrono::milliseconds(300));

  const auto mark = playing.mark();
  playing.player().seek(1000);

  auto streaming = playing.nextOf(mark, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(streaming.has_value()) << "never resumed";
  EXPECT_NEAR(streaming->position, 1000, 100);
}

TEST_F(PlaybackPositionTest, AQueuedTrackTakesOverAtItsOwnStart) {
  Playing playing;
  playing.player().append(1, fixture("ladder.flac"), AudioFormat::FormatFlac,
                          kTrackMs - 1000);
  ASSERT_TRUE(playing.nextOf(0, AudioGraphNodeState::STREAMING).has_value());
  const auto mark = playing.mark();
  playing.player().append(2, fixture("ladder.flac"), AudioFormat::FormatFlac,
                          4000);

  auto handover = playing.nextOf(mark, AudioGraphNodeState::SOURCE_CHANGED,
                                 std::chrono::milliseconds(8000));
  ASSERT_TRUE(handover.has_value()) << "never handed over";
  auto streaming = playing.nextOf(mark + 1, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(streaming.has_value());
  EXPECT_NEAR(streaming->position, 4000, 200);
}

} // namespace
