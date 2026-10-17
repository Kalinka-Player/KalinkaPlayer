#include "SampleDecoder.h"
#include "AacDecoder.h"
#include "FlacFrameDecoder.h"

std::unique_ptr<SampleDecoder> sampleDecoder(const mp4::AudioTrack &track) {
  if (track.codec == mp4::AudioTrack::Codec::AAC)
    return std::make_unique<AacDecoder>(track.config);
  return std::make_unique<FlacFrameDecoder>(track.config);
}
