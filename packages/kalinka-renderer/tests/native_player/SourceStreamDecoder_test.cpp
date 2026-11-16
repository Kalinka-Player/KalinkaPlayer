#include "SourceStreamDecoder.h"

#include <gtest/gtest.h>

#include <array>
#include <atomic>
#include <chrono>
#include <memory>
#include <thread>

#include "LiveInputNode.h"

namespace {
using namespace std::chrono_literals;

/// Reads what it can of its source on opening, and nothing more.
class FirstBytes final : public SourceStreamDecoder {
public:
  FirstBytes() : AudioGraphNode(1), SourceStreamDecoder(64, 0) {}
  ~FirstBytes() override { stop(); }

  std::atomic<long> got{-1};

private:
  std::string name() const override { return "First"; }
  StreamInfo open() override {
    std::array<uint8_t, 4096> bytes;
    got = long(readAvailable(bytes.data(), bytes.size()));
    return {{1000, 1, 16, PCM16_LE}, StreamType::FRAMES, 10};
  }
  bool decodeNext() override { return false; }
  void seekSource(uint64_t) override {}
};
} // namespace

TEST(SourceStreamDecoder,
     ReadAvailableTakesWhatHasArrivedWithoutWaitingForMore) {
  auto source = std::make_shared<LiveInputNode>();
  const std::array<uint8_t, 10> sent{};
  ASSERT_EQ(source->bytes.write(sent.data(), sent.size()), sent.size());
  FirstBytes decoder;
  decoder.connectTo(source);

  const auto deadline = std::chrono::steady_clock::now() + 2s;
  while (decoder.got < 0 && std::chrono::steady_clock::now() < deadline)
    std::this_thread::sleep_for(1ms);

  EXPECT_EQ(decoder.got, long(sent.size()))
      << "a paced producer sends no more until what it sent has played";
  decoder.disconnect(source);
}
