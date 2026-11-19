#include <gtest/gtest.h>

#include <optional>
#include <string>

#include "player/StateTranslator.h"

namespace renderer_pb = kalinka::renderer::v1;
using state_translator::fillAudioFormat;
using state_translator::fillPlaybackStateChanged;
using state_translator::fillVolume;
using state_translator::toProto;

TEST(StreamClock, PollingBetweenStateChangesAdvancesOnlyWhilePlaying) {
  StreamState state(AudioGraphNodeState::STREAMING, 68000);
  state.timestamp = 1000000000;
  EXPECT_EQ(state.positionAt(2000000000), 69000);
  EXPECT_EQ(state.positionAt(3000000000), 70000);
  EXPECT_EQ(state.positionAt(500000000), 68000);
  EXPECT_EQ(state.position, 68000);
  for (auto mode : {AudioGraphNodeState::PAUSED, AudioGraphNodeState::PREPARING,
                    AudioGraphNodeState::STOPPED, AudioGraphNodeState::FINISHED,
                    AudioGraphNodeState::ERROR}) {
    state.state = mode;
    EXPECT_EQ(state.positionAt(3000000000), 68000);
  }
}

TEST(StateTranslator, EveryGraphStateHasExactlyOneProtoState) {
  EXPECT_EQ(toProto(AudioGraphNodeState::STOPPED),
            renderer_pb::PLAYBACK_STATE_STOPPED);
  EXPECT_EQ(toProto(AudioGraphNodeState::PREPARING),
            renderer_pb::PLAYBACK_STATE_PREPARING);
  EXPECT_EQ(toProto(AudioGraphNodeState::STREAMING),
            renderer_pb::PLAYBACK_STATE_PLAYING);
  EXPECT_EQ(toProto(AudioGraphNodeState::PAUSED),
            renderer_pb::PLAYBACK_STATE_PAUSED);
  EXPECT_EQ(toProto(AudioGraphNodeState::FINISHED),
            renderer_pb::PLAYBACK_STATE_FINISHED);
  EXPECT_EQ(toProto(AudioGraphNodeState::ERROR),
            renderer_pb::PLAYBACK_STATE_ERROR);
  // A transition, not a state: reported as SourceChanged by the caller.
  EXPECT_EQ(toProto(AudioGraphNodeState::SOURCE_CHANGED),
            renderer_pb::PLAYBACK_STATE_UNSPECIFIED);
}

TEST(StateTranslator, EveryErrorSourceMaps) {
  EXPECT_EQ(toProto(StreamErrorSource::NONE), renderer_pb::ERROR_SOURCE_NONE);
  EXPECT_EQ(toProto(StreamErrorSource::HTTP_STREAM),
            renderer_pb::ERROR_SOURCE_HTTP_STREAM);
  EXPECT_EQ(toProto(StreamErrorSource::AUDIO_OUTPUT),
            renderer_pb::ERROR_SOURCE_AUDIO_OUTPUT);
  EXPECT_EQ(toProto(StreamErrorSource::DECODER),
            renderer_pb::ERROR_SOURCE_DECODER);
}

TEST(StateTranslator, EveryDeviceAccessMaps) {
  EXPECT_EQ(toProto(DeviceAccess::Unknown),
            renderer_pb::DEVICE_ACCESS_UNSPECIFIED);
  EXPECT_EQ(toProto(DeviceAccess::Exclusive),
            renderer_pb::DEVICE_ACCESS_EXCLUSIVE);
  EXPECT_EQ(toProto(DeviceAccess::Shared), renderer_pb::DEVICE_ACCESS_SHARED);
}

TEST(StateTranslator, EveryVolumeBackendMaps) {
  EXPECT_EQ(toProto(VolumeBackend::None), renderer_pb::VOLUME_BACKEND_NONE);
  EXPECT_EQ(toProto(VolumeBackend::Hardware),
            renderer_pb::VOLUME_BACKEND_HARDWARE);
  EXPECT_EQ(toProto(VolumeBackend::Software),
            renderer_pb::VOLUME_BACKEND_SOFTWARE);
}

TEST(StateTranslator, StreamingCarriesTokenAndLivePosition) {
  StreamState state(AudioGraphNodeState::STREAMING, 41750);
  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, "tok-7", 1234, out);

  EXPECT_EQ(out.state(), renderer_pb::PLAYBACK_STATE_PLAYING);
  EXPECT_EQ(out.position_ms(), 41750u);
  EXPECT_TRUE(out.position_valid());
  EXPECT_EQ(out.source_token(), "tok-7");
  EXPECT_EQ(out.at_unix_ms(), 1234);
  EXPECT_FALSE(out.has_error());
}

TEST(StateTranslator, StoppedWithoutTokenMeansTornDown) {
  StreamState state(AudioGraphNodeState::STOPPED);
  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, std::nullopt, 1234, out);

  EXPECT_EQ(out.state(), renderer_pb::PLAYBACK_STATE_STOPPED);
  EXPECT_FALSE(out.has_source_token());
  EXPECT_FALSE(out.position_valid());
}

TEST(StateTranslator, ErrorsCarrySourceAndFaultedToken) {
  StreamState state(AudioGraphNodeState::ERROR,
                    StreamError{StreamErrorSource::HTTP_STREAM, "410 gone"});
  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, "tok-7", 1234, out);

  EXPECT_EQ(out.state(), renderer_pb::PLAYBACK_STATE_ERROR);
  ASSERT_TRUE(out.has_error());
  EXPECT_EQ(out.error().source(), renderer_pb::ERROR_SOURCE_HTTP_STREAM);
  EXPECT_EQ(out.error().message(), "410 gone");
  EXPECT_EQ(out.error().source_token(), "tok-7");
}

TEST(StateTranslator, NegativePositionIsClampedNotWrapped) {
  StreamState state(AudioGraphNodeState::STREAMING, -1);
  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, std::nullopt, 0, out);
  EXPECT_EQ(out.position_ms(), 0u);
}

TEST(StateTranslator, FormatCarriesEveryField) {
  renderer_pb::AudioFormat out;
  fillAudioFormat(StreamAudioFormat{44100, 2, 16, AudioSampleFormat::PCM16_LE},
                  out);

  EXPECT_EQ(out.sample_rate_hz(), 44100u);
  EXPECT_EQ(out.channels(), 2u);
  EXPECT_EQ(out.bits_per_sample(), 16u);
  EXPECT_EQ(out.sample_format(),
            sampleFormatToString(AudioSampleFormat::PCM16_LE));
}

TEST(StateTranslator, TheDurationIsTheStreamsNotTheFormats) {
  StreamInfo info;
  info.format = StreamAudioFormat{44100, 2, 16, AudioSampleFormat::PCM16_LE};
  info.streamType = StreamType::FRAMES;
  info.streamSize = 9876543;

  StreamState state(AudioGraphNodeState::STREAMING, 0, info);
  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, "tok-7", 1234, out);

  ASSERT_TRUE(out.has_duration_ms());
  // What the duration is belongs to AudioInfo_test; this pins that it survives.
  EXPECT_EQ(out.duration_ms(), info.durationMs());
}

TEST(StateTranslator, AStreamOfUnknownLengthReportsNoDuration) {
  StreamInfo info;
  info.format = StreamAudioFormat{44100, 2, 16, AudioSampleFormat::PCM16_LE};
  info.streamType = StreamType::FRAMES;
  info.streamSize = 0;

  StreamState state(AudioGraphNodeState::STREAMING, 0, info);
  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, "tok-7", 1234, out);

  EXPECT_TRUE(out.has_format());
  EXPECT_FALSE(out.has_duration_ms());
}

TEST(StateTranslator, AStateCarriesTheFormatItWasReportedWith) {
  StreamInfo info;
  info.format = StreamAudioFormat{44100, 2, 16, AudioSampleFormat::PCM16_LE};
  info.streamType = StreamType::FRAMES;
  info.streamSize = 9876543;

  // A pause carries the format forward, so it is not a playing-only field.
  StreamState state(AudioGraphNodeState::PAUSED, 1000, info);
  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, "tok-7", 1234, out);

  ASSERT_TRUE(out.has_format());
  EXPECT_EQ(out.format().sample_rate_hz(), 44100u);
  EXPECT_EQ(out.duration_ms(), info.durationMs());
}

TEST(StateTranslator, AStateWithNoFormatSaysSoRatherThanSendingAnEmptyOne) {
  StreamState state(AudioGraphNodeState::STOPPED);
  ASSERT_FALSE(state.streamInfo.has_value());

  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, std::nullopt, 1234, out);

  EXPECT_FALSE(out.has_format());
}

TEST(StateTranslator, TheDeviceFormatRidesAlongsideTheDecodedOne) {
  StreamInfo info;
  info.format = StreamAudioFormat{44100, 2, 24, AudioSampleFormat::PCM24_LE};
  info.streamType = StreamType::FRAMES;
  info.streamSize = 9876543;

  StreamState state(AudioGraphNodeState::STREAMING, 0, info);
  // What a device that cannot take 24-bit substitutes, held exclusively.
  state.deviceInfo = DeviceInfo{
      StreamAudioFormat{44100, 2, 24, AudioSampleFormat::PCM32_LE},
      DeviceAccess::Exclusive};

  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, "tok-7", 1234, out);

  ASSERT_TRUE(out.has_format());
  ASSERT_TRUE(out.has_device_info());
  EXPECT_EQ(out.format().sample_format(),
            sampleFormatToString(AudioSampleFormat::PCM24_LE));
  EXPECT_EQ(out.device_info().format().sample_format(),
            sampleFormatToString(AudioSampleFormat::PCM32_LE));
  EXPECT_EQ(out.device_info().access(), renderer_pb::DEVICE_ACCESS_EXCLUSIVE);
}

TEST(StateTranslator, DsdConvertedToPcmIsReportedAsTheDsdItWas) {
  StreamInfo info;
  info.format = StreamAudioFormat{176400, 2, 24, AudioSampleFormat::PCM24_LE};
  info.decodedFormat =
      StreamAudioFormat{352800, 2, 1, AudioSampleFormat::DSD_U8, 2822400};
  info.streamType = StreamType::FRAMES;
  StreamState state(AudioGraphNodeState::STREAMING, 0, info);
  state.deviceInfo = DeviceInfo{info.format, DeviceAccess::Exclusive};

  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, "tok-7", 1234, out);

  EXPECT_EQ(out.format().sample_format(), "DSD");
  EXPECT_EQ(out.format().sample_rate_hz(), 2822400u);
  EXPECT_EQ(out.format().bits_per_sample(), 1u);
  EXPECT_EQ(out.device_info().format().sample_rate_hz(), 176400u);
  EXPECT_EQ(out.device_info().format().sample_format(),
            sampleFormatToString(AudioSampleFormat::PCM24_LE));
}

TEST(StateTranslator, FloatQuantisedForTheDeviceIsReportedAsTheFloatItWas) {
  StreamInfo info;
  info.format = StreamAudioFormat{96000, 2, 24, AudioSampleFormat::PCM32_LE};
  info.decodedFormat =
      StreamAudioFormat{96000, 2, 32, AudioSampleFormat::PCM_FLOAT32_LE};
  info.streamType = StreamType::FRAMES;
  StreamState state(AudioGraphNodeState::STREAMING, 0, info);

  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, "tok-7", 1234, out);

  EXPECT_EQ(out.format().sample_format(),
            sampleFormatToString(AudioSampleFormat::PCM_FLOAT32_LE));
  EXPECT_EQ(out.format().bits_per_sample(), 32u);
}

TEST(StateTranslator, ADeviceThatIsNotOpenReportsNoFormat) {
  StreamInfo info;
  info.format = StreamAudioFormat{44100, 2, 16, AudioSampleFormat::PCM16_LE};
  info.streamType = StreamType::FRAMES;
  info.streamSize = 9876543;

  StreamState state(AudioGraphNodeState::STREAMING, 0, info);
  ASSERT_FALSE(state.deviceInfo.has_value());

  renderer_pb::PlaybackStateChanged out;
  fillPlaybackStateChanged(state, "tok-7", 1234, out);

  EXPECT_TRUE(out.has_format());
  EXPECT_FALSE(out.has_device_info());
}

TEST(StateTranslator, VolumeCarriesEveryField) {
  VolumeState volume;
  volume.supported = true;
  volume.current = 42;
  volume.max = 100;
  volume.backend = static_cast<int>(VolumeBackend::Hardware);
  volume.dbValues = {-200.0, -60.0, 0.0};
  volume.controlMode = "auto";

  renderer_pb::VolumeState out;
  fillVolume(volume, out);

  EXPECT_TRUE(out.supported());
  EXPECT_EQ(out.current(), 42u);
  EXPECT_EQ(out.max(), 100u);
  EXPECT_EQ(out.backend(), renderer_pb::VOLUME_BACKEND_HARDWARE);
  ASSERT_EQ(out.db_values_size(), 3);
  EXPECT_DOUBLE_EQ(out.db_values(1), -60.0);
  EXPECT_EQ(out.control_mode(), "auto");
}

TEST(StateTranslator, ASignalPathKeepsItsStepsInOrder) {
  renderer_pb::SignalPath out;
  out.add_steps()->set_kind("stale");
  state_translator::fillSignalPath(
      {{{"dsd_to_pcm", "DSD to PCM", "DSD64 to 176.4 kHz, 30 kHz filter"},
        {"dither", "Dither", "TPDF at 24 bits"}},
       0.25},
      out);
  ASSERT_EQ(out.steps_size(), 2) << "replaced, not appended to";
  EXPECT_EQ(out.steps(0).kind(), "dsd_to_pcm");
  EXPECT_EQ(out.steps(0).name(), "DSD to PCM");
  EXPECT_EQ(out.steps(0).summary(), "DSD64 to 176.4 kHz, 30 kHz filter");
  EXPECT_EQ(out.steps(1).kind(), "dither");
  EXPECT_DOUBLE_EQ(out.latency_ms(), 0.25);
}

TEST(StateTranslator, ClippingIsAnEventForTheSourceItHappenedTo) {
  renderer_pb::PlaybackEvent out;
  out.set_source_token("stale");
  state_translator::fillPlaybackEvent({StreamId(3), Clipping{1200}}, "track",
                                      1234, out);
  EXPECT_EQ(out.source_token(), "track");
  EXPECT_EQ(out.at_unix_ms(), 1234);
  ASSERT_EQ(out.kind_case(), renderer_pb::PlaybackEvent::kClipping);
  EXPECT_EQ(out.clipping().clipped_samples(), 1200u);

  state_translator::fillPlaybackEvent({std::nullopt, Clipping{1}},
                                      std::nullopt, 1, out);
  EXPECT_FALSE(out.has_source_token()) << "replaced, not merged";
}
