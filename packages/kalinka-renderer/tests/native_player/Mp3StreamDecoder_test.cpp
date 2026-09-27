#include "ErrorFakeNode.h"
#include "FileInputNode.h"
#include "Mp3StreamDecoder.h"
#include "TestHelpers.h"


#include <gmock/gmock.h>
#include <gtest/gtest.h>
#include <memory>

class MockAudioGraphOutputNode : public AudioGraphOutputNode {
public:
  MOCK_METHOD(size_t, read, (void *data, size_t size), (override));
  MOCK_METHOD(size_t, waitForData, (std::stop_token stopToken, size_t size),
              (override));
  MOCK_METHOD(size_t, waitForDataFor,
              (std::stop_token stopToken, std::chrono::milliseconds timeout,
               size_t size),
              (override));
  MOCK_METHOD(size_t, seekTo, (size_t absolutePosition), (override));
};

class Mp3StreamDecoderTest : public ::testing::Test {
protected:
  void SetUp() override {
    decoder = std::make_unique<Mp3StreamDecoder>(1, 1024);
  }

  std::unique_ptr<Mp3StreamDecoder> decoder;

  const std::string mp3file = testFile("tone440.mp3");
};

TEST_F(Mp3StreamDecoderTest, ConstructorInitializesCorrectly) {
  ASSERT_NE(decoder, nullptr);
}

TEST_F(Mp3StreamDecoderTest, ConnectToThrowsIfInputNodeIsNull) {
  std::shared_ptr<AudioGraphOutputNode> outputNode = nullptr;
  ASSERT_THROW(decoder->connectTo(outputNode), std::runtime_error);
}

TEST_F(Mp3StreamDecoderTest, ConnectToThrowsIfInputNodeAlreadyConnected) {
  std::shared_ptr<AudioGraphOutputNode> outputNode =
      std::make_shared<MockAudioGraphOutputNode>();
  decoder->connectTo(outputNode);
  ASSERT_THROW(decoder->connectTo(outputNode), std::runtime_error);
}

TEST_F(Mp3StreamDecoderTest, DisconnectDoesNotThrow) {
  std::shared_ptr<AudioGraphOutputNode> outputNode =
      std::make_shared<MockAudioGraphOutputNode>();
  decoder->connectTo(outputNode);
  ASSERT_NO_THROW(decoder->disconnect(outputNode));
}

TEST_F(Mp3StreamDecoderTest, ReadFromFileStream) {
  auto outputNode = std::make_shared<FileInputNode>(1, mp3file);
  decoder->connectTo(outputNode);

  auto state = waitForStatus(*decoder, AudioGraphNodeState::STREAMING,
                             std::chrono::seconds(2));

  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(state.streamInfo.has_value());
  EXPECT_EQ(state.streamInfo.value().streamType, StreamType::FRAMES);
  EXPECT_EQ(state.streamInfo.value().format.sampleRate, 44100);
  EXPECT_EQ(state.streamInfo.value().format.channels, 2);
  EXPECT_EQ(state.streamInfo.value().format.bitsPerSample, 16);
  EXPECT_EQ(state.streamInfo.value().streamSize, 44100 * 5);
  EXPECT_EQ(state.streamInfo.value().format.sampleFormat,
            AudioSampleFormat::PCM16_LE);

  std::vector<uint8_t> data(256);

  auto dataAvailable = decoder->waitForDataFor(
      std::stop_token(), std::chrono::milliseconds(1000), 256);

  EXPECT_GE(dataAvailable, 256);
  EXPECT_EQ(decoder->read(data.data(), 256), 256);
}

TEST_F(Mp3StreamDecoderTest, ReadWholeFile) {
  auto outputNode = std::make_shared<FileInputNode>(1, mp3file);
  decoder->connectTo(outputNode);

  EXPECT_EQ(waitForStatus(*decoder, AudioGraphNodeState::STREAMING,
                          std::chrono::seconds(2))
                .state,
            AudioGraphNodeState::STREAMING);

  std::vector<uint8_t> data(256);

  while (decoder->getState().state != AudioGraphNodeState::FINISHED) {
    auto dataAvailable = decoder->waitForDataFor(
        std::stop_token(), std::chrono::milliseconds(2000), 256);

    EXPECT_GE(dataAvailable, 0);

    decoder->read(data.data(), std::min(data.size(), dataAvailable));
  }

  EXPECT_EQ(decoder->getState().state, AudioGraphNodeState::FINISHED);
}

TEST_F(Mp3StreamDecoderTest, test_SeekTo_consistent) {
  auto outputNode = std::make_shared<FileInputNode>(1, mp3file);
  decoder->connectTo(outputNode);

  // Seek to 2s from the start
  decoder->seekTo(44100 * 2);

  auto state = waitForStatus(*decoder, AudioGraphNodeState::STREAMING,
                             std::chrono::seconds(2));
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.position, 44100 * 2);

  std::vector<uint8_t> data(256);

  auto dataAvailable = decoder->waitForDataFor(
      std::stop_token(), std::chrono::milliseconds(1000), 256);

  EXPECT_GE(dataAvailable, 256);
  EXPECT_EQ(decoder->read(data.data(), 256), 256);

  // Seek to 2s and read again
  decoder->seekTo(44100 * 2); // 2s from the start

  state = waitForStatus(*decoder, AudioGraphNodeState::STREAMING,
                        std::chrono::seconds(2));
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.position, 44100 * 2);

  std::vector<uint8_t> newData(256);

  dataAvailable = decoder->waitForDataFor(std::stop_token(),
                                          std::chrono::milliseconds(1000), 256);

  EXPECT_GE(dataAvailable, 256);
  EXPECT_EQ(decoder->read(newData.data(), 256), 256);
  EXPECT_EQ(data, newData);
}

TEST_F(Mp3StreamDecoderTest, test_seekTo_correctness) {
  auto outputNode = std::make_shared<FileInputNode>(1, mp3file);
  decoder->connectTo(outputNode);

  auto state = waitForStatus(*decoder, AudioGraphNodeState::STREAMING,
                             std::chrono::seconds(2));
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  const auto totalFrames = state.streamInfo.value().streamSize;
  const auto totalBytes = (state.streamInfo.value().format.bitsPerSample / 8) *
                          state.streamInfo.value().format.channels *
                          totalFrames;
  const auto seekPos = totalFrames / 2;

  decoder->seekTo(seekPos);
  EXPECT_EQ(waitForStatus(*decoder, AudioGraphNodeState::STREAMING,
                          std::chrono::seconds(2))
                .state,
            AudioGraphNodeState::STREAMING);

  std::vector<uint8_t> data(1024);
  size_t dataRead = 0;

  while (decoder->getState().state != AudioGraphNodeState::FINISHED) {
    auto dataAvailable = decoder->waitForDataFor(
        std::stop_token(), std::chrono::milliseconds(2000), 1024);

    EXPECT_GE(dataAvailable, 0);

    dataRead +=
        decoder->read(data.data(), std::min(data.size(), dataAvailable));
  }

  EXPECT_EQ(dataRead, totalBytes / (totalFrames / seekPos));
}

// A decoder whose thread ended before the stream opened must still answer a
// seek; it used to wait forever for an initialisation that could not come.
TEST_F(Mp3StreamDecoderTest, seek_after_the_decoder_has_stopped_returns) {
  auto decoder = std::make_shared<Mp3StreamDecoder>(1, 16384);
  decoder->connectTo(std::make_shared<ErrorFakeNode>());
  waitForStatus(*decoder, AudioGraphNodeState::STOPPED,
                std::chrono::milliseconds(2000));
  ASSERT_EQ(decoder->getState().state, AudioGraphNodeState::STOPPED);

  EXPECT_TRUE(returnsWithin([decoder] { decoder->seekTo(0); },
                            std::chrono::milliseconds(2000)));
}
