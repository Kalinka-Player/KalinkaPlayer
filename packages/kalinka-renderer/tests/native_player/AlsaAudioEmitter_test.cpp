#include "AlsaAudioEmitter.h"
#include "ByteRing.h"
#include "SineWaveNode.h"

#include <algorithm>
#include <cmath>
#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <functional>
#include <future>
#include <gtest/gtest.h>
#include <mutex>
#include <unistd.h>

#include "AacTestData.h"
#include "AdtsStreamDecoder.h"
#include "AudioGraphHttpStream.h"
#include "Config.h"
#include "CountingNode.h"
#include "ErrorFakeNode.h"
#include "LiveInputNode.h"
#include "LocalHttpServer.h"
#include "ScopedAlsaConfig.h"
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
  explicit StarvedOutputNode(
      std::optional<LiveAudioPolicy> live = std::nullopt) {
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.format = {.sampleRate = 44100,
                                    .channels = 2,
                                    .bitsPerSample = 16},
                         .streamType = StreamType::FRAMES,
                         .live = live}});
  }
  size_t read(void *data, size_t size) override {
    return buffer.read(data, size);
  }
  size_t waitForData(std::stop_token token, size_t size) override {
    if (!waiting.exchange(true)) {
      firstWait.set_value(size);
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

  std::promise<size_t> firstWait;
  std::atomic<size_t> soughtTo{0};

private:
  ByteRing buffer{16384};
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
    return buffer.read(data, size);
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
  ByteRing buffer{kFrames * 4};

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
  }

  // What the sink reads: muted, and through the stage
  // KALINKA_TEST_DSP_DELAY_MS asks for when play()ed.
  std::shared_ptr<AudioGraphOutputNode> playing;

  void play(std::shared_ptr<AudioGraphOutputNode> source) {
    connect(throughTestDsp(std::move(source)));
  }

  // Straight to the sink, as a source that tests the sink itself hands it.
  void connect(std::shared_ptr<AudioGraphOutputNode> source) {
    playing = silenced(std::move(source));
    alsaAudioEmitter->connectTo(playing);
  }

  void stopPlaying() { alsaAudioEmitter->disconnect(playing); }
};

TEST(AlsaAudioEmitter, BroadcastWaitsForAReserveAndCanPauseWhileBuffering) {
  using namespace std::chrono_literals;
  class Broadcast : public LiveInputNode {
  public:
    Broadcast() : LiveInputNode(44100 * 4) {
      setState(StreamState{AudioGraphNodeState::PREPARING});
      setState({AudioGraphNodeState::STREAMING, 0,
                StreamInfo{.format = {44100, 2, 16, PCM16_LE},
                           .streamType = StreamType::FRAMES,
                           .prebufferMs = 1000}});
    }
    size_t waitForData(std::stop_token token, size_t size) override {
      if (!waiting.exchange(true))
        firstWait.set_value(size);
      return LiveInputNode::waitForData(token, size);
    }
    std::promise<size_t> firstWait;
    std::atomic<bool> waiting = false;
  };
  auto source = std::make_shared<Broadcast>();
  std::vector<uint8_t> silence(44100 * 4);
  // The device could start with this, but a broadcast needs more in reserve.
  source->bytes.write(silence.data(), 4410 * 4);
  auto waiting = source->firstWait.get_future();
  auto sink = std::make_shared<AlsaAudioEmitter>(
      Config{{"output.alsa.device", "null"}});
  std::promise<void> started;
  auto playing = started.get_future();
  sink->onStateChange([&](AudioGraphNode *, StreamState state) {
    if (state.state != AudioGraphNodeState::STREAMING)
      return true;
    started.set_value();
    return false;
  });
  sink->connectTo(source);
  ASSERT_EQ(waiting.wait_for(2s), std::future_status::ready);
  EXPECT_EQ(waiting.get(), 44100u * 4);
  EXPECT_EQ(source->bytes.size(), 4410u * 4);
  EXPECT_TRUE(returnsWithin([sink] { sink->pause(true); }, 2s));
  EXPECT_EQ(sink->getState().state, AudioGraphNodeState::PAUSED);
  sink->pause(false);
  source->bytes.write(silence.data(), 39690 * 4);
  EXPECT_EQ(playing.wait_for(2s), std::future_status::ready);
  sink->disconnect(source);
}

TEST_F(AlsaAudioEmitterTest, constructor_destructor) {}

TEST(AlsaAudioEmitter, AacBroadcastStartsWithAnUnalignedDecoderCapacity) {
  using namespace std::chrono_literals;
  LocalHttpServer server(aacFixturePath("tones.aac"));
  auto source = std::make_shared<AudioGraphHttpStream>(
      1, server.url("/radio"), 32768, 4096);
  auto decoder = std::make_shared<AdtsStreamDecoder>(1, 65535);
  decoder->connectTo(source);
  std::promise<void> started;
  auto playing = started.get_future();
  AlsaAudioEmitter sink(Config{{"output.alsa.device", "null"}});
  sink.onStateChange([&](AudioGraphNode *, StreamState state) {
    if (state.state != AudioGraphNodeState::STREAMING)
      return true;
    started.set_value();
    return false;
  });
  sink.connectTo(decoder);
  EXPECT_EQ(playing.wait_for(2s), std::future_status::ready);
  sink.disconnect(decoder);
}

TEST(PlayedFramesCounter, ACancelledCallbackNeverRunsAndTheOthersStill) {
  PlayedFramesCounter counter;
  std::vector<int> ran;
  counter.callOnOrAfterFrame(100,
                             [&ran](snd_pcm_sframes_t) { ran.push_back(1); });
  const int cancelled = counter.callOnOrAfterFrame(
      200, [&ran](snd_pcm_sframes_t) { ran.push_back(2); });
  counter.callOnOrAfterFrame(300,
                             [&ran](snd_pcm_sframes_t) { ran.push_back(3); });

  counter.cancel(cancelled);
  EXPECT_EQ(counter.drainSequence(),
            (std::vector<snd_pcm_sframes_t>{100, 300}));
  counter.update(400);
  counter.cancel(cancelled);

  EXPECT_EQ(ran, (std::vector<int>{1, 3}));
}

TEST(PlayedFramesCounter, PlayingOutRunsEveryCallbackOnItsOwnFrame) {
  PlayedFramesCounter counter;
  counter.update(50);
  std::vector<std::pair<int, snd_pcm_sframes_t>> ran;
  for (const int frame : {300, 100})
    counter.callOnOrAfterFrame(frame, [&ran, frame](snd_pcm_sframes_t late) {
      ran.emplace_back(frame, late);
    });

  counter.playOut();

  EXPECT_EQ(ran, (std::vector<std::pair<int, snd_pcm_sframes_t>>{{100, 0},
                                                                  {300, 0}}));
  EXPECT_EQ(counter.getPlayedFrames(), 350);
}

TEST(PlayedFramesCounter, PlayingOutWaitsOutTheFramesBeforeEachCallback) {
  PlayedFramesCounter counter;
  counter.update(50);
  std::vector<snd_pcm_sframes_t> steps;
  for (const int frame : {300, 100})
    counter.callOnOrAfterFrame(frame, [&steps](snd_pcm_sframes_t) {
      steps.push_back(-1);
    });

  counter.playOut(
      [&steps](snd_pcm_sframes_t frames) { steps.push_back(frames); });

  EXPECT_EQ(steps, (std::vector<snd_pcm_sframes_t>{100, -1, 200, -1}));
}

TEST(AlsaAudioEmitter, ExplicitBufferSizesApplyOnlyToNonLiveAudio) {
  using namespace std::chrono_literals;
  Config config{{"output.alsa.device", "null"},
                {"output.alsa.buffer_size", "44100"},
                {"output.alsa.period_size", "8820"}};
  AlsaAudioEmitter emitter(config);
  // Switch back to ordinary playback too: its explicit sizes must survive
  // the live stream's temporary latency policy.
  for (bool live : {false, true, false}) {
    SCOPED_TRACE(live ? "live" : "ordinary");
    auto source = std::make_shared<StarvedOutputNode>(
        live ? std::optional{LiveAudioPolicy{60, 250}} : std::nullopt);
    auto waiting = source->firstWait.get_future();
    emitter.connectTo(source);
    ASSERT_EQ(waiting.wait_for(2s), std::future_status::ready);
    // The first read fills the negotiated ALSA buffer. At 44.1 kHz,
    // stereo S16, live's 30 ms hardware buffer needs 5292 bytes; ordinary
    // playback keeps the configured one-second buffer. A regressed live
    // setup negotiates ALSA's minimum of one frame and asks for 4 bytes.
    EXPECT_EQ(waiting.get(), (live ? 1323u : 44100u) * 4);
    EXPECT_EQ(emitter.getState().state, AudioGraphNodeState::PREPARING);
    EXPECT_EQ(emitter.getState().liveReady, live);
    emitter.disconnect(source);
  }
}

TEST_F(AlsaAudioEmitterTest, starved_live_input_buffers_then_resumes_without_pause) {
  auto source = std::make_shared<StarvedOutputNode>();
  auto waiting = source->firstWait.get_future();
  StateMonitor monitor(alsaAudioEmitter.get());
  connect(source);
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
  stopPlaying();
}

TEST_F(AlsaAudioEmitterTest, pause_while_live_input_is_starved) {
  auto source = std::make_shared<StarvedOutputNode>();
  auto waiting = source->firstWait.get_future();
  connect(source);
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
  stopPlaying();
  pause.get();
}

TEST_F(AlsaAudioEmitterTest, seek_while_live_input_is_starved) {
  auto source = std::make_shared<StarvedOutputNode>();
  auto waiting = source->firstWait.get_future();
  connect(source);
  ASSERT_EQ(waiting.wait_for(std::chrono::seconds(1)), std::future_status::ready);
  auto seek = std::async(std::launch::async, [emitter = alsaAudioEmitter] {
    return emitter->seek(12000);
  });
  const auto result = seek.wait_for(std::chrono::milliseconds(300));
  EXPECT_EQ(result, std::future_status::ready);
  EXPECT_EQ(source->soughtTo.load(), 12 * 44100);
  stopPlaying();
  if (result == std::future_status::ready) {
    EXPECT_EQ(seek.get(), 12000);
  }
}

TEST_F(AlsaAudioEmitterTest, a_pause_before_the_device_starts_holds_until_resumed) {
  auto source = std::make_shared<StarvedOutputNode>();
  auto waiting = source->firstWait.get_future();
  connect(source);
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
  // The null device can play the supplied PCM and run dry before pause()
  // returns. Observe the already-subscribed monitor so that transition is kept.
  bool resumed = false;
  const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(1);
  while (!resumed && std::chrono::steady_clock::now() < deadline) {
    resumed = reported(drainStates(monitor), AudioGraphNodeState::STREAMING);
    if (!resumed)
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  EXPECT_TRUE(resumed);
  stopPlaying();
}

TEST_F(AlsaAudioEmitterTest, an_input_that_runs_dry_buffers_where_playback_reached) {
  auto source = std::make_shared<DryingOutputNode>();
  StateMonitor monitor(alsaAudioEmitter.get());
  connect(source);
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
  stopPlaying();
}

TEST_F(AlsaAudioEmitterTest, connectTo) {
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, 1000);
  play(outputNode);
  stopPlaying();
}

TEST_F(AlsaAudioEmitterTest, connectTo_nullptr) {
  EXPECT_THROW(alsaAudioEmitter->connectTo(nullptr), std::runtime_error);
}

TEST_F(AlsaAudioEmitterTest, disconnect) {
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, 1000);
  EXPECT_EQ(outputNode->getState().state, AudioGraphNodeState::STREAMING);
  play(outputNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  stopPlaying();
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::STOPPED);
}

TEST_F(AlsaAudioEmitterTest, a_stop_waits_only_for_what_is_past_the_buffer) {
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, 10000);
  play(outputNode);
  ASSERT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);

  auto emitter = alsaAudioEmitter;
  EXPECT_TRUE(returnsWithin([emitter, playing = playing] {
    emitter->disconnect(playing);
  }, std::chrono::milliseconds(500)))
      << "not the 10 s still to come, nor the cap for a wrong delay";
}

TEST(FramesBeyondBufferTest, ASoundServerStillPlaysItsOwnLatency) {
  // As PipeWire reported it at 160 ms: 7552 frames ours, 9958 still to hear.
  EXPECT_EQ(framesBeyondBuffer(9958, 7680, 128), 2406);
}

TEST(FramesBeyondBufferTest, ACardHoldsNothingPastTheBuffer) {
  EXPECT_EQ(framesBeyondBuffer(3000, 4000, 1000), 0);
  EXPECT_EQ(framesBeyondBuffer(2990, 4000, 1000), 0) << "never negative";
}

TEST(FramesBeyondBufferTest, AnEmptyBufferLeavesTheWholeDelay) {
  EXPECT_EQ(framesBeyondBuffer(2400, 4000, 4000), 2400);
  EXPECT_EQ(framesBeyondBuffer(2400, 4000, 4100), 2400)
      << "an avail past the buffer is still nothing of ours";
}

TEST_F(AlsaAudioEmitterTest, getState) {
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, 1000);
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::STOPPED);
  play(outputNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  std::this_thread::sleep_for(std::chrono::milliseconds(1010));
  EXPECT_EQ(outputNode->getState().state, AudioGraphNodeState::FINISHED);
  std::this_thread::sleep_for(std::chrono::milliseconds(341));
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::FINISHED);
  stopPlaying();
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::STOPPED);
}

TEST_F(AlsaAudioEmitterTest, pause) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  const auto totalDuration = 1000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  play(outputNode);
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
  play(outputNode);

  const auto state =
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(state.streamInfo.has_value());
  ASSERT_TRUE(state.deviceInfo.has_value());
  // The null device takes whatever it is handed. On an S32-only DAC the 16-bit
  // sine is padded, and the bits must still agree or bit-perfect reads false.
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

  stopPlaying();
  EXPECT_FALSE(alsaAudioEmitter->getState().deviceInfo.has_value())
      << "a closed device is open at nothing";
}

TEST_F(AlsaAudioEmitterTest, a_failure_in_the_worker_leaves_no_device_behind) {
  auto outputNode = std::make_shared<ThrowingOutputNode>();
  connect(outputNode);

  const auto state =
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::ERROR);
  ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
  // Nothing is reported after this one, so it must not be the state that
  // leaves a closed device on record as open.
  EXPECT_FALSE(state.deviceInfo.has_value());
  EXPECT_FALSE(alsaAudioEmitter->getState().deviceInfo.has_value());

  stopPlaying();
}

TEST_F(AlsaAudioEmitterTest, stream_error) {
  auto outputNode = std::make_shared<ErrorFakeNode>();
  connect(outputNode);
  std::this_thread::sleep_for(std::chrono::milliseconds(100));
  auto streamState = alsaAudioEmitter->getState();
  EXPECT_EQ(streamState.state, AudioGraphNodeState::ERROR);
  EXPECT_EQ(streamState.error->message, "Fake error message");
  stopPlaying();
}

TEST_F(AlsaAudioEmitterTest, slow_output_node_goes_into_preparing) {
  const size_t duration = 2000;
  auto outputNode = std::make_shared<SlowOutputNode>(duration, 1000);
  connect(outputNode);
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

  stopPlaying();
}

TEST_F(AlsaAudioEmitterTest, test_seekToForward) {
  const auto totalDuration = 2000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  play(outputNode);
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
  stopPlaying();
}

TEST_F(AlsaAudioEmitterTest, test_seekBackwards) {
  const auto totalDuration = 2000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  play(outputNode);
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
  stopPlaying();
}

namespace {
class UnseekableSineNode : public SineWaveNode {
public:
  UnseekableSineNode() : AudioGraphNode(1), SineWaveNode(1, 440, 2000) {}
  size_t seekTo(size_t) override { return size_t(-1); }
};
} // namespace

TEST_F(AlsaAudioEmitterTest, ARefusedSeekIsRefusedAndPlaybackCarriesOn) {
  play(std::make_shared<UnseekableSineNode>());
  waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING);

  EXPECT_EQ(alsaAudioEmitter->seek(1000), size_t(-1));
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);
  stopPlaying();
}

TEST_F(AlsaAudioEmitterTest, test_seekWhenStopped) {
  EXPECT_EQ(alsaAudioEmitter->seek(0), -1);
}

TEST_F(AlsaAudioEmitterTest, test_seekAfterFinished) {
  const auto totalDuration = 2000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  play(outputNode);
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
  play(outputNode);
  waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(alsaAudioEmitter->seek(totalDuration + 100), totalDuration);
  auto state = waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED,
                             std::chrono::milliseconds(2000));
  EXPECT_EQ(state.state, AudioGraphNodeState::FINISHED);
}

TEST_F(AlsaAudioEmitterTest, test_seekToEndAndBack) {
  const auto totalDuration = 2000;
  auto outputNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  play(outputNode);
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
  play(firstNode);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);

  EXPECT_EQ(alsaAudioEmitter->seek(totalDuration + 200), totalDuration);
  EXPECT_EQ(waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED,
                          std::chrono::milliseconds(1000))
                .state,
            AudioGraphNodeState::FINISHED);

  stopPlaying();

  auto secondNode = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  play(secondNode);

  auto state = waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING,
                             std::chrono::milliseconds(1000));
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);

  EXPECT_EQ(waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED,
                          std::chrono::milliseconds(1500))
                .state,
            AudioGraphNodeState::FINISHED);
}

namespace {
constexpr size_t kFadeFrames = 480; // 10 ms at 48 kHz
double fallen(size_t k) { return 1.0 - double(k + 1) / double(kFadeFrames); }
double risen(size_t k) { return double(k + 1) / double(kFadeFrames); }

/// A counting source slow enough that a command lands before its end: the
/// null device would otherwise take it all at once.
class PacedCountingNode : public CountingNode {
public:
  // An inherited constructor would leave the virtual base without the id.
  PacedCountingNode(StreamId id, int32_t base, size_t frames,
                    Options options = {})
      : AudioGraphNode(id), CountingNode(id, base, frames, options) {}
  size_t read(void *data, size_t size) override {
    std::this_thread::sleep_for(std::chrono::microseconds(100));
    return CountingNode::read(data, std::min<size_t>(size, 1024 * 8));
  }
};

/// A paced counting source whose end can be brought forward, so that its
/// track ends inside a pause's fade.
class EndingCountingNode : public PacedCountingNode {
public:
  EndingCountingNode(StreamId id, int32_t base, size_t frames)
      : AudioGraphNode(id), PacedCountingNode(id, base, frames) {}

  void endAfter(size_t frames) { end = size_t(*streamReadPosition()) + frames; }
  size_t read(void *data, size_t size) override {
    const size_t at = size_t(*streamReadPosition());
    const size_t last = end.load();
    if (last != kNoEnd) {
      if (at >= last)
        return 0;
      size = std::min(size, (last - at) * 8);
    }
    const size_t got = PacedCountingNode::read(data, size);
    if (last != kNoEnd && at + got / 8 == last)
      setState(StreamState(AudioGraphNodeState::FINISHED));
    return got;
  }

private:
  static constexpr size_t kNoEnd = size_t(-1);
  std::atomic<size_t> end{kNoEnd};
};

/// A paced counting source whose input runs dry partway, as a network
/// stream's does, until released.
class StallingCountingNode : public PacedCountingNode {
public:
  StallingCountingNode(StreamId id, int32_t base, size_t frames)
      : AudioGraphNode(id), PacedCountingNode(id, base, frames) {}

  void stallAfter(size_t frames) {
    stallAt = size_t(*streamReadPosition()) + frames;
  }
  void release() {
    {
      std::lock_guard lock(mutex);
      stallAt = kNever;
    }
    released.notify_all();
    setState({AudioGraphNodeState::STREAMING, *streamReadPosition(), info});
  }
  size_t read(void *data, size_t size) override {
    const size_t at = size_t(*streamReadPosition());
    const size_t last = stallAt.load();
    if (at >= last)
      return 0;
    const size_t got =
        PacedCountingNode::read(data, std::min(size, (last - at) * 8));
    if (at + got / 8 == last) {
      info = getState().streamInfo;
      setState(StreamState(AudioGraphNodeState::PREPARING));
    }
    return got;
  }
  size_t waitForData(std::stop_token token, size_t size) override {
    std::unique_lock lock(mutex);
    released.wait(lock, token, [this] { return !stalled(); });
    lock.unlock();
    return stalled() ? 0 : PacedCountingNode::waitForData(token, size);
  }
  size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds,
                        size_t size) override {
    return stalled() ? 0 : PacedCountingNode::waitForData(token, size);
  }

private:
  static constexpr size_t kNever = size_t(-1);
  std::atomic<size_t> stallAt{kNever};
  std::mutex mutex;
  std::condition_variable_any released;
  std::optional<StreamInfo> info;

  bool stalled() const { return size_t(*streamReadPosition()) >= stallAt; }
};

/// A paced counting source whose read blocks once, @p at frames in, for
/// @p stall: a decoder held up, which the emitter cannot see coming.
class BlockingCountingNode : public PacedCountingNode {
public:
  BlockingCountingNode(StreamId id, int32_t base, size_t frames, size_t at,
                       std::chrono::milliseconds stall)
      : AudioGraphNode(id), PacedCountingNode(id, base, frames), at(at),
        stall(stall) {}
  size_t read(void *data, size_t size) override {
    if (size_t(*streamReadPosition()) >= at && !blocked.exchange(true))
      std::this_thread::sleep_for(stall);
    return PacedCountingNode::read(data, size);
  }

private:
  const size_t at;
  const std::chrono::milliseconds stall;
  std::atomic<bool> blocked{false};
};

/// The stage's fades, running @p onFadeOut as a pause's fade begins.
class HookedFade : public OutputFade {
public:
  HookedFade(std::shared_ptr<OutputStage> stage,
             std::function<void()> onFadeOut)
      : stage(std::move(stage)), onFadeOut(std::move(onFadeOut)) {}
  void fadeOut() override {
    onFadeOut();
    stage->fadeOut();
  }
  void fadeIn() override { stage->fadeIn(); }
  void settle(double level) override { stage->settle(level); }
  bool silent() const override { return stage->silent(); }

private:
  const std::shared_ptr<OutputStage> stage;
  const std::function<void()> onFadeOut;
};

/// A counting source played behind an output stage the emitter fades with,
/// and what reached the device, captured by ALSA's file plugin. The null
/// device behind it takes everything at once, so a pause or a seek lands
/// wherever writing has got to, and nothing waits for real time.
class FadedCapture : public ::testing::Test {
protected:
  const std::filesystem::path directory =
      std::filesystem::temp_directory_path() /
      ("kalinka-fades-" + std::to_string(getpid()));
  const std::filesystem::path file = directory / "written.raw";
  std::optional<ScopedAlsaConfig> alsa;
  std::shared_ptr<PacedCountingNode> source;
  std::shared_ptr<OutputStage> stage =
      std::make_shared<OutputStage>(std::vector<StreamAudioFormat>{});
  std::shared_ptr<AlsaAudioEmitter> emitter;
  std::mutex reportedMutex;
  std::vector<StreamState> reported;

  FadedCapture() {
    std::filesystem::create_directories(directory);
    alsa.emplace("pcm.capture { type file slave.pcm { type null } file \"" +
                 file.string() + "\" format raw }\n");
    emitter = std::make_shared<AlsaAudioEmitter>(
        Config{{"output.alsa.device", "capture"},
               {"output.alsa.latency_ms", "80"},
               {"output.alsa.period_ms", "20"}});
  }

  ~FadedCapture() override {
    emitter.reset();
    alsa.reset();
    std::error_code error;
    std::filesystem::remove_all(directory, error);
  }

  void play(size_t frames) {
    source = std::make_shared<PacedCountingNode>(1, 1000000, frames);
    stage->connectTo(source);
    emitter->fadeWith(stage);
    emitter->connectTo(stage);
    ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::STREAMING).state,
              AudioGraphNodeState::STREAMING);
  }

  /// The left samples written, once the source has played to its end.
  std::vector<int32_t> written() {
    EXPECT_EQ(waitForStatus(*emitter, AudioGraphNodeState::FINISHED,
                            std::chrono::seconds(30))
                  .state,
              AudioGraphNodeState::FINISHED);
    return stopped();
  }

  /// The left samples written, once the emitter has been stopped.
  std::vector<int32_t> stopped() {
    emitter->disconnect(stage);
    std::ifstream in(file, std::ios::binary);
    std::vector<int32_t> frame(2), left;
    while (in.read(reinterpret_cast<char *>(frame.data()), 8))
      left.push_back(frame[0]);
    return left;
  }

  /// The first frame of @p left that is not the source's own.
  size_t firstChanged(const std::vector<int32_t> &left) const {
    size_t at = 0;
    while (at < left.size() && left[at] == source->value(at))
      ++at;
    return at;
  }

  /// @p left is the source fading out where it was taken back, silence,
  /// and then @p after.
  void expectCut(const std::vector<int32_t> &left,
                 const std::vector<int32_t> &after = {}) const {
    size_t at = firstChanged(left);
    ASSERT_GT(at, 0u);
    ASSERT_GT(left.size(), at + kFadeFrames) << "the rest taken back";
    expectFade(left, at, at, true);
    at += kFadeFrames;
    while (at < left.size() && left[at] == 0)
      ++at;
    ASSERT_EQ(left.size() - at, after.size()) << "frames after the silence";
    for (size_t k = 0; k < after.size(); ++k)
      ASSERT_EQ(left[at + k], after[k]) << "frame " << k << " after the silence";
  }

  /// Keeps every state the emitter reports from now on.
  void recordReports() {
    emitter->onStateChange([this](AudioGraphNode *, StreamState state) {
      std::lock_guard lock(reportedMutex);
      reported.push_back(state);
      return true;
    });
  }

  /// Whether, since recordReports(), the emitter reported @p state for @p id.
  bool hasReported(AudioGraphNodeState state, StreamId id) {
    std::lock_guard lock(reportedMutex);
    return std::any_of(reported.begin(), reported.end(),
                       [&](const StreamState &seen) {
                         return seen.state == state && seen.streamId == id;
                       });
  }

  /// Waits until the emitter reports @p state for stream @p id, which a
  /// wait for the state alone would take from the stream before it.
  void waitFor(AudioGraphNodeState state, StreamId id) {
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(10);
    for (auto now = emitter->getState();
         !(now.state == state && now.streamId == id);
         now = emitter->getState()) {
      ASSERT_LT(std::chrono::steady_clock::now(), deadline);
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
  }

  /// Plays a stalling source of @p frames frames and pauses it, its input
  /// running dry @p tail frames into the pause's fade.
  std::shared_ptr<StallingCountingNode> stallInsideAPause(size_t frames,
                                                          size_t tail) {
    auto stalling = std::make_shared<StallingCountingNode>(1, 1000000, frames);
    emitter->fadeWith(std::make_shared<HookedFade>(
        stage, [stalling, tail] { stalling->stallAfter(tail); }));
    source = stalling;
    stage->connectTo(stalling);
    emitter->connectTo(stage);
    EXPECT_EQ(waitForStatus(*emitter, AudioGraphNodeState::STREAMING).state,
              AudioGraphNodeState::STREAMING);
    emitter->pause(true);
    EXPECT_EQ(waitForStatus(*emitter, AudioGraphNodeState::PREPARING,
                            std::chrono::seconds(10))
                  .state,
              AudioGraphNodeState::PREPARING)
        << "the input ran dry";
    return stalling;
  }

  /// @p left is the whole source of @p frames frames, its pause's fade cut
  /// short @p tail frames in by the input running dry: the queue's last
  /// 10 ms fade out over it, and the input fades back in where it ran dry.
  void expectStalledPause(const std::vector<int32_t> &left, size_t frames,
                          size_t tail) const {
    ASSERT_EQ(left.size(), frames) << "nothing lost or repeated";
    const size_t faded = firstChanged(left);
    const size_t dry = faded + kFadeFrames;
    const size_t paused = dry - tail;
    for (size_t k = 0; k < kFadeFrames; ++k) {
      const size_t frame = faded + k;
      const double gain =
          fallen(k) * (frame < paused ? 1.0 : fallen(frame - paused));
      ASSERT_LE(std::abs(double(left[frame]) - gain * source->value(frame)),
                4.0)
          << "fading out, frame " << k;
    }
    expectFade(left, dry, dry, false);
    for (size_t frame = dry + kFadeFrames; frame < frames; ++frame)
      ASSERT_EQ(left[frame], source->value(frame)) << "frame " << frame;
  }

  /// Plays @p first and then @p rest through @p switcher, and pauses so
  /// that the fade ends with @p first: the next track's run starts paused.
  void pauseAtTheEndOf(
      const std::shared_ptr<AudioStreamSwitcher> &switcher,
      const std::shared_ptr<EndingCountingNode> &first,
      const std::vector<std::shared_ptr<PacedCountingNode>> &rest) {
    emitter->fadeWith(std::make_shared<HookedFade>(
        stage, [first] { first->endAfter(kFadeFrames); }));
    source = first;
    stage->connectTo(switcher);
    switcher->connectTo(first);
    for (const auto &track : rest)
      switcher->connectTo(track);
    emitter->connectTo(stage);
    ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::STREAMING).state,
              AudioGraphNodeState::STREAMING);
    emitter->pause(true);
    waitFor(AudioGraphNodeState::PAUSED, 2);
  }

  /// Plays @p tracks one after another through @p switcher, the first of
  /// them as the source.
  void playThrough(
      const std::shared_ptr<AudioStreamSwitcher> &switcher,
      const std::vector<std::shared_ptr<PacedCountingNode>> &tracks) {
    source = tracks.front();
    stage->connectTo(switcher);
    for (const auto &track : tracks)
      switcher->connectTo(track);
    emitter->fadeWith(stage);
    emitter->connectTo(stage);
    ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::STREAMING).state,
              AudioGraphNodeState::STREAMING);
  }

  /// Frames from @p at on fade as the stage's fade does: down from the
  /// source's frames from @p frame, or up to them.
  void expectFade(const std::vector<int32_t> &left, size_t at, size_t frame,
                  bool down) const {
    for (size_t k = 0; k < kFadeFrames; ++k) {
      const double rise = double(k + 1) / double(kFadeFrames);
      const double gain = down ? 1.0 - rise : rise;
      ASSERT_LE(
          std::abs(double(left[at + k]) - gain * source->value(frame + k)), 2.0)
          << (down ? "fading out" : "fading in") << ", frame " << k;
    }
  }
};
} // namespace

TEST_F(FadedCapture, APauseFadesOutAndItsResumeFadesInLosingNothing) {
  constexpr size_t kFrames = 1'000'000;
  play(kFrames);
  emitter->pause(true);
  ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::PAUSED,
                          std::chrono::seconds(10))
                .state,
            AudioGraphNodeState::PAUSED)
      << "paused once the fade has been written";
  emitter->pause(false);
  const auto left = written();
  ASSERT_GE(left.size(), kFrames);
  const size_t silence = left.size() - kFrames;
  const size_t paused = firstChanged(left);
  ASSERT_GT(paused, 0u);
  ASSERT_LT(paused + 2 * kFadeFrames, kFrames);
  expectFade(left, paused, paused, true);
  for (size_t k = 0; k < silence; ++k)
    ASSERT_EQ(left[paused + kFadeFrames + k], 0) << "silence while paused";
  expectFade(left, paused + kFadeFrames + silence, paused + kFadeFrames, false);
  for (size_t frame = paused + 2 * kFadeFrames; frame < kFrames; ++frame)
    ASSERT_EQ(left[frame + silence], source->value(frame))
        << "frame " << frame << " after the resume";
}

TEST_F(FadedCapture, ASeekFadesWhatTheDeviceHeldOutAndWhatItLandedOnIn) {
  constexpr size_t kFrames = 1'000'000;
  constexpr size_t kTargetMs = 15'000;
  constexpr size_t kTarget = kTargetMs * 48;
  play(kFrames);
  ASSERT_EQ(emitter->seek(kTargetMs), kTargetMs);
  const auto left = written();
  // The file plugin takes back what it has not yet written out, a buffer's
  // worth here: the device's queue, but for the margin left playing.
  const size_t taken = firstChanged(left);
  ASSERT_GT(taken, 0u);
  ASSERT_LT(taken, kTarget);
  expectFade(left, taken, taken, true);
  size_t landed = taken + kFadeFrames;
  while (landed < left.size() && left[landed] == 0)
    ++landed;
  EXPECT_GT(landed - taken, kFadeFrames) << "the rest taken back is silent";
  ASSERT_EQ(left.size(), landed + (kFrames - kTarget));
  expectFade(left, landed, kTarget, false);
  for (size_t frame = kTarget + kFadeFrames; frame < kFrames; ++frame)
    ASSERT_EQ(left[landed + frame - kTarget], source->value(frame))
        << "frame " << frame << " after the seek";
}

TEST_F(FadedCapture, TheNextTrackAfterAPausePlaysAtFullLevel) {
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  stage->connectTo(switcher);
  source = std::make_shared<PacedCountingNode>(1, 1000000, 1'000'000);
  auto next = std::make_shared<PacedCountingNode>(2, 2000000, 100'000);
  switcher->connectTo(source);
  switcher->connectTo(next);
  emitter->fadeWith(stage);
  emitter->connectTo(stage);
  ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::STREAMING).state,
            AudioGraphNodeState::STREAMING);
  emitter->pause(true);
  ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::PAUSED,
                          std::chrono::seconds(10))
                .state,
            AudioGraphNodeState::PAUSED);
  // Skipping ahead while paused: what played is removed, and the next
  // track takes over in a run of its own.
  emitter->cut(1);
  switcher->disconnect(source);
  const auto left = written();
  size_t at = firstChanged(left);
  ASSERT_GT(at, 0u);
  expectFade(left, at, at, true);
  at += kFadeFrames;
  while (at < left.size() && left[at] == 0)
    ++at;
  ASSERT_EQ(left.size() - at, 100'000u);
  for (size_t frame = 0; frame < 100'000; ++frame)
    ASSERT_EQ(left[at + frame], next->value(frame)) << "frame " << frame;
}

TEST_F(FadedCapture, APauseFadingAcrossAGaplessJoinFadesIntoTheNextTrack) {
  constexpr size_t kNext = 100'000, kTail = 100;
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  stage->connectTo(switcher);
  auto first = std::make_shared<EndingCountingNode>(1, 1000000, 1'000'000);
  auto next = std::make_shared<PacedCountingNode>(2, 2000000, kNext);
  source = first;
  switcher->connectTo(first);
  switcher->connectTo(next);
  emitter->fadeWith(
      std::make_shared<HookedFade>(stage, [first] { first->endAfter(kTail); }));
  emitter->connectTo(stage);
  ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::STREAMING).state,
            AudioGraphNodeState::STREAMING);
  emitter->pause(true);
  const auto pausedState = waitForStatus(*emitter, AudioGraphNodeState::PAUSED,
                                         std::chrono::seconds(10));
  ASSERT_EQ(pausedState.state, AudioGraphNodeState::PAUSED);
  EXPECT_EQ(pausedState.streamId, 2u);
  ASSERT_TRUE(pausedState.streamInfo.has_value());
  EXPECT_EQ(pausedState.streamInfo->streamSize, kNext)
      << "paused in the track joined, and named as such";
  emitter->pause(false);
  const auto left = written();
  const size_t paused = firstChanged(left);
  ASSERT_GE(left.size(), paused + kTail + kNext);
  const size_t silence = left.size() - (paused + kTail + kNext);
  for (size_t k = 0; k < kFadeFrames; ++k) {
    const double v =
        k < kTail ? first->value(paused + k) : next->value(k - kTail);
    ASSERT_LE(std::abs(double(left[paused + k]) - fallen(k) * v), 2.0)
        << "fading out across the join, frame " << k;
  }
  for (size_t k = 0; k < silence; ++k)
    ASSERT_EQ(left[paused + kFadeFrames + k], 0) << "silence while paused";
  const size_t resumed = kFadeFrames - kTail;
  const size_t at = paused + kFadeFrames + silence;
  for (size_t k = 0; k < kFadeFrames; ++k)
    ASSERT_LE(
        std::abs(double(left[at + k]) - risen(k) * next->value(resumed + k)),
        2.0)
        << "fading in, frame " << k;
  for (size_t frame = resumed + kFadeFrames; frame < kNext; ++frame)
    ASSERT_EQ(left[at + frame - resumed], next->value(frame))
        << "next track's frame " << frame;
}

TEST_F(FadedCapture, ATrackEndingInsideAPausesFadeLeavesTheNextOnePaused) {
  constexpr size_t kNext = 100'000, kTail = 100;
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  stage->connectTo(switcher);
  auto first = std::make_shared<EndingCountingNode>(1, 1000000, 1'000'000);
  // Another rate: no gapless join, so the next track starts a run of its own.
  auto next = std::make_shared<PacedCountingNode>(
      2, 2000000, kNext, CountingNode::Options{.rate = 44100});
  source = first;
  switcher->connectTo(first);
  switcher->connectTo(next);
  emitter->fadeWith(
      std::make_shared<HookedFade>(stage, [first] { first->endAfter(kTail); }));
  emitter->connectTo(stage);
  ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::STREAMING).state,
            AudioGraphNodeState::STREAMING);
  emitter->pause(true);
  ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::PAUSED,
                          std::chrono::seconds(10))
                .state,
            AudioGraphNodeState::PAUSED)
      << "the pause holds into the next track";
  emitter->pause(false);
  const auto left = written();
  const size_t paused = firstChanged(left);
  ASSERT_EQ(left.size(), paused + kTail + kNext)
      << "the first track played out, and nothing was written while paused";
  for (size_t k = 0; k < kTail; ++k)
    ASSERT_LE(std::abs(double(left[paused + k]) -
                       fallen(k) * first->value(paused + k)),
              2.0)
        << "fading out to the end, frame " << k;
  for (size_t frame = 0; frame < kNext; ++frame)
    ASSERT_EQ(left[paused + kTail + frame], next->value(frame))
        << "next track's frame " << frame << ", from its start";
}

namespace {
std::shared_ptr<PacedCountingNode> track(StreamId id, size_t frames) {
  return std::make_shared<PacedCountingNode>(id, int32_t(id) * 1000000, frames);
}
} // namespace

TEST_F(FadedCapture, AStopFadesOutWhatTheDeviceHeld) {
  play(1'000'000);
  expectCut(stopped());
}

TEST_F(FadedCapture, ACutTrackFadesOutAndTheNextStartsInARunOfItsOwn) {
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  const auto prepared = track(2, 100'000), next = track(3, 100'000);
  playThrough(switcher, {track(1, 1'000'000), prepared, next});
  recordReports();
  // A skip as the renderer makes one: what plays and what was prepared go.
  emitter->cut(1);
  emitter->cut(2);
  switcher->disconnect(prepared);
  switcher->disconnect(source);
  expectCut(written(), next->values(0, 100'000));
  EXPECT_TRUE(hasReported(AudioGraphNodeState::PREPARING, 3))
      << "the next track starts a run of its own";
}

TEST_F(FadedCapture, ACutAfterAGaplessJoinFadesTheTrackJoined) {
  constexpr size_t kFirst = 50'000;
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  const auto first = track(1, kFirst), joined = track(2, 1'000'000),
             next = track(3, 100'000);
  playThrough(switcher, {first, joined, next});
  waitFor(AudioGraphNodeState::STREAMING, 2);
  emitter->cut(2);
  switcher->disconnect(joined);
  const auto left = written();
  ASSERT_GT(left.size(), kFirst);
  for (size_t frame = 0; frame < kFirst; ++frame)
    ASSERT_EQ(left[frame], first->value(frame)) << "first track's " << frame;
  source = joined;
  expectCut(std::vector<int32_t>(left.begin() + kFirst, left.end()),
            next->values(0, 100'000));
}

TEST_F(FadedCapture, CuttingTheLastTrackFadesItOut) {
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  playThrough(switcher, {track(1, 1'000'000)});
  emitter->cut(1);
  switcher->disconnect(source);
  expectCut(written());
}

TEST_F(FadedCapture, ACutOfATrackPlayedOutDoesNotReachTheNext) {
  constexpr size_t kFrames = 50'000;
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  playThrough(switcher, {track(1, kFrames)});
  waitFor(AudioGraphNodeState::FINISHED, 1);
  emitter->cut(1);
  switcher->disconnect(source);
  const auto next = track(2, kFrames);
  switcher->connectTo(next);
  waitFor(AudioGraphNodeState::FINISHED, 2);
  auto both = source->values(0, kFrames);
  const auto after = next->values(0, kFrames);
  both.insert(both.end(), after.begin(), after.end());
  const auto left = written();
  ASSERT_EQ(left.size(), both.size());
  for (size_t frame = 0; frame < both.size(); ++frame)
    ASSERT_EQ(left[frame], both[frame]) << "frame " << frame;
}

TEST_F(FadedCapture, ACutAfterAStopDoesNotReachTheNextPlay) {
  constexpr size_t kFrames = 50'000;
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  playThrough(switcher, {track(1, 1'000'000)});
  // A stop as the player makes one: the emitter first, then the streams.
  emitter->disconnect(stage);
  emitter->cut(1);
  switcher->disconnect(source);
  const auto again = track(1, kFrames);
  switcher->connectTo(again);
  emitter->connectTo(stage);
  const auto left = written();
  ASSERT_GE(left.size(), kFrames);
  const size_t start = left.size() - kFrames;
  for (size_t frame = 0; frame < kFrames; ++frame)
    ASSERT_EQ(left[start + frame], again->value(frame)) << "frame " << frame;
}

TEST_F(FadedCapture, CuttingAStreamNotYetPlayingLeavesTheOnePlayingAlone) {
  constexpr size_t kFrames = 200'000;
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  const auto prepared = track(2, kFrames);
  playThrough(switcher, {track(1, kFrames), prepared});
  emitter->cut(2);
  switcher->disconnect(prepared);
  const auto left = written();
  ASSERT_EQ(left.size(), kFrames);
  ASSERT_EQ(firstChanged(left), kFrames) << "played to its end untouched";
}

TEST_F(FadedCapture, AStopBeforeTheDeviceOpensHasNothingToFade) {
  emitter->fadeWith(stage);
  emitter->connectTo(stage);
  emitter->disconnect(stage);
  EXPECT_EQ(emitter->getState().state, AudioGraphNodeState::STOPPED);
}

TEST_F(FadedCapture, ATrackEndingAsAPausesFadeEndsLeavesTheNextOnePaused) {
  constexpr size_t kNext = 100'000;
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  const auto first = std::make_shared<EndingCountingNode>(1, 1000000, 1'000'000);
  const auto next = track(2, kNext);
  pauseAtTheEndOf(switcher, first, {next});
  emitter->pause(false);
  const auto left = written();
  const size_t paused = firstChanged(left);
  ASSERT_EQ(left.size(), paused + kFadeFrames + kNext);
  expectFade(left, paused, paused, true);
  for (size_t frame = 0; frame < kNext; ++frame)
    ASSERT_EQ(left[paused + kFadeFrames + frame], next->value(frame))
        << "next track's frame " << frame;
}

TEST_F(FadedCapture, ASkipInsideAPausesFadePlaysTheNextTrack) {
  constexpr size_t kNext = 100'000, kTail = 100;
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  const auto first = std::make_shared<EndingCountingNode>(1, 1000000, 1'000'000);
  const auto next = track(2, kNext);
  // The skip's removal, as the switcher sees it: the track ends there.
  emitter->fadeWith(std::make_shared<HookedFade>(stage, [this, first] {
    emitter->cut(1);
    first->endAfter(kTail);
  }));
  source = first;
  stage->connectTo(switcher);
  switcher->connectTo(first);
  switcher->connectTo(next);
  emitter->connectTo(stage);
  ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::STREAMING).state,
            AudioGraphNodeState::STREAMING);
  emitter->pause(true);
  waitFor(AudioGraphNodeState::STREAMING, 2);
  const auto left = written();
  ASSERT_GE(left.size(), kNext);
  const size_t start = left.size() - kNext;
  for (size_t frame = 0; frame < kNext; ++frame)
    ASSERT_EQ(left[start + frame], next->value(frame))
        << "next track's frame " << frame;
}

TEST_F(FadedCapture, AnInputRunningDryInAPausesFadeHoldsThePauseLosingNothing) {
  constexpr size_t kFrames = 1'000'000, kTail = 100;
  const auto stalling = stallInsideAPause(kFrames, kTail);
  stalling->release();
  ASSERT_EQ(waitForStatus(*emitter, AudioGraphNodeState::PAUSED,
                          std::chrono::seconds(10))
                .state,
            AudioGraphNodeState::PAUSED)
      << "the pause holds once the input is back";
  emitter->pause(false);
  expectStalledPause(written(), kFrames, kTail);
}

TEST_F(FadedCapture, AResumeWhileTheInputIsDryPlaysOnceItIsBack) {
  constexpr size_t kFrames = 1'000'000, kTail = 100;
  const auto stalling = stallInsideAPause(kFrames, kTail);
  emitter->pause(false);
  stalling->release();
  expectStalledPause(written(), kFrames, kTail);
}

TEST_F(FadedCapture, ASeekWhileAPauseIsHeldForTheInputPlaysFromThere) {
  const auto stalling = stallInsideAPause(1'000'000, 100);
  ASSERT_EQ(emitter->seek(0), 0u);
  EXPECT_EQ(waitForStatus(*emitter, AudioGraphNodeState::STREAMING,
                          std::chrono::seconds(10))
                .state,
            AudioGraphNodeState::STREAMING)
      << "as a seek under any pause plays";
  stalling->release();
}

TEST_F(FadedCapture, AStopForgetsAPauseHeldForTheInput) {
  constexpr size_t kFrames = 50'000;
  const auto stalling = stallInsideAPause(1'000'000, 100);
  emitter->disconnect(stage);
  stage->disconnect(stalling);
  source = track(2, kFrames);
  stage->connectTo(source);
  emitter->fadeWith(stage);
  emitter->connectTo(stage);
  const auto left = written();
  ASSERT_GE(left.size(), kFrames);
  const size_t start = left.size() - kFrames;
  for (size_t frame = 0; frame < kFrames; ++frame)
    ASSERT_EQ(left[start + frame], source->value(frame))
        << "frame " << frame << ", played at once at full level";
}

TEST_F(FadedCapture, ASkipWhileAPauseHoldsTheNextTrackPlaysTheOneAfter) {
  constexpr size_t kNext = 100'000;
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  const auto first = std::make_shared<EndingCountingNode>(1, 1000000, 1'000'000);
  const auto held = track(2, kNext), after = track(3, kNext);
  pauseAtTheEndOf(switcher, first, {held, after});
  emitter->cut(2);
  switcher->disconnect(held);
  waitFor(AudioGraphNodeState::STREAMING, 3);
  const auto left = written();
  const size_t paused = firstChanged(left);
  ASSERT_EQ(left.size(), paused + kFadeFrames + kNext)
      << "nothing written while the run was held";
  for (size_t frame = 0; frame < kNext; ++frame)
    ASSERT_EQ(left[paused + kFadeFrames + frame], after->value(frame))
        << "the track after's frame " << frame;
}

namespace {
/// FadedCapture's playback to a device that plays in real time, as a card
/// does, so that a source held up for longer than the buffer starves it.
class PacedCapture : public FadedCapture {
protected:
  PacedCapture() {
    emitter.reset();
    alsa.emplace("pcm_type.kalinka_paced { lib \"" KALINKA_PACED_PCM "\" }\n"
                 "pcm.paced { type kalinka_paced file \"" +
                 file.string() + "\" }\n");
    emitter = std::make_shared<AlsaAudioEmitter>(
        Config{{"output.alsa.device", "paced"},
               {"output.alsa.latency_ms", "80"},
               {"output.alsa.period_ms", "20"}});
  }

  bool failed() {
    std::lock_guard lock(reportedMutex);
    return std::ranges::any_of(reported, [](const StreamState &seen) {
      return seen.state == AudioGraphNodeState::ERROR;
    });
  }
};

// Held up for twice the device's 80 ms.
constexpr auto kStarving = std::chrono::milliseconds(160);
} // namespace

TEST_F(PacedCapture, AnUnderrunCostsADropoutAndTheTrackPlaysOn) {
  constexpr size_t kFrames = 24000;
  recordReports();
  playThrough(std::make_shared<AudioStreamSwitcher>(),
              {std::make_shared<BlockingCountingNode>(1, 1000000, kFrames, 4800,
                                                      kStarving)});
  const auto left = written();

  EXPECT_FALSE(failed());
  ASSERT_FALSE(left.empty());
  EXPECT_EQ(left.front(), source->value(0));
  EXPECT_LT(left.size(), kFrames) << "what came while starved is dropped";
  EXPECT_EQ(left.back(), source->value(kFrames - 1)) << "played to its end";
}

TEST_F(PacedCapture, ANextTrackLateAtAGaplessJoinStillPlays) {
  constexpr size_t kFrames = 12000;
  const auto next = std::make_shared<BlockingCountingNode>(2, 2000000, kFrames,
                                                           0, kStarving);
  recordReports();
  playThrough(std::make_shared<AudioStreamSwitcher>(),
              {std::make_shared<PacedCountingNode>(1, 1000000, kFrames), next});
  waitFor(AudioGraphNodeState::FINISHED, 2);
  const auto left = stopped();

  EXPECT_FALSE(failed());
  EXPECT_TRUE(hasReported(AudioGraphNodeState::STREAMING, 2));
  ASSERT_FALSE(left.empty());
  EXPECT_LT(left.size(), 2 * kFrames) << "what came while starved is dropped";
  EXPECT_EQ(left.back(), next->value(kFrames - 1)) << "the next track played";
}

TEST_F(PacedCapture, TheQueuesLastTrackIsHeardToItsEnd) {
  constexpr size_t kFrames = 12000;
  playThrough(std::make_shared<AudioStreamSwitcher>(),
              {std::make_shared<PacedCountingNode>(1, 1000000, kFrames)});
  const auto left = written();

  ASSERT_EQ(left.size(), kFrames) << "what the device held when it ended";
  EXPECT_TRUE(left == source->values(0, kFrames));
}

TEST_F(PacedCapture, ATrackEndingItsRunIsHeardToItsEnd) {
  // A next track in another format starts a run of its own.
  constexpr size_t kFrames = 12000;
  const auto next = std::make_shared<PacedCountingNode>(
      2, 2000000, kFrames, CountingNode::Options{.rate = 44100});
  playThrough(std::make_shared<AudioStreamSwitcher>(),
              {std::make_shared<PacedCountingNode>(1, 1000000, kFrames), next});
  const auto left = written();

  ASSERT_EQ(left.size(), 2 * kFrames) << "what the device held at each end";
  EXPECT_TRUE(std::equal(left.begin(), left.begin() + kFrames,
                         source->values(0, kFrames).begin()));
  EXPECT_TRUE(std::equal(left.begin() + kFrames, left.end(),
                         next->values(0, kFrames).begin()));
}

TEST_F(PacedCapture, AJoinRefilledAfterAnUnderrunIsReportedAsTheQueueEnds) {
  // The underrun's refill takes the rest of the first track and all of the
  // second: the run ends before the device is started again.
  const auto last = std::make_shared<PacedCountingNode>(2, 2000000, 1000);
  recordReports();
  playThrough(std::make_shared<AudioStreamSwitcher>(),
              {std::make_shared<BlockingCountingNode>(1, 1000000, 6000, 4800,
                                                      kStarving),
               last});
  const auto left = written();

  EXPECT_FALSE(failed());
  EXPECT_TRUE(hasReported(AudioGraphNodeState::STREAMING, 2));
  ASSERT_FALSE(left.empty());
  EXPECT_EQ(left.back(), last->value(999)) << "the last track played";
}
