#include "AudioGraphHttpStream.h"

#include <curl/curl.h>
#include <gmock/gmock.h>
#include <gtest/gtest.h>

#include <algorithm>
#include <fstream>
#include <iterator>
#include <memory>
#include <vector>

#include "LocalHttpServer.h"
#include "TestHelpers.h"

namespace {
std::vector<uint8_t> readToEnd(AudioGraphHttpStream &stream) {
  std::vector<uint8_t> content;
  size_t available = 0;
  while ((available = stream.waitForData(std::stop_token(), 1)) != 0) {
    const size_t start = content.size();
    content.resize(start + available);
    content.resize(start + stream.read(content.data() + start, available));
  }
  return content;
}

std::vector<uint8_t> fileContent(const std::string &path) {
  std::ifstream file(path, std::ios::binary);
  return {std::istreambuf_iterator<char>(file),
          std::istreambuf_iterator<char>()};
}
} // namespace

class AudioGraphHttpStreamTest : public ::testing::Test {
protected:
  // Several buffers long, so seeking past what is buffered asks for a range.
  const std::string file = testFile("tone880.flac");
  LocalHttpServer server{file};
  const std::string url = server.url("/ranged");
  const std::string urlNoRanges = server.url("/whole");
  const size_t bufferSize = 32768;
  const std::chrono::seconds stallTimeout{1};

  void SetUp() override {}
};

TEST_F(AudioGraphHttpStreamTest, constructor_destructor) {
  auto audioGraphHttpStream =
      std::make_shared<AudioGraphHttpStream>(1, url, bufferSize);
}

TEST_F(AudioGraphHttpStreamTest, read) {
  auto audioGraphHttpStream =
      std::make_shared<AudioGraphHttpStream>(1, url, bufferSize);
  std::vector<uint8_t> data(bufferSize);
  auto state =
      waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.streamInfo.value().streamType, StreamType::BYTES);
  size_t totalLength = state.streamInfo.value().streamSize;
  size_t bytesToRead = 0;
  size_t totalBytesRead = 0;
  while ((bytesToRead =
              audioGraphHttpStream->waitForData(std::stop_token(), 1)) != 0) {
    audioGraphHttpStream->read(data.data(), bytesToRead);
    totalBytesRead += bytesToRead;
  }
  EXPECT_EQ(audioGraphHttpStream->getState().state,
            AudioGraphNodeState::FINISHED);
  EXPECT_EQ(totalBytesRead, totalLength);
}

TEST_F(AudioGraphHttpStreamTest, read_no_ranges) {
  auto audioGraphHttpStream =
      std::make_shared<AudioGraphHttpStream>(1, urlNoRanges, bufferSize);
  auto audioGraphHttpStreamChunked = std::make_shared<AudioGraphHttpStream>(1, 
      urlNoRanges, bufferSize, bufferSize / 2);
  std::vector<uint8_t> data(bufferSize);
  auto state =
      waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::STREAMING);
  size_t totalLength = state.streamInfo.value().streamSize;
  // Streaming data, no length available
  EXPECT_EQ(totalLength, 1);
  size_t bytesToRead = 0;
  size_t totalBytesRead = 0;
  while ((bytesToRead =
              audioGraphHttpStream->waitForData(std::stop_token(), 1)) != 0) {
    audioGraphHttpStream->read(data.data(), bytesToRead);
    totalBytesRead += bytesToRead;
  }
  size_t totalBytesReadChunked = 0;
  while ((bytesToRead = audioGraphHttpStreamChunked->waitForData(
              std::stop_token(), 1)) != 0) {
    audioGraphHttpStreamChunked->read(data.data(), bytesToRead);
    totalBytesReadChunked += bytesToRead;
  }
  EXPECT_EQ(audioGraphHttpStream->getState().state,
            AudioGraphNodeState::FINISHED);
  EXPECT_EQ(audioGraphHttpStreamChunked->getState().state,
            AudioGraphNodeState::FINISHED);
  EXPECT_GT(totalBytesRead, 0);
  EXPECT_EQ(totalBytesRead, totalBytesReadChunked);
}

TEST_F(AudioGraphHttpStreamTest, test_broken_url_set_error_status) {
  auto audioGraphHttpStream = std::make_shared<AudioGraphHttpStream>(1, 
      server.url("/missing"), bufferSize);
  std::vector<uint8_t> data(bufferSize);
  size_t bytesToRead = 0;
  size_t totalBytesRead = 0;
  while ((bytesToRead =
              audioGraphHttpStream->waitForData(std::stop_token(), 1)) != 0) {
    audioGraphHttpStream->read(data.data(), bytesToRead);
    totalBytesRead += bytesToRead;
  }
  EXPECT_EQ(audioGraphHttpStream->getState().state, AudioGraphNodeState::ERROR);
  EXPECT_EQ(totalBytesRead, 0);
}

TEST_F(AudioGraphHttpStreamTest, seekTo_forward) {
  auto audioGraphHttpStream =
      std::make_shared<AudioGraphHttpStream>(1, url, bufferSize);
  std::vector<uint8_t> data(bufferSize);
  auto state =
      waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::STREAMING);
  auto contentLength = state.streamInfo.value().streamSize;
  size_t bytesToRead = 0;
  size_t totalBytesRead = 0;
  size_t halfContent = contentLength / 2;
  EXPECT_GT(contentLength, 0);
  while ((bytesToRead =
              audioGraphHttpStream->waitForData(std::stop_token(), 1)) != 0) {
    size_t sizeToRead = std::min(halfContent - totalBytesRead, bytesToRead);
    audioGraphHttpStream->read(data.data(), sizeToRead);
    totalBytesRead += sizeToRead;

    if (totalBytesRead == halfContent) {
      break;
    };
  }
  // Wait for the buffers to be full
  std::this_thread::sleep_for(std::chrono::milliseconds(2000));
  audioGraphHttpStream->seekTo(halfContent + halfContent / 2);

  while ((bytesToRead =
              audioGraphHttpStream->waitForData(std::stop_token(), 1)) != 0) {
    audioGraphHttpStream->read(data.data(), bytesToRead);
    totalBytesRead += bytesToRead;
  }

  EXPECT_EQ(audioGraphHttpStream->getState().state,
            AudioGraphNodeState::FINISHED);
  EXPECT_EQ(totalBytesRead, contentLength - halfContent / 2);
}

TEST_F(AudioGraphHttpStreamTest, seekTo_backward) {
  auto audioGraphHttpStream =
      std::make_shared<AudioGraphHttpStream>(1, url, bufferSize);
  std::vector<uint8_t> data(bufferSize);
  auto state =
      waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::STREAMING);
  auto contentLength = state.streamInfo.value().streamSize;
  size_t bytesToRead = 0;
  size_t totalBytesRead = 0;
  size_t halfContent = contentLength / 2;
  EXPECT_GT(contentLength, 0);
  while ((bytesToRead =
              audioGraphHttpStream->waitForData(std::stop_token(), 1)) != 0) {
    size_t sizeToRead = std::min(halfContent - totalBytesRead, bytesToRead);
    audioGraphHttpStream->read(data.data(), sizeToRead);
    totalBytesRead += sizeToRead;

    if (totalBytesRead == halfContent) {
      break;
    };
  }

  // Wait for the buffers to be full
  std::this_thread::sleep_for(std::chrono::milliseconds(2000));
  audioGraphHttpStream->seekTo(halfContent / 2);

  while ((bytesToRead =
              audioGraphHttpStream->waitForData(std::stop_token(), 1)) != 0) {
    audioGraphHttpStream->read(data.data(), bytesToRead);
    totalBytesRead += bytesToRead;
  }

  EXPECT_EQ(audioGraphHttpStream->getState().state,
            AudioGraphNodeState::FINISHED);
  EXPECT_EQ(totalBytesRead, halfContent + (contentLength - halfContent / 2));
}

TEST_F(AudioGraphHttpStreamTest, seekTo_backward_after_finished) {
  auto audioGraphHttpStream =
      std::make_shared<AudioGraphHttpStream>(1, url, bufferSize);
  std::vector<uint8_t> data(bufferSize);
  auto state =
      waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::STREAMING);
  auto contentLength = state.streamInfo.value().streamSize;
  size_t bytesToRead = 0;
  size_t totalBytesRead = 0;
  size_t halfContent = contentLength - bufferSize / 2;
  EXPECT_GT(contentLength, 0);
  while ((bytesToRead =
              audioGraphHttpStream->waitForData(std::stop_token(), 1)) != 0) {
    size_t sizeToRead = std::min(halfContent - totalBytesRead, bytesToRead);
    audioGraphHttpStream->read(data.data(), sizeToRead);
    totalBytesRead += sizeToRead;

    if (totalBytesRead == halfContent) {
      break;
    };
  }

  audioGraphHttpStream->seekTo(0);

  totalBytesRead = 0;

  while ((bytesToRead =
              audioGraphHttpStream->waitForData(std::stop_token(), 1)) != 0) {
    audioGraphHttpStream->read(data.data(), bytesToRead);
    totalBytesRead += bytesToRead;
  }

  EXPECT_EQ(audioGraphHttpStream->getState().state,
            AudioGraphNodeState::FINISHED);
  EXPECT_EQ(totalBytesRead, contentLength);
}

TEST_F(AudioGraphHttpStreamTest, seekToEnd) {
  auto audioGraphHttpStream =
      std::make_shared<AudioGraphHttpStream>(1, url, bufferSize);
  std::vector<uint8_t> data(bufferSize);
  auto state =
      waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::STREAMING);
  auto contentLength = state.streamInfo.value().streamSize;

  audioGraphHttpStream->seekTo(contentLength);
  state = waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::FINISHED,
                        std::chrono::milliseconds(1000));
  EXPECT_EQ(state.state, AudioGraphNodeState::FINISHED);
}

TEST_F(AudioGraphHttpStreamTest, seekToEndAndBack) {
  auto audioGraphHttpStream =
      std::make_shared<AudioGraphHttpStream>(1, url, bufferSize);
  std::vector<uint8_t> data(bufferSize);
  auto state =
      waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::STREAMING);
  auto contentLength = state.streamInfo.value().streamSize;

  audioGraphHttpStream->seekTo(contentLength);
  state = waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::FINISHED,
                        std::chrono::milliseconds(1000));
  EXPECT_EQ(state.state, AudioGraphNodeState::FINISHED);

  audioGraphHttpStream->seekTo(0);
  state = waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.state, AudioGraphNodeState::STREAMING);
}

TEST_F(AudioGraphHttpStreamTest, read_whole_dump) {
  auto audioGraphHttpStreamChunked =
      std::make_shared<AudioGraphHttpStream>(1, url, bufferSize, bufferSize / 2);
  auto audioGraphHttpStream =
      std::make_shared<AudioGraphHttpStream>(1, url, bufferSize, 0);
  std::vector<uint8_t> data(bufferSize);

  size_t bytesRead = 0;

  while (audioGraphHttpStream->waitForData(std::stop_token(), 1) != 0) {
    bytesRead += audioGraphHttpStream->read(data.data(), bufferSize);
  }
  size_t bytesReadChunked = 0;
  while (audioGraphHttpStreamChunked->waitForData(std::stop_token(), 1) != 0) {
    bytesReadChunked +=
        audioGraphHttpStreamChunked->read(data.data(), bufferSize);
  }

  EXPECT_EQ(bytesRead, bytesReadChunked);
}

TEST_F(AudioGraphHttpStreamTest, stalled_transfer_ends_in_timeout_error) {
  auto audioGraphHttpStream = std::make_shared<AudioGraphHttpStream>(
      1, server.url("/stall"), bufferSize, 0, stallTimeout);

  // Every attempt stalls: the first request and the three retries.
  auto state = waitForStatus(*audioGraphHttpStream, AudioGraphNodeState::ERROR,
                             std::chrono::seconds(20));

  ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
  ASSERT_TRUE(state.error.has_value());
  EXPECT_EQ(state.error->source, StreamErrorSource::HTTP_STREAM);
  EXPECT_THAT(state.error->message,
              ::testing::HasSubstr(curl_easy_strerror(CURLE_OPERATION_TIMEDOUT)));
}

TEST_F(AudioGraphHttpStreamTest, stalled_transfer_resumes_where_it_stopped) {
  const auto started = std::chrono::steady_clock::now();
  auto audioGraphHttpStream = std::make_shared<AudioGraphHttpStream>(
      1, server.url("/stall-once"), bufferSize, 0, stallTimeout);

  const auto content = readToEnd(*audioGraphHttpStream);

  // Otherwise the first request never stalled and nothing was resumed.
  EXPECT_GE(std::chrono::steady_clock::now() - started, stallTimeout);
  EXPECT_EQ(audioGraphHttpStream->getState().state,
            AudioGraphNodeState::FINISHED);
  const auto expected = fileContent(file);
  ASSERT_EQ(content.size(), expected.size());
  const auto differs =
      std::mismatch(content.begin(), content.end(), expected.begin()).first;
  EXPECT_TRUE(differs == content.end())
      << "first difference at byte " << differs - content.begin();
}

TEST_F(AudioGraphHttpStreamTest, full_buffer_is_not_a_stall) {
  // Without ranges there is no retry to hide a stall wrongly seen.
  auto audioGraphHttpStream = std::make_shared<AudioGraphHttpStream>(
      1, urlNoRanges, bufferSize, 0, stallTimeout);

  // A paused player: the whole file is one request, held up on a full buffer.
  audioGraphHttpStream->waitForData(std::stop_token(), bufferSize);
  std::this_thread::sleep_for(stallTimeout * 3);
  const auto content = readToEnd(*audioGraphHttpStream);

  EXPECT_EQ(audioGraphHttpStream->getState().state,
            AudioGraphNodeState::FINISHED);
  EXPECT_EQ(content.size(), fileContent(file).size());
}
