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
/// Why a DSD track that does not play natively is refused under direct output,
/// which converts nothing.
inline constexpr char DSD_DIRECT_ERROR[] =
    "Direct output plays DSD only as native DSD or DoP";
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
/**
 * @brief The transport DSD at @p rate leaves in under `output.dsd_mode`
 * @p mode, one of native, dop and pcm, on an output that takes @p caps.
 * @throws std::runtime_error, its message for the listener, when it cannot.
 */
StreamAudioFormat chooseDsdOutput(const OutputCapabilities &caps,
                                  const std::string &mode, unsigned rate,
                                  unsigned channels);
/**
 * @brief Automatic's transport: native DSD when the output takes @p rate so
 * and @p nativeAllowed, else DSD_RAW for the DSP stage to convert to a PCM
 * rate @p converted holds. Never DoP.
 * @param native What the output takes as itself.
 * @param nativeAllowed Whether native DSD may play: only at Fixed output
 * volume.
 * @throws std::runtime_error, its message for the listener, when it can
 * neither.
 */
StreamAudioFormat
chooseAutomaticDsdOutput(const OutputCapabilities &native,
                         std::span<const StreamAudioFormat> converted,
                         bool nativeAllowed, unsigned rate, unsigned channels);
// Input is interleaved, chronological, MSB-first DSD bytes. A final partial
// transport frame is filled with the DSD silence pattern, never PCM zeros.
// DSD_U8 and DSD_RAW hand the input back as it came, without a copy.
std::vector<uint8_t> packDsd(std::vector<uint8_t> input, unsigned channels,
                             AudioSampleFormat format, uint64_t firstFrame);
// The emitter owns the marker clock, including silence during a pause and
// source changes. Updates only marker/padding bytes, never the DSD payload.
void stampDopMarkers(std::span<uint8_t> output, unsigned channels,
                     AudioSampleFormat format, uint64_t &frame);
