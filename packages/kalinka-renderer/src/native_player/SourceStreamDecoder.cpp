#include "SourceStreamDecoder.h"

#include <algorithm>
#include <stdexcept>

SourceStreamDecoder::SourceStreamDecoder(size_t bufferSize,
                                         size_t startOffsetMs)
    : BufferedStreamDecoder(bufferSize, startOffsetMs) {}

void SourceStreamDecoder::connectTo(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (!source || input)
    throw std::invalid_argument("Invalid " + name() + " input connection");
  input = std::move(source);
  position = 0;
  seekable = false;
  forget();
  start();
}

void SourceStreamDecoder::disconnect(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (source != input)
    return;
  stop();
  input.reset();
}

size_t SourceStreamDecoder::readSome(void *dest, size_t bytes) {
  auto combined = interruption();
  auto *p = static_cast<uint8_t *>(dest);
  size_t done = 0;
  while (done < bytes) {
    const auto available = input->waitForData(
        combined.get_token(), std::min(bytes - done, size_t(4096)));
    if (combined.get_token().stop_requested())
      throw Interrupted{};
    const auto state = input->getState();
    if (state.state == AudioGraphNodeState::ERROR)
      throw SourceFailed{state.error.value_or(
          StreamError{StreamErrorSource::DECODER, name() + " input failed"})};
    const auto count =
        available ? input->read(p + done, std::min(bytes - done, available))
                  : 0;
    if (!count)
      break;
    // Counted as it arrives, so an interrupted read leaves it exact.
    position += count;
    done += count;
  }
  return done;
}

void SourceStreamDecoder::readExact(void *dest, size_t bytes) {
  if (readSome(dest, bytes) < bytes)
    throw std::runtime_error("Truncated " + name() + " file");
}

bool SourceStreamDecoder::reposition(uint64_t offset) {
  if (input->seekTo(offset) != offset)
    return false;
  position = offset;
  return true;
}

uint64_t SourceStreamDecoder::sourceLength() const {
  const auto info = input->getState().streamInfo;
  return info ? info->streamSize : 0;
}
