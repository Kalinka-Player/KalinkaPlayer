#include "TrackDecoder.h"

#include <algorithm>
#include <stdexcept>

TrackDecoder::TrackDecoder(std::unique_ptr<SampleDecoder> decoder,
                           uint64_t start, uint64_t length)
    : decoder(std::move(decoder)),
      frameBytes(this->decoder->format().channels *
                 sampleSize(this->decoder->format().sampleFormat)),
      begin(std::max(start, this->decoder->delay())),
      editEnd(length ? start + length : 0) {}

std::unique_ptr<TrackDecoder>
TrackDecoder::forTrack(const mp4::AudioTrack &track) {
  auto decoder = sampleDecoder(track);
  const auto rate = decoder->format().sampleRate;
  const auto start = samplesNear(track.editStart, track.timescale, rate);
  const auto length = samplesNear(track.editLength, track.timescale, rate);
  return std::make_unique<TrackDecoder>(std::move(decoder), start, length);
}

uint64_t TrackDecoder::setEnd(uint64_t timelineEnd) {
  end = timelineEnd && editEnd ? std::min(timelineEnd, editEnd)
                               : std::max(timelineEnd, editEnd);
  if (!end) {
    end = ~uint64_t(0);
    return 0;
  }
  return end > begin ? end - begin : 0;
}

uint64_t TrackDecoder::restart(uint64_t frame) {
  decoder->reset();
  target = next = begin + frame;
  return target - decoder->delay();
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
  const auto stop = end > first ? std::min(frames, end - first) : 0;
  if (skip >= stop)
    return {};
  return pcm.subspan(skip * frameBytes, (stop - skip) * frameBytes);
}
