#include "AudioGraphHttpStream.h"
#include "AudioPlayer.h"
#include "ContainerTestSupport.h"
#include "LocalHttpServer.h"
#include "TestHelpers.h"
#include "WavContainer.h"
#include "WavTestData.h"

#include <unistd.h>

namespace {
using container_test::BytesInput;
using container_test::drain;
using wav_test::Bytes;
using namespace std::chrono_literals;
Bytes expected(unsigned frames = 9600, unsigned channels = 2,
               unsigned bits = 24, bool floating = false) {
  auto raw = wav_test::samples(frames, channels, bits, bits, floating);
  if (bits != 24)
    return raw;
  Bytes result;
  for (size_t i = 0; i < raw.size(); i += 3) {
    result.insert(result.end(), raw.begin() + i, raw.begin() + i + 3);
    result.push_back(0);
  }
  return result;
}
std::unique_ptr<ContainerFormat> wav() {
  return std::make_unique<WavContainer>();
}
} // namespace

TEST(WavDecoder, PreservesPcmSamplesRatesAndChannels) {
  for (auto bits : {16u, 24u, 32u})
    for (auto rate : {44100u, 96000u, 192000u})
      for (auto channels : {1u, 2u}) {
        SCOPED_TRACE(std::to_string(bits) + "/" + std::to_string(rate) + "/" +
                     std::to_string(channels));
        ContainerStreamDecoder decoder(1, wav(), 1001);
        decoder.connectTo(std::make_shared<BytesInput>(
            wav_test::file(bits, rate, channels, 9001)));
        EXPECT_EQ(drain(decoder), expected(9001, channels, bits));
        const auto state = decoder.getState();
        ASSERT_EQ(state.state, AudioGraphNodeState::FINISHED);
        ASSERT_TRUE(state.streamInfo);
        EXPECT_EQ(state.streamInfo->format.sampleRate, rate);
        EXPECT_EQ(state.streamInfo->format.bitsPerSample, bits);
        EXPECT_EQ(state.streamInfo->format.channels, channels);
        EXPECT_EQ(state.streamInfo->streamSize, 9001u);
        EXPECT_EQ(decoder.streamReadPosition(), 9001);
      }
}

TEST(WavDecoder, ExtensiblePcmPreservesValidBitsInWiderContainers) {
  for (auto bits : {16u, 24u, 32u})
    for (auto width : {bits, 32u})
      for (auto channels : {1u, 2u}) {
        ContainerStreamDecoder decoder(1, wav(), 1000);
        decoder.connectTo(std::make_shared<BytesInput>(
            wav_test::file(bits, 192000, channels, 9600, true, width)));
        EXPECT_EQ(drain(decoder), expected(9600, channels, bits));
      }
}

TEST(WavDecoder, FloatSamplesKeepTheirOriginalBitsIncludingTinyValues) {
  for (auto bits : {32u, 64u})
    for (bool extensible : {false, true})
      for (auto channels : {1u, 2u}) {
        ContainerStreamDecoder decoder(1, wav(), 1001);
        decoder.connectTo(std::make_shared<BytesInput>(wav_test::file(
            bits, 192000, channels, 9600, extensible, bits, true)));
        EXPECT_EQ(drain(decoder), expected(9600, channels, bits, true));
        ASSERT_TRUE(decoder.getState().streamInfo);
        EXPECT_EQ(decoder.getState().streamInfo->format.sampleFormat,
                  bits == 32 ? PCM_FLOAT32_LE : PCM_FLOAT64_LE);
      }
}

TEST(WavDecoder, StartOffsetAndRepeatedAbsoluteSeeks) {
  ContainerStreamDecoder decoder(1, wav(), 1024, 10);
  decoder.connectTo(std::make_shared<BytesInput>(wav_test::file()));
  const auto all = expected();
  EXPECT_EQ(drain(decoder), Bytes(all.begin() + 960 * 8, all.end()));
  for (size_t frame : {100u, 8000u, 10u, 9600u, 10000u, 0u}) {
    const auto target = std::min(frame, size_t(9600));
    ASSERT_EQ(decoder.seekTo(frame), target);
    EXPECT_EQ(drain(decoder), Bytes(all.begin() + target * 8, all.end()));
    EXPECT_EQ(decoder.streamReadPosition(), 9600);
  }
}

TEST(WavDecoder, RejectsMalformedAndUnsupportedFiles) {
  std::vector<Bytes> invalid;
  auto change = [&](size_t offset, uint8_t value, bool extensible = false) {
    auto bytes = wav_test::file(24, 96000, 2, 9600, extensible);
    bytes[offset] = value;
    invalid.push_back(std::move(bytes));
  };
  change(0, 'X'); // Container signature.
  change(8, 'X'); // Form type.
  change(4, 0);
  change(5, 0);         // RIFF boundary inside the data.
  change(16, 15);       // Short format.
  change(20, 6);        // A-law compression.
  change(22, 6);        // Multichannel.
  change(24, 1);        // Inconsistent sample rate and byte rate.
  change(32, 5);        // Block alignment.
  change(34, 20);       // Unsupported integer width.
  change(52, 0xff);     // Data chunk size extends beyond RIFF.
  change(52, 1);        // Data chunk contains a partial frame.
  change(36, 21, true); // Extensible cbSize too small.
  change(38, 25, true); // Unsupported precision.
  change(40, 4, true);  // Stereo data with mono channel mask.
  change(44, 6, true);  // Unsupported extensible codec.
  auto truncated = wav_test::file();
  truncated.resize(100);
  invalid.push_back(truncated);
  invalid.push_back(Bytes{'R', 'I', 'F', 'F'});
  for (const auto &bytes : invalid) {
    SCOPED_TRACE(&bytes - invalid.data());
    for (bool knownLength : {false, true}) {
      ContainerStreamDecoder decoder(1, wav(), 1024);
      decoder.connectTo(std::make_shared<BytesInput>(bytes, true, knownLength));
      drain(decoder);
      ASSERT_EQ(decoder.getState().state, AudioGraphNodeState::ERROR);
      ASSERT_TRUE(decoder.getState().error);
      EXPECT_EQ(decoder.getState().error->source, StreamErrorSource::DECODER);
      EXPECT_EQ(decoder.seekTo(0), size_t(-1));
    }
  }
}

TEST(WavDecoder, RefusesMetadataThatPushesTheAudioPastTheLimit) {
  auto bytes = wav_test::file();
  // The JUNK chunk before the audio claims 64 MB more, and the RIFF size
  // agrees: these are the top bytes of the two sizes.
  bytes[7] = bytes[43] = 0x04;
  ContainerStreamDecoder decoder(1, wav(), 1024);
  decoder.connectTo(std::make_shared<BytesInput>(bytes, true, false));
  EXPECT_TRUE(drain(decoder).empty());
  ASSERT_TRUE(decoder.getState().error);
  EXPECT_EQ(decoder.getState().error->message, "WAV header is too large");
}

TEST(WavDecoder, EmptyAudioFinishes) {
  ContainerStreamDecoder empty(1, wav(), 1024);
  empty.connectTo(
      std::make_shared<BytesInput>(wav_test::file(24, 96000, 2, 0)));
  EXPECT_TRUE(drain(empty).empty());
  EXPECT_EQ(empty.getState().state, AudioGraphNodeState::FINISHED);
}

TEST(WavDecoder, HttpRangeSeekingSequentialPlaybackAndInputErrors) {
  const auto path = std::filesystem::temp_directory_path() /
                    ("kalinka-wav-http-" + std::to_string(getpid()) + ".wav");
  wav_test::write(path, wav_test::file());
  {
    LocalHttpServer server(path.string());
    for (const auto &route : {"/ranged", "/whole"}) {
      auto input = std::make_shared<AudioGraphHttpStream>(1, server.url(route),
                                                          32768, 4096);
      ContainerStreamDecoder decoder(1, wav(), 1024);
      decoder.connectTo(input);
      EXPECT_EQ(drain(decoder), expected());
      if (std::string(route) == "/ranged") {
        ASSERT_EQ(decoder.seekTo(8000), 8000u);
        auto all = expected();
        EXPECT_EQ(drain(decoder), Bytes(all.begin() + 8000 * 8, all.end()));
      }
    }
    ContainerStreamDecoder missing(2, wav(), 1024);
    missing.connectTo(std::make_shared<AudioGraphHttpStream>(
        2, server.url("/missing"), 32768, 4096));
    EXPECT_TRUE(drain(missing).empty());
    ASSERT_TRUE(missing.getState().error);
    EXPECT_EQ(missing.getState().error->source, StreamErrorSource::HTTP_STREAM);
  }
  std::filesystem::remove(path);
}

TEST(WavPlayback, HiResFormatReachesAlsaWithFixedAndSoftwareVolume) {
  const auto path = std::filesystem::temp_directory_path() /
                    ("kalinka-wav-output-" + std::to_string(getpid()) + ".wav");
  for (auto rate : {96000u, 192000u}) {
    for (auto [bits, floating] :
         {std::pair{24u, false}, {32u, false}, {32u, true}, {64u, true}}) {
      for (auto channels : {1u, 2u}) {
        wav_test::write(path, wav_test::file(bits, rate, channels, rate / 4,
                                             false, bits, floating));
        for (const auto &mode : {"fixed", "software"}) {
          AudioPlayer player(Config{{"output.alsa.device", "null"}});
          ASSERT_TRUE(player.configureVolume(mode, ""));
          player.setVolume(50);
          auto monitor = player.monitor();
          player.append(1, "file://" + path.string(), FormatWav);
          const auto deadline = std::chrono::steady_clock::now() + 3s;
          bool finished = false, outputSeen = false;
          while (!finished && std::chrono::steady_clock::now() < deadline) {
            for (const auto &state : drainStates(*monitor)) {
              ASSERT_NE(state.state, AudioGraphNodeState::ERROR)
                  << state.toString();
              if (state.state == AudioGraphNodeState::STREAMING &&
                  state.deviceInfo) {
                outputSeen = true;
                EXPECT_EQ(state.deviceInfo->format.sampleRate, rate);
                EXPECT_EQ(state.deviceInfo->format.bitsPerSample, bits);
                EXPECT_EQ(state.deviceInfo->format.channels, 2u);
              }
              finished |= state.state == AudioGraphNodeState::FINISHED;
              if (state.state == AudioGraphNodeState::FINISHED)
                EXPECT_EQ(state.position, 250);
            }
            std::this_thread::sleep_for(1ms);
          }
          EXPECT_TRUE(finished);
          EXPECT_TRUE(outputSeen);
        }
      }
    }
  }
  std::filesystem::remove(path);
}

TEST(WavPlayback, AlsaReceivesExactSamplesAtUnityIncludingMonoExpansion) {
  const auto base = std::filesystem::temp_directory_path() /
                    ("kalinka-wav-capture-" + std::to_string(getpid()));
  const auto source = base.string() + ".wav";
  const auto capture = base.string() + ".raw";
  for (auto [bits, floating] :
       {std::pair{24u, false}, {32u, false}, {32u, true}, {64u, true}}) {
    for (auto channels : {1u, 2u}) {
      SCOPED_TRACE(std::to_string(bits) + "/" + std::to_string(floating) + "/" +
                   std::to_string(channels));
      wav_test::write(source, wav_test::file(bits, 96000, channels, 24000,
                                             false, bits, floating));
      {
        AudioPlayer player(Config{{"output.alsa.device", "file:" + capture}});
        player.append(1, "file://" + source, FormatWav);
        const auto deadline = std::chrono::steady_clock::now() + 3s;
        while (player.getState().state != AudioGraphNodeState::FINISHED &&
               player.getState().state != AudioGraphNodeState::ERROR &&
               std::chrono::steady_clock::now() < deadline)
          std::this_thread::sleep_for(1ms);
        ASSERT_EQ(player.getState().state, AudioGraphNodeState::FINISHED)
            << player.getState().toString();
        player.stop(); // Close ALSA so the file plugin flushes its final bytes.
      }
      auto wanted = expected(24000, channels, bits, floating);
      if (channels == 1) {
        Bytes stereo;
        const auto width = bits == 24 ? 4 : bits / 8;
        for (size_t i = 0; i < wanted.size(); i += width) {
          stereo.insert(stereo.end(), wanted.begin() + i,
                        wanted.begin() + i + width);
          stereo.insert(stereo.end(), wanted.begin() + i,
                        wanted.begin() + i + width);
        }
        wanted = std::move(stereo);
      }
      std::ifstream file(capture, std::ios::binary);
      const Bytes actual{std::istreambuf_iterator<char>(file), {}};
      EXPECT_EQ(actual, wanted);
      std::filesystem::remove(capture);
    }
  }
  std::filesystem::remove(source);
}
