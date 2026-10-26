#include <gtest/gtest.h>

#include "AlsaAudioEmitter.h"
#include "AlsaDeviceEnumeration.h"
#include "AlsaVolumeControl.h"
#include "AudioStreamSwitcher.h"
#include "DsdFormat.h"
#include "PlaybackPcm.h"
#include "TestHelpers.h"

#include <array>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <memory>
#include <unistd.h>

namespace {
using AlsaConfig = std::unique_ptr<snd_config_t, decltype(&snd_config_delete)>;

AlsaConfig configFrom(const std::string &text) {
  snd_config_t *config = nullptr;
  EXPECT_EQ(snd_config_load_string(&config, text.c_str(), text.size()), 0);
  return {config, snd_config_delete};
}

std::string hdmiDefinition(const std::string &pcm) {
  return "pcm.hdmi { @args [CARD] @args.CARD { type string } " + pcm + " }";
}

TEST(PlaybackPcm, HdmiFramingThroughHooksToHardwareIsDirect) {
  auto config = configFrom(hdmiDefinition(R"(
    type empty
    slave.pcm {
      type iec958
      slave.format IEC958_SUBFRAME_LE
      slave.pcm { type hooks slave.pcm { type hw card $CARD device 0 } }
    }
  )"));
  const auto route = inspectHdmiRoute(config.get(), "hdmi:CARD=vc4hdmi0");
  EXPECT_EQ(route.access, DeviceAccess::Exclusive);
  EXPECT_TRUE(route.iec958);
}

TEST(PlaybackPcm, HdmiHardwareWithHooksNeedsNoSoftwareFraming) {
  auto config = configFrom(
      hdmiDefinition("type hooks slave.pcm { type hw card $CARD device 3 }"));
  const auto route = inspectHdmiRoute(config.get(), "hdmi:CARD=PCH");
  EXPECT_EQ(route.access, DeviceAccess::Exclusive);
  EXPECT_FALSE(route.iec958);
}

TEST(PlaybackPcm, HdmiNameDoesNotMakeAnAlteringPluginDirect) {
  for (const auto *type :
       {"plug", "rate", "dmix", "softvol", "route", "unknown"}) {
    SCOPED_TRACE(type);
    const std::string altering =
        std::string("type ") + type + " slave.pcm { type hw card $CARD }";
    for (const auto &pcm :
         {altering, "type iec958 slave.format IEC958_SUBFRAME_LE "
                    "slave.pcm { " +
                        altering + " }"}) {
      auto config = configFrom(hdmiDefinition(pcm));
      EXPECT_EQ(inspectHdmiRoute(config.get(), "hdmi:CARD=0").access,
                DeviceAccess::Shared);
    }
  }
}

TEST(PlaybackPcm, NamedSlavesAreCheckedAndCyclesAreRejected) {
  auto config = configFrom(hdmiDefinition("type hooks slave.pcm card") +
                           " pcm.card { type hw card 0 } ");
  EXPECT_EQ(inspectHdmiRoute(config.get(), "hdmi:CARD=0").access,
            DeviceAccess::Exclusive);
  config = configFrom(hdmiDefinition("type hooks slave.pcm loop") +
                      " pcm.loop { type empty slave.pcm loop } ");
  EXPECT_EQ(inspectHdmiRoute(config.get(), "hdmi:CARD=0").access,
            DeviceAccess::Shared);
}

TEST(PlaybackPcm, APluginIsShared) {
  const auto opened = openPlayback("null", 0);
  ASSERT_TRUE(opened);
  EXPECT_EQ(opened->access, DeviceAccess::Shared);
  EXPECT_FALSE(opened->iec958);
  snd_pcm_close(opened->handle);
}

TEST(PlaybackPcm, AMissingDeviceGivesAlsasError) {
  const auto opened = openPlayback("kalinka-device-that-does-not-exist", 0);
  ASSERT_FALSE(opened);
  EXPECT_LT(opened.error(), 0);
}

TEST(PlaybackPcm, AnHdmiPortWithNothingToPlayToSaysSo) {
  for (const auto *device :
       {"hdmi:CARD=vc4hdmi1,DEV=0,AES3=0x01", "default:CARD=vc4hdmi1",
        "plughw:CARD=vc4hdmi0,DEV=0", "HDMI:CARD=PCH,DEV=0"}) {
    SCOPED_TRACE(device);
    EXPECT_EQ(openFailure(device, -524),
              "no screen or receiver that takes HDMI audio is connected to "
              "this port and switched on");
  }
}

TEST(PlaybackPcm, OtherOpenFailuresKeepAlsasWords) {
  EXPECT_EQ(openFailure("hw:CARD=AUDIO,DEV=0", -524),
            std::string(snd_strerror(-524)));
  EXPECT_EQ(openFailure("hdmi:CARD=vc4hdmi0,DEV=0", -ENODEV),
            std::string(snd_strerror(-ENODEV)));
}

class PcmFrames : public AudioGraphOutputNode {
public:
  PcmFrames(StreamId id, unsigned rate, unsigned bits) : AudioGraphNode(id) {
    setState({AudioGraphNodeState::STREAMING, 0,
              StreamInfo{.format = {rate, 2, bits, PCM32_LE},
                         .streamType = StreamType::FRAMES,
                         .streamSize = 4096}});
  }
  size_t read(void *data, size_t size) override {
    const size_t bytes = std::min(size, remaining);
    std::memset(data, 0, bytes);
    remaining -= bytes;
    if (!remaining)
      setState(StreamState(AudioGraphNodeState::FINISHED));
    return bytes;
  }
  size_t waitForData(std::stop_token = {}, size_t size = 1) override {
    return std::min(size, remaining);
  }
  size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds,
                        size_t size) override {
    return waitForData(token, size);
  }

private:
  size_t remaining = 4096 * 2 * sizeof(uint32_t);
};

class Iec958PlaybackTest : public ::testing::Test {
protected:
  void SetUp() override {
    char pattern[] = "/tmp/kalinka-iec958-XXXXXX";
    const char *path = mkdtemp(pattern);
    ASSERT_NE(path, nullptr);
    directory = path;
    capture = directory / "frames.raw";
    const std::string text =
        "pcm.framed { type iec958 slave.format IEC958_SUBFRAME_LE "
        "slave.pcm { type file slave.pcm { type null } file \"" +
        capture.string() +
        "\" format raw } "
        "status [ 0x04 0x82 0x00 0x01 ] hdmi_mode true }" +
        hdmiDefinition("type hooks slave.pcm { type hw card $CARD device 0 }");
    const auto file = directory / "alsa.conf";
    std::ofstream(file) << text;
    if (const char *previous = getenv("ALSA_CONFIG_PATH"))
      previousConfig = previous;
    setenv("ALSA_CONFIG_PATH", file.c_str(), 1);
    snd_config_update_free_global();
  }
  void TearDown() override {
    snd_config_update_free_global();
    if (previousConfig)
      setenv("ALSA_CONFIG_PATH", previousConfig->c_str(), 1);
    else
      unsetenv("ALSA_CONFIG_PATH");
    std::filesystem::remove_all(directory);
  }
  std::vector<uint32_t> captured() {
    std::ifstream file(capture, std::ios::binary | std::ios::ate);
    if (!file)
      return {};
    const auto bytes = file.tellg();
    std::vector<uint32_t> samples(static_cast<size_t>(bytes) / 4);
    file.seekg(0);
    file.read(reinterpret_cast<char *>(samples.data()), bytes);
    return samples;
  }
  Config emitterConfig() {
    return {{"output.alsa.device", "framed"},
            {"output.alsa.buffer_size", "384"},
            {"output.alsa.period_size", "192"}};
  }
  std::filesystem::path directory, capture;
  std::optional<std::string> previousConfig;
};

TEST_F(Iec958PlaybackTest, S32ContainersOnlyCarry24SignificantBits) {
  const auto opened = openPlayback("framed", 0);
  ASSERT_TRUE(opened);
  std::unique_ptr<snd_pcm_t, decltype(&snd_pcm_close)> pcm(opened->handle,
                                                           snd_pcm_close);
  snd_pcm_hw_params_t *params;
  snd_pcm_hw_params_alloca(&params);
  ASSERT_GE(snd_pcm_hw_params_any(pcm.get(), params), 0);
  ASSERT_GE(
      snd_pcm_hw_params_set_format(pcm.get(), params, SND_PCM_FORMAT_S32_LE),
      0);
  EXPECT_EQ(opened->significantBits(params), 24);
  const auto caps = probeOutput("framed");
  ASSERT_FALSE(caps.formats.empty());
  for (const auto &format : caps.formats)
    EXPECT_LE(format.bitsPerSample, 24u);
}

TEST_F(Iec958PlaybackTest, DirectHdmiSkipsControlsLeftBehindBySoftVolume) {
  EXPECT_TRUE(reachesTheCardItself("hdmi:CARD=0"));
  EXPECT_FALSE(reachesTheCardItself("framed"));
}

TEST_F(Iec958PlaybackTest, CustomHdmiWithoutAes3KeepsUsablePlaybackNames) {
  std::ofstream(directory / "alsa.conf") << R"(
    pcm.hdmi {
      @args [CARD DEV]
      @args.CARD { type string }
      @args.DEV { type integer }
      type null
    }
  )";
  snd_config_update_free_global();
  const std::string name = "hdmi:CARD=vc4hdmi0,DEV=0";
  EXPECT_EQ(playbackDeviceName(name), name);
  const AlsaCard card{
      "vc4hdmi0", "vc4-hdmi-0", {{0, "MAI PCM", false}}, "vc4-hdmi", 0};
  const auto devices =
      withCardPcms({describeAlsaPcm(name, "vc4-hdmi-0")}, {card});
  ASSERT_EQ(devices.size(), 2u);
  EXPECT_EQ(devices.front().name, name);
  EXPECT_EQ(playbackDeviceName("hw:CARD=vc4hdmi0,DEV=0", {card}), name);
  const auto opened = openPlayback(devices.front().name, 0);
  ASSERT_TRUE(opened) << snd_strerror(opened.error());
  snd_pcm_close(opened->handle);
}

TEST_F(Iec958PlaybackTest, HdmiAliasesDeclareWhetherRateStatusCanBeSet) {
  std::ofstream(directory / "alsa.conf") << R"(
    pcm.hdmi cards.pcm.hdmi
    cards.pcm.hdmi {
      @args [CARD DEV AES3]
      @args.CARD { type string }
      @args.DEV { type integer }
      @args.AES3 { type integer default 2 }
      type null
    }
  )";
  snd_config_update_free_global();
  const std::string name = "hdmi:CARD=Custom,DEV=0";
  EXPECT_EQ(playbackDeviceName(name), name + ",AES3=0x01");
  EXPECT_EQ(playbackDeviceName(name + ",AES3=0x02"), name + ",AES3=0x02");
  const auto opened = openPlayback(playbackDeviceName(name), 0);
  ASSERT_TRUE(opened) << snd_strerror(opened.error());
  snd_pcm_close(opened->handle);
}

TEST_F(Iec958PlaybackTest, True32BitPcmIsRejectedBeforeSamplesAreWritten) {
  auto input = std::make_shared<PcmFrames>(1, 44100, 32);
  AlsaAudioEmitter emitter(emitterConfig());
  emitter.connectTo(input);
  const auto state = waitForStatus(emitter, AudioGraphNodeState::ERROR,
                                   std::chrono::seconds(2));
  EXPECT_EQ(state.state, AudioGraphNodeState::ERROR);
  ASSERT_TRUE(state.error);
  EXPECT_NE(state.error->message.find("preserve source PCM precision"),
            std::string::npos);
  emitter.disconnect(input);
  EXPECT_TRUE(captured().empty());
}

TEST_F(Iec958PlaybackTest, FormatChangesRefreshTheIec958ChannelStatus) {
  auto input = std::make_shared<AudioStreamSwitcher>();
  input->connectTo(std::make_shared<PcmFrames>(1, 44100, 16));
  input->connectTo(std::make_shared<PcmFrames>(2, 176400, 24));
  AlsaAudioEmitter emitter(emitterConfig());
  StateMonitor monitor(&emitter);
  emitter.connectTo(input);
  const auto state = waitForStatus(emitter, AudioGraphNodeState::FINISHED,
                                   std::chrono::seconds(3));
  EXPECT_EQ(state.state, AudioGraphNodeState::FINISHED);
  emitter.disconnect(input);
  expectPlayedInTurn(drainStates(monitor), {1, 2});
  const auto samples = captured();
  ASSERT_GE(samples.size(), 384u);
  auto status = [&](unsigned byte) {
    unsigned value = 0;
    for (unsigned bit = 0; bit < 8; ++bit)
      value |= ((samples[2 * (byte * 8 + bit)] >> 30) & 1) << bit;
    return value;
  };
  EXPECT_EQ(status(3) & IEC958_AES3_CON_FS, IEC958_AES3_CON_FS_176400);
  EXPECT_EQ(status(4) &
                (IEC958_AES4_CON_WORDLEN | IEC958_AES4_CON_MAX_WORDLEN_24),
            IEC958_AES4_CON_WORDLEN_24_20 | IEC958_AES4_CON_MAX_WORDLEN_24);
}

TEST_F(Iec958PlaybackTest,
       EveryDopContainerPreservesPayloadAndMarkersThroughFraming) {
  for (auto format : {DOP24_LE, DOP24_3LE, DOP32_LE}) {
    SCOPED_TRACE(sampleFormatToString(format));
    const auto opened = openPlayback("framed", 0);
    ASSERT_TRUE(opened);
    std::unique_ptr<snd_pcm_t, decltype(&snd_pcm_close)> pcm(opened->handle,
                                                             snd_pcm_close);
    ASSERT_GE(snd_pcm_set_params(pcm.get(), alsaFormat(format),
                                 SND_PCM_ACCESS_RW_INTERLEAVED, 2, 176400, 0,
                                 10000),
              0);
    std::vector<uint8_t> input(192 * 2 * 2);
    for (size_t i = 0; i < input.size(); ++i)
      input[i] = (i * 17 + 43) & 255;
    const auto packed = packDsd(input, 2, format, 0);
    ASSERT_EQ(snd_pcm_writei(pcm.get(), packed.data(), 192), 192);
    ASSERT_GE(snd_pcm_drain(pcm.get()), 0);
    pcm.reset();
    const auto samples = captured();
    ASSERT_EQ(samples.size(), 384u);
    for (size_t i = 0; i < samples.size(); ++i) {
      const unsigned marker = ((i / 2) & 1) ? 0xfa : 0x05;
      const size_t first = (i / 2) * 4 + i % 2;
      const unsigned expected =
          (marker << 16) | (input[first] << 8) | input[first + 2];
      EXPECT_EQ((samples[i] >> 4) & 0xffffff, expected) << "sample " << i;
    }
  }
}
} // namespace
