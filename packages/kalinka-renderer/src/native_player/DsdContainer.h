#pragma once

#include "ContainerFormat.h"

#include <functional>

/**
 * @brief DSF and uncompressed DSDIFF, mono or stereo, leaving as native DSD
 * or DoP: container extraction and lossless transport packing, no PCM.
 *
 * `select` chooses the transport for the file's rate and channel count once
 * the header is read; it may refuse by throwing, and its message reaches the
 * listener. Frames are transport frames, so a seek lands on a transport frame
 * and a DSF seek re-reads the channel block it falls in.
 */
class DsdContainer final : public ContainerFormat {
public:
  using SelectOutput = std::function<StreamAudioFormat(unsigned, unsigned)>;
  explicit DsdContainer(SelectOutput select);
  std::string name() const override { return "DSD"; }
  Header parseHeader(ByteReader &source) override;
  uint64_t seek(uint64_t frame) override;
  std::vector<uint8_t> nextBlock(ByteReader &source) override;

private:
  const SelectOutput select;
  uint64_t sampleCount = 0, nextByte = 0;
  unsigned channels = 0, rate = 0, blockSize = 0;
  bool dsf = false, reverseBits = false;
  AudioSampleFormat transport = DSD_U8;
  void parseDsf(ByteReader &source);
  void parseDsdiff(ByteReader &source);
};
