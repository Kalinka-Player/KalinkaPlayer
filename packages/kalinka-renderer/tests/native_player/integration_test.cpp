#include "AlsaAudioEmitter.h"
#include "AudioGraphHttpStream.h"
#include "AudioStreamSwitcher.h"
#include "FileInputNode.h"
#include "FlacStreamDecoder.h"
#include "SineWaveNode.h"

#include <gtest/gtest.h>
#include <memory>

#include "Config.h"
#include "LocalHttpServer.h"
#include "TestHelpers.h"
#include <Mp3StreamDecoder.h>
#include <thread>

class IntegrationTest : public ::testing::Test {
protected:
  Config config = {{"output.alsa.device", testDevice()},
                   {"output.alsa.buffer_size", "16384"},
                   {"output.alsa.period_size", "1024"},
                   {"fixups.alsa_reopen_device_with_new_format", "true"}};

  const std::string filename = testFile("tone440.flac");

  std::shared_ptr<FlacStreamDecoder> flacStreamDecoder;
  std::shared_ptr<AlsaAudioEmitter> alsaAudioEmitter;

  IntegrationTest()
      : flacStreamDecoder(std::make_shared<FlacStreamDecoder>(1, 65536)),
        alsaAudioEmitter(std::make_shared<AlsaAudioEmitter>(config)) {
    alsaAudioEmitter->setSoftwareVolume(0.0f);
  }
};

TEST_F(IntegrationTest, fileInputIntegration) {
  auto fileInputNode = std::make_shared<FileInputNode>(1, filename);
  flacStreamDecoder->connectTo(fileInputNode);
  flacStreamDecoder->waitForData(std::stop_token(), 8192);
  EXPECT_EQ(waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING,
                          std::chrono::milliseconds(1000))
                .state,
            AudioGraphNodeState::STREAMING);
  alsaAudioEmitter->connectTo(flacStreamDecoder);
  std::this_thread::sleep_for(std::chrono::milliseconds(1200));
  alsaAudioEmitter->disconnect(flacStreamDecoder);
  flacStreamDecoder->disconnect(fileInputNode);
}

TEST_F(IntegrationTest, httpInputIntegration) {
  LocalHttpServer server(filename);
  auto audioGraphHttpStream = std::make_shared<AudioGraphHttpStream>(1, 
      server.url("/ranged"), 16384);
  flacStreamDecoder->connectTo(audioGraphHttpStream);
  EXPECT_EQ(flacStreamDecoder->getState().state,
            AudioGraphNodeState::PREPARING);
  alsaAudioEmitter->connectTo(flacStreamDecoder);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED);

  alsaAudioEmitter->disconnect(flacStreamDecoder);
  flacStreamDecoder->disconnect(audioGraphHttpStream);
}

TEST_F(IntegrationTest, streamSwitch) {
  const int duration1 = 500;
  const int duration2 = 200;
  auto sineWaveNode440 = std::make_shared<SineWaveNode>(1, 440, duration1);
  auto sineWaveNode880 = std::make_shared<SineWaveNode>(1, 880, duration2);
  auto streamSwitchNode = std::make_shared<AudioStreamSwitcher>();
  EXPECT_EQ(streamSwitchNode->getState().state, AudioGraphNodeState::STOPPED);
  alsaAudioEmitter->connectTo(streamSwitchNode);
  EXPECT_EQ(alsaAudioEmitter->getState().state, AudioGraphNodeState::STOPPED);
  StateMonitor monitor(alsaAudioEmitter.get());
  streamSwitchNode->connectTo(sineWaveNode440);
  streamSwitchNode->connectTo(sineWaveNode880);

  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);

  AudioGraphNodeState states[] = {
      AudioGraphNodeState::STOPPED,        AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING,      AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::SOURCE_CHANGED, AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::FINISHED};

  const auto statesCount = sizeof(states) / sizeof(AudioGraphNodeState);

  int i = 0;
  for (; i < statesCount && monitor.hasData(); ++i) {
    auto state = monitor.waitState();
    EXPECT_EQ(state.state, states[i]) << "i=" << i;
  }

  EXPECT_EQ(i, statesCount);
}

TEST_F(IntegrationTest, fileInputSearchForward) {
  auto fileInputNode = std::make_shared<FileInputNode>(1, filename);
  flacStreamDecoder->connectTo(fileInputNode);
  auto state =
      waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  std::vector<uint8_t> buffer(4096);
  flacStreamDecoder->waitForData(std::stop_token(), 4096);
  flacStreamDecoder->read(buffer.data(), 4096);
  EXPECT_EQ(flacStreamDecoder->seekTo(16384), 16384);
  // Preparing, unless the seek landed before this line ran: it is a state
  // passed through, and a reader that samples can be too late for it.
  const auto afterSeek = flacStreamDecoder->getState().state;
  EXPECT_TRUE(afterSeek == AudioGraphNodeState::PREPARING ||
              afterSeek == AudioGraphNodeState::STREAMING)
      << "unexpected state after seeking: " << stateToString(afterSeek);
  state = waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  EXPECT_TRUE(state.streamInfo.has_value());
  EXPECT_EQ(state.position, 16384);
  flacStreamDecoder->waitForData(std::stop_token(), 4096);
  flacStreamDecoder->read(buffer.data(), 4096);
  flacStreamDecoder->disconnect(fileInputNode);
}

TEST_F(IntegrationTest, fileInputSearchBackward) {
  auto fileInputNode = std::make_shared<FileInputNode>(1, filename);
  flacStreamDecoder->connectTo(fileInputNode);
  auto state =
      waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  std::vector<uint8_t> buffer(4096);
  flacStreamDecoder->waitForData(std::stop_token(), 4096);
  flacStreamDecoder->read(buffer.data(), 4096);
  EXPECT_EQ(flacStreamDecoder->seekTo(100), 100);
  EXPECT_GE(flacStreamDecoder->waitForData(std::stop_token(), 4096), 4096);
  EXPECT_EQ(flacStreamDecoder->read(buffer.data(), 4096), 4096);
  flacStreamDecoder->disconnect(fileInputNode);
}

TEST_F(IntegrationTest, httpInputSearchForward) {
  LocalHttpServer server(filename);
  auto audioGraphHttpStream = std::make_shared<AudioGraphHttpStream>(1, 
      server.url("/ranged"), 16384);
  flacStreamDecoder->connectTo(audioGraphHttpStream);
  auto state =
      waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  std::vector<uint8_t> buffer(4096);
  flacStreamDecoder->waitForData(std::stop_token(), 4096);
  flacStreamDecoder->read(buffer.data(), 4096);
  EXPECT_EQ(flacStreamDecoder->seekTo(16384), 16384);
  // Preparing, unless the seek landed before this line ran: it is a state
  // passed through, and a reader that samples can be too late for it.
  const auto afterSeek = flacStreamDecoder->getState().state;
  EXPECT_TRUE(afterSeek == AudioGraphNodeState::PREPARING ||
              afterSeek == AudioGraphNodeState::STREAMING)
      << "unexpected state after seeking: " << stateToString(afterSeek);
  state = waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  EXPECT_TRUE(state.streamInfo.has_value());
  EXPECT_EQ(state.position, 16384);
  flacStreamDecoder->waitForData(std::stop_token(), 4096);
  flacStreamDecoder->read(buffer.data(), 4096);
  flacStreamDecoder->disconnect(audioGraphHttpStream);
}

TEST_F(IntegrationTest, fileInputReadAllAndSearchToBeg) {
  auto fileInputNode = std::make_shared<FileInputNode>(1, filename);
  flacStreamDecoder->connectTo(fileInputNode);
  auto state =
      waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  auto contentLength = state.streamInfo.value().streamSize;
  std::vector<uint8_t> buffer(4096);
  size_t contentRead = 0;
  while (flacStreamDecoder->waitForData(std::stop_token(), 4096)) {
    contentRead += flacStreamDecoder->read(buffer.data(), 4096);
  }
  EXPECT_EQ(contentRead, contentLength * 4);

  EXPECT_EQ(flacStreamDecoder->getState().state, AudioGraphNodeState::FINISHED);

  flacStreamDecoder->seekTo(0);

  state = waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  EXPECT_TRUE(state.streamInfo.has_value());
  EXPECT_EQ(state.position, 0);
  contentRead = 0;
  while (flacStreamDecoder->waitForData(std::stop_token(), 4096)) {
    contentRead += flacStreamDecoder->read(buffer.data(), 4096);
  }
  EXPECT_EQ(contentRead, contentLength * 4);
  flacStreamDecoder->disconnect(fileInputNode);
}

TEST_F(IntegrationTest, fileInputSeekToEnd) {
  auto fileInputNode = std::make_shared<FileInputNode>(1, filename);
  flacStreamDecoder->connectTo(fileInputNode);
  auto state =
      waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  auto contentLength = state.streamInfo.value().streamSize;

  flacStreamDecoder->seekTo(contentLength);

  state = waitForStatus(*flacStreamDecoder, AudioGraphNodeState::FINISHED,
                        std::chrono::milliseconds(1000));

  EXPECT_EQ(state.state, AudioGraphNodeState::FINISHED);
}

TEST_F(IntegrationTest, fileInputSeekToEnd_and_back) {
  auto fileInputNode = std::make_shared<FileInputNode>(1, filename);
  flacStreamDecoder->connectTo(fileInputNode);
  auto state =
      waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  auto contentLength = state.streamInfo.value().streamSize;

  flacStreamDecoder->seekTo(contentLength);

  state = waitForStatus(*flacStreamDecoder, AudioGraphNodeState::FINISHED,
                        std::chrono::milliseconds(1000));

  EXPECT_EQ(state.state, AudioGraphNodeState::FINISHED);

  flacStreamDecoder->seekTo(0);
  state = waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING,
                        std::chrono::milliseconds(1000));
}

TEST_F(IntegrationTest, test_play_after_finished) {
  const auto totalDuration = 1000;
  auto outputNode1 = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  auto outputNode2 = std::make_shared<SineWaveNode>(1, 880, totalDuration);
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  switcher->connectTo(outputNode1);
  alsaAudioEmitter->connectTo(switcher);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);
  switcher->connectTo(outputNode2);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);
}

TEST_F(IntegrationTest, test_switch_different_formats_sine) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  const auto totalDuration = 2000;
  const auto freq1 = 48000;
  const auto freq2 = 96000;
  auto outputNode1 = std::make_shared<SineWaveNode>(1, 440, totalDuration, freq1);
  auto outputNode2 = std::make_shared<SineWaveNode>(1, 880, totalDuration, freq2);
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  StateMonitor monitor(alsaAudioEmitter.get());
  switcher->connectTo(outputNode1);
  alsaAudioEmitter->connectTo(switcher);
  std::this_thread::sleep_for(std::chrono::milliseconds(1000));
  switcher->connectTo(outputNode2);
  switcher->disconnect(outputNode1);

  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);

  AudioGraphNodeState states[] = {
      AudioGraphNodeState::STOPPED,        AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING,      AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::SOURCE_CHANGED, AudioGraphNodeState::PREPARING,
      AudioGraphNodeState::STREAMING,      AudioGraphNodeState::FINISHED};

  for (int i = 0; monitor.hasData(); ++i) {
    auto state = monitor.waitState();
    EXPECT_EQ(state.state, states[i]) << "i=" << i;
    if (i == 3) {
      EXPECT_EQ(state.streamInfo.value().format.sampleRate, freq1);
    } else if (i == 6) {
      EXPECT_EQ(state.streamInfo.value().format.sampleRate, freq2);
    }
  }
}

TEST_F(IntegrationTest, test_switch_different_formats_files) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  const auto freq1 = 44100;
  const auto freq2 = 96000;
  auto outputNode1 = std::make_shared<FileInputNode>(1, testFile("tone440.flac"));
  auto outputNode2 = std::make_shared<FileInputNode>(1, testFile("tone880.flac"));
  auto codec1 = std::make_shared<FlacStreamDecoder>(1, 65536);
  auto codec2 = std::make_shared<FlacStreamDecoder>(1, 65536);
  codec1->connectTo(outputNode1);
  codec2->connectTo(outputNode2);
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  StateMonitor monitor(alsaAudioEmitter.get());
  switcher->connectTo(codec1);
  switcher->connectTo(codec2);
  alsaAudioEmitter->connectTo(switcher);
  std::this_thread::sleep_for(std::chrono::milliseconds(500));

  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);

  AudioGraphNodeState states[] = {
      AudioGraphNodeState::STOPPED,        AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING,      AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::SOURCE_CHANGED, AudioGraphNodeState::PREPARING,
      AudioGraphNodeState::STREAMING,      AudioGraphNodeState::FINISHED};

  for (int i = 0; monitor.hasData(); ++i) {
    auto state = monitor.waitState();
    EXPECT_EQ(state.state, states[i]) << "i=" << i;
    if (i == 3) {
      EXPECT_EQ(state.streamInfo.value().format.sampleRate, freq1);
    } else if (i == 6) {
      EXPECT_EQ(state.streamInfo.value().format.sampleRate, freq2);
    }
  }
}

TEST_F(IntegrationTest, test_play_mp3_file) {
  auto outputNode = std::make_shared<FileInputNode>(1, testFile("tone440.mp3"));
  auto codec = std::make_shared<Mp3StreamDecoder>(1, 65536);
  codec->connectTo(outputNode);
  alsaAudioEmitter->connectTo(codec);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED).state,
      AudioGraphNodeState::FINISHED);
}

TEST_F(IntegrationTest, test_next_track_after_pause) {
  SKIP_UNLESS_PLAYED_IN_REAL_TIME();
  const auto totalDuration = 1000;
  auto outputNode1 = std::make_shared<SineWaveNode>(1, 440, totalDuration);
  auto outputNode2 = std::make_shared<SineWaveNode>(1, 880, totalDuration);
  auto switcher = std::make_shared<AudioStreamSwitcher>();
  StateMonitor monitor(alsaAudioEmitter.get());
  switcher->connectTo(outputNode1);
  switcher->connectTo(outputNode2);
  alsaAudioEmitter->connectTo(switcher);
  EXPECT_EQ(
      waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::STREAMING).state,
      AudioGraphNodeState::STREAMING);

  std::this_thread::sleep_for(std::chrono::milliseconds(200));

  alsaAudioEmitter->pause(true);
  EXPECT_EQ(waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::PAUSED,
                          std::chrono::milliseconds(1000))
                .state,
            AudioGraphNodeState::PAUSED);

  switcher->disconnect(outputNode1);
  EXPECT_EQ(waitForStatus(*alsaAudioEmitter, AudioGraphNodeState::FINISHED,
                          std::chrono::seconds(3))
                .state,
            AudioGraphNodeState::FINISHED);

  AudioGraphNodeState states[] = {
      AudioGraphNodeState::STOPPED,   AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::PAUSED,    AudioGraphNodeState::SOURCE_CHANGED,
      AudioGraphNodeState::PREPARING, AudioGraphNodeState::STREAMING,
      AudioGraphNodeState::FINISHED};

  const auto statesCount = sizeof(states) / sizeof(AudioGraphNodeState);

  int i = 0;
  for (; i < statesCount && monitor.hasData(); ++i) {
    auto state = monitor.waitState();
    EXPECT_EQ(state.state, states[i]) << "i=" << i;
  }

  EXPECT_EQ(i, statesCount);
}