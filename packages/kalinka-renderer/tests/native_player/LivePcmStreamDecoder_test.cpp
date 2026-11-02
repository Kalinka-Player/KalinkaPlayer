#include "AlsaAudioEmitter.h"
#include "AudioStreamSwitcher.h"
#include "Config.h"
#include "ContainerTestSupport.h"
#include "LivePcmStreamDecoder.h"
#include "StateMonitor.h"
#include <thread>

#include <gtest/gtest.h>

namespace {
using container_test::Bytes;
using container_test::BytesInput;
using container_test::drain;

void number(Bytes &out, uint64_t value, unsigned width) {
  for (unsigned i = 0; i < width; ++i)
    out.push_back(value >> (8 * i));
}

Bytes header(unsigned code = 2, unsigned rate = 48000) {
  Bytes out{'K', 'A', 'L', 'P', 'C', 'M', '1', 0};
  number(out, rate, 4);
  number(out, 2, 2);
  number(out, code, 2);
  number(out, 1, 4);
  number(out, 60, 4);
  number(out, 250, 4);
  number(out, 0, 4);
  return out;
}

void record(Bytes &out, uint64_t first, unsigned frames, const Bytes &pcm) {
  number(out, first, 8);
  number(out, frames, 4);
  number(out, pcm.size(), 4);
  out.insert(out.end(), pcm.begin(), pcm.end());
}
} // namespace

TEST(LivePcm, EveryLayoutDecodesAcrossFragmentedRecords) {
  const Bytes values[] = {{0x10, 0x27},
                          {0, 0, 0x20},
                          {0, 0, 0x20, 0},
                          {0, 0, 0, 0x20},
                          {0, 0, 0x80, 0x3e}};
  for (unsigned code = 1; code <= 5; ++code) {
    const auto &sample = values[code - 1];
    auto wire = header(code);
    Bytes pcm;
    for (size_t i = 0; i < 480 * 2; ++i)
      pcm.insert(pcm.end(), sample.begin(), sample.end());
    for (unsigned block = 0; block < 6; ++block)
      record(wire, 123456 + block * 480, 480, pcm);
    LivePcmStreamDecoder decoder(12);
    decoder.connectTo(std::make_shared<BytesInput>(wire, false, false));
    const auto decoded = drain(decoder);
    EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
    const auto info = decoder.getState().streamInfo;
    ASSERT_TRUE(info && info->live);
    const auto count = decoded.size() / sample.size();
    ASSERT_GT(count, 1920u);
    const double expected = code == 1 ? 10000. / 32768 : .25;
    for (size_t i = 1920; i < count; ++i) {
      const auto *at = decoded.data() + i * sample.size();
      double value;
      if (code == 5) {
        float floating;
        std::memcpy(&floating, at, sizeof(floating));
        value = floating;
      } else {
        uint32_t raw = 0;
        for (size_t byte = 0; byte < sample.size(); ++byte)
          raw |= uint32_t(at[byte]) << (8 * byte);
        value = raw / (code == 1 ? 32768. : code == 4 ? 2147483648. : 8388608.);
      }
      ASSERT_NEAR(value, expected, .0001) << "layout " << code;
    }
    ASSERT_TRUE(decoder.liveSourceFrame());
    EXPECT_GT(*decoder.liveSourceFrame(), 123456u + 1920);
    EXPECT_LE(*decoder.liveSourceFrame(), 123456u + 2880);
    EXPECT_EQ(decoder.seekTo(1), size_t(-1));
  }
}

TEST(LivePcm, RejectsMalformedHeadersAndRecords) {
  for (unsigned test = 0; test < 5; ++test) {
    auto wire = header();
    if (test == 0)
      wire[0] = 'X';
    if (test == 1)
      wire[12] = 8;
    if (test == 2)
      wire[14] = 99;
    if (test == 3)
      record(wire, 0, 480, Bytes(479 * 6));
    if (test == 4) {
      record(wire, 0, 480, Bytes(480 * 6));
      record(wire, 999, 480, Bytes(480 * 6));
    }
    LivePcmStreamDecoder decoder(1);
    decoder.connectTo(std::make_shared<BytesInput>(wire, false, false));
    drain(decoder);
    EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::ERROR) << test;
  }
}

TEST(LivePcm, ClockServoBoundsTwoHoursOfIndependentClockDrift) {
  for (double drift : {-.0002, -.0001, .0001, .0002}) {
    LiveClockServo servo(48000);
    double queued = 720, ratio = 1;
    for (int block = 0; block < 720000; ++block) {
      const double requested = servo.update(size_t(queued), 480);
      ratio = servo.active() ? requested : 1;
      queued += 480 * (1 + drift) / ratio - 480;
      ASSERT_GT(queued, 0);
      ASSERT_LT(queued, 1440);
    }
    EXPECT_NEAR(ratio, 1 + drift, .000003);
  }
}

TEST(LivePcm, ClockAdapterProcessesEveryLayoutWithinBoundedBlocks) {
  for (auto layout :
       {PCM16_LE, PCM24_3LE, PCM24_LE, PCM32_LE, PCM_FLOAT32_LE}) {
    LivePcmClock clock({48000, 2, unsigned(sampleBits(layout)), layout});
    Bytes silence(480 * 2 * sampleSize(layout));
    size_t frames = 0;
    for (int block = 0; block < 500; ++block) {
      const auto output = clock.process(silence, 500);
      frames += output.size() / (2 * sampleSize(layout));
      EXPECT_TRUE(std::all_of(output.begin(), output.end(),
                              [](uint8_t x) { return x == 0; }));
      EXPECT_LE(output.size(), (480 + 256) * 2 * sampleSize(layout));
    }
    EXPECT_GT(frames, 239000);
    EXPECT_LT(frames, 241000);
  }
}

// Withhold every PCM frame until the output explicitly reports readiness.
// This catches a deadlock where hardware setup waits for audio while Core
// waits for hardware setup, and exercises live ALSA settings on the null DAC.

namespace {
class GatedPcmInput : public AudioGraphOutputNode {
public:
  GatedPcmInput() {
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.streamType = StreamType::BYTES}});
    offer(header());
  }
  void offer(const Bytes &bytes) { buffer.write(bytes.data(), bytes.size()); }
  size_t read(void *out, size_t size) override {
    return buffer.read(static_cast<uint8_t *>(out), size);
  }
  size_t waitForData(std::stop_token token, size_t size) override {
    return buffer.waitForData(token, size);
  }
  size_t waitForDataFor(std::stop_token token,
                        std::chrono::milliseconds timeout,
                        size_t size) override {
    return buffer.waitForDataFor(token, timeout, size);
  }

private:
  Buffer<uint8_t> buffer{128 * 1024};
};
} // namespace

TEST(LivePcm, OutputReadinessPrecedesFirstPcmFrame) {
  using namespace std::chrono_literals;
  Config config{{"output.alsa.device", "null"},
                {"output.alsa.latency_ms", "1000"},
                {"output.alsa.period_ms", "200"}};
  auto input = std::make_shared<GatedPcmInput>();
  auto decoder = std::make_shared<LivePcmStreamDecoder>(42);
  decoder->connectTo(input);
  AlsaAudioEmitter emitter(config);
  StateMonitor states(&emitter);
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  switcher->connectTo(decoder);
  emitter.connectTo(switcher);
  bool ready = false;
  auto deadline = std::chrono::steady_clock::now() + 2s;
  while (!ready && std::chrono::steady_clock::now() < deadline) {
    while (states.hasData()) {
      const auto state = states.waitState();
      EXPECT_NE(state.state, AudioGraphNodeState::ERROR);
      if (state.liveReady) {
        ready = true;
        EXPECT_EQ(state.state, AudioGraphNodeState::PREPARING);
        EXPECT_EQ(state.streamId, 42);
      }
    }
    std::this_thread::sleep_for(1ms);
  }
  EXPECT_TRUE(ready);
  Bytes wire;
  for (unsigned block = 0; block < 10; ++block)
    record(wire, 10000 + block * 480, 480, Bytes(480 * 6));
  input->offer(wire);
  bool played = false;
  deadline = std::chrono::steady_clock::now() + 2s;
  while (!played && std::chrono::steady_clock::now() < deadline) {
    while (states.hasData())
      played |= states.waitState().state == AudioGraphNodeState::STREAMING;
    std::this_thread::sleep_for(1ms);
  }
  EXPECT_TRUE(played);
  emitter.disconnect(switcher);
  switcher->disconnect(decoder);
}

TEST(LivePcm, ClockCorrectionDoesNotIntroduceASecondStartupTransient) {
  LivePcmClock clock({48000, 2, 16, PCM16_LE});
  Bytes pcm(480 * 2 * 2);
  for (size_t sample = 0; sample < pcm.size(); sample += 2) {
    pcm[sample] = 0x10;
    pcm[sample + 1] = 0x27; // Constant 10000 on both channels.
  }
  int largestError = 0;
  for (unsigned block = 0; block < 250; ++block) {
    const auto output = clock.process(pcm, 720);
    if (block < 10)
      continue;
    for (size_t sample = 0; sample < output.size(); sample += 2) {
      const int value =
          int16_t(output[sample] | (unsigned(output[sample + 1]) << 8));
      largestError = std::max(largestError, std::abs(value - 10000));
    }
  }
  EXPECT_LE(largestError, 3);
}
