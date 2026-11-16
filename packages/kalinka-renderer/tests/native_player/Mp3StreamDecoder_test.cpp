#include "AudioGraphHttpStream.h"
#include "ContainerTestSupport.h"
#include "ErrorFakeNode.h"
#include "FileInputNode.h"
#include "LiveInputNode.h"
#include "LocalHttpServer.h"
#include "Mp3StreamDecoder.h"
#include "TestHelpers.h"
#include "minimp3/minimp3_ex.h"

#include <fstream>
#include <gmock/gmock.h>
#include <gtest/gtest.h>
#include <iterator>
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

TEST_F(Mp3StreamDecoderTest, ASeekOnceDecodedToTheEndNeverFinishes) {
  decoder = std::make_unique<Mp3StreamDecoder>(1, 64 * 1024 * 1024);
  decoder->connectTo(std::make_shared<FileInputNode>(1, mp3file));

  const auto seen = seekOnceDecodedToTheEnd(*decoder, 1000);

  EXPECT_EQ(std::count(seen.begin(), seen.end(), AudioGraphNodeState::FINISHED),
            0)
      << "a queued track would take over from a seek";
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
  waitForStatus(*decoder, AudioGraphNodeState::ERROR,
                std::chrono::milliseconds(2000));
  ASSERT_EQ(decoder->getState().state, AudioGraphNodeState::ERROR);
  ASSERT_TRUE(decoder->getState().error);
  EXPECT_EQ(decoder->getState().error->message, "Fake error message");

  EXPECT_TRUE(returnsWithin([decoder] { decoder->seekTo(0); },
                            std::chrono::milliseconds(2000)));
}

TEST_F(Mp3StreamDecoderTest, StartsAndContinuesWithoutEndOfStreamOrSeeking) {
  std::ifstream file(mp3file, std::ios::binary);
  const std::vector<uint8_t> encoded(std::istreambuf_iterator<char>(file), {});
  constexpr size_t prefix = 16384;
  ASSERT_GT(encoded.size(), prefix);
  auto source = std::make_shared<LiveInputNode>(encoded.size(), 1000);
  ASSERT_EQ(source->bytes.write(encoded.data(), prefix), prefix);
  decoder->connectTo(source);
  ASSERT_GE(decoder->waitForDataFor({}, std::chrono::seconds(2), 1024), 1024u);
  auto state = decoder->getState();
  ASSERT_TRUE(state.streamInfo);
  EXPECT_EQ(state.streamInfo->streamSize, 0u);
  EXPECT_EQ(state.streamInfo->format.sampleRate, 44100u);
  EXPECT_EQ(state.streamInfo->prebufferMs, 1000u);
  EXPECT_EQ(decoder->seekTo(0), size_t(-1));
  EXPECT_EQ(decoder->seekTo(44100), size_t(-1));
  ASSERT_EQ(
      source->bytes.write(encoded.data() + prefix, encoded.size() - prefix),
      encoded.size() - prefix);
  std::array<uint8_t, 1024> pcm;
  size_t total = 0;
  while (total < 44100 * 4) {
    ASSERT_GT(decoder->waitForDataFor({}, std::chrono::seconds(2), 1), 0u);
    total += decoder->read(pcm.data(), pcm.size());
  }
  EXPECT_EQ(decoder->getState().state, AudioGraphNodeState::STREAMING);
  decoder->disconnect(source);
}

TEST_F(Mp3StreamDecoderTest, RadioPlaysThroughTheEndOfItsConnection) {
  LocalHttpServer server(mp3file);
  auto source = std::make_shared<AudioGraphHttpStream>(1, server.url("/radio"),
                                                       32768, 4096);
  decoder->connectTo(source);
  std::array<uint8_t, 1024> pcm;
  size_t total = 0;
  while (decoder->waitForDataFor({}, std::chrono::seconds(2), 1))
    total += decoder->read(pcm.data(), pcm.size());
  EXPECT_EQ(decoder->getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_NEAR(double(total), 5 * 44100 * 4, 44100 * 4 / 5);
  EXPECT_EQ(server.requestsTo("/radio"), 2u);
}

namespace {
std::vector<uint8_t> mp3Fixture(const std::string &path = testFile("tone440.mp3")) {
  std::ifstream file(path, std::ios::binary);
  return {std::istreambuf_iterator<char>(file), {}};
}

// Remove the fixture's Info frame to exercise the scanned CBR path.
std::vector<uint8_t> withoutInfoFrame() {
  auto bytes = mp3Fixture();
  mp3dec_t decoder{};
  mp3dec_init(&decoder);
  mp3dec_frame_info_t info{};
  std::array<mp3d_sample_t, MINIMP3_MAX_SAMPLES_PER_FRAME> pcm;
  mp3dec_decode_frame(&decoder, bytes.data(), bytes.size(), pcm.data(), &info);
  EXPECT_GT(info.frame_bytes, 0);
  bytes.erase(bytes.begin(), bytes.begin() + info.frame_bytes);
  return bytes;
}

std::vector<uint8_t> decodeMp3(std::shared_ptr<AudioGraphOutputNode> source) {
  Mp3StreamDecoder decoder(1, 16384);
  decoder.connectTo(std::move(source));
  auto pcm = container_test::drain(decoder);
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
  return pcm;
}

// Reference the codec's existing PCM, including how it treats trailing tags.
// The renderer must preserve those samples and finish so the queue can advance.
std::vector<uint8_t> codecPcm(const std::vector<uint8_t> &bytes) {
  mp3dec_ex_t decoder{};
  EXPECT_EQ(mp3dec_ex_open_buf(&decoder, bytes.data(), bytes.size(),
                               MP3D_SEEK_TO_SAMPLE), 0);
  std::array<mp3d_sample_t, MINIMP3_MAX_SAMPLES_PER_FRAME> pcm;
  std::vector<uint8_t> result;
  while (auto count = mp3dec_ex_read(&decoder, pcm.data(), pcm.size())) {
    const auto *data = reinterpret_cast<const uint8_t *>(pcm.data());
    result.insert(result.end(), data, data + count * sizeof(mp3d_sample_t));
  }
  mp3dec_ex_close(&decoder);
  return result;
}

std::vector<uint8_t> id3Trailer(size_t textBytes, bool footer) {
  auto appendSize = [](std::vector<uint8_t> &bytes, size_t size) {
    for (int shift = 21; shift >= 0; shift -= 7)
      bytes.push_back((size >> shift) & 0x7f);
  };
  std::vector<uint8_t> tag{'I', 'D', '3', 4, 0, uint8_t(footer ? 0x10 : 0)};
  appendSize(tag, 11 + textBytes);
  const auto header = tag;
  tag.insert(tag.end(), {'T', 'I', 'T', '2'});
  appendSize(tag, 1 + textBytes);
  tag.insert(tag.end(), {0, 0, 3}); // Flags and UTF-8 encoding.
  tag.resize(tag.size() + textBytes, 'x');
  if (footer) {
    tag.insert(tag.end(), {'3', 'D', 'I'});
    tag.insert(tag.end(), header.begin() + 3, header.end());
  }
  return tag;
}
} // namespace

TEST_F(Mp3StreamDecoderTest, FinishesCbrAudioWithTrailingId3v24Metadata) {
  auto bytes = withoutInfoFrame();
  LocalHttpServer plain({{"", std::string(bytes.begin(), bytes.end())}});
  const auto reference = decodeMp3(std::make_shared<AudioGraphHttpStream>(
      1, plain.url("/ranged"), 32768, 4096));
  ASSERT_FALSE(reference.empty());
  ASSERT_EQ(reference, codecPcm(bytes));
  // Include a tag larger than minimp3's input window: validating it must
  // neither allocate its declared size nor read its entire payload.
  for (const auto &[size, footer] :
       {std::pair<size_t, bool>{7, false}, {7, true}, {256 * 1024, true}}) {
    SCOPED_TRACE(size);
    SCOPED_TRACE(footer);
    auto tagged = bytes;
    const auto tag = id3Trailer(size, footer);
    tagged.insert(tagged.end(), tag.begin(), tag.end());
    LocalHttpServer server({{"", std::string(tagged.begin(), tagged.end())}});
    EXPECT_EQ(decodeMp3(std::make_shared<AudioGraphHttpStream>(
                  1, server.url("/ranged"), 32768, 4096)),
              codecPcm(tagged));
  }
}

TEST_F(Mp3StreamDecoderTest, DoesNotAcceptATruncatedTrailerAsNormalEof) {
  auto encoded = withoutInfoFrame();
  auto tag = id3Trailer(7, false);
  tag.pop_back();
  encoded.insert(encoded.end(), tag.begin(), tag.end());
  decoder->connectTo(std::make_shared<container_test::BytesInput>(encoded));
  EXPECT_FALSE(container_test::drain(*decoder).empty());
  EXPECT_EQ(decoder->getState().state, AudioGraphNodeState::ERROR);
}

TEST_F(Mp3StreamDecoderTest, PreservesGaplessSamplesWithoutContentLength) {
  const auto reference =
      decodeMp3(std::make_shared<FileInputNode>(1, mp3file));
  ASSERT_EQ(reference.size(), 5u * 44100 * 4);
  LocalHttpServer server(mp3file);
  for (const auto *path : {"/close-delimited", "/refuses-ranges"}) {
    SCOPED_TRACE(path);
    EXPECT_EQ(decodeMp3(std::make_shared<AudioGraphHttpStream>(
                  1, server.url(path), 32768, 4096)),
              reference);
  }
}

TEST_F(Mp3StreamDecoderTest, PreservesGaplessWithLeadingTagsAndShortReads) {
  for (const auto &path : {testFile("silence30.mp3"),
                           std::string(KALINKA_TEST_DATA_DIR) + "/ladder.mp3"}) {
    SCOPED_TRACE(path);
    const auto reference = decodeMp3(std::make_shared<FileInputNode>(1, path));
    const auto encoded = mp3Fixture(path);
    EXPECT_EQ(decodeMp3(std::make_shared<container_test::BytesInput>(
                  encoded, false, false)),
              reference);
  }
}

TEST_F(Mp3StreamDecoderTest, PreservesGaplessBeforeATrailerWithoutContentLength) {
  const auto reference =
      decodeMp3(std::make_shared<FileInputNode>(1, mp3file));
  auto encoded = mp3Fixture();
  const auto tag = id3Trailer(7, true);
  encoded.insert(encoded.end(), tag.begin(), tag.end());
  EXPECT_EQ(decodeMp3(std::make_shared<container_test::BytesInput>(
                encoded, false, false)),
            reference);
}

TEST_F(Mp3StreamDecoderTest, ContinuesPastTheXingLengthInABroadcast) {
  auto encoded = mp3Fixture();
  const auto continuation = withoutInfoFrame();
  encoded.insert(encoded.end(), continuation.begin(), continuation.end());
  auto source = std::make_shared<LiveInputNode>(encoded.size());
  ASSERT_EQ(source->bytes.write(encoded.data(), encoded.size()), encoded.size());
  decoder->connectTo(source);
  std::array<uint8_t, 1024> pcm;
  size_t total = 0;
  // The first track's Info header advertises five seconds. More audio follows
  // on the same connection, which deliberately never signals EOF.
  while (total < 6u * 44100 * 4) {
    ASSERT_GT(decoder->waitForDataFor({}, std::chrono::seconds(2), 1), 0u);
    total += decoder->read(pcm.data(), pcm.size());
  }
  EXPECT_EQ(decoder->getState().state, AudioGraphNodeState::STREAMING);
  decoder->disconnect(source);
}

TEST_F(Mp3StreamDecoderTest, AFormatFailureBeforeTheIndexedEndStillErrors) {
  auto encoded = withoutInfoFrame();
  const auto changed = mp3Fixture(std::string(KALINKA_TEST_DATA_DIR) + "/ladder.mp3");
  encoded.insert(encoded.end(), changed.begin(), changed.end());
  decoder->connectTo(std::make_shared<container_test::BytesInput>(encoded));
  EXPECT_FALSE(container_test::drain(*decoder).empty());
  ASSERT_EQ(decoder->getState().state, AudioGraphNodeState::ERROR);
  ASSERT_TRUE(decoder->getState().error);
  EXPECT_NE(decoder->getState().error->message.find("MP3 decode failure"),
            std::string::npos);
}
