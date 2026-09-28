#include <gtest/gtest.h>

#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "native_player/FileInputNode.h"
#include "native_player/FlacStreamDecoder.h"
#include "native_player/Mp3StreamDecoder.h"
#include "native_player/VorbisStreamDecoder.h"

// All decoders against the same fixture: 6 s of a 440 Hz sine whose amplitude
// steps once a second (tests/data/README.md). Peak amplitude therefore names
// the second the decoder landed in, which survives MP3's lossy coding where
// exact sample values would not.
namespace {

constexpr unsigned int kSampleRate = 22050;
constexpr unsigned int kChannels = 2;
constexpr unsigned int kSeconds = 6;
constexpr size_t kBufferSize = 1 << 20;

// Amplitude of the sine during `second`, as written by the generator.
double levelOf(int second) { return 0.10 + 0.15 * second; }

std::string fixture(const std::string &name) {
  return std::string(KALINKA_TEST_DATA_DIR) + "/" + name;
}

using Decoder = AudioGraphOutputNode;

struct Format {
  const char *name;
  const char *file;
  std::shared_ptr<Decoder> (*make)(size_t startOffsetMs);
};

// Keeps ctest's test names readable rather than "24-byte object <...>".
void PrintTo(const Format &format, std::ostream *out) { *out << format.name; }

std::shared_ptr<Decoder> makeFlac(size_t startOffsetMs) {
  return std::make_shared<FlacStreamDecoder>(1, kBufferSize, startOffsetMs);
}

std::shared_ptr<Decoder> makeMp3(size_t startOffsetMs) {
  return std::make_shared<Mp3StreamDecoder>(1, kBufferSize, startOffsetMs);
}

std::shared_ptr<Decoder> makeVorbis(size_t startOffsetMs) {
  return std::make_shared<VorbisStreamDecoder>(1, kBufferSize, startOffsetMs);
}

// A decoder reading the fixture, held together so the input node outlives it.
class Playing {
public:
  Playing(const Format &format, size_t startOffsetMs)
      : source_(std::make_shared<FileInputNode>(1, fixture(format.file))),
        decoder_(format.make(startOffsetMs)) {
    // Subscribed rather than polled: a state can be superseded between two
    // reads of it, and a failure is followed straight away by STOPPED.
    decoder_->onStateChange([this](AudioGraphNode *, StreamState state) {
      std::lock_guard<std::mutex> lock(mutex_);
      seen_.push_back(state.state);
      return true;
    });
    std::dynamic_pointer_cast<AudioGraphInputNode>(decoder_)->connectTo(
        source_);
  }

  ~Playing() {
    std::dynamic_pointer_cast<AudioGraphInputNode>(decoder_)->disconnect(
        source_);
  }

  Decoder &decoder() { return *decoder_; }

  // The state once the decoder has settled, so a test never reads PREPARING.
  StreamState
  settled(std::chrono::milliseconds timeout = std::chrono::milliseconds(5000)) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    while (std::chrono::steady_clock::now() < deadline) {
      auto state = decoder_->getState();
      if (state.state == AudioGraphNodeState::STREAMING ||
          state.state == AudioGraphNodeState::ERROR ||
          state.state == AudioGraphNodeState::FINISHED) {
        return state;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    return decoder_->getState();
  }

  // Frames of interleaved 16-bit stereo, or fewer at the end of the stream.
  std::vector<int16_t> frames(size_t count) {
    const size_t wanted = count * kChannels * sizeof(int16_t);
    std::vector<uint8_t> bytes(wanted);
    size_t filled = 0;
    while (filled < wanted) {
      if (decoder_->waitForData(std::stop_token(), 1) == 0) {
        break; // end of stream
      }
      const size_t read =
          decoder_->read(bytes.data() + filled, wanted - filled);
      if (read == 0) {
        break;
      }
      filled += read;
    }
    std::vector<int16_t> samples(filled / sizeof(int16_t));
    std::memcpy(samples.data(), bytes.data(), samples.size() * sizeof(int16_t));
    return samples;
  }

  // Discards `count` frames, for landing clear of a block boundary.
  void skip(size_t count) { frames(count); }

  // Whether the decoder ever reported this state, however briefly.
  bool reported(
      AudioGraphNodeState wanted,
      std::chrono::milliseconds timeout = std::chrono::milliseconds(5000)) {
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    do {
      std::unique_lock<std::mutex> lock(mutex_);
      for (AudioGraphNodeState state : seen_) {
        if (state == wanted) {
          return true;
        }
      }
      lock.unlock();
      std::this_thread::sleep_for(std::chrono::milliseconds(10));
    } while (std::chrono::steady_clock::now() < deadline);
    return false;
  }

private:
  std::shared_ptr<FileInputNode> source_;
  std::shared_ptr<Decoder> decoder_;
  std::mutex mutex_;
  std::vector<AudioGraphNodeState> seen_;
};

double peak(const std::vector<int16_t> &samples) {
  double loudest = 0;
  for (int16_t sample : samples) {
    loudest =
        std::max(loudest, std::abs(static_cast<double>(sample)) / 32768.0);
  }
  return loudest;
}

// Which second of the fixture these samples come from, by amplitude. Steps are
// 0.15 apart, so half a step of slack absorbs MP3's coding error.
int secondOf(const std::vector<int16_t> &samples) {
  const double loudest = peak(samples);
  for (int second = 0; second < static_cast<int>(kSeconds); ++second) {
    if (std::abs(loudest - levelOf(second) * (32000.0 / 32768.0)) < 0.06) {
      return second;
    }
  }
  return -1;
}

class DecoderTest : public testing::TestWithParam<Format> {};

INSTANTIATE_TEST_SUITE_P(
    Formats, DecoderTest,
    testing::Values(Format{"flac", "ladder.flac", &makeFlac},
                    Format{"mp3", "ladder.mp3", &makeMp3},
                    Format{"vorbis", "ladder.ogg", &makeVorbis}),
    [](const testing::TestParamInfo<Format> &info) { return info.param.name; });

TEST_P(DecoderTest, ReportsTheFormatItIsDecoding) {
  Playing playing(GetParam(), 0);

  auto state = playing.settled();

  ASSERT_EQ(state.state, AudioGraphNodeState::STREAMING);
  ASSERT_TRUE(state.streamInfo.has_value());
  EXPECT_EQ(state.streamInfo->format.sampleRate, kSampleRate);
  EXPECT_EQ(state.streamInfo->format.channels, kChannels);
  EXPECT_EQ(state.streamInfo->format.bitsPerSample, 16u);
  EXPECT_EQ(state.streamInfo->streamType, StreamType::FRAMES);
  // Frame count, give or take the encoder's padding.
  EXPECT_NEAR(static_cast<double>(state.streamInfo->streamSize),
              kSampleRate * kSeconds, kSampleRate * 0.1);
}

TEST_P(DecoderTest, WithoutAnOffsetItStartsAtTheBeginning) {
  Playing playing(GetParam(), 0);
  ASSERT_EQ(playing.settled().state, AudioGraphNodeState::STREAMING);

  playing.skip(kSampleRate / 5); // clear of the encoder's first frames

  EXPECT_EQ(secondOf(playing.frames(kSampleRate / 4)), 0);
}

TEST_P(DecoderTest, AnOffsetStartsThereInsteadOfSeekingToIt) {
  Playing playing(GetParam(), 3000);
  ASSERT_EQ(playing.settled().state, AudioGraphNodeState::STREAMING);

  playing.skip(kSampleRate / 5);

  EXPECT_EQ(secondOf(playing.frames(kSampleRate / 4)), 3);
}

TEST_P(DecoderTest, TheOffsetIsWhereTheFirstFrameComesFrom) {
  // Nothing before the offset is emitted: the very first samples handed out
  // are already the ones at the offset, so there is no jump to hear.
  Playing playing(GetParam(), 4000);
  ASSERT_EQ(playing.settled().state, AudioGraphNodeState::STREAMING);

  EXPECT_EQ(secondOf(playing.frames(kSampleRate / 4)), 4);
}

TEST_P(DecoderTest, TheReportedPositionStartsAtTheOffset) {
  Playing playing(GetParam(), 2000);

  auto state = playing.settled();

  ASSERT_EQ(state.state, AudioGraphNodeState::STREAMING);
  EXPECT_NEAR(static_cast<double>(state.position), 2 * kSampleRate,
              kSampleRate * 0.1);
}

TEST_P(DecoderTest, TheReadPositionFollowsWhatHasBeenHandedOut) {
  // In frames, and absolute in the track: the sink re-bases its own count on
  // this, so bytes counted as frames would race the position away.
  Playing playing(GetParam(), 2000);
  ASSERT_EQ(playing.settled().state, AudioGraphNodeState::STREAMING);

  ASSERT_EQ(playing.decoder().streamReadPosition().value_or(-1),
            static_cast<long>(2 * kSampleRate));

  playing.skip(kSampleRate / 2);

  EXPECT_NEAR(playing.decoder().streamReadPosition().value_or(-1),
              2.5 * kSampleRate, 32);
}

TEST_P(DecoderTest, SeekingAfterAnOffsetStartIsStillAbsolute) {
  // The offset moves where playback begins, not where the stream begins: a
  // seek that follows is measured from the start of the track as ever.
  Playing playing(GetParam(), 3000);
  ASSERT_EQ(playing.settled().state, AudioGraphNodeState::STREAMING);

  ASSERT_NE(playing.decoder().seekTo(kSampleRate), static_cast<size_t>(-1));
  playing.skip(kSampleRate / 5);

  EXPECT_EQ(secondOf(playing.frames(kSampleRate / 4)), 1);
}

TEST_P(DecoderTest, AnOffsetPastTheEndFailsRatherThanHanging) {
  Playing playing(GetParam(), 1000 * kSeconds + 5000);

  EXPECT_TRUE(playing.reported(AudioGraphNodeState::ERROR));
  EXPECT_EQ(playing.frames(1).size(), 0u) << "nothing to play";
}

TEST_P(DecoderTest, PlaysToTheEndAndFinishes) {
  Playing playing(GetParam(), 0);
  ASSERT_EQ(playing.settled().state, AudioGraphNodeState::STREAMING);

  const auto decoded = playing.frames(kSampleRate * (kSeconds + 1));

  EXPECT_NEAR(static_cast<double>(decoded.size() / kChannels),
              kSampleRate * kSeconds, kSampleRate * 0.2);
  EXPECT_EQ(playing.decoder().getState().state, AudioGraphNodeState::FINISHED);
}

} // namespace
