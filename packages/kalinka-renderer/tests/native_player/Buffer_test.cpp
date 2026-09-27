#include "Buffer.h"
#include "PerfMon.h"

#include <atomic>
#include <thread>

#include <gtest/gtest.h>

class BufferTest : public ::testing::Test {
protected:
};

TEST_F(BufferTest, constructor_destructor) {
  Buffer<uint8_t> buffer(20);
  EXPECT_EQ(buffer.max_size(), 20);
  EXPECT_EQ(buffer.size(), 0);
}

TEST_F(BufferTest, write_read) {
  Buffer<uint8_t> buffer(20);
  const std::string expectedData = "Hello, World!";
  const std::string actualData(expectedData.size(), '\0');

  buffer.write((uint8_t *)expectedData.data(), expectedData.size());
  buffer.read((uint8_t *)actualData.data(), actualData.size());

  EXPECT_EQ(expectedData, actualData);
}

TEST_F(BufferTest, waitForData) {
  Buffer<uint8_t> buffer(20);
  const std::string expectedData = "Hello, World!";
  const std::string actualData(expectedData.size(), '\0');

  std::jthread thread([&]() {
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    buffer.write((uint8_t *)expectedData.data(), expectedData.size());
  });

  const size_t size = buffer.waitForData(std::stop_token(), actualData.size());
  EXPECT_EQ(size, expectedData.size());

  buffer.read((uint8_t *)actualData.data(), actualData.size());

  EXPECT_EQ(expectedData, actualData);
}

TEST_F(BufferTest, waitForSpace) {
  Buffer<uint8_t> buffer(20);
  const std::string expectedData = "Hello, World!";
  const std::string actualData(expectedData.size(), '\0');

  auto size = buffer.write((uint8_t *)expectedData.data(), expectedData.size());
  EXPECT_EQ(size, expectedData.size());
  size = buffer.write((uint8_t *)expectedData.data(), expectedData.size());
  EXPECT_EQ(size, buffer.max_size() - expectedData.size());

  std::jthread thread([&]() {
    std::this_thread::sleep_for(std::chrono::milliseconds(500));
    buffer.read((uint8_t *)actualData.data(), actualData.size());
  });

  size = buffer.waitForSpace(std::stop_token(), actualData.size());
  EXPECT_EQ(size, expectedData.size());

  buffer.read((uint8_t *)actualData.data(), actualData.size());

  EXPECT_EQ(expectedData, actualData);
}

TEST_F(BufferTest, eof_is_seen_only_once_the_empty_callback_has_run) {
  for (int attempt = 0; attempt < 2000; ++attempt) {
    std::atomic<bool> calledBack = false;
    Buffer<uint8_t> buffer(20, [&](Buffer<uint8_t> &) { calledBack = true; });
    std::jthread writer([&] { buffer.setEof(); });

    // Close behind the writer, as a reader draining the buffer is.
    while (!buffer.isEof()) {
    }
    ASSERT_EQ(buffer.waitForData(), 0u);
    ASSERT_TRUE(calledBack) << "attempt " << attempt;
  }
}

#ifdef PROFILE

TEST_F(BufferTest, read_write_performance_test) {
  Buffer<uint8_t> buffer(14000);
  const std::vector<uint8_t> data(8192, 111);
  std::vector<uint8_t> actualData(data.size(), 0);
  const char *keyRead = "BufferTest.read";
  const char *keyWrite = "BufferTest.write";
  const char *keyMemcpy = "BufferTest.memcpy";
  const char *keyCopyn = "BufferTest.copyn";

  auto &perfMon = PerfMon::getInstance();

  for (int i = 0; i < 10; ++i) {
    perfMon.begin(keyWrite);
    buffer.write(data.data(), data.size());
    perfMon.end(keyWrite);
    perfMon.begin(keyRead);
    buffer.read(actualData.data(), actualData.size());
    perfMon.end(keyRead);
  }

  for (int i = 0; i < 10; ++i) {
    perfMon.begin(keyCopyn);
    std::copy_n(data.begin(), data.size(), actualData.begin());
    perfMon.end(keyCopyn);
  }

  for (int i = 0; i < 10; ++i) {
    perfMon.begin(keyMemcpy);
    std::memcpy(actualData.data(), data.data(), data.size());
    perfMon.end(keyMemcpy);
  }
  EXPECT_LE(perfMon.getAverageNs(keyRead), 5 * perfMon.getAverageNs(keyMemcpy));
}

#endif // PROFILE