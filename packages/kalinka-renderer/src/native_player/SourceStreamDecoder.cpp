#include "SourceStreamDecoder.h"

#include <algorithm>
#include <stdexcept>
#include <utility>

SourceStreamDecoder::SourceStreamDecoder(size_t bufferSize,
                                         size_t startOffsetMs)
    : BufferedStreamDecoder(bufferSize, startOffsetMs) {}

void SourceStreamDecoder::connectTo(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (!source || input)
    throw std::invalid_argument("Invalid " + name() + " input connection");
  // First, so a forget() that throws leaves the decoder free to connect again.
  forget();
  input = std::move(source);
  position = 0;
  length = 0;
  callbackFailure = nullptr;
  seekable = false;
  start();
}

void SourceStreamDecoder::disconnect(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (source != input)
    return;
  stop();
  input.reset();
}

size_t SourceStreamDecoder::take(void *dest, size_t bytes, size_t wanted) {
  auto combined = interruption();
  const auto available = input->waitForData(combined.get_token(), wanted);
  if (combined.get_token().stop_requested())
    throw Interrupted{};
  const auto state = input->getState();
  if (state.state == AudioGraphNodeState::ERROR)
    throw SourceFailed{state.error.value_or(
        StreamError{StreamErrorSource::DECODER, name() + " input failed"})};
  // Kept while it is known: a later range request can hide it for a while.
  if (!length && state.streamInfo)
    length = state.streamInfo->streamSize;
  const auto count =
      available ? input->read(dest, std::min(bytes, available)) : 0;
  // Counted as it arrives, so an interrupted read leaves it exact.
  position += count;
  return count;
}

size_t SourceStreamDecoder::readSome(void *dest, size_t bytes) {
  auto *p = static_cast<uint8_t *>(dest);
  size_t done = 0;
  while (done < bytes) {
    const auto count =
        take(p + done, bytes - done, std::min(bytes - done, size_t(4096)));
    if (!count)
      break;
    done += count;
  }
  return done;
}

size_t SourceStreamDecoder::readAvailable(void *dest, size_t bytes) {
  return bytes ? take(dest, bytes, 1) : 0;
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

void SourceStreamDecoder::keepCallbackFailure(
    std::exception_ptr failure) noexcept {
  callbackFailure = std::move(failure);
}

void SourceStreamDecoder::rethrowCallbackFailure() {
  if (callbackFailure)
    std::rethrow_exception(std::exchange(callbackFailure, nullptr));
}

uint64_t SourceStreamDecoder::sourceLength() const {
  if (!length) {
    const auto info = input->getState().streamInfo;
    length = info ? info->streamSize : 0;
  }
  return length;
}

unsigned SourceStreamDecoder::sourcePrebufferMs() const {
  const auto info = input->getState().streamInfo;
  return info ? info->prebufferMs : 0;
}
