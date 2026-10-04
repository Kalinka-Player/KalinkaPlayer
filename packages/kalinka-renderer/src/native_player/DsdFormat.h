#pragma once

#include "AudioInfo.h"
#include <alsa/asoundlib.h>
#include <span>
#include <vector>

struct OutputCapabilities {
  DeviceAccess access = DeviceAccess::Unknown;
  /// Why the device could not be opened; set only when access is Unknown.
  std::string error;
  std::vector<StreamAudioFormat> formats;
};

/// Why a DSD source is refused while `output.dsd_mode` is "disabled".
inline constexpr char DSD_DISABLED_ERROR[] =
    "DSD playback is disabled in renderer settings";
/// Why DSD is refused on an output that is not the card itself.
inline constexpr char DSD_SHARED_OUTPUT_ERROR[] =
    "DSD needs an output device marked (direct)";

snd_pcm_format_t alsaFormat(AudioSampleFormat format);
/// Whether @p mode is one of the `output.dsd_mode` setting's values.
bool isDsdMode(const std::string &mode);
// Nonblocking probe. A busy device returns its last successful probe, if any;
// playback always validates the actual parameters again.
OutputCapabilities probeOutput(const std::string &device);
unsigned dsdBitsPerFrame(AudioSampleFormat format);
StreamAudioFormat chooseDsdOutput(const OutputCapabilities &caps,
                                  const std::string &mode, unsigned rate,
                                  unsigned channels);
// Input is interleaved, chronological, MSB-first DSD bytes. A final partial
// transport frame is filled with the DSD silence pattern, never PCM zeros.
std::vector<uint8_t> packDsd(std::span<const uint8_t> input, unsigned channels,
                             AudioSampleFormat format, uint64_t firstFrame);
// The emitter owns the marker clock, including silence during a pause and
// source changes. Updates only marker/padding bytes, never the DSD payload.
void stampDopMarkers(std::span<uint8_t> output, unsigned channels,
                     AudioSampleFormat format, uint64_t &frame);
