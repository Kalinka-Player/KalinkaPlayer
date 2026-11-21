#pragma once

#include <cstdint>

/// Oldest protocol version this binary still speaks. Version 1 left the Core to
/// derive a duration from a frame count this renderer no longer sends.
constexpr uint32_t kMinRendererProtocolVersion = 2;

/// Newest protocol version this binary speaks. Both bounds ride in Hello as
/// the range a Core picks from, so a Core that moved on stays reachable for
/// as long as it still speaks something in here.
constexpr uint32_t kMaxRendererProtocolVersion = 3;

/// Whether a Core announcing @p version speaks something this binary can.
constexpr bool rendererProtocolSupported(int version) {
  return version >= static_cast<int>(kMinRendererProtocolVersion) &&
         version <= static_cast<int>(kMaxRendererProtocolVersion);
}

/// mDNS TXT key a Core advertises its version under; no match, no connection.
constexpr const char *kRendererProtoTxtKey = "renderer_proto";
