#include "ContainerStreamDecoder.h"

#include <algorithm>
#include <stdexcept>

ContainerStreamDecoder::ContainerStreamDecoder(
    std::optional<StreamId> id, std::unique_ptr<ContainerFormat> format,
    size_t bufferSize, size_t startOffsetMs)
    : AudioGraphNode(id), BufferedStreamDecoder(bufferSize, startOffsetMs),
      format(std::move(format)) {
  if (!this->format)
    throw std::invalid_argument("A container decoder needs a format");
}

ContainerStreamDecoder::~ContainerStreamDecoder() { stop(); }

void ContainerStreamDecoder::connectTo(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (!source || input)
    throw std::invalid_argument("Invalid " + format->name() +
                                " input connection");
  input = std::move(source);
  sourcePosition = dataOffset = 0;
  seekable = false;
  start();
}

void ContainerStreamDecoder::disconnect(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (source != input)
    return;
  stop();
  input.reset();
}

void ContainerStreamDecoder::readExact(void *dest, size_t bytes) {
  auto combined = interruption();
  auto *p = static_cast<uint8_t *>(dest);
  while (bytes) {
    const auto available =
        input->waitForData(combined.get_token(), std::min(bytes, size_t(4096)));
    if (combined.get_token().stop_requested())
      throw Interrupted{};
    const auto state = input->getState();
    if (state.state == AudioGraphNodeState::ERROR)
      throw SourceFailed{state.error.value_or(StreamError{
          StreamErrorSource::DECODER, format->name() + " input failed"})};
    const auto count =
        available ? input->read(p, std::min(bytes, available)) : 0;
    if (!count)
      throw std::runtime_error("Truncated " + format->name() + " file");
    sourcePosition += count;
    p += count;
    bytes -= count;
  }
}

StreamInfo ContainerStreamDecoder::open() {
  const auto header = format->parseHeader(*this);
  dataOffset = sourcePosition;
  const auto source = input->getState().streamInfo;
  if (source && source->streamSize &&
      source->streamSize < dataOffset + header.audioBytes)
    throw std::runtime_error("Truncated " + format->name() + " audio data");
  // Probe without moving to any other part of the file.
  seekable = input->seekTo(dataOffset) == dataOffset;
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
  const auto offset = dataOffset + format->seek(frame);
  if (input->seekTo(offset) != offset)
    throw std::runtime_error(format->name() + " stream seek failed");
  sourcePosition = offset;
}
