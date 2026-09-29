#include "AlsaAudioEmitter.h"
#include "SineWaveNode.h"

#include <cmath>
#include <gtest/gtest.h>

#include "Config.h"
#include "ErrorFakeNode.h"
#include "SlowOutputNode.h"
#include "TestHelpers.h"

// Fails from inside the audio path, which reaches the worker as an exception
// rather than as an ERROR state the way ErrorFakeNode does.
class ThrowingOutputNode : public AudioGraphOutputNode {
public:
  ThrowingOutputNode() {
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.format = {.sampleRate = 44100,
                                    .channels = 2,
                                    .bitsPerSample = 16},
                         .streamType = StreamType::FRAMES,
                         .streamSize = 44100}});
  }
  size_t read(void *, size_t) override {
    throw std::runtime_error("the device went away mid-write");
  }
  size_t waitForData(std::stop_token = std::stop_token(),
                     size_t = 1) override {
    return 4096;
  }
  size_t waitForDataFor(std::stop_token, std::chrono::milliseconds,
                        size_t) override {
    return 4096;
  }
};

class AlsaAudioEmitterTest : public ::testing::Test {
protected:
  Config config = {{"output.alsa.device", testDevice()},
                   {"output.alsa.latency_ms", "80"},
                   {"output.alsa.period_ms", "20"},
                   {"fixups.alsa_reopen_device_with_new_format", "true"}};

  std::shared_ptr<AlsaAudioEmitter> alsaAudioEmitter;

  void SetUp() override {
    alsaAudioEmitter = std::make_shared<AlsaAudioEmitter>(config);
    alsaAudioEmitter->setSoftwareVolume(0.0f);
  }
};

TEST_F(AlsaAudioEmitterTest, constructor_destructor) {}

TEST_F(AlsaAudioEmitterTest, connectTo) {
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, 1000);
  alsaAudioEmitter->connectTo(outputNode);
  alsaAudioEmitter->disconnect(outputNode);
}

TEST_F(AlsaAudioEmitterTest, connectTo_nullptr) {
  EXPECT_THROW(alsaAudioEmitter->connectTo(nullptr), std::runtime_error);
}

TEST_F(AlsaAudioEmitterTest, disconnect) {
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, 1000);
  EXPECT_EQ(outputNode->getState().state, AudioGraphNodeState::STREAMING);
  alsaAudioEmitter->connectTo(outputNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  alsaAudioEmitter->disconnect(outputNode);
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::STOPPED);
}

TEST_F(AlsaAudioEmitterTest, getState) {
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, 1000);
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::STOPPED);
  alsaAudioEmitter->connectTo(outputNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  std::this_thread::sleep_for(std::chrono::milliseconds(1010));
  EXPECT_EQ(outputNode->getState().state, AudioGraphNodeState::FINISHED);
  std::this_thread::sleep_for(std::chrono::milliseconds(341));
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::FINISHED);
  alsaAudioEmitter->disconnect(outputNode);
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::STOPPED);
}

TEST_F(AlsaAudioEmitterTest, pause) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  const auto totalDuration = 1000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  alsaAudioEmitter->connectTo(outputNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  auto sleepAmount = totalDuration / 2;
  std::this_thread::sleep_for(std::chrono::milliseconds(sleepAmount));
  alsaAudioEmitter->pause(true);
  auto state = waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::PAUSED,
                             std::chrono::milliseconds(1000));
  EXPECT_EQ(state.state, AudioGraphNodeState::PAUSED);
  EXPECT_NEAR(state.position,
              sleepAmount +
                  value<size_t>(config, "output.alsa.latency_ms").value(),
              30);
  ASSERT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::PAUSED);
  std::this_thread::sleep_for(std::chrono::milliseconds(500));
  alsaAudioEmitter->pause(false);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);
}

TEST_F(AlsaAudioEmitterTest, a_playing_state_names_what_the_device_opened) {
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, 1000);
  alsaAudioEmitter->connectTo(outputNode);

  const auto state =
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(state.streamInfo.has_value());
  ASSERT_TRUE(state.deviceInfo.has_value());
  // The null device takes whatever it is handed, so the two agree here. The
  // field earns its place on a device that substitutes, which this is not.
  EXPECT_EQ(state.deviceInfo->format.sampleRate,
            state.streamInfo->format.sampleRate);
  EXPECT_EQ(state.deviceInfo->format.bitsPerSample,
            state.streamInfo->format.bitsPerSample);
  EXPECT_EQ(state.deviceInfo->format.channels, 2u);
  if (testDevice() == "null") {
    // SND_PCM_TYPE_NULL, like every type but HW, sits between us and a card.
    // Exclusive needs real hardware: set KALINKA_TEST_ALSA_DEVICE.
    EXPECT_EQ(state.deviceInfo->access, DeviceAccess::Shared);
  } else {
    EXPECT_NE(state.deviceInfo->access, DeviceAccess::Unknown);
  }

  alsaAudioEmitter->disconnect(outputNode);
  EXPECT_FALSE(alsaAudioEmitter->getState().deviceInfo.has_value())
      << "a closed device is open at nothing";
}

TEST_F(AlsaAudioEmitterTest, a_failure_in_the_worker_leaves_no_device_behind) {
  auto outputNode = std::make_shared<ThrowingOutputNode>();
  alsaAudioEmitter->connectTo(outputNode);

  const auto state =
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::ERROR);
  ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
  // Nothing is reported after this one, so it must not be the state that
  // leaves a closed device on record as open.
  EXPECT_FALSE(state.deviceInfo.has_value());
  EXPECT_FALSE(alsaAudioEmitter->getState().deviceInfo.has_value());

  alsaAudioEmitter->disconnect(outputNode);
}

TEST_F(AlsaAudioEmitterTest, stream_error) {
  auto outputNode = std::make_shared<ErrorFakeNode>();
  alsaAudioEmitter->connectTo(outputNode);
  std::this_thread::sleep_for(std::chrono::milliseconds(100));
  auto streamState = alsaAudioEmitter->getState();
  EXPECT_EQ(streamState.state, AudioGraphNodeState::ERROR);
  EXPECT_EQ(streamState.error->message, "Fake error message");
  alsaAudioEmitter->disconnect(outputNode);
}

TEST_F(AlsaAudioEmitterTest, slow_output_node_goes_into_preparing) {
  const size_t duration = 2000;
  auto outputNode = std::make_shared<SlowOutputNode>(duration, 1000);
  alsaAudioEmitter->connectTo(outputNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);

  const auto buffering =
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::PREPARING);
  EXPECT_EQ(buffering.state, AudioGraphNodeState::PREPARING);
  EXPECT_EQ(buffering.position, duration / 2);
  EXPECT_TRUE(buffering.streamInfo.has_value());

  auto state = waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING);

  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.position, duration / 2);

  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);

  alsaAudioEmitter->disconnect(outputNode);
}

TEST_F(AlsaAudioEmitterTest, test_seekToForward) {
  const auto totalDuration = 2000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  alsaAudioEmitter->connectTo(outputNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  auto sleepAmount = totalDuration / 4;
  std::this_thread::sleep_for(std::chrono::milliseconds(sleepAmount));
  EXPECT_EQ(alsaAudioEmitter->seek(totalDuration / 2), totalDuration / 2);
  auto state = waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING);
  EXPECT_NEAR(state.position, 1000, 10);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);
  alsaAudioEmitter->disconnect(outputNode);
}

TEST_F(AlsaAudioEmitterTest, test_seekBackwards) {
  const auto totalDuration = 2000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  alsaAudioEmitter->connectTo(outputNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  auto sleepAmount = totalDuration / 4;
  std::this_thread::sleep_for(std::chrono::milliseconds(sleepAmount));
  alsaAudioEmitter->seek(0);
  auto state = waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING);
  EXPECT_NEAR(state.position, 0, 10);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);
  alsaAudioEmitter->disconnect(outputNode);
}

TEST_F(AlsaAudioEmitterTest, test_seekWhenStopped) {
  EXPECT_EQ(alsaAudioEmitter->seek(0), -1);
}

TEST_F(AlsaAudioEmitterTest, test_seekAfterFinished) {
  const auto totalDuration = 2000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  alsaAudioEmitter->connectTo(outputNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);
  alsaAudioEmitter->seek(10);
  auto state = waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.position, 10);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);
}

TEST_F(AlsaAudioEmitterTest, test_seekPastEnd) {
  const auto totalDuration = 2000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  alsaAudioEmitter->connectTo(outputNode);
  waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(alsaAudioEmitter->seek(totalDuration + 100), totalDuration);
  auto state = waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED,
                             std::chrono::milliseconds(2000));
  EXPECT_EQ(state.state, AudioGraphNodeState::FINISHED);
}

TEST_F(AlsaAudioEmitterTest, test_seekToEndAndBack) {
  const auto totalDuration = 2000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  alsaAudioEmitter->connectTo(outputNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  EXPECT_EQ(alsaAudioEmitter->seek(totalDuration), totalDuration);
  EXPECT_EQ(waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED,
                          std::chrono::milliseconds(1000))
                .state,
            AudioGraphNodeState::FINISHED);
  EXPECT_EQ(alsaAudioEmitter->seek(0), 0);
  auto state = waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.position, 0);
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
}

TEST_F(AlsaAudioEmitterTest, test_play_after_finished) {
  const auto totalDuration = 1000;

  auto firstNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  alsaAudioEmitter->connectTo(firstNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);

  EXPECT_EQ(alsaAudioEmitter->seek(totalDuration + 200), totalDuration);
  EXPECT_EQ(waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED,
                          std::chrono::milliseconds(1000))
                .state,
            AudioGraphNodeState::FINISHED);

  alsaAudioEmitter->disconnect(firstNode);

  auto secondNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  alsaAudioEmitter->connectTo(secondNode);

  auto state = waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING,
                             std::chrono::milliseconds(1000));
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);

  EXPECT_EQ(waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED,
                          std::chrono::milliseconds(1500))
                .state,
            AudioGraphNodeState::FINISHED);
}
