#include "VorbisStreamDecoder.h"

#include "AlsaAudioEmitter.h"
#include "AudioGraphHttpStream.h"
#include "FileInputNode.h"
#include "LocalHttpServer.h"
#include "TestHelpers.h"

#include <array>
#include <fstream>
#include <future>
#include <iterator>

namespace {
using namespace std::chrono_literals;
constexpr size_t kFrames = 22050 * 6;
constexpr size_t kFrameBytes = 4;

std::string fixture(const char *name = "ladder.ogg") {
  return std::string(KALINKA_TEST_DATA_DIR) + "/" + name;
}

std::vector<uint8_t> encoded(const char *name = "ladder.ogg") {
  std::ifstream file(fixture(name), std::ios::binary);
  return {std::istreambuf_iterator<char>(file),
          std::istreambuf_iterator<char>()};
}

std::vector<uint8_t> decode(AudioGraphOutputNode &decoder) {
  std::vector<uint8_t> pcm;
  std::array<uint8_t, 4096> chunk;
  const auto deadline = std::chrono::steady_clock::now() + 5s;
  while (std::chrono::steady_clock::now() < deadline) {
    if (decoder.waitForDataFor({}, 100ms, 1) > 0) {
      const auto bytes = decoder.read(chunk.data(), chunk.size());
      pcm.insert(pcm.end(), chunk.begin(), chunk.begin() + bytes);
    } else {
      const auto state = decoder.getState().state;
      if (state == AudioGraphNodeState::FINISHED ||
          state == AudioGraphNodeState::ERROR) {
        break;
      }
    }
  }
  return pcm;
}

// A sequential source that fragments every request, with no length metadata.
class SequentialInput : public AudioGraphOutputNode {
public:
  explicit SequentialInput(std::vector<uint8_t> bytes)
      : bytes(std::move(bytes)) {
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.streamType = StreamType::BYTES}});
  }
  size_t read(void *out, size_t size) override {
    const auto count = std::min({size, bytes.size() - position, size_t{13}});
    std::copy_n(bytes.data() + position, count, static_cast<uint8_t *>(out));
    position += count;
    if (position == bytes.size()) {
      setState(StreamState{AudioGraphNodeState::FINISHED});
    }
    return count;
  }
  size_t waitForData(std::stop_token, size_t size) override {
    return std::min({size, bytes.size() - position, size_t{13}});
  }
  size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds,
                        size_t size) override {
    return waitForData(token, size);
  }

private:
  std::vector<uint8_t> bytes;
  size_t position = 0;
};

/// @brief Seekable input whose reads can stall until cancelled or released.
class StallingFileInput : public AudioGraphOutputNode {
public:
  StreamState getState() override { return source.getState(); }
  size_t read(void *data, size_t size) override {
    return source.read(data, size);
  }
  size_t seekTo(size_t position) override { return source.seekTo(position); }
  size_t waitForData(std::stop_token token, size_t size) override {
    std::unique_lock lock(mutex);
    if (stalled) {
      ++blockedReads;
      changed.notify_all();
      changed.wait(lock, token, [this] { return !stalled; });
    }
    if (token.stop_requested()) {
      return 0;
    }
    if (failed) {
      throw std::runtime_error("Test input failed during seek");
    }
    lock.unlock();
    return source.waitForData(token, size);
  }
  size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds,
                        size_t size) override {
    return waitForData(token, size);
  }

  void stall() {
    std::lock_guard lock(mutex);
    stalled = true;
  }
  void release(bool fail = false) {
    std::lock_guard lock(mutex);
    stalled = false;
    failed = fail;
    changed.notify_all();
  }
  bool waitUntilStalled(size_t count = 1) {
    std::unique_lock lock(mutex);
    return changed.wait_for(lock, 1s,
                            [this, count] { return blockedReads >= count; });
  }

private:
  FileInputNode source{1, fixture()};
  std::mutex mutex;
  std::condition_variable_any changed;
  bool stalled = false;
  bool failed = false;
  size_t blockedReads = 0;
};

TEST(VorbisStreamDecoderTest, DecodesExactFrameCountAndTracksPartialReads) {
  auto source = std::make_shared<FileInputNode>(7, fixture());
  VorbisStreamDecoder decoder(7, 1024);
  decoder.connectTo(source);
  const auto state = waitForStatus(decoder, AudioGraphNodeState::STREAMING, 5s);
  ASSERT_EQ(state.state, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(state.streamInfo);
  EXPECT_EQ(state.streamInfo->streamSize, kFrames);
  EXPECT_EQ(state.streamInfo->format.sampleFormat, AudioSampleFormat::PCM16_LE);
  EXPECT_EQ(state.streamId, 7);

  std::array<uint8_t, 7> first;
  ASSERT_GE(decoder.waitForDataFor({}, 5s, first.size()), first.size());
  ASSERT_EQ(decoder.read(first.data(), first.size()), first.size());
  EXPECT_EQ(decoder.streamReadPosition(), 1);
  EXPECT_EQ(decode(decoder).size() + first.size(), kFrames * kFrameBytes);
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(decoder.streamReadPosition(), kFrames);
  EXPECT_EQ(decoder.getState().position, kFrames);
}

TEST(VorbisStreamDecoderTest, SeeksWhileBufferIsFullAndAfterEndOfStream) {
  auto source = std::make_shared<FileInputNode>(1, fixture());
  VorbisStreamDecoder decoder(1, 1024);
  decoder.connectTo(source);
  ASSERT_EQ(decoder.waitForDataFor({}, 5s, 1024), 1024);
  ASSERT_EQ(decoder.seekTo(22050 * 4), 22050 * 4);
  EXPECT_EQ(decode(decoder).size(), 22050 * 2 * kFrameBytes);
  ASSERT_EQ(decoder.seekTo(kFrames + 1), kFrames);
  EXPECT_TRUE(decode(decoder).empty());
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
  ASSERT_EQ(decoder.seekTo(0), 0);
  EXPECT_EQ(decode(decoder).size(), kFrames * kFrameBytes);
}

TEST(VorbisStreamDecoderTest,
     StalledSeekDoesNotBlockPlaybackControlOrShutdown) {
  auto source = std::make_shared<StallingFileInput>();
  auto decoder = std::make_shared<VorbisStreamDecoder>(1, 1024);
  decoder->connectTo(source);
  AlsaAudioEmitter emitter(Config{{"output.alsa.device", "null"}});
  emitter.connectTo(decoder);
  ASSERT_EQ(waitForStatus(emitter, AudioGraphNodeState::FINISHED, 5s).state,
            AudioGraphNodeState::FINISHED);

  source->stall();
  auto seeking =
      std::async(std::launch::async, [&] { return emitter.seek(2000); });
  EXPECT_TRUE(source->waitUntilStalled());
  EXPECT_EQ(seeking.wait_for(1s), std::future_status::ready);
  EXPECT_EQ(decoder->getState().state, AudioGraphNodeState::PREPARING);

  auto stopping = std::async(std::launch::async, [&] {
    emitter.disconnect(decoder);
    decoder->disconnect(source);
  });
  EXPECT_EQ(stopping.wait_for(1s), std::future_status::ready);
  source->release(); // Unblock cleanup if the regression returns.
  stopping.get();
  EXPECT_EQ(seeking.get(), 2000);
  EXPECT_EQ(decoder->getState().state, AudioGraphNodeState::STOPPED);
}

TEST(VorbisStreamDecoderTest,
     LaterSeekInterruptsStalledSeekAndResumesAtItsTarget) {
  auto source = std::make_shared<StallingFileInput>();
  VorbisStreamDecoder decoder(1, 1024);
  decoder.connectTo(source);
  const auto reference = decode(decoder);
  ASSERT_EQ(reference.size(), kFrames * kFrameBytes);

  source->stall();
  auto first =
      std::async(std::launch::async, [&] { return decoder.seekTo(22050); });
  EXPECT_TRUE(source->waitUntilStalled());
  const auto firstStatus = first.wait_for(1s);
  if (firstStatus != std::future_status::ready) {
    source->release();
  }
  EXPECT_EQ(first.get(), 22050);
  ASSERT_EQ(firstStatus, std::future_status::ready);

  constexpr size_t target = 22050 * 4;
  auto second =
      std::async(std::launch::async, [&] { return decoder.seekTo(target); });
  EXPECT_TRUE(source->waitUntilStalled(2));
  EXPECT_EQ(second.wait_for(1s), std::future_status::ready);
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::PREPARING);
  source->release();
  EXPECT_EQ(second.get(), target);

  const auto resumed = decode(decoder);
  ASSERT_NE(decoder.getState().state, AudioGraphNodeState::ERROR)
      << decoder.getState().toString();
  const std::vector<uint8_t> expected(reference.begin() + target * kFrameBytes,
                                      reference.end());
  EXPECT_EQ(resumed, expected);
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(decoder.streamReadPosition(), kFrames);
}

TEST(VorbisStreamDecoderTest, ReportsSeekFailureAfterAcknowledgement) {
  auto source = std::make_shared<StallingFileInput>();
  VorbisStreamDecoder decoder(1, 1024);
  decoder.connectTo(source);
  ASSERT_EQ(decode(decoder).size(), kFrames * kFrameBytes);

  source->stall();
  auto seeking =
      std::async(std::launch::async, [&] { return decoder.seekTo(22050); });
  EXPECT_TRUE(source->waitUntilStalled());
  EXPECT_EQ(seeking.wait_for(1s), std::future_status::ready);
  source->release(true);
  EXPECT_EQ(seeking.get(), 22050);

  const auto state = waitForStatus(decoder, AudioGraphNodeState::ERROR, 5s);
  ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
  ASSERT_TRUE(state.error);
  EXPECT_EQ(state.error->source, StreamErrorSource::DECODER);
  EXPECT_EQ(state.error->message, "Test input failed during seek");
  EXPECT_EQ(decoder.waitForDataFor({}, 1s, 1), 0);
  EXPECT_EQ(decoder.seekTo(0), static_cast<size_t>(-1));
}

TEST(VorbisStreamDecoderTest, DecodesFragmentedNonseekableInput) {
  auto source = std::make_shared<SequentialInput>(encoded());
  VorbisStreamDecoder decoder(1, 1024);
  decoder.connectTo(source);
  const auto state = waitForStatus(decoder, AudioGraphNodeState::STREAMING, 5s);
  ASSERT_EQ(state.state, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(state.streamInfo);
  EXPECT_FALSE(state.streamInfo->durationMs());
  EXPECT_EQ(decoder.seekTo(22050), static_cast<size_t>(-1));
  EXPECT_EQ(decode(decoder).size(), kFrames * kFrameBytes);
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
}

TEST(VorbisStreamDecoderTest, RejectsOffsetOnNonseekableInput) {
  auto source = std::make_shared<SequentialInput>(encoded());
  VorbisStreamDecoder decoder(1, 1024, 1000);
  decoder.connectTo(source);
  EXPECT_EQ(waitForStatus(decoder, AudioGraphNodeState::ERROR, 5s).state,
            AudioGraphNodeState::ERROR);
  EXPECT_TRUE(decode(decoder).empty());
  EXPECT_EQ(decoder.seekTo(0), static_cast<size_t>(-1));
}

TEST(VorbisStreamDecoderTest, DecodesMono) {
  auto source = std::make_shared<FileInputNode>(1, fixture("mono.ogg"));
  VorbisStreamDecoder decoder(1, 1024);
  decoder.connectTo(source);
  const auto state = waitForStatus(decoder, AudioGraphNodeState::STREAMING, 5s);
  ASSERT_EQ(state.state, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(state.streamInfo);
  EXPECT_EQ(state.streamInfo->format.channels, 1);
  EXPECT_EQ(decode(decoder).size(), 11025 * sizeof(int16_t));
}

TEST(VorbisStreamDecoderTest, RejectsInvalidInputAndUnblocksReadersAndSeekers) {
  for (auto bytes : {std::vector<uint8_t>{}, std::vector<uint8_t>(100, 'x'),
                     encoded("ladder.flac")}) {
    auto source = std::make_shared<SequentialInput>(std::move(bytes));
    VorbisStreamDecoder decoder(1, 1024);
    decoder.connectTo(source);
    const auto state = waitForStatus(decoder, AudioGraphNodeState::ERROR, 5s);
    ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
    ASSERT_TRUE(state.error);
    EXPECT_EQ(state.error->source, StreamErrorSource::DECODER);
    EXPECT_EQ(decoder.waitForDataFor({}, 1s, 1), 0);
    EXPECT_EQ(decoder.seekTo(0), static_cast<size_t>(-1));
  }
}

TEST(VorbisStreamDecoderTest, PlaysAndSeeksOverRangedHttp) {
  LocalHttpServer server(fixture());
  auto source = std::make_shared<AudioGraphHttpStream>(1, server.url("/ranged"),
                                                       16384, 4096);
  VorbisStreamDecoder decoder(1, 1024, 2000);
  decoder.connectTo(source);
  ASSERT_EQ(waitForStatus(decoder, AudioGraphNodeState::STREAMING, 5s).state,
            AudioGraphNodeState::STREAMING);
  EXPECT_EQ(decoder.streamReadPosition(), 44100);
  ASSERT_EQ(decoder.seekTo(22050 * 4), 22050 * 4);
  EXPECT_EQ(decode(decoder).size(), 22050 * 2 * kFrameBytes);
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
}

TEST(VorbisStreamDecoderTest, PlaysHttpWithoutRanges) {
  LocalHttpServer server(fixture());
  auto source =
      std::make_shared<AudioGraphHttpStream>(1, server.url("/whole"), 16384);
  VorbisStreamDecoder decoder(1, 1024);
  decoder.connectTo(source);
  EXPECT_EQ(decode(decoder).size(), kFrames * kFrameBytes);
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
}

TEST(VorbisStreamDecoderTest, PreservesHttpErrors) {
  LocalHttpServer server(fixture());
  auto source =
      std::make_shared<AudioGraphHttpStream>(1, server.url("/missing"), 16384);
  VorbisStreamDecoder decoder(1, 1024);
  decoder.connectTo(source);
  const auto state = waitForStatus(decoder, AudioGraphNodeState::ERROR, 5s);
  ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
  ASSERT_TRUE(state.error);
  EXPECT_EQ(state.error->source, StreamErrorSource::HTTP_STREAM);
  EXPECT_EQ(decoder.waitForDataFor({}, 1s, 1), 0);
}

TEST(VorbisStreamDecoderTest, DisconnectUnblocksAFullBufferAndAllowsReconnect) {
  auto source = std::make_shared<FileInputNode>(1, fixture());
  auto decoder = std::make_shared<VorbisStreamDecoder>(1, 1024);
  decoder->connectTo(source);
  ASSERT_EQ(decoder->waitForDataFor({}, 5s, 1024), 1024);
  ASSERT_TRUE(
      returnsWithin([decoder, source] { decoder->disconnect(source); }, 1s));
  EXPECT_EQ(decoder->seekTo(0), static_cast<size_t>(-1));
  source = std::make_shared<FileInputNode>(1, fixture());
  decoder->connectTo(source);
  EXPECT_EQ(decode(*decoder).size(), kFrames * kFrameBytes);
}

TEST(VorbisStreamDecoderTest, DisconnectUnblocksInputWaitingForData) {
  class WaitingInput : public AudioGraphOutputNode {
  public:
    size_t read(void *, size_t) override { return 0; }
    size_t waitForData(std::stop_token token, size_t) override {
      std::mutex mutex;
      std::condition_variable_any cv;
      std::unique_lock lock(mutex);
      cv.wait(lock, token, [] { return false; });
      return 0;
    }
    size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds,
                          size_t size) override {
      return waitForData(token, size);
    }
  };
  auto source = std::make_shared<WaitingInput>();
  auto decoder = std::make_shared<VorbisStreamDecoder>(1, 1024);
  decoder->connectTo(source);
  ASSERT_TRUE(
      returnsWithin([decoder, source] { decoder->disconnect(source); }, 1s));
}
} // namespace

TEST(VorbisStreamDecoderTest, PlaysChunkedOggWithUnknownLength) {
  LocalHttpServer server(fixture());
  auto source = std::make_shared<AudioGraphHttpStream>(1, server.url("/live"), 16384);
  VorbisStreamDecoder decoder(1, 16384);
  decoder.connectTo(source);
  const auto pcm = decode(decoder);
  EXPECT_EQ(pcm.size(), kFrames * kFrameBytes);
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(server.requestsTo("/live"), 1);
}

TEST(VorbisStreamDecoderTest, StartsBeforeTheNextLivePageOrEndOfStream) {
  class LiveInput : public AudioGraphOutputNode {
  public:
    Buffer<uint8_t> bytes{16384};
    LiveInput() {
      setState({AudioGraphNodeState::STREAMING, 0,
                StreamInfo{.streamType = StreamType::BYTES}});
    }
    size_t read(void *out, size_t count) override {
      return bytes.read(static_cast<uint8_t *>(out), count);
    }
    size_t waitForData(std::stop_token token, size_t count) override {
      return bytes.waitForData(token, count);
    }
    size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds timeout,
                         size_t count) override {
      return bytes.waitForDataFor(token, timeout, count);
    }
  };
  const auto ogg = encoded();
  // Three pages: identification, remaining headers, and about one second of
  // audio. The producer waits for playback feedback before sending any more.
  size_t prefix = 0;
  for (int page = 0; page < 3; ++page) {
    const auto segments = ogg.at(prefix + 26);
    size_t length = 27 + segments;
    for (size_t i = 0; i < segments; ++i) {
      length += ogg.at(prefix + 27 + i);
    }
    prefix += length;
  }
  auto source = std::make_shared<LiveInput>();
  ASSERT_EQ(source->bytes.write(ogg.data(), prefix), prefix);
  VorbisStreamDecoder decoder(1, 16384);
  decoder.connectTo(source);
  EXPECT_GE(decoder.waitForDataFor({}, 1s, 1024), 1024);
  decoder.disconnect(source);
}
