#include "ContainerStreamDecoder.h"

#include <stdexcept>

ContainerStreamDecoder::ContainerStreamDecoder(
    std::optional<StreamId> id, std::unique_ptr<ContainerFormat> format,
    size_t bufferSize, size_t startOffsetMs)
    : AudioGraphNode(id), SourceStreamDecoder(bufferSize, startOffsetMs),
      format(std::move(format)) {
  if (!this->format)
    throw std::invalid_argument("A container decoder needs a format");
}

ContainerStreamDecoder::~ContainerStreamDecoder() { stop(); }

StreamInfo ContainerStreamDecoder::open() {
  const auto header = format->parseHeader(*this);
  dataOffset = sourcePosition();
  const auto length = sourceLength();
  if (length && length < dataOffset + header.audioBytes)
    throw std::runtime_error("Truncated " + format->name() + " audio data");
  // Probe without moving to any other part of the file.
  setSeekable(reposition(dataOffset));
  return header.info;
}

bool ContainerStreamDecoder::decodeNext() {
  const auto block = format->nextBlock(*this);
  if (block.empty())
    return false;
  write(block);
  return true;
}

void ContainerStreamDecoder::seekSource(uint64_t frame) {
  if (!reposition(dataOffset + format->seek(frame)))
    throw std::runtime_error(format->name() + " stream seek failed");
}
