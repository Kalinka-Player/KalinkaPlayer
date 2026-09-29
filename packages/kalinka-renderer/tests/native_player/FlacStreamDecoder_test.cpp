#include "FileInputNode.h"
#include "FlacStreamDecoder.h"

#include <fstream>
#include <gtest/gtest.h>
#include <iterator>
#include <memory>

#include "ErrorFakeNode.h"
#include "LiveInputNode.h"
#include "TestHelpers.h"

class FlacStreamDecoderTest : public ::testing::Test {
protected:
  static constexpr size_t bufferSize = 16384;

  void SetUp() override {}
};

TEST_F(FlacStreamDecoderTest, constructor_destructor) {}

TEST_F(FlacStreamDecoderTest, connectTo) {
  auto flacStreamDecoder = std::make_shared<FlacStreamDecoder>(1, bufferSize);
  auto inputNode = std::make_shared<FileInputNode>(1, testFile("tone440.flac"));
  flacStreamDecoder->connectTo(inputNode);
}

TEST_F(FlacStreamDecoderTest, connectTo_nullptr) {
  auto flacStreamDecoder = std::make_shared<FlacStreamDecoder>(1, bufferSize);
  EXPECT_THROW(flacStreamDecoder->connectTo(nullptr), std::runtime_error);
}

TEST_F(FlacStreamDecoderTest, disconnect) {
  auto flacStreamDecoder = std::make_shared<FlacStreamDecoder>(1, bufferSize);
  auto inputNode = std::make_shared<FileInputNode>(1, testFile("tone440.flac"));
  flacStreamDecoder->connectTo(inputNode);
  std::this_thread::sleep_for(std::chrono::milliseconds(500));
  flacStreamDecoder->disconnect(inputNode);
}

TEST_F(FlacStreamDecoderTest, read) {
  auto flacStreamDecoder = std::make_shared<FlacStreamDecoder>(1, bufferSize);
  auto inputNode = std::make_shared<FileInputNode>(1, testFile("tone440.flac"));
  flacStreamDecoder->connectTo(inputNode);
  uint8_t data[bufferSize];
  auto state =
      waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(state.streamInfo.has_value());
  EXPECT_EQ(state.streamInfo.value().streamType, StreamType::FRAMES);
  std::this_thread::sleep_for(std::chrono::milliseconds(1000));
  EXPECT_EQ(flacStreamDecoder->read(data, 100), 100);
  flacStreamDecoder->disconnect(inputNode);
}

TEST_F(FlacStreamDecoderTest, getStreamInfo) {
  auto flacStreamDecoder = std::make_shared<FlacStreamDecoder>(1, bufferSize);
  auto inputNode = std::make_shared<FileInputNode>(1, testFile("tone440.flac"));
  flacStreamDecoder->connectTo(inputNode);
  auto state =
      waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(state.streamInfo.has_value());
  auto audioInfo = state.streamInfo.value();
  EXPECT_EQ(audioInfo.format.sampleRate, 44100);
  EXPECT_EQ(audioInfo.format.channels, 2);
  EXPECT_EQ(audioInfo.format.bitsPerSample, 16);
  EXPECT_EQ(audioInfo.streamSize, 44100);
}

TEST_F(FlacStreamDecoderTest, getState) {
  auto flacStreamDecoder = std::make_shared<FlacStreamDecoder>(1, bufferSize);
  auto inputNode = std::make_shared<FileInputNode>(1, testFile("tone440.flac"));
  ASSERT_EQ(flacStreamDecoder->getState().state, AudioGraphNodeState::STOPPED);
  flacStreamDecoder->connectTo(inputNode);
  EXPECT_EQ(flacStreamDecoder->getState().state,
            AudioGraphNodeState::PREPARING);
  waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(flacStreamDecoder->getState().state,
            AudioGraphNodeState::STREAMING);
  uint8_t data[1000];
  int totalSize = 0;
  int num = 0;
  do {
    flacStreamDecoder->waitForData();
    num = flacStreamDecoder->read(data, 1000);
    totalSize += num;
  } while (num > 0 && flacStreamDecoder->getState().state !=
                          AudioGraphNodeState::FINISHED);
  EXPECT_EQ(flacStreamDecoder->getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(totalSize, 44100 * 4);
  flacStreamDecoder->disconnect(inputNode);
  EXPECT_EQ(flacStreamDecoder->getState().state, AudioGraphNodeState::STOPPED);
}

TEST_F(FlacStreamDecoderTest, stream_error) {
  auto flacStreamDecoder = std::make_shared<FlacStreamDecoder>(1, bufferSize);
  auto inputNode = std::make_shared<ErrorFakeNode>();
  flacStreamDecoder->connectTo(inputNode);
  std::this_thread::sleep_for(std::chrono::milliseconds(500));
  auto streamState = flacStreamDecoder->getState();
  EXPECT_EQ(streamState.state, AudioGraphNodeState::ERROR);
  EXPECT_EQ(streamState.error->message, "Fake error message");
}

// A decoder whose thread has already ended, here on a start offset past the
// end of the stream, must still answer a seek: the renderer asks from its
// only command thread, and an unanswered seek froze it for good.
TEST_F(FlacStreamDecoderTest, seek_after_the_decoder_has_stopped_returns) {
  auto flacStreamDecoder =
      std::make_shared<FlacStreamDecoder>(1, bufferSize, 60000);
  auto inputNode = std::make_shared<FileInputNode>(1, testFile("tone440.flac"));
  flacStreamDecoder->connectTo(inputNode);
  waitForStatus(*flacStreamDecoder, AudioGraphNodeState::STOPPED,
                std::chrono::milliseconds(2000));
  ASSERT_EQ(flacStreamDecoder->getState().state, AudioGraphNodeState::STOPPED);

  EXPECT_TRUE(returnsWithin(
      [flacStreamDecoder] { flacStreamDecoder->seekTo(0); },
      std::chrono::milliseconds(2000)));
}

TEST_F(FlacStreamDecoderTest, starts_before_a_live_producer_sends_the_rest) {
  std::ifstream file(testFile("tone440.flac"), std::ios::binary);
  const std::vector<uint8_t> flac(std::istreambuf_iterator<char>(file), {});
  const size_t prefix = 4096;
  ASSERT_GT(flac.size(), prefix);
  auto source = std::make_shared<LiveInputNode>(1 << 20);
  ASSERT_EQ(source->bytes.write(flac.data(), prefix), prefix);
  auto decoder = std::make_shared<FlacStreamDecoder>(1, bufferSize);
  decoder->connectTo(source);
  EXPECT_GE(decoder->waitForDataFor({}, std::chrono::seconds(1), 1024), 1024u);
  decoder->disconnect(source);
}
