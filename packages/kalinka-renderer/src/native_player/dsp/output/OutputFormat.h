#pragma once

#include "AudioInfo.h"

#include <vector>

/// The format the output stage presents for a source, and the bits it dithers
/// at when it changes the samples.
struct OutputChoice {
  AudioSampleFormat format;
  unsigned bits;
};

/**
 * @brief What to present for @p source: its own format when the device takes
 * it at the source's rate on @p channels channels, else the first wider
 * container pcmOutputFormats() would try that the device takes. The bits are
 * those the device keeps in that format. Float stays float where the device
 * takes it, and goes to the widest integer container it takes otherwise. With
 * no matching device format, integer PCM keeps its declared precision, or
 * its container width if unspecified. DSD and DoP stay as they are.
 * @param device What the device was probed to take.
 */
OutputChoice chooseOutput(const StreamAudioFormat &source,
                          const std::vector<StreamAudioFormat> &device,
                          unsigned channels);
