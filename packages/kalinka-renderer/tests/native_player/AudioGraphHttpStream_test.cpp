#include "AudioGraphHttpStream.h"

#include <gtest/gtest.h>
#include <memory>
#include <vector>

#include "TestHelpers.h"

class AudioGraphHttpStreamTest : public ::testing::Test {
protected:
  const std::string url =
      "https://getsamplefiles.com/download/flac/sample-2.flac";
  const std::string urlNoRanges =
      "https://filesamples.com/samples/audio/flac/sample1.flac";
  const size_t bufferSize = 32768;

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
      "http://httpstat.us/404", bufferSize);
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