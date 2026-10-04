#include "StateTranslator.h"

#include <algorithm>

namespace pb = kalinka::renderer::v1;

namespace state_translator {

pb::PlaybackState toProto(AudioGraphNodeState state) {
  switch (state) {
  case AudioGraphNodeState::STOPPED:
    return pb::PLAYBACK_STATE_STOPPED;
  case AudioGraphNodeState::PREPARING:
    return pb::PLAYBACK_STATE_PREPARING;
  case AudioGraphNodeState::STREAMING:
    return pb::PLAYBACK_STATE_PLAYING;
  case AudioGraphNodeState::PAUSED:
    return pb::PLAYBACK_STATE_PAUSED;
  case AudioGraphNodeState::FINISHED:
    return pb::PLAYBACK_STATE_FINISHED;
  case AudioGraphNodeState::ERROR:
    return pb::PLAYBACK_STATE_ERROR;
  case AudioGraphNodeState::SOURCE_CHANGED:
    break;  // a transition, not a state; the caller reports SourceChanged
  }
  return pb::PLAYBACK_STATE_UNSPECIFIED;
}

pb::ErrorSource toProto(StreamErrorSource source) {
  switch (source) {
  case StreamErrorSource::NONE:
    return pb::ERROR_SOURCE_NONE;
  case StreamErrorSource::HTTP_STREAM:
    return pb::ERROR_SOURCE_HTTP_STREAM;
  case StreamErrorSource::AUDIO_OUTPUT:
    return pb::ERROR_SOURCE_AUDIO_OUTPUT;
  case StreamErrorSource::DECODER:
    return pb::ERROR_SOURCE_DECODER;
  }
  return pb::ERROR_SOURCE_UNSPECIFIED;
}

pb::DeviceAccess toProto(DeviceAccess access) {
  switch (access) {
  case DeviceAccess::Exclusive:
    return pb::DEVICE_ACCESS_EXCLUSIVE;
  case DeviceAccess::Shared:
    return pb::DEVICE_ACCESS_SHARED;
  case DeviceAccess::Unknown:
    break;
  }
  return pb::DEVICE_ACCESS_UNSPECIFIED;
}

pb::VolumeBackend toProto(VolumeBackend backend) {
  switch (backend) {
  case VolumeBackend::None:
    return pb::VOLUME_BACKEND_NONE;
  case VolumeBackend::Hardware:
    return pb::VOLUME_BACKEND_HARDWARE;
  case VolumeBackend::Software:
    return pb::VOLUME_BACKEND_SOFTWARE;
  }
  return pb::VOLUME_BACKEND_UNSPECIFIED;
}

void fillAudioFormat(const StreamAudioFormat &format, pb::AudioFormat &out) {
  out.set_sample_rate_hz(format.sampleRate);
  out.set_channels(format.channels);
  out.set_bits_per_sample(format.bitsPerSample);
  out.set_sample_format(sampleFormatToString(format.sampleFormat));
}

void fillDeviceInfo(const DeviceInfo &device, pb::DeviceInfo &out) {
  fillAudioFormat(device.format, *out.mutable_format());
  out.set_access(toProto(device.access));
}

void fillVolume(const VolumeState &volume, pb::VolumeState &out) {
  out.set_supported(volume.supported);
  out.set_current(std::max(volume.current, 0));
  out.set_max(std::max(volume.max, 0));
  // Stored as int in the native struct, for the Python binding's sake.
  out.set_backend(toProto(static_cast<VolumeBackend>(volume.backend)));
}

void fillPlaybackStateChanged(const StreamState &state,
                              const std::optional<std::string> &sourceToken,
                              int64_t atUnixMs,
                              pb::PlaybackStateChanged &out) {
  out.set_state(toProto(state.state));
  out.set_position_ms(state.position < 0 ? 0 : state.position);
  // The graph reports a live position only while a stream is on the clock.
  out.set_position_valid(state.state == AudioGraphNodeState::STREAMING ||
                         state.state == AudioGraphNodeState::PAUSED);
  if (sourceToken) {
    out.set_source_token(*sourceToken);
  }
  out.set_at_unix_ms(atUnixMs);
  if (state.streamInfo) {
    fillAudioFormat(state.streamInfo->format, *out.mutable_format());
    if (state.streamInfo->format.dsdSampleRate) {
      out.mutable_format()->set_sample_rate_hz(
          state.streamInfo->format.dsdSampleRate);
      out.mutable_format()->set_bits_per_sample(1);
      out.mutable_format()->set_sample_format("DSD");
    }
    if (const auto duration = state.streamInfo->durationMs()) {
      out.set_duration_ms(*duration);
    }
  }
  if (state.deviceInfo) {
    fillDeviceInfo(*state.deviceInfo, *out.mutable_device_info());
  }
  if (state.error) {
    pb::ErrorInfo *error = out.mutable_error();
    error->set_source(toProto(state.error->source));
    error->set_message(state.error->message);
    if (sourceToken) {
      error->set_source_token(*sourceToken);
    }
  }
}

}  // namespace state_translator
