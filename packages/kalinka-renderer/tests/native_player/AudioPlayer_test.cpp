#include "AudioGraphNode.h"
#include "AudioPlayer.h"
#include "Config.h"
#include "StateMonitor.h"
#include "StreamState.h"


#include <gtest/gtest.h>

#include "LocalHttpServer.h"
#include "TestHelpers.h"

class AudioPlayerTest : public ::testing::Test {
protected:
  // Local, where the tests once fetched samples over the network: a fixed
  // sleep raced the download, and what is being tested is the player, not the
  // fetch. AudioGraphHttpStream_test still covers streaming over HTTP.
  // Long enough to still be playing where the tests expect it to be, and
  // silent, so a run on a real device is quiet whatever the volume.
  const std::string url1 = "file://" + testFile("silence30.flac");
  const std::string url2 = "file://" + testFile("tone880.flac");
  const std::string url3 = "file://" + testFile("silence30.flac");
  const std::string url4 = "file://" + testFile("silence30.mp3");
  const std::string url5 = "file://" + testFile("silence30.mp3");
  LocalHttpServer server{testFile("tone880.flac")};
  const std::string httpUrl = server.url("/ranged");

  Config config = {{"input.http.buffer_size", "768000"},
                   {"input.http.chunk_size", "384000"},
                   {"decoder.flac.buffer_size", "1536000"},
                   {"output.alsa.device", testDevice()},
                   {"output.alsa.buffer_size", "16384"},
                   {"output.alsa.period_size", "1024"}};

  AudioPlayer audioPlayer;

  AudioPlayerTest() : audioPlayer(config) {
    audioPlayer.configureVolume("software", "");
    audioPlayer.setVolume(0);
  }
};

TEST_F(AudioPlayerTest, constructor_destructor) {}

TEST_F(AudioPlayerTest, play) {
  audioPlayer.append(4, url1);
  std::this_thread::sleep_for(std::chrono::seconds(4));
}

TEST_F(AudioPlayerTest, playNext) {
  audioPlayer.append(5, url1);
  audioPlayer.append(6, url2);
  while (audioPlayer.getState().state != AudioGraphNodeState::FINISHED) {
    std::this_thread::sleep_for(std::chrono::milliseconds(1000));
  }
}

TEST_F(AudioPlayerTest, monitor) {
  auto monitor = audioPlayer.monitor();
  auto state = monitor->waitState();
  EXPECT_EQ(state.state, AudioGraphNodeState::STOPPED);
}

TEST_F(AudioPlayerTest, play_one_after_another) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  auto monitor = audioPlayer.monitor();
  audioPlayer.append(7, url3);
  std::this_thread::sleep_for(std::chrono::seconds(1));
  audioPlayer.clearAll();
  audioPlayer.append(8, url2);
  std::this_thread::sleep_for(std::chrono::seconds(1));
  audioPlayer.clearAll();
  audioPlayer.append(9, url1);
  while (audioPlayer.getState().state != AudioGraphNodeState::FINISHED) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
  }

  // clearAll() causes a STOPPED/FINISHED before each SOURCE_CHANGED
  auto seen = drainStates(*monitor);
  expectPlayedInTurn(seen, {7, 8, 9});
  EXPECT_TRUE(reported(seen, AudioGraphNodeState::FINISHED));
}

TEST_F(AudioPlayerTest, test_play_next_then_play) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  auto monitor = audioPlayer.monitor();
  audioPlayer.append(10, url1);
  std::this_thread::sleep_for(std::chrono::seconds(1));
  audioPlayer.append(11, url3);
  std::this_thread::sleep_for(std::chrono::seconds(1));
  audioPlayer.clearAll();
  audioPlayer.append(12, url2);

  while (audioPlayer.getState().state != AudioGraphNodeState::FINISHED) {
    std::this_thread::sleep_for(std::chrono::milliseconds(1000));
  }

  auto seen = drainStates(*monitor);
  expectPlayedInTurn(seen, {10, 12});
  EXPECT_TRUE(reported(seen, AudioGraphNodeState::FINISHED));
}

TEST_F(AudioPlayerTest, test_play_pause_stop_play) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  auto monitor = audioPlayer.monitor();
  audioPlayer.append(13, url1);
  std::this_thread::sleep_for(std::chrono::seconds(2));
  audioPlayer.stop();
  std::this_thread::sleep_for(std::chrono::seconds(2));
  audioPlayer.append(14, url2);

  while (audioPlayer.getState().state != AudioGraphNodeState::FINISHED) {
    std::this_thread::sleep_for(std::chrono::milliseconds(1000));
  }

  AudioGraphNodeState states[] = {
      AudioGraphNodeState::STOPPED,   AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::STOPPED,   AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::FINISHED};

  int i = 0;
  while (monitor->hasData()) {
    auto state = monitor->waitState();
    ASSERT_LT(i, sizeof(states) / sizeof(states[0]));
    EXPECT_EQ(state.state, states[i++]);
  }
  EXPECT_EQ(i, sizeof(states) / sizeof(states[0]));
}

TEST_F(AudioPlayerTest, seek_forward) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  auto monitor = audioPlayer.monitor();
  audioPlayer.append(15, url3);
  std::this_thread::sleep_for(std::chrono::seconds(4));
  audioPlayer.seek(6000);

  std::this_thread::sleep_for(std::chrono::milliseconds(2000));

  AudioGraphNodeState states[] = {
      AudioGraphNodeState::STOPPED,   AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING};

  int i = 0;
  while (monitor->hasData()) {
    auto state = monitor->waitState();
    ASSERT_LT(i, sizeof(states) / sizeof(states[0]));
    EXPECT_EQ(state.state, states[i++]);

    if (i == 6) {
      EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
      EXPECT_NEAR(state.position, 6000, 10);
    }
  }

  EXPECT_EQ(i, sizeof(states) / sizeof(states[0]));
}

TEST_F(AudioPlayerTest, seek_backward) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  auto monitor = audioPlayer.monitor();
  audioPlayer.append(16, url3);
  std::this_thread::sleep_for(std::chrono::seconds(4));
  audioPlayer.seek(0);

  std::this_thread::sleep_for(std::chrono::milliseconds(2000));

  AudioGraphNodeState states[] = {
      AudioGraphNodeState::STOPPED,   AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING};

  int i = 0;
  while (monitor->hasData()) {
    auto state = monitor->waitState();
    ASSERT_LT(i, sizeof(states) / sizeof(states[0]));
    EXPECT_EQ(state.state, states[i++]);

    if (i == 6) {
      EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
      EXPECT_NEAR(state.position, 0, 10);
    }
  }

  EXPECT_EQ(i, sizeof(states) / sizeof(states[0]));
}

TEST_F(AudioPlayerTest, seek_one_after_another) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  auto monitor = audioPlayer.monitor();
  audioPlayer.append(17, url3);

  std::this_thread::sleep_for(std::chrono::seconds(4));
  EXPECT_EQ(audioPlayer.seek(5000), 5000);
  EXPECT_EQ(audioPlayer.seek(500), 500);

  std::this_thread::sleep_for(std::chrono::seconds(4));

  AudioGraphNodeState states[] = {
      AudioGraphNodeState::STOPPED,   AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING};

  int i = 0;
  while (monitor->hasData()) {
    auto state = monitor->waitState();
    ASSERT_LT(i, sizeof(states) / sizeof(states[0]));
    EXPECT_EQ(state.state, states[i++]);

    if (i == 6) {
      EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
      EXPECT_NEAR(state.position, 0, 500);
    }
  }

  EXPECT_EQ(i, sizeof(states) / sizeof(states[0]));
}

TEST_F(AudioPlayerTest, seek_to_end_finishes) {
  auto monitor = audioPlayer.monitor();
  audioPlayer.append(18, url3);

  StreamState streamingState(AudioGraphNodeState::STOPPED);
  while (streamingState.state != AudioGraphNodeState::STREAMING) {
    streamingState = monitor->waitState();
  }

  ASSERT_TRUE(streamingState.streamInfo.has_value());
  const auto &streamInfo = streamingState.streamInfo.value();
  ASSERT_EQ(streamInfo.streamType, StreamType::FRAMES);
  ASSERT_GT(streamInfo.format.sampleRate, 0u);
  ASSERT_GT(streamInfo.streamSize, 0u);

  const size_t durationMs = static_cast<size_t>(
      (1000.0 * streamInfo.streamSize) / streamInfo.format.sampleRate);
  const size_t seekTargetMs = durationMs + 1000;

  audioPlayer.seek(seekTargetMs);

  bool finished = false;
  StreamState lastState = streamingState;
  auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(15);

  while (!finished && std::chrono::steady_clock::now() < deadline) {
    if (monitor->hasData()) {
      lastState = monitor->waitState();
      finished = lastState.state == AudioGraphNodeState::FINISHED;
    } else {
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
  }

  EXPECT_TRUE(finished) << "Last state: " << stateToString(lastState.state);

  // Start another stream after finishing and ensure it begins streaming.
  while (monitor->hasData()) {
    monitor->waitState();
  }

  audioPlayer.append(19, url1);

  bool streamedAgain = false;
  auto restartDeadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(10);

  while (!streamedAgain && std::chrono::steady_clock::now() < restartDeadline) {
    if (monitor->hasData()) {
      auto state = monitor->waitState();
      streamedAgain = state.state == AudioGraphNodeState::STREAMING;
    } else {
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }
  }

  EXPECT_TRUE(streamedAgain) << "Second stream failed to reach STREAMING";
}

TEST_F(AudioPlayerTest, test_play_pause_next) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  auto monitor = audioPlayer.monitor();
  audioPlayer.append(20, url1);
  std::this_thread::sleep_for(std::chrono::seconds(2));
  audioPlayer.pause();
  std::this_thread::sleep_for(std::chrono::seconds(2));
  audioPlayer.clearAll();
  audioPlayer.resume();
  audioPlayer.append(21, url2);

  // Emptying the queue finishes the graph too, so wait for this stream's own
  // ending rather than for the first FINISHED to come along.
  const auto deadline =
      std::chrono::steady_clock::now() + std::chrono::seconds(30);
  while (std::chrono::steady_clock::now() < deadline) {
    const auto state = audioPlayer.getState();
    if (state.state == AudioGraphNodeState::FINISHED && state.streamId == 21) {
      break;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }

  auto seen = drainStates(*monitor);
  expectPlayedInTurn(seen, {20, 21});
  EXPECT_TRUE(reported(seen, AudioGraphNodeState::PAUSED));
  EXPECT_TRUE(reported(seen, AudioGraphNodeState::FINISHED));
}

TEST_F(AudioPlayerTest, test_remove_stream) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  auto monitor = audioPlayer.monitor();
  const StreamId id1 = 1;
  audioPlayer.append(id1, url1);
  std::this_thread::sleep_for(std::chrono::seconds(2));
  audioPlayer.remove(id1);
  std::this_thread::sleep_for(std::chrono::seconds(2));
  EXPECT_EQ(audioPlayer.getState().state, AudioGraphNodeState::FINISHED);

  const StreamId id1b = 2;
  audioPlayer.append(id1b, url1);
  const StreamId id2 = 3;
  audioPlayer.append(id2, url2);
  std::this_thread::sleep_for(std::chrono::seconds(2));
  audioPlayer.remove(id2);

  while (audioPlayer.getState().state != AudioGraphNodeState::FINISHED) {
    std::this_thread::sleep_for(std::chrono::milliseconds(1000));
  }

  AudioGraphNodeState states[] = {
      AudioGraphNodeState::STOPPED,   AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::FINISHED,  AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::FINISHED};

  const auto statesCount = sizeof(states) / sizeof(AudioGraphNodeState);

  int i = 0;
  for (; i < statesCount && monitor->hasData(); ++i) {
    auto state = monitor->waitState();
    EXPECT_EQ(state.state, states[i]) << "i=" << i;
  }

  EXPECT_EQ(i, statesCount);
}

TEST_F(AudioPlayerTest, play_different_formats) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  auto monitor = audioPlayer.monitor();
  audioPlayer.append(22, url3, AudioFormat::FormatFlac);
  std::this_thread::sleep_for(std::chrono::seconds(2));
  audioPlayer.clearAll();
  audioPlayer.append(23, url5, AudioFormat::FormatMpeg);
  std::this_thread::sleep_for(std::chrono::seconds(2));
  audioPlayer.clearAll();
  audioPlayer.append(24, url4, AudioFormat::FormatMpeg);
  for (int i = 0;
       i < 7 && audioPlayer.getState().state != AudioGraphNodeState::FINISHED;
       ++i) {
    std::this_thread::sleep_for(std::chrono::seconds(1));
  }

  auto seen = drainStates(*monitor);
  expectPlayedInTurn(seen, {22, 23, 24});
}

TEST_F(AudioPlayerTest, test_protocol_detection) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  auto monitor = audioPlayer.monitor();

  // Test with a local file URL (file://)
  std::string fileUrl = "file://" + testFile("tone440.mp3");
  audioPlayer.append(25, fileUrl, AudioFormat::FormatMpeg);
  std::this_thread::sleep_for(std::chrono::seconds(2));

  // Switch to an HTTP URL
  audioPlayer.clearAll();
  audioPlayer.append(26, httpUrl);

  while (audioPlayer.getState().state != AudioGraphNodeState::FINISHED) {
    std::this_thread::sleep_for(std::chrono::milliseconds(1000));
  }

  auto seen = drainStates(*monitor);
  expectPlayedInTurn(seen, {25, 26});
  EXPECT_TRUE(reported(seen, AudioGraphNodeState::FINISHED));
}
