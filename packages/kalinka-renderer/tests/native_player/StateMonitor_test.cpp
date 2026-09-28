#include "AlsaAudioEmitter.h"
#include "AudioGraphNode.h"
#include "FileInputNode.h"
#include "FlacStreamDecoder.h"

#include <gtest/gtest.h>
#include <atomic>
#include <memory>
#include <thread>
#include <vector>

#include "TestHelpers.h"

class AudioGraphNodeTest : public ::testing::Test {
protected:
  Config config = {{"output.alsa.device", testDevice()},
                   {"output.alsa.buffer_size", "16384"},
                   {"output.alsa.period_size", "1024"},
                   {"fixups.alsa_reopen_device_with_new_format", "true"}};

  const std::string filename = testFile("tone440.flac");

  std::shared_ptr<FlacStreamDecoder> flacStreamDecoder;
  std::shared_ptr<AlsaAudioEmitter> alsaAudioEmitter;

  AudioGraphNodeTest()
      : flacStreamDecoder(std::make_shared<FlacStreamDecoder>(1, 65536)),
        alsaAudioEmitter(std::make_shared<AlsaAudioEmitter>(config)) {}
};

TEST_F(AudioGraphNodeTest, stateMonitor) {
  auto monitor = std::make_shared<StateMonitor>(alsaAudioEmitter.get());
  EXPECT_TRUE(monitor->isRunning());
  auto fileInputNode = std::make_shared<FileInputNode>(1, filename);
  flacStreamDecoder->connectTo(fileInputNode);
  alsaAudioEmitter->connectTo(flacStreamDecoder);
  waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED);
  std::vector<AudioGraphNodeState> seen;
  while (monitor->hasData()) {
    seen.push_back(monitor->waitState().state);
  }

  // Stopped, then preparing and streaming until the track runs out. Refilling
  // an emptied buffer is preparing again, so those two can come round more
  // than once without the run being any different.
  ASSERT_GE(seen.size(), 4u);
  EXPECT_EQ(seen.front(), AudioGraphNodeState::STOPPED);
  EXPECT_EQ(seen.back(), AudioGraphNodeState::FINISHED);
  for (size_t i = 1; i + 1 < seen.size(); ++i) {
    EXPECT_EQ(seen[i], i % 2 == 1 ? AudioGraphNodeState::PREPARING
                                  : AudioGraphNodeState::STREAMING);
  }

  monitor->stop();
  EXPECT_FALSE(monitor->isRunning());

  alsaAudioEmitter->disconnect(flacStreamDecoder);
  flacStreamDecoder->disconnect(fileInputNode);
}

// A stop that lands between the waiter's check and its sleep hangs the join.
TEST_F(AudioGraphNodeTest, stop_wakes_a_waiter_however_the_two_interleave) {
  for (int i = 0; i < 5000; ++i) {
    StateMonitor monitor(alsaAudioEmitter.get());
    std::atomic<bool> started = false;
    std::thread waiter([&monitor, &started] {
      while (monitor.isRunning()) {
        started = true;
        monitor.waitState();
      }
    });
    while (!started) {
    }
    monitor.stop();
    waiter.join();
  }
}