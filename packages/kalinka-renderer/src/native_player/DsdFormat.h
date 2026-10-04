#pragma once

#include "AudioInfo.h"
#include <alsa/asoundlib.h>
#include <span>
#include <vector>

struct OutputCapabilities {
  DeviceAccess access = DeviceAccess::Unknown;
  std::string status;
  std::vector<StreamAudioFormat> formats;
};

snd_pcm_format_t alsaFormat(AudioSampleFormat format);
// Nonblocking probe. A busy device may return its last successful probe,
// labelled as cached; playback always validates the actual parameters again.
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
