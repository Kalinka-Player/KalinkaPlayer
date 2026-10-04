#include "AlsaAudioEmitter.h"
#include "AudioGraphHttpStream.h"
#include "AudioPlayer.h"
#include "AudioSampleFormat.h"
#include "DsdStreamDecoder.h"
#include "LocalHttpServer.h"
#include "player/StateTranslator.h"
#include <array>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <future>
#include <gtest/gtest.h>
#include <unistd.h>

namespace {
using Bytes = std::vector<uint8_t>;
using namespace std::chrono_literals;
void append(Bytes &out, std::span<const uint8_t> data) {
  out.insert(out.end(), data.begin(), data.end());
}
void tag(Bytes &out, const char *s) { out.insert(out.end(), s, s + 4); }
void num(Bytes &out, uint64_t n, unsigned width, bool little = false) {
  for (unsigned i = 0; i < width; ++i)
    out.push_back(n >> (8 * (little ? i : width - 1 - i)));
}
Bytes dsf(unsigned order = 8, unsigned channels = 2, unsigned rate = 2822400,
          unsigned trailingBlocks = 0) {
  const uint64_t data = uint64_t(channels) * 4096 * (1 + trailingBlocks);
  Bytes out;
  tag(out, "DSD ");
  num(out, 28, 8, true);
  num(out, 92 + data, 8, true);
  num(out, 0, 8, true);
  tag(out, "fmt ");
  num(out, 52, 8, true);
  for (auto n : {1u, 0u, channels, channels, rate, order})
    num(out, n, 4, true);
  num(out, 4096 * 8, 8, true);
  num(out, 4096, 4, true);
  num(out, 0, 4, true);
  tag(out, "data");
  num(out, 12 + data, 8, true);
  for (unsigned c = 0; c < channels; ++c)
    for (unsigned i = 0; i < 4096; ++i)
      out.push_back((i + 17 * c) & 255);
  out.resize(out.size() + data - channels * 4096, 0x5a);
  return out;
}
Bytes chunk(const char *name, const Bytes &data) {
  Bytes out;
  tag(out, name);
  num(out, data.size(), 8);
  append(out, data);
  if (data.size() & 1)
    out.push_back(0);
  return out;
}
Bytes dff(bool dst = false) {
  Bytes prop;
  tag(prop, "SND ");
  Bytes rate;
  num(rate, 2822400, 4);
  append(prop, chunk("FS  ", rate));
  Bytes channels;
  num(channels, 2, 2);
  tag(channels, "SLFT");
  tag(channels, "SRGT");
  append(prop, chunk("CHNL", channels));
  Bytes compression;
  tag(compression, dst ? "DST " : "DSD ");
  compression.push_back(0);
  append(prop, chunk("CMPR", compression));
  Bytes body;
  tag(body, "DSD ");
  append(body, chunk("PROP", prop));
  append(body, chunk("DSD ", {0x01, 0x81, 0x02, 0x82, 0x03, 0x83, 0x04, 0x84}));
  return chunk("FRM8", body);
}
class Input : public AudioGraphOutputNode {
public:
  Bytes bytes;
  size_t position = 0;
  bool seekable;
  explicit Input(Bytes data, bool seekable = true)
      : bytes(std::move(data)), seekable(seekable) {
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.streamType = StreamType::BYTES,
                         .streamSize = bytes.size()}});
  }
  size_t read(void *dest, size_t n) override {
    n = std::min({n, bytes.size() - position, size_t(13)});
    std::memcpy(dest, bytes.data() + position, n);
    position += n;
    return n;
  }
  size_t waitForData(std::stop_token, size_t n) override {
    return std::min(n, bytes.size() - position);
  }
  size_t waitForDataFor(std::stop_token t, std::chrono::milliseconds,
                        size_t n) override {
    return waitForData(t, n);
  }
  size_t seekTo(size_t n) override {
    if (!seekable || n > bytes.size())
      return size_t(-1);
    position = n;
    return n;
  }
};
DsdStreamDecoder::SelectOutput select(AudioSampleFormat format) {
  return [format](unsigned rate, unsigned channels) {
    return StreamAudioFormat{rate / dsdBitsPerFrame(format), channels, 1,
                             format, rate};
  };
}
Bytes drain(DsdStreamDecoder &node) {
  Bytes result;
  std::array<uint8_t, 2400> data;
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  while (std::chrono::steady_clock::now() < deadline) {
    if (node.waitForDataFor({}, 50ms, 1)) {
      auto n = node.read(data.data(), data.size());
      append(result, {data.data(), n});
    } else if (node.getState().state == AudioGraphNodeState::FINISHED ||
               node.getState().state == AudioGraphNodeState::ERROR)
      return result;
  }
  ADD_FAILURE() << "DSD decoder did not finish";
  return result;
}
} // namespace

TEST(DsdPacking, NativeAndDopVectorsPreserveChronologicalBits) {
  const Bytes input{0x01, 0x81, 0x02, 0x82, 0x03, 0x83, 0x04, 0x84};
  EXPECT_EQ(packDsd(input, 2, DSD_U8, 0), input);
  EXPECT_EQ(packDsd(input, 2, DSD_U16_LE, 0),
            (Bytes{2, 1, 0x82, 0x81, 4, 3, 0x84, 0x83}));
  EXPECT_EQ(packDsd(input, 2, DSD_U16_BE, 0),
            (Bytes{1, 2, 0x81, 0x82, 3, 4, 0x83, 0x84}));
  EXPECT_EQ(packDsd(input, 2, DSD_U32_LE, 0),
            (Bytes{4, 3, 2, 1, 0x84, 0x83, 0x82, 0x81}));
  EXPECT_EQ(packDsd(input, 2, DSD_U32_BE, 0),
            (Bytes{1, 2, 3, 4, 0x81, 0x82, 0x83, 0x84}));
  EXPECT_EQ(packDsd(input, 2, DOP24_3LE, 0),
            (Bytes{2, 1, 5, 0x82, 0x81, 5, 4, 3, 0xfa, 0x84, 0x83, 0xfa}));
  EXPECT_EQ(packDsd(input, 2, DOP32_LE, 1),
            (Bytes{0, 2, 1, 0xfa, 0, 0x82, 0x81, 0xfa, 0, 4, 3, 5, 0, 0x84,
                   0x83, 5}));
}
TEST(DsdPacking, FinalFrameUsesDsdSilence) {
  EXPECT_EQ(packDsd(Bytes{1, 0x81}, 2, DOP24_LE, 0),
            (Bytes{0x69, 1, 5, 0, 0x69, 0x81, 5, 0}));
  EXPECT_THROW(packDsd(Bytes{1}, 2, DSD_U8, 0), std::invalid_argument);
}
std::string refusal(const OutputCapabilities &caps, const char *mode,
                    unsigned rate, unsigned channels) {
  try {
    chooseDsdOutput(caps, mode, rate, channels);
  } catch (const std::runtime_error &ex) {
    return ex.what();
  }
  return "accepted";
}
TEST(DsdCapabilities, ExactCombinationRequiredAndAutomaticNeverAssumesDop) {
  OutputCapabilities caps{
      DeviceAccess::Exclusive,
      "",
      {{176400, 2, 24, PCM24_LE}, {88200, 2, 1, DSD_U32_LE, 2822400}}};
  EXPECT_EQ(chooseDsdOutput(caps, "auto", 2822400, 2).sampleFormat, DSD_U32_LE);
  EXPECT_EQ(chooseDsdOutput(caps, "dop", 2822400, 2).sampleFormat, DOP24_LE);
  EXPECT_EQ(refusal(caps, "auto", 5644800, 2),
            "This output cannot play DSD128 stereo as native DSD");
  EXPECT_EQ(refusal(caps, "native", 2822400, 1),
            "This output cannot play DSD64 mono as native DSD");
  EXPECT_EQ(refusal(caps, "dop", 5644800, 2),
            "This output cannot play DSD128 stereo as DoP");
  EXPECT_EQ(refusal(caps, "native", 3072000, 2),
            "This output cannot play DSD64 stereo as native DSD");
  EXPECT_EQ(refusal(caps, "disabled", 2822400, 2), DSD_DISABLED_ERROR);
  caps.formats.pop_back();
  EXPECT_EQ(refusal(caps, "auto", 2822400, 2),
            "This output cannot play DSD64 stereo as native DSD");
  caps.access = DeviceAccess::Shared;
  EXPECT_EQ(refusal(caps, "dop", 2822400, 2), DSD_SHARED_OUTPUT_ERROR);
  EXPECT_EQ(refusal({DeviceAccess::Unknown, "No such device", {}}, "native",
                    2822400, 2),
            "Output device unavailable: No such device");
}
TEST(DsdCapabilities, NullIsNotHardwareDsd) {
  const auto caps = probeOutput("null");
  EXPECT_EQ(caps.access, DeviceAccess::Shared);
  for (const auto &format : caps.formats)
    EXPECT_FALSE(isDsd(format.sampleFormat));
  const auto missing = probeOutput("kalinka-device-that-does-not-exist");
  EXPECT_EQ(missing.access, DeviceAccess::Unknown);
  EXPECT_FALSE(missing.error.empty());
}
TEST(DsdDecoder, DsfBlocksAndBothBitOrders) {
  for (auto order : {1u, 8u}) {
    DsdStreamDecoder decoder(1, select(DSD_U8));
    decoder.connectTo(std::make_shared<Input>(dsf(order)));
    const auto result = drain(decoder);
    ASSERT_EQ(result.size(), 8192u);
    EXPECT_EQ(result[2], order == 1 ? 0x80 : 1);
    EXPECT_EQ(result[3], order == 1 ? 0x48 : 18);
    EXPECT_EQ(decoder.getState().streamInfo->durationMs(), 11u);
  }
}
TEST(DsdDecoder, DsfBlocksPastTheSampleCountAreNotPlayed) {
  DsdStreamDecoder exact(1, select(DSD_U8));
  exact.connectTo(std::make_shared<Input>(dsf()));
  DsdStreamDecoder padded(1, select(DSD_U8));
  padded.connectTo(std::make_shared<Input>(dsf(8, 2, 2822400, 1)));
  EXPECT_EQ(drain(padded), drain(exact));
  EXPECT_EQ(padded.getState().state, AudioGraphNodeState::FINISHED);
}
TEST(DsdDecoder, DffStreamsWithoutSeekingAndPacksDop) {
  DsdStreamDecoder decoder(1, select(DOP24_3LE));
  decoder.connectTo(std::make_shared<Input>(dff(), false));
  EXPECT_EQ(drain(decoder),
            (Bytes{2, 1, 5, 0x82, 0x81, 5, 4, 3, 0xfa, 0x84, 0x83, 0xfa}));
  EXPECT_EQ(decoder.seekTo(0), size_t(-1));
}
TEST(DsdDecoder, SeeksInsideDsfChannelBlocksAndRestartsAfterEof) {
  DsdStreamDecoder decoder(1, select(DOP24_LE));
  decoder.connectTo(std::make_shared<Input>(dsf()));
  ASSERT_EQ(drain(decoder).size(), 16384u);
  ASSERT_EQ(decoder.seekTo(10), 10u);
  const auto result = drain(decoder);
  ASSERT_EQ(result.size(), 16304u);
  EXPECT_EQ(result[0], 21);
  EXPECT_EQ(result[1], 20);
  EXPECT_EQ(result[4], 38);
  EXPECT_EQ(result[5], 37);
  EXPECT_EQ(decoder.streamReadPosition(), 2048);
}
TEST(DsdDecoder, FailedInputSeekIsReportedAsAFailedSeek) {
  class ProbeOnlyInput : public Input {
  public:
    using Input::Input;
    bool probed = false;
    size_t seekTo(size_t n) override {
      if (probed)
        return size_t(-1);
      probed = true;
      return Input::seekTo(n);
    }
  };
  DsdStreamDecoder decoder(1, select(DOP24_LE));
  decoder.connectTo(std::make_shared<ProbeOnlyInput>(dsf()));
  ASSERT_EQ(drain(decoder).size(), 16384u);
  EXPECT_EQ(decoder.seekTo(10), size_t(-1));
  EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::ERROR);
}
TEST(DsdDecoder, StartOffsetUsesTransportFrames) {
  DsdStreamDecoder decoder(1, select(DSD_U32_LE), 1);
  decoder.connectTo(std::make_shared<Input>(dsf()));
  const auto result = drain(decoder);
  ASSERT_EQ(result.size(), (1024u - 88) * 8);
  EXPECT_EQ(result[0], 99);
  EXPECT_EQ(result[3], 96);
}
TEST(DsdDecoder, MonoAndHigherRate) {
  DsdStreamDecoder decoder(1, select(DSD_U16_BE));
  decoder.connectTo(std::make_shared<Input>(dsf(8, 1, 11289600)));
  ASSERT_EQ(drain(decoder).size(), 4096u);
  EXPECT_EQ(decoder.getState().streamInfo->format.dsdSampleRate, 11289600u);
}
TEST(DsdDecoder, MalformedAndCompressedFilesFailWithoutPcmFallback) {
  auto truncated = dsf();
  truncated.resize(100);
  auto malformed = dsf();
  malformed[4] = 0xff;
  auto badChannels = dsf();
  badChannels[52] = 6;
  auto overcounted = dsf();
  overcounted[64] = 1;
  auto badSize = dff();
  badSize[4] = 0xff;
  for (auto bytes : {truncated, malformed, badChannels, overcounted, badSize,
                     dff(true), Bytes{1, 2, 3}}) {
    DsdStreamDecoder decoder(1, select(DSD_U8));
    decoder.connectTo(std::make_shared<Input>(bytes));
    EXPECT_TRUE(drain(decoder).empty());
    EXPECT_EQ(decoder.getState().state, AudioGraphNodeState::ERROR);
  }
}
TEST(DsdState, SourceAndCarrierRatesAreDistinct) {
  StreamInfo info{{176400, 2, 1, DOP24_LE, 2822400}, FRAMES, 176400};
  StreamState state{AudioGraphNodeState::STREAMING, 500, info};
  state.deviceInfo =
      DeviceInfo{{176400, 2, 24, DOP24_LE}, DeviceAccess::Exclusive};
  kalinka::renderer::v1::PlaybackStateChanged out;
  state_translator::fillPlaybackStateChanged(state, "track", 0, out);
  EXPECT_EQ(out.format().sample_rate_hz(), 2822400u);
  EXPECT_EQ(out.format().bits_per_sample(), 1u);
  EXPECT_EQ(out.device_info().format().sample_rate_hz(), 176400u);
  EXPECT_EQ(out.duration_ms(), 1000u);
}

TEST(DsdPacking, EmitterMarkersSurviveOddBuffersPauseSilenceAndSourceChange) {
  uint64_t frame = 0;
  auto first = packDsd(Bytes{1, 2}, 1, DOP24_LE, 0);
  stampDopMarkers(first, 1, DOP24_LE, frame);
  EXPECT_EQ(first[2], 5);
  Bytes paused(12, 0x69);
  stampDopMarkers(paused, 1, DOP24_LE, frame);
  EXPECT_EQ(paused, (Bytes{0x69, 0x69, 0xfa, 0, 0x69, 0x69, 5, 0, 0x69, 0x69,
                           0xfa, 0}));
  auto next = packDsd(Bytes{3, 4}, 1, DOP24_LE, 0);
  stampDopMarkers(next, 1, DOP24_LE, frame);
  EXPECT_EQ(next, (Bytes{4, 3, 5, 0}));
  EXPECT_EQ(frame, 5u);
}
TEST(DsdPacking, PcmOperationsRejectDsdAndLeaveBitsUntouched) {
  Bytes source{1, 2, 3, 4}, dest(16);
  EXPECT_THROW(convertSampleFormat(source.data(), DSD_U8, 4, dest.data(),
                                   PCM16_LE, dest.size()),
               std::invalid_argument);
  EXPECT_THROW(convertSampleFormat(source.data(), PCM16_LE, 2, dest.data(),
                                   DOP24_LE, dest.size()),
               std::invalid_argument);
  EXPECT_THROW(applyGainInPlace(source.data(), source.size(), DSD_U8, 0.5f),
               std::invalid_argument);
  EXPECT_THROW(applyGainInPlace(source.data(), source.size(), DOP24_LE, 0.0f),
               std::invalid_argument);
  EXPECT_EQ(source, (Bytes{1, 2, 3, 4}));
}
StreamState awaitError(AudioPlayer &player) {
  const auto deadline = std::chrono::steady_clock::now() + 3s;
  auto state = player.getState();
  while (state.state != AudioGraphNodeState::ERROR &&
         std::chrono::steady_clock::now() < deadline) {
    std::this_thread::sleep_for(1ms);
    state = player.getState();
  }
  return state;
}
TEST(DsdPlayback, OutputModeAndDeviceReachTheContainerReader) {
  const auto path = std::filesystem::temp_directory_path() /
                    ("kalinka-dsd-" + std::to_string(getpid()) + ".dsf");
  {
    std::ofstream file(path, std::ios::binary);
    const auto bytes = dsf();
    file.write(reinterpret_cast<const char *>(bytes.data()), bytes.size());
  }
  AudioPlayer player(
      {{"output.alsa.device", "null"}, {"output.dsd_mode", "dop"}});
  player.append(1, "file://" + path.string(), FormatDsd);
  const auto state = awaitError(player);
  ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
  ASSERT_TRUE(state.error);
  EXPECT_EQ(state.error->message, DSD_SHARED_OUTPUT_ERROR);
  std::filesystem::remove(path);
}
TEST(DsdPlayback, DisabledOutputRefusesBeforeReadingTheFile) {
  AudioPlayer player(
      {{"output.alsa.device", "null"}, {"output.dsd_mode", "disabled"}});
  player.append(1, "file:///nonexistent/track.dsf", FormatDsd);
  const auto state = awaitError(player);
  ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
  ASSERT_TRUE(state.error);
  EXPECT_EQ(state.error->message, DSD_DISABLED_ERROR);
}

TEST(DsdDecoder, StalledInputIsInterruptedBySeekAndDestruction) {
  class StalledInput : public Input {
  public:
    using Input::Input;
    std::promise<void> blocked;
    bool notified = false;
    size_t waitForData(std::stop_token token, size_t n) override {
      if (position < 92)
        return Input::waitForData(token, n);
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
  auto input = std::make_shared<StalledInput>(dsf());
  auto decoder = std::make_unique<DsdStreamDecoder>(1, select(DOP24_LE));
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

TEST(DsdCapabilities, DopNeedsTwentyFourSignificantBits) {
  OutputCapabilities caps{
      DeviceAccess::Exclusive, "test", {{176400, 2, 16, PCM32_LE}}};
  EXPECT_THROW(chooseDsdOutput(caps, "dop", 2822400, 2), std::runtime_error);
  caps.formats[0].bitsPerSample = 24;
  EXPECT_EQ(chooseDsdOutput(caps, "dop", 2822400, 2).sampleFormat, DOP32_LE);
}

TEST(DsdDecoder, HttpRangeSeekingAndSequentialPlaybackUseExistingInput) {
  const auto path = std::filesystem::temp_directory_path() /
                    ("kalinka-dsd-http-" + std::to_string(getpid()) + ".dsf");
  {
    std::ofstream file(path, std::ios::binary);
    const auto bytes = dsf();
    file.write(reinterpret_cast<const char *>(bytes.data()), bytes.size());
  }
  {
    LocalHttpServer server(path.string());
    for (const auto &route : {"/ranged", "/whole"}) {
      auto input = std::make_shared<AudioGraphHttpStream>(1, server.url(route),
                                                          32768, 4096);
      DsdStreamDecoder decoder(1, select(DOP24_LE));
      decoder.connectTo(input);
      EXPECT_EQ(drain(decoder).size(), 16384u);
      if (std::string(route) == "/ranged") {
        ASSERT_EQ(decoder.seekTo(10), 10u);
        auto result = drain(decoder);
        ASSERT_EQ(result.size(), 16304u);
        EXPECT_EQ(result[0], 21);
      }
    }
    auto missing = std::make_shared<AudioGraphHttpStream>(
        2, server.url("/missing"), 32768, 4096);
    DsdStreamDecoder decoder(2, select(DOP24_LE));
    decoder.connectTo(missing);
    EXPECT_TRUE(drain(decoder).empty());
    ASSERT_TRUE(decoder.getState().error);
    EXPECT_EQ(decoder.getState().error->source, StreamErrorSource::HTTP_STREAM);
  }
  std::filesystem::remove(path);
}

TEST(DsdPlayback, ActiveVolumeIsRefusedBeforeEmittingDsd) {
  for (auto format : {DSD_U8, DOP24_LE}) {
    auto decoder = std::make_shared<DsdStreamDecoder>(1, select(format));
    decoder->connectTo(std::make_shared<Input>(dsf()));
    AlsaAudioEmitter output(Config{{"output.alsa.device", "null"}});
    output.setDsdAllowed(false);
    output.connectTo(decoder);
    const auto deadline = std::chrono::steady_clock::now() + 2s;
    auto state = output.getState();
    while (state.state != AudioGraphNodeState::ERROR &&
           std::chrono::steady_clock::now() < deadline) {
      std::this_thread::sleep_for(1ms);
      state = output.getState();
    }
    ASSERT_EQ(state.state, AudioGraphNodeState::ERROR);
    ASSERT_TRUE(state.error);
    EXPECT_EQ(state.error->message,
              "DSD needs Volume control set to Fixed output");
  }
}
