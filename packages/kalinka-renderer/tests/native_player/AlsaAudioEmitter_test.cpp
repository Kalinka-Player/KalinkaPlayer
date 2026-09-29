#include "AlsaAudioEmitter.h"
#include "SineWaveNode.h"

#include <cmath>
#include <future>
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

// A live source that runs out of bytes without reaching EOF.
class StarvedOutputNode : public AudioGraphOutputNode {
public:
  StarvedOutputNode() {
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.format = {.sampleRate = 44100,
                                    .channels = 2,
                                    .bitsPerSample = 16},
                         .streamType = StreamType::FRAMES}});
  }
  size_t read(void *data, size_t size) override {
    return buffer.read(static_cast<uint8_t *>(data), size);
  }
  size_t waitForData(std::stop_token token, size_t size) override {
    if (!waiting.exchange(true)) {
      firstWait.set_value();
    }
    return buffer.waitForData(token, size);
  }
  size_t waitForDataFor(std::stop_token token,
                        std::chrono::milliseconds timeout,
                        size_t size) override {
    return buffer.waitForDataFor(token, timeout, size);
  }
  size_t seekTo(size_t position) override {
    soughtTo = position;
    return position;
  }
  void provideAudio() {
    std::array<uint8_t, 16384> silence{};
    buffer.write(silence.data(), silence.size());
  }

  std::promise<void> firstWait;
  std::atomic<size_t> soughtTo{0};

private:
  Buffer<uint8_t> buffer{16384};
  std::atomic<bool> waiting{false};
};

// Reports buffering once its bytes run out, as an input fetching more does.
class DryingOutputNode : public AudioGraphOutputNode {
public:
  static constexpr size_t kFrames = 4096;

  DryingOutputNode() {
    setState({AudioGraphNodeState::STREAMING, 0, info});
    provideAudio();
  }
  size_t read(void *data, size_t size) override {
    return buffer.read(static_cast<uint8_t *>(data), size);
  }
  size_t waitForData(std::stop_token token, size_t size) override {
    runDryIfEmpty();
    return buffer.waitForData(token, size);
  }
  size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds timeout,
                        size_t size) override {
    runDryIfEmpty();
    return buffer.waitForDataFor(token, timeout, size);
  }
  void catchUp() {
    setState({AudioGraphNodeState::STREAMING, 0, info});
    provideAudio();
  }

private:
  const StreamInfo info{.format = {.sampleRate = 44100,
                                   .channels = 2,
                                   .bitsPerSample = 16},
                        .streamType = StreamType::FRAMES};
  Buffer<uint8_t> buffer{kFrames * 4};

  void provideAudio() {
    std::array<uint8_t, kFrames * 4> silence{};
    buffer.write(silence.data(), silence.size());
  }
  void runDryIfEmpty() {
    if (buffer.size() == 0) {
      setState({AudioGraphNodeState::PREPARING, 0, info});
    }
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

TEST_F(AlsaAudioEmitterTest, starved_live_input_buffers_then_resumes_without_pause) {
  auto source = std::make_shared<StarvedOutputNode>();
  auto waiting = source->firstWait.get_future();
  StateMonitor monitor(alsaAudioEmitter.get());
  alsaAudioEmitter->connectTo(source);
  ASSERT_EQ(waiting.wait_for(std::chrono::seconds(1)), std::future_status::ready);
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::PREPARING);
  source->provideAudio();
  bool resumed = false;
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1);
  while (!resumed && std::chrono::steady_clock::now() < deadline) {
    for (const auto &state : drainStates(monitor)) {
      EXPECT_NE(state.state, AudioGraphNodeState::PAUSED);
      resumed |= state.state == AudioGraphNodeState::STREAMING;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  EXPECT_TRUE(resumed);
  alsaAudioEmitter->disconnect(source);
}

TEST_F(AlsaAudioEmitterTest, pause_while_live_input_is_starved) {
  auto source = std::make_shared<StarvedOutputNode>();
  auto waiting = source->firstWait.get_future();
  alsaAudioEmitter->connectTo(source);
  ASSERT_EQ(waiting.wait_for(std::chrono::seconds(1)), std::future_status::ready);
  auto pause = std::async(std::launch::async, [emitter = alsaAudioEmitter] {
    emitter->pause(true);
  });
  const auto result = pause.wait_for(std::chrono::milliseconds(300));
  EXPECT_EQ(result, std::future_status::ready);
  if (result == std::future_status::ready) {
    EXPECT_EQ(waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::PAUSED,
                            std::chrono::milliseconds(500)).state,
              AudioGraphNodeState::PAUSED);
  }
  // Also releases a regressed pause so failure does not hang the whole suite.
  alsaAudioEmitter->disconnect(source);
  pause.get();
}

TEST_F(AlsaAudioEmitterTest, seek_while_live_input_is_starved) {
  auto source = std::make_shared<StarvedOutputNode>();
  auto waiting = source->firstWait.get_future();
  alsaAudioEmitter->connectTo(source);
  ASSERT_EQ(waiting.wait_for(std::chrono::seconds(1)), std::future_status::ready);
  auto seek = std::async(std::launch::async, [emitter = alsaAudioEmitter] {
    return emitter->seek(12000);
  });
  const auto result = seek.wait_for(std::chrono::milliseconds(300));
  EXPECT_EQ(result, std::future_status::ready);
  EXPECT_EQ(source->soughtTo.load(), 12 * 44100);
  alsaAudioEmitter->disconnect(source);
  if (result == std::future_status::ready) {
    EXPECT_EQ(seek.get(), 12000);
  }
}

TEST_F(AlsaAudioEmitterTest, a_pause_before_the_device_starts_holds_until_resumed) {
  auto source = std::make_shared<StarvedOutputNode>();
  auto waiting = source->firstWait.get_future();
  alsaAudioEmitter->connectTo(source);
  ASSERT_EQ(waiting.wait_for(std::chrono::seconds(1)), std::future_status::ready);
  StateMonitor monitor(alsaAudioEmitter.get());
  auto emitter = alsaAudioEmitter;
  ASSERT_TRUE(returnsWithin([emitter] { emitter->pause(true); },
                            std::chrono::milliseconds(300)));
  source->provideAudio();
  std::this_thread::sleep_for(std::chrono::milliseconds(200));
  const auto whilePaused = drainStates(monitor);
  EXPECT_TRUE(reported(whilePaused, AudioGraphNodeState::PAUSED));
  EXPECT_FALSE(reported(whilePaused, AudioGraphNodeState::STREAMING));
  ASSERT_TRUE(returnsWithin([emitter] { emitter->pause(false); },
                            std::chrono::milliseconds(300)));
  EXPECT_EQ(waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING,
                          std::chrono::seconds(1))
                .state,
            AudioGraphNodeState::STREAMING);
  alsaAudioEmitter->disconnect(source);
}

TEST_F(AlsaAudioEmitterTest, an_input_that_runs_dry_buffers_where_playback_reached) {
  auto source = std::make_shared<DryingOutputNode>();
  StateMonitor monitor(alsaAudioEmitter.get());
  alsaAudioEmitter->connectTo(source);
  std::optional<StreamState> buffering;
  bool streamed = false;
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1);
  while (!buffering && std::chrono::steady_clock::now() < deadline) {
    for (const auto &state : drainStates(monitor)) {
      streamed |= state.state == AudioGraphNodeState::STREAMING;
      if (streamed && !buffering && state.state == AudioGraphNodeState::PREPARING) {
        buffering = state;
      }
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  ASSERT_TRUE(buffering.has_value());
  EXPECT_EQ(buffering->position, 1000 * DryingOutputNode::kFrames / 44100);
  source->catchUp();
  EXPECT_EQ(waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING,
                          std::chrono::seconds(1))
                .state,
            AudioGraphNodeState::STREAMING);
  alsaAudioEmitter->disconnect(source);
}

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
