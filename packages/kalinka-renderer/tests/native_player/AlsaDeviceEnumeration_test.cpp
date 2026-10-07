#include <gtest/gtest.h>

#include "AlsaDeviceEnumeration.h"

namespace {

const char *kHwDesc =
    "bcm2835 Headphones, bcm2835 Headphones\n"
    "Direct hardware device without any conversions";
const char *kHifiberryDesc =
    "snd_rpi_hifiberry_digi, HiFiBerry Digi+ Pro HiFi wm8804-spdif-0\n"
    "Hardware device with all software conversions";
const char *kHdmiDesc = "vc4-hdmi-1, MAI PCM i2s-hifi-0\nDefault Audio Device";

TEST(AlsaDeviceNaming, TheChipIdInFrontOfAConnectorGoesAway) {
  EXPECT_EQ(describeAlsaPcm("hw:CARD=Headphones,DEV=0", kHwDesc).label,
            "Headphones (direct)");
}

TEST(AlsaDeviceNaming, ADriverModuleIdYieldsToTheReadablePcmName) {
  const AlsaPcmDevice device =
      describeAlsaPcm("plughw:CARD=sndrpihifiberry,DEV=0", kHifiberryDesc);
  EXPECT_EQ(device.label, "HiFiBerry Digi+ Pro (converted)");
}

TEST(AlsaDeviceNaming, HdmiPortsAreCountedTheWayTheBoardIsLabelled) {
  EXPECT_EQ(describeAlsaPcm("default:CARD=vc4hdmi1", kHdmiDesc).label,
            "HDMI 2 (shared)");
}

TEST(AlsaDeviceNaming, ASecondSubdeviceStaysDistinguishable) {
  EXPECT_EQ(describeAlsaPcm("hw:CARD=USB,DEV=1",
                            "Topping D10, USB Audio #1\nDirect hardware device "
                            "without any conversions")
                .label,
            "Topping D10 #1 (direct)");
}

TEST(AlsaDeviceNaming, TheAccessModeDecidesWhatTheDescriptionSays) {
  EXPECT_NE(describeAlsaPcm("hw:CARD=Headphones,DEV=0", kHwDesc)
                .description.find("unchanged"),
            std::string::npos);
  EXPECT_NE(describeAlsaPcm("plughw:CARD=sndrpihifiberry,DEV=0", kHifiberryDesc)
                .description.find("software conversion"),
            std::string::npos);
}

TEST(AlsaDeviceNaming, TheDefaultAndTheNullSinkReadAsPlainEnglish) {
  EXPECT_EQ(describeAlsaPcm("default", "").label, "System default");
  EXPECT_EQ(describeAlsaPcm("null", "Discard all samples (playback)").label,
            "No output");
}

// "Default ALSA Output (currently PipeWire Media Server)" and friends.
TEST(AlsaDeviceNaming, AParentheticalAsideMovesToTheDescription) {
  const AlsaPcmDevice device = describeAlsaPcm(
      "default", "Default ALSA Output (currently PipeWire Media Server)");
  EXPECT_EQ(device.label, "System default");
  EXPECT_NE(device.description.find("Currently PipeWire Media Server."),
            std::string::npos);
}

TEST(AlsaDeviceNaming, AnAsideOnAnUnknownDeviceStillLeavesTheNameAlone) {
  const AlsaPcmDevice device =
      describeAlsaPcm("somedev", "Some Device (currently idle)\nA device");
  EXPECT_EQ(device.label, "Some Device");
  EXPECT_EQ(device.description, "A device. Currently idle.");
}

// No hint at all: a device configured before the card was unplugged.
TEST(AlsaDeviceNaming, ANameWithoutAHintStillGetsItsMode) {
  const AlsaPcmDevice device = describeAlsaPcm("hw:CARD=Gone,DEV=0", "");
  EXPECT_EQ(device.label, "hw:CARD=Gone,DEV=0 (direct)");
}

// What Fedora's ALSA hints with PipeWire installed: no hw: or plughw: at all.
std::vector<AlsaPcmDevice> hintsWithoutCards() {
  return {describeAlsaPcm("null", ""), describeAlsaPcm("pipewire", ""),
          describeAlsaPcm("default", ""),
          describeAlsaPcm("sysdefault:CARD=AUDIO",
                          "SMSL USB AUDIO, USB Audio\nDefault Audio Device")};
}

const AlsaCard kSmsl{"AUDIO", "SMSL USB AUDIO", {{0, "USB Audio", false}}};

std::vector<std::string> names(const std::vector<AlsaPcmDevice> &devices) {
  std::vector<std::string> out;
  for (const AlsaPcmDevice &device : devices) {
    out.push_back(device.name);
  }
  return out;
}

TEST(AlsaCardPcms, ACardAlsaDidNotHintIsStillOfferedDirectAndConverted) {
  const std::vector<AlsaPcmDevice> devices =
      withCardPcms(hintsWithoutCards(), {kSmsl});
  ASSERT_EQ(devices.size(), 6u);
  EXPECT_EQ(devices[4].name, "hw:CARD=AUDIO,DEV=0");
  EXPECT_EQ(devices[4].label, "SMSL USB AUDIO (direct)");
  EXPECT_EQ(devices[4].ioid, "Output");
  EXPECT_EQ(devices[5].name, "plughw:CARD=AUDIO,DEV=0");
  EXPECT_EQ(devices[5].label, "SMSL USB AUDIO (converted)");
}

TEST(AlsaCardPcms, AHintedPcmKeepsItsHintAndIsNotRepeated) {
  std::vector<AlsaPcmDevice> hinted = hintsWithoutCards();
  AlsaPcmDevice hint = describeAlsaPcm(
      "hw:CARD=AUDIO,DEV=0",
      "SMSL USB AUDIO, USB Audio\nDirect hardware device without any "
      "conversions");
  hint.ioid = "Output";
  hinted.push_back(hint);

  const std::vector<AlsaPcmDevice> devices = withCardPcms(hinted, {kSmsl});
  EXPECT_EQ(names(devices),
            (std::vector<std::string>{"null", "pipewire", "default",
                                      "sysdefault:CARD=AUDIO",
                                      "hw:CARD=AUDIO,DEV=0",
                                      "plughw:CARD=AUDIO,DEV=0"}));
}

TEST(AlsaCardPcms, EachCardListsItsDirectPcmsBeforeItsConvertedOnes) {
  const AlsaCard laptop{"sofhdadsp", "sof-hda-dsp",
                        {{0, "", true}, {3, "HDMI 1", false}}};
  const std::vector<AlsaPcmDevice> devices =
      withCardPcms({}, {laptop, kSmsl});
  EXPECT_EQ(names(devices),
            (std::vector<std::string>{
                "hw:CARD=sofhdadsp,DEV=0", "hw:CARD=sofhdadsp,DEV=3",
                "plughw:CARD=sofhdadsp,DEV=0", "plughw:CARD=sofhdadsp,DEV=3",
                "hw:CARD=AUDIO,DEV=0", "plughw:CARD=AUDIO,DEV=0"}));
}

TEST(AlsaCardPcms, APcmThatAlsoRecordsIsMarkedForBothDirections) {
  const AlsaCard card{"PCH", "HDA Intel PCH", {{0, "ALC3246 Analog", true}}};
  for (const AlsaPcmDevice &device : withCardPcms({}, {card})) {
    EXPECT_EQ(device.ioid, "") << device.name;
  }
}

}  // namespace
