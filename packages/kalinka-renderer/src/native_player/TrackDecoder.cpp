#include "TrackDecoder.h"

#include <algorithm>
#include <stdexcept>

TrackDecoder::TrackDecoder(std::unique_ptr<SampleDecoder> decoder)
    : decoder(std::move(decoder)),
      frameBytes(this->decoder->format().channels *
                 sampleSize(this->decoder->format().sampleFormat)) {}

void TrackDecoder::restart(uint64_t frame) {
  decoder->reset();
  target = next = frame;
}

std::span<const uint8_t> TrackDecoder::decode(std::span<const uint8_t> unit,
                                              uint64_t first, uint64_t frames) {
  const auto pcm = decoder->decode(unit);
  if (pcm.empty())
    return {};
  if (pcm.size() < frames * frameBytes ||
      (!decoder->padded() && pcm.size() != frames * frameBytes))
    throw std::runtime_error(
        "MP4 audio sample duration does not match its audio");
  const auto skip = std::min(target > first ? target - first : 0, frames);
  if (first + skip > next)
    throw std::runtime_error("An MP4 audio sample is missing");
  next = std::max(next, first + frames);
  return pcm.subspan(skip * frameBytes, (frames - skip) * frameBytes);
}
