#include "ContainerStreamDecoder.h"

#include <algorithm>
#include <limits>
#include <stdexcept>

namespace {
struct Interrupted {};
struct SourceFailed {
  StreamError error;
};
} // namespace

ContainerStreamDecoder::ContainerStreamDecoder(
    std::optional<StreamId> id, std::unique_ptr<ContainerFormat> format,
    size_t bufferSize, size_t startOffsetMs)
    : AudioGraphNode(id), format(std::move(format)), buffer(bufferSize),
      startOffsetMs(startOffsetMs) {
  if (!this->format)
    throw std::invalid_argument("A container decoder needs a format");
}

ContainerStreamDecoder::~ContainerStreamDecoder() { stop(); }

void ContainerStreamDecoder::stop() {
  if (worker.joinable()) {
    worker.request_stop();
    worker.join();
  }
}

void ContainerStreamDecoder::connectTo(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (!source || input)
    throw std::invalid_argument("Invalid " + format->name() +
                                " input connection");
  input = std::move(source);
  sourcePosition = dataOffset = 0;
  info = {};
  startFrame = bytesRead = 0;
  frameBytes = 0;
  seekable = false;
  seekSignal.reopen();
  ready.reopen();
  buffer.clear();
  buffer.resetEof();
  setState(StreamState{AudioGraphNodeState::PREPARING});
  worker = std::jthread([this](std::stop_token token) { run(token); });
}

void ContainerStreamDecoder::disconnect(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (source != input)
    return;
  stop();
  input.reset();
}

std::optional<long> ContainerStreamDecoder::streamReadPosition() const {
  const auto width = frameBytes.load();
  return startFrame.load() + (width ? bytesRead.load() / width : 0);
}

void ContainerStreamDecoder::finishIfDrained() {
  if (buffer.isEof() && buffer.size() == 0 &&
      getState().state == AudioGraphNodeState::STREAMING)
    setState({AudioGraphNodeState::FINISHED, *streamReadPosition(), info});
}

size_t ContainerStreamDecoder::read(void *dest, size_t size) {
  const auto width = frameBytes.load();
  if (!width)
    return 0;
  // A partial frame would misalign the sink's channels or DoP markers.
  size = std::min(size, buffer.size());
  const auto count =
      buffer.read(static_cast<uint8_t *>(dest), size - size % width);
  bytesRead += count;
  finishIfDrained();
  return count;
}

size_t ContainerStreamDecoder::waitForData(std::stop_token token, size_t size) {
  auto combined = combineStopTokens(token, seekSignal.getStopToken());
  const auto count = buffer.waitForData(combined.get_token(), size);
  finishIfDrained();
  return count;
}

size_t ContainerStreamDecoder::waitForDataFor(std::stop_token token,
                                              std::chrono::milliseconds timeout,
                                              size_t size) {
  auto combined = combineStopTokens(token, seekSignal.getStopToken());
  const auto count = buffer.waitForDataFor(combined.get_token(), timeout, size);
  finishIfDrained();
  return count;
}

size_t ContainerStreamDecoder::seekTo(size_t frame) {
  if (!worker.joinable() || getState().state == AudioGraphNodeState::ERROR ||
      seekSignal.getValue())
    return size_t(-1);
  auto token = worker.get_stop_token();
  if (!ready.waitValue(token).value_or(false) || !seekable)
    return size_t(-1);
  seekSignal.sendValue(frame);
  return seekSignal.getResponse(token);
}

void ContainerStreamDecoder::readExact(void *dest, size_t bytes) {
  auto combined = combineStopTokens(workerToken, seekSignal.getStopToken());
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

void ContainerStreamDecoder::open() {
  const auto header = format->parseHeader(*this);
  dataOffset = sourcePosition;
  const auto source = input->getState().streamInfo;
  if (source && source->streamSize &&
      source->streamSize < dataOffset + header.audioBytes)
    throw std::runtime_error("Truncated " + format->name() + " audio data");
  info = header.info;
  frameBytes = info.format.channels * sampleSize(info.format.sampleFormat);
  if (!frameBytes || !info.format.sampleRate)
    throw std::runtime_error(format->name() +
                             " header describes no audio format");
  if (frameBytes > buffer.max_size())
    throw std::runtime_error(format->name() +
                             " buffer is smaller than one frame");
  // Probe without moving to any other part of the file.
  seekable = input->seekTo(dataOffset) == dataOffset;
  if (!startOffsetMs)
    return;
  if (!seekable)
    throw std::runtime_error(format->name() +
                             " start offset requires a seekable stream");
  if (startOffsetMs >
      std::numeric_limits<uint64_t>::max() / info.format.sampleRate)
    throw std::runtime_error(format->name() + " start offset is too large");
  const auto frame = uint64_t(startOffsetMs) * info.format.sampleRate / 1000;
  if (frame >= info.streamSize)
    throw std::runtime_error(format->name() + " start offset is past the end");
  seek(frame);
}

void ContainerStreamDecoder::seek(size_t frame) {
  frame = std::min(frame, size_t(info.streamSize));
  setState(StreamState{AudioGraphNodeState::PREPARING});
  buffer.clear();
  buffer.resetEof();
  const auto offset = dataOffset + format->seek(frame);
  if (input->seekTo(offset) != offset)
    throw std::runtime_error(format->name() + " stream seek failed");
  sourcePosition = offset;
  startFrame = frame;
  bytesRead = 0;
  setState({AudioGraphNodeState::STREAMING, long(frame), info});
  seekSignal.respond(frame);
}

void ContainerStreamDecoder::write(std::span<const uint8_t> block) {
  auto combined = combineStopTokens(workerToken, seekSignal.getStopToken());
  size_t written = 0;
  while (written < block.size()) {
    auto space = buffer.waitForSpace(combined.get_token(), frameBytes);
    if (combined.get_token().stop_requested())
      throw Interrupted{};
    space -= space % frameBytes;
    written += buffer.write(block.data() + written,
                            std::min(space, block.size() - written));
  }
}

void ContainerStreamDecoder::run(std::stop_token token) {
  workerToken = token;
  try {
    open();
    setState({AudioGraphNodeState::STREAMING, *streamReadPosition(), info});
    ready.sendValue(true);
    while (!token.stop_requested()) {
      try {
        if (auto target = seekSignal.getValue())
          seek(*target);
        const auto block = format->nextBlock(*this);
        if (block.empty()) {
          buffer.setEof();
          seekSignal.waitValue(token);
        } else
          write(block);
      } catch (const Interrupted &) { // A seek or shutdown cut a wait short.
      }
    }
  } catch (const Interrupted &) {
  } catch (const SourceFailed &failed) {
    if (!token.stop_requested())
      setState({AudioGraphNodeState::ERROR, failed.error});
  } catch (const std::exception &ex) {
    if (!token.stop_requested())
      setState({AudioGraphNodeState::ERROR,
                StreamError{StreamErrorSource::DECODER, ex.what()}});
  }
  seekSignal.close(size_t(-1));
  ready.close(false);
  buffer.setEof();
}
