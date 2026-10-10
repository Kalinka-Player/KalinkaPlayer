#pragma once

#include "ContainerFormat.h"

/**
 * @brief RIFF/WAVE with integer or IEEE float PCM, plain or
 * WAVE_FORMAT_EXTENSIBLE, mono or stereo.
 *
 * Frames leave at the source's rate and precision: 24-bit audio as PCM24_LE
 * (three significant bytes in a four-byte word), float as float. Extensible
 * audio with fewer valid bits than its container keeps only the valid bits.
 */
class WavContainer final : public ContainerFormat {
public:
  std::string name() const override { return "WAV"; }
  Header parseHeader(ByteReader &source) override;
  uint64_t seek(uint64_t frame) override;
  std::vector<uint8_t> nextBlock(ByteReader &source) override;

private:
  StreamAudioFormat format{};
  uint64_t frames = 0, nextFrame = 0;
  unsigned sourceFrameBytes = 0, containerBits = 0;
  void parseFormat(ByteReader &source, uint64_t size);
};
