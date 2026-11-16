#include "FileInputNode.h"

#include "TestHelpers.h"
#include <gtest/gtest.h>

class FileInputNodeTest : public ::testing::Test {
protected:
  const std::string filename = testFile("tone440.flac");
};

TEST_F(FileInputNodeTest, constructor_destructor) {
  FileInputNode fileInputNode(1, filename);
  EXPECT_EQ(fileInputNode.getState().state, AudioGraphNodeState::STREAMING);
}

TEST_F(FileInputNodeTest, read) {
  FileInputNode fileInputNode(1, filename);
  auto state = waitForStatus(fileInputNode, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(state.streamInfo.value().streamType, StreamType::BYTES);
  size_t fileSize = state.streamInfo.value().streamSize;
  uint8_t data[100];
  int bytesRead = 0;
  int num = 0;
  do {
    num = fileInputNode.read(data, 100);
    bytesRead += num;
  } while (num != 0);
  EXPECT_EQ(fileInputNode.getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(bytesRead, fileSize);
}

TEST_F(FileInputNodeTest, waitForDataReportsAllThatIsLeft) {
  FileInputNode fileInputNode(1, filename);
  const auto fileSize = fileInputNode.getState().streamInfo->streamSize;
  uint8_t data[100];
  ASSERT_EQ(fileInputNode.read(data, sizeof(data)), sizeof(data));
  EXPECT_EQ(fileInputNode.waitForData({}, 1), fileSize - sizeof(data));
}

TEST_F(FileInputNodeTest, waitForDataReportsNothingOnceRead) {
  FileInputNode fileInputNode(1, filename);
  uint8_t data[100];
  while (fileInputNode.read(data, sizeof(data))) {
  }
  EXPECT_EQ(fileInputNode.waitForData({}, 1), 0u);
}

TEST_F(FileInputNodeTest, seek) {
  FileInputNode fileInputNode(1, filename);
  auto state = waitForStatus(fileInputNode, AudioGraphNodeState::STREAMING);
  size_t fileSize = state.streamInfo.value().streamSize;
  uint8_t data[100];
  int bytesRead = 0;
  int num = 0;
  do {
    num = fileInputNode.read(data, 100);
    bytesRead += num;
  } while (num != 0);
  EXPECT_EQ(fileInputNode.getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(bytesRead, fileSize);

  EXPECT_EQ(fileInputNode.seekTo(0), 0);
  bytesRead = 0;
  do {
    num = fileInputNode.read(data, 100);
    bytesRead += num;
  } while (num != 0);
  EXPECT_EQ(fileInputNode.getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(bytesRead, fileSize);
}

TEST_F(FileInputNodeTest, seekToEnd) {
  FileInputNode fileInputNode(1, filename);
  auto state = waitForStatus(fileInputNode, AudioGraphNodeState::STREAMING);
  size_t fileSize = state.streamInfo.value().streamSize;
  EXPECT_EQ(fileInputNode.seekTo(fileSize), fileSize);
  EXPECT_EQ(fileInputNode.getState().state, AudioGraphNodeState::FINISHED);
}

TEST_F(FileInputNodeTest, seekPastEnd) {
  FileInputNode fileInputNode(1, filename);
  auto state = waitForStatus(fileInputNode, AudioGraphNodeState::STREAMING);
  size_t fileSize = state.streamInfo.value().streamSize;
  EXPECT_EQ(fileInputNode.seekTo(fileSize + 1), fileSize);
  EXPECT_EQ(fileInputNode.getState().state, AudioGraphNodeState::FINISHED);
}