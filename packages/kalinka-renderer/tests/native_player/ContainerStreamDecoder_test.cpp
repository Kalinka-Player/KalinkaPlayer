#include "ContainerStreamDecoder.h"
#include "ContainerTestSupport.h"
#include "ErrorFakeNode.h"

#include <array>
#include <condition_variable>
#include <future>
#include <gtest/gtest.h>
#include <mutex>
#include <optional>

namespace {
using container_test::Bytes;
using container_test::BytesInput;
using container_test::drain;
using namespace std::chrono_literals;

constexpr size_t FRAME_BYTES = 4;
constexpr size_t BUFFER_SIZE = 1024;

/// "RAW!", a four-byte frame count, then 16-bit stereo frames at 48 kHz.
class RawFormat final : public ContainerFormat {
public:
  /// Frames per block: small, so a short file still spans many blocks.
  uint64_t blockFrames = 100;
  /// Throw from nextBlock() once the cursor reaches this frame.
  std::optional<uint64_t> failAt;
  /// Describe the audio with a rate of 0, as a broken format would.
  bool describeNothing = false;

  std::string name() const override { return "Raw"; }
  Header parseHeader(ByteReader &source) override {
    nextFrame = 0;
    std::array<uint8_t, 8> header;
    source.readExact(header.data(), header.size());
    container::require(container::tagged(header.data(), "RAW!"),
                       "Not a raw file");
    frames = container::number(header.data() + 4, 4);
    const unsigned rate = describeNothing ? 0 : 48000;
    return {{{rate, 2, 16, PCM16_LE},
             StreamType::FRAMES,
             static_cast<unsigned long>(frames)},
            frames * FRAME_BYTES};
  }
  uint64_t seek(uint64_t frame) override {
    nextFrame = frame;
    return frame * FRAME_BYTES;
  }
  std::vector<uint8_t> nextBlock(ByteReader &source) override {
    if (failAt && nextFrame >= *failAt)
      throw std::runtime_error("Raw block went bad");
    const auto count = std::min(blockFrames, frames - nextFrame);
    std::vector<uint8_t> block(count * FRAME_BYTES);
    source.readExact(block.data(), block.size());
    nextFrame += count;
    return block;
  }

private:
  uint64_t frames = 0, nextFrame = 0;
};

Bytes payload(unsigned frames, uint8_t seed = 1) {
  Bytes out;
  for (size_t i = 0; i < size_t(frames) * FRAME_BYTES; ++i)
    out.push_back(uint8_t(seed + i * 7));
  return out;
}

Bytes raw(unsigned frames, uint8_t seed = 1) {
  Bytes out{'R', 'A', 'W', '!'};
  for (unsigned i = 0; i < 4; ++i)
    out.push_back(frames >> (8 * i));
  const auto body = payload(frames, seed);
  out.insert(out.end(), body.begin(), body.end());
  return out;
}

Bytes from(const Bytes &all, size_t frame) {
  return Bytes(all.begin() + frame * FRAME_BYTES, all.end());
}

std::unique_ptr<RawFormat> rawFormat() { return std::make_unique<RawFormat>(); }
} // namespace

TEST(ContainerDecoder, StreamsWholeFramesAndStampsItsStream) {
  for (auto blockFrames : {100u, 1000u}) {
    SCOPED_TRACE(blockFrames);
    auto format = rawFormat();
    format->blockFrames = blockFrames;
    ContainerStreamDecoder decoder(7, std::move(format), BUFFER_SIZE);
    decoder.connectTo(std::make_shared<BytesInput>(raw(1234)));
    EXPECT_EQ(drain(decoder), payload(1234));
    const auto state = decoder.getState();
    EXPECT_EQ(state.state, AudioGraphNodeState::FINISHED);
    EXPECT_EQ(state.streamId, 7u);
    ASSERT_TRUE(state.streamInfo);
    EXPECT_EQ(state.streamInfo->streamSize, 1234u);
    EXPECT_EQ(state.streamInfo->format.sampleFormat, PCM16_LE);
    EXPECT_EQ(decoder.streamReadPosition(), 1234);
  }
}

TEST(ContainerDecoder, SeeksStartOffsetsAndClampsToTheEnd) {
  ContainerStreamDecoder decoder(1, rawFormat(), BUFFER_SIZE, 10);
  decoder.connectTo(std::make_shared<BytesInput>(raw(1234)));
  const auto all = payload(1234);
  EXPECT_EQ(drain(decoder), from(all, 480));
  for (size_t frame : {100u, 1200u, 10u, 1234u, 5000u, 0u}) {
    const auto target = std::min(frame, size_t(1234));
    ASSERT_EQ(decoder.seekTo(frame), target);
    EXPECT_EQ(drain(decoder), from(all, target));
    EXPECT_EQ(decoder.streamReadPosition(), 1234);
  }
}

TEST(ContainerDecoder, SequentialSourcesPlayButRefuseSeekingAndStartOffsets) {
  ContainerStreamDecoder decoder(1, rawFormat(), BUFFER_SIZE);
  decoder.connectTo(std::make_shared<BytesInput>(raw(300), false, false));
  EXPECT_EQ(drain(decoder), payload(300));
  EXPECT_EQ(decoder.seekTo(0), size_t(-1));
  ContainerStreamDecoder offset(1, rawFormat(), BUFFER_SIZE, 1);
  offset.connectTo(std::make_shared<BytesInput>(raw(300), false, false));
  EXPECT_TRUE(drain(offset).empty());
  ASSERT_TRUE(offset.getState().error);
  EXPECT_EQ(offset.getState().error->message,
            "Raw start offset requires a seekable stream");
  ContainerStreamDecoder late(1, rawFormat(), BUFFER_SIZE, 100);
  late.connectTo(std::make_shared<BytesInput>(raw(300)));
  EXPECT_TRUE(drain(late).empty());
  ASSERT_TRUE(late.getState().error);
  EXPECT_EQ(late.getState().error->message, "Raw start offset is past the end");
}

TEST(ContainerDecoder, RefusesWhatTheFormatRefuses) {
  ContainerStreamDecoder decoder(1, rawFormat(), BUFFER_SIZE);
  decoder.connectTo(
      std::make_shared<BytesInput>(Bytes{'N', 'O', 'P', 'E', 0, 0, 0, 0}));
  EXPECT_TRUE(drain(decoder).empty());
  const auto state = decoder.getState();
  ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
  ASSERT_TRUE(state.error);
  EXPECT_EQ(state.error->source, StreamErrorSource::DECODER);
  EXPECT_EQ(state.error->message, "Not a raw file");
  EXPECT_EQ(decoder.seekTo(0), size_t(-1));

  auto failing = rawFormat();
  failing->failAt = 300;
  ContainerStreamDecoder midway(1, std::move(failing), BUFFER_SIZE);
  midway.connectTo(std::make_shared<BytesInput>(raw(1000)));
  EXPECT_EQ(drain(midway), payload(300));
  ASSERT_TRUE(midway.getState().error);
  EXPECT_EQ(midway.getState().error->message, "Raw block went bad");

  auto blank = rawFormat();
  blank->describeNothing = true;
  ContainerStreamDecoder unplayable(1, std::move(blank), BUFFER_SIZE);
  unplayable.connectTo(std::make_shared<BytesInput>(raw(10)));
  EXPECT_TRUE(drain(unplayable).empty());
  ASSERT_TRUE(unplayable.getState().error);
  EXPECT_EQ(unplayable.getState().error->message,
            "Raw header describes no audio format");
}

TEST(ContainerDecoder, ReportsTheSourcesOwnFailure) {
  ContainerStreamDecoder decoder(1, rawFormat(), BUFFER_SIZE);
  decoder.connectTo(std::make_shared<ErrorFakeNode>());
  EXPECT_TRUE(drain(decoder).empty());
  ASSERT_TRUE(decoder.getState().error);
  EXPECT_EQ(decoder.getState().error->source, StreamErrorSource::NONE);
  EXPECT_EQ(decoder.getState().error->message, "Fake error message");

  auto cut = raw(1000);
  cut.resize(8 + 500 * FRAME_BYTES);
  ContainerStreamDecoder known(1, rawFormat(), BUFFER_SIZE);
  known.connectTo(std::make_shared<BytesInput>(cut));
  EXPECT_TRUE(drain(known).empty());
  ASSERT_TRUE(known.getState().error);
  EXPECT_EQ(known.getState().error->message, "Truncated Raw audio data");

  ContainerStreamDecoder unknown(1, rawFormat(), BUFFER_SIZE);
  unknown.connectTo(std::make_shared<BytesInput>(cut, true, false));
  EXPECT_EQ(drain(unknown), payload(500));
  ASSERT_TRUE(unknown.getState().error);
  EXPECT_EQ(unknown.getState().error->message, "Truncated Raw file");
}

TEST(ContainerDecoder, FailedSourceSeekFailsTheSeek) {
  class ProbeOnly : public BytesInput {
  public:
    using BytesInput::BytesInput;
    bool probed = false;
    size_t seekTo(size_t n) override {
      if (probed)
        return size_t(-1);
      probed = true;
      return BytesInput::seekTo(n);
    }
  };
  ContainerStreamDecoder decoder(1, rawFormat(), BUFFER_SIZE);
  decoder.connectTo(std::make_shared<ProbeOnly>(raw(300)));
  EXPECT_EQ(drain(decoder), payload(300));
  EXPECT_EQ(decoder.seekTo(10), size_t(-1));
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::ERROR);
}

TEST(ContainerDecoder, SeekAndShutdownInterruptAStalledSource) {
  class Stalled : public BytesInput {
  public:
    using BytesInput::BytesInput;
    std::promise<void> blocked;
    bool notified = false;
    size_t waitForData(std::stop_token token, size_t n) override {
      if (position < 8 + 50 * FRAME_BYTES)
        return BytesInput::waitForData(token, n);
      if (!notified) {
        notified = true;
        blocked.set_value();
      }
      std::mutex mutex;
      std::condition_variable_any changed;
      std::unique_lock lock(mutex);
      changed.wait(lock, token, [] { return false; });
      return 0;
    }
  };
  auto input = std::make_shared<Stalled>(raw(300));
  auto decoder =
      std::make_unique<ContainerStreamDecoder>(1, rawFormat(), BUFFER_SIZE);
  decoder->connectTo(input);
  ASSERT_EQ(input->blocked.get_future().wait_for(1s),
            std::future_status::ready);
  auto seek =
      std::async(std::launch::async, [&] { return decoder->seekTo(100); });
  ASSERT_EQ(seek.wait_for(1s), std::future_status::ready);
  EXPECT_EQ(seek.get(), 100u);
  auto stop = std::async(std::launch::async, [&] { decoder.reset(); });
  EXPECT_EQ(stop.wait_for(1s), std::future_status::ready);
}

TEST(ContainerDecoder, BufferMustHoldAFrame) {
  ContainerStreamDecoder decoder(1, rawFormat(), FRAME_BYTES - 1);
  decoder.connectTo(std::make_shared<BytesInput>(raw(10)));
  EXPECT_TRUE(drain(decoder).empty());
  ASSERT_TRUE(decoder.getState().error);
  EXPECT_EQ(decoder.getState().error->message,
            "Raw buffer is smaller than one frame");
}

TEST(ContainerDecoder, ReconnectingReadsTheNewSource) {
  ContainerStreamDecoder decoder(1, rawFormat(), BUFFER_SIZE);
  auto first = std::make_shared<BytesInput>(raw(250, 1));
  decoder.connectTo(first);
  EXPECT_EQ(drain(decoder), payload(250, 1));
  decoder.disconnect(first);
  decoder.connectTo(std::make_shared<BytesInput>(raw(120, 9)));
  EXPECT_EQ(drain(decoder), payload(120, 9));
  EXPECT_EQ(decoder.streamReadPosition(), 120);
}
