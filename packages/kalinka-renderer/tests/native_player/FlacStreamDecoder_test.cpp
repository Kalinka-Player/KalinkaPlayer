#include "AudioGraphHttpStream.h"
#include "FileInputNode.h"
#include "FlacStreamDecoder.h"

#include <filesystem>
#include <fstream>
#include <future>
#include <gtest/gtest.h>
#include <iterator>
#include <memory>
#include <unistd.h>

#include "ContainerTestSupport.h"
#include "ErrorFakeNode.h"
#include "LiveInputNode.h"
#include "LocalHttpServer.h"
#include "StallingFileInput.h"
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
  EXPECT_THROW(flacStreamDecoder->connectTo(nullptr), std::invalid_argument);
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
TEST_F(FlacStreamDecoderTest, a_seek_once_decoded_to_the_end_never_finishes) {
  auto decoder = std::make_shared<FlacStreamDecoder>(1, 64 * 1024 * 1024);
  decoder->connectTo(
      std::make_shared<FileInputNode>(1, testFile("tone440.flac")));

  const auto seen = seekOnceDecodedToTheEnd(*decoder, 1000);

  EXPECT_EQ(std::count(seen.begin(), seen.end(), AudioGraphNodeState::FINISHED),
            0)
      << "a queued track would take over from a seek";
}

TEST_F(FlacStreamDecoderTest, a_seek_cut_short_by_another_lands_on_the_second) {
  using namespace std::chrono_literals;
  auto source = std::make_shared<StallingFileInput>(testFile("tone440.flac"));
  FlacStreamDecoder decoder(1, bufferSize);
  decoder.connectTo(source);
  ASSERT_EQ(waitForStatus(decoder, AudioGraphNodeState::STREAMING, 5s).state,
            AudioGraphNodeState::STREAMING);

  source->stall();
  ASSERT_EQ(decoder.seekTo(11025), 11025u) << "answered while its read stalls";
  ASSERT_TRUE(source->waitUntilStalled());
  constexpr size_t second = 22050;
  auto seeking = std::async(std::launch::async,
                            [&decoder] { return decoder.seekTo(second); });
  const bool answered = seeking.wait_for(1s) == std::future_status::ready;
  source->release();
  ASSERT_TRUE(answered) << "the second seek waited on the first's read";
  EXPECT_EQ(seeking.get(), second);

  EXPECT_EQ(container_test::drain(decoder).size(), (44100 - second) * 4);
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
}

TEST_F(FlacStreamDecoderTest, seek_after_the_decoder_has_stopped_returns) {
  auto flacStreamDecoder =
      std::make_shared<FlacStreamDecoder>(1, bufferSize, 60000);
  auto inputNode = std::make_shared<FileInputNode>(1, testFile("tone440.flac"));
  flacStreamDecoder->connectTo(inputNode);
  waitForStatus(*flacStreamDecoder, AudioGraphNodeState::ERROR,
                std::chrono::milliseconds(2000));
  ASSERT_EQ(flacStreamDecoder->getState().state, AudioGraphNodeState::ERROR);

  EXPECT_TRUE(returnsWithin(
      [flacStreamDecoder] { flacStreamDecoder->seekTo(0); },
      std::chrono::milliseconds(2000)));
}

TEST_F(FlacStreamDecoderTest, plays_to_the_end_from_a_server_that_refuses_every_range) {
  LocalHttpServer server(testFile("tone440.flac"));
  auto source = std::make_shared<AudioGraphHttpStream>(
      1, server.url("/refuses-ranges"), 2 * bufferSize, bufferSize);
  auto decoder = std::make_shared<FlacStreamDecoder>(1, bufferSize);
  decoder->connectTo(source);
  uint8_t data[1000];
  size_t total = 0;
  while (decoder->waitForData() != 0) {
    total += decoder->read(data, sizeof(data));
  }

  EXPECT_EQ(decoder->getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(total, 44100u * 4);
  decoder->disconnect(source);
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

TEST_F(FlacStreamDecoderTest, UnknownLengthInheritsTheSourceBufferingPolicy) {
  std::ifstream file(testFile("tone440.flac"), std::ios::binary);
  std::vector<uint8_t> flac(std::istreambuf_iterator<char>(file), {});
  ASSERT_GT(flac.size(), 26u);
  // STREAMINFO's low 36 bits of bytes 18..25 hold total_samples; zero means
  // unknown length. Keep its sample rate, channel count and bit depth intact.
  flac[21] &= 0xf0;
  std::fill(flac.begin() + 22, flac.begin() + 26, 0);
  for (unsigned prebufferMs : {0u, 1000u}) {
    auto source = std::make_shared<LiveInputNode>(flac.size(), prebufferMs);
    source->bytes.write(flac.data(), flac.size());
    FlacStreamDecoder decoder(1, bufferSize);
    decoder.connectTo(source);
    const auto state = waitForStatus(decoder, AudioGraphNodeState::STREAMING,
                                     std::chrono::seconds(2));
    ASSERT_TRUE(state.streamInfo);
    EXPECT_EQ(state.streamInfo->streamSize, 0u);
    EXPECT_EQ(state.streamInfo->prebufferMs, prebufferMs);
    EXPECT_EQ(state.streamInfo->format.sampleRate, 44100u);
    decoder.disconnect(source);
  }
}

TEST_F(FlacStreamDecoderTest, UnknownLengthRefusesToSeekEvenOnASeekableSource) {
  std::ifstream file(testFile("tone440.flac"), std::ios::binary);
  std::vector<uint8_t> flac(std::istreambuf_iterator<char>(file), {});
  ASSERT_GT(flac.size(), 26u);
  flac[21] &= 0xf0;
  std::fill(flac.begin() + 22, flac.begin() + 26, 0);
  const auto path =
      std::filesystem::temp_directory_path() /
      ("kalinka-flac-unknown-length-" + std::to_string(getpid()) + ".flac");
  std::ofstream(path, std::ios::binary)
      .write(reinterpret_cast<const char *>(flac.data()), flac.size());

  auto source = std::make_shared<FileInputNode>(1, path.string());
  FlacStreamDecoder decoder(1, bufferSize);
  decoder.connectTo(source);
  ASSERT_EQ(waitForStatus(decoder, AudioGraphNodeState::STREAMING,
                          std::chrono::seconds(2))
                .state,
            AudioGraphNodeState::STREAMING);
  // Clamped to a length of zero, the seek would replay the track instead.
  EXPECT_EQ(decoder.seekTo(1000), size_t(-1));
  decoder.disconnect(source);
  std::filesystem::remove(path);
}
