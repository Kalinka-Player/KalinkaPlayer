#pragma once

#include <optional>
#include <string>

#include "../native_player/AlsaVolumeControl.h"
#include "../native_player/PlaybackEvents.h"
#include "../native_player/SignalPath.h"
#include "../native_player/StreamState.h"
#include "kalinka/renderer/v1/renderer.pb.h"

/**
 * @brief Native graph state to protocol messages, as pure functions.
 *
 * This is where the "don't change existing semantics" contract is pinned
 * down: every AudioGraphNodeState, StreamErrorSource and VolumeBackend maps
 * to exactly one protocol value, and the mapping has no other inputs. The
 * one thing the native state does not carry — which source is current — is
 * passed in, because only NativePlayer's queue bookkeeping knows it.
 */
namespace state_translator {

/// SOURCE_CHANGED has no equivalent here — it is a transition, reported as
/// SourceChanged by the caller — and maps to UNSPECIFIED.
kalinka::renderer::v1::PlaybackState toProto(AudioGraphNodeState state);

kalinka::renderer::v1::ErrorSource toProto(StreamErrorSource source);

kalinka::renderer::v1::VolumeBackend toProto(VolumeBackend backend);

kalinka::renderer::v1::DeviceAccess toProto(DeviceAccess access);

void fillAudioFormat(const StreamAudioFormat &format,
                     kalinka::renderer::v1::AudioFormat &out);

void fillDeviceInfo(const DeviceInfo &device,
                    kalinka::renderer::v1::DeviceInfo &out);

void fillVolume(const VolumeState &volume,
                kalinka::renderer::v1::VolumeState &out);

void fillSignalPath(const SignalPath &path,
                    kalinka::renderer::v1::SignalPath &out);

/// @param sourceToken The source @p event concerns, when it concerns one.
/// @return false for a kind the protocol has no event for.
bool fillPlaybackEvent(const PlaybackEvent &event,
                       const std::optional<std::string> &sourceToken,
                       int64_t atUnixMs,
                       kalinka::renderer::v1::PlaybackEvent &out);

/**
 * @brief One native state change as a PlaybackStateChanged.
 *
 * @param state       What the graph reported. Must not be SOURCE_CHANGED.
 * @param sourceToken The current source, absent when the graph holds none —
 *                    how "the track ended" differs from "torn down".
 * @param atUnixMs    Wall-clock send time; the native steady_clock timestamp
 *                    is not on the wire.
 */
void fillPlaybackStateChanged(const StreamState &state,
                              const std::optional<std::string> &sourceToken,
                              int64_t atUnixMs,
                              kalinka::renderer::v1::PlaybackStateChanged &out);

}  // namespace state_translator
