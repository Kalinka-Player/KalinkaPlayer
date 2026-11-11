#include "StateTranslator.h"

#include <algorithm>

namespace renderer_pb = kalinka::renderer::v1;

namespace state_translator {

renderer_pb::PlaybackState toProto(AudioGraphNodeState state) {
  switch (state) {
  case AudioGraphNodeState::STOPPED:
    return renderer_pb::PLAYBACK_STATE_STOPPED;
  case AudioGraphNodeState::PREPARING:
    return renderer_pb::PLAYBACK_STATE_PREPARING;
  case AudioGraphNodeState::STREAMING:
    return renderer_pb::PLAYBACK_STATE_PLAYING;
  case AudioGraphNodeState::PAUSED:
    return renderer_pb::PLAYBACK_STATE_PAUSED;
  case AudioGraphNodeState::FINISHED:
    return renderer_pb::PLAYBACK_STATE_FINISHED;
  case AudioGraphNodeState::ERROR:
    return renderer_pb::PLAYBACK_STATE_ERROR;
  case AudioGraphNodeState::SOURCE_CHANGED:
    break;  // a transition, not a state; the caller reports SourceChanged
  }
  return renderer_pb::PLAYBACK_STATE_UNSPECIFIED;
}

renderer_pb::ErrorSource toProto(StreamErrorSource source) {
  switch (source) {
  case StreamErrorSource::NONE:
    return renderer_pb::ERROR_SOURCE_NONE;
  case StreamErrorSource::HTTP_STREAM:
    return renderer_pb::ERROR_SOURCE_HTTP_STREAM;
  case StreamErrorSource::AUDIO_OUTPUT:
    return renderer_pb::ERROR_SOURCE_AUDIO_OUTPUT;
  case StreamErrorSource::DECODER:
    return renderer_pb::ERROR_SOURCE_DECODER;
  }
  return renderer_pb::ERROR_SOURCE_UNSPECIFIED;
}

renderer_pb::DeviceAccess toProto(DeviceAccess access) {
  switch (access) {
  case DeviceAccess::Exclusive:
    return renderer_pb::DEVICE_ACCESS_EXCLUSIVE;
  case DeviceAccess::Shared:
    return renderer_pb::DEVICE_ACCESS_SHARED;
  case DeviceAccess::Unknown:
    break;
  }
  return renderer_pb::DEVICE_ACCESS_UNSPECIFIED;
}

renderer_pb::VolumeBackend toProto(VolumeBackend backend) {
  switch (backend) {
  case VolumeBackend::None:
    return renderer_pb::VOLUME_BACKEND_NONE;
  case VolumeBackend::Hardware:
    return renderer_pb::VOLUME_BACKEND_HARDWARE;
  case VolumeBackend::Software:
    return renderer_pb::VOLUME_BACKEND_SOFTWARE;
  }
  return renderer_pb::VOLUME_BACKEND_UNSPECIFIED;
}

void fillAudioFormat(const StreamAudioFormat &format,
                     renderer_pb::AudioFormat &out) {
  out.set_sample_rate_hz(format.sampleRate);
  out.set_channels(format.channels);
  out.set_bits_per_sample(format.bitsPerSample);
  out.set_sample_format(sampleFormatToString(format.sampleFormat));
}

void fillDeviceInfo(const DeviceInfo &device, renderer_pb::DeviceInfo &out) {
  fillAudioFormat(device.format, *out.mutable_format());
  out.set_access(toProto(device.access));
}

void fillVolume(const VolumeState &volume, renderer_pb::VolumeState &out) {
  out.set_supported(volume.supported);
  out.set_current(std::max(volume.current, 0));
  out.set_max(std::max(volume.max, 0));
  // Stored as int in the native struct, for the Python binding's sake.
  out.set_backend(toProto(static_cast<VolumeBackend>(volume.backend)));
  out.clear_db_values();
  for (double db : volume.dbValues) {
    out.add_db_values(db);
  }
  out.set_control_mode(volume.controlMode);
}

void fillPlaybackStateChanged(const StreamState &state,
                              const std::optional<std::string> &sourceToken,
                              int64_t atUnixMs,
                              renderer_pb::PlaybackStateChanged &out) {
  out.set_state(toProto(state.state));
  out.set_position_ms(state.position < 0 ? 0 : state.position);
  // The graph reports a live position only while a stream is on the clock.
  out.set_position_valid(state.state == AudioGraphNodeState::STREAMING ||
                         state.state == AudioGraphNodeState::PAUSED);
  if (sourceToken) {
    out.set_source_token(*sourceToken);
  }
  out.set_at_unix_ms(atUnixMs);
  out.set_live_ready(state.liveReady);
  if (state.liveSourceFrame)
    out.set_live_source_frame(*state.liveSourceFrame);
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
    renderer_pb::ErrorInfo *error = out.mutable_error();
    error->set_source(toProto(state.error->source));
    error->set_message(state.error->message);
    if (sourceToken) {
      error->set_source_token(*sourceToken);
    }
  }
}

}  // namespace state_translator
