#include "SampleDecoder.h"
#include "AacDecoder.h"
#include "AlacDecoder.h"
#include "FlacFrameDecoder.h"

std::unique_ptr<SampleDecoder> sampleDecoder(const mp4::AudioTrack &track) {
  switch (track.codec) {
  case mp4::AudioTrack::Codec::AAC:
    return std::make_unique<AacDecoder>(track.config);
  case mp4::AudioTrack::Codec::FLAC:
    return std::make_unique<FlacFrameDecoder>(track.config);
  case mp4::AudioTrack::Codec::ALAC:
    return std::make_unique<AlacDecoder>(track.config);
  }
  mp4::invalid();
}
