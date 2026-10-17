#include "BufferedStreamDecoder.h"

#include <algorithm>
#include <limits>
#include <stdexcept>

BufferedStreamDecoder::BufferedStreamDecoder(size_t bufferSize,
                                             size_t startOffsetMs)
    : buffer(bufferSize), startOffsetMs(startOffsetMs) {}

void BufferedStreamDecoder::start() {
  info = {};
  startFrame = bytesRead = 0;
  frameBytes = 0;
  seekSignal.reopen();
  ready.reopen();
  buffer.clear();
  buffer.resetEof();
  setState(StreamState{AudioGraphNodeState::PREPARING});
  worker = std::jthread([this](std::stop_token token) { run(token); });
}

void BufferedStreamDecoder::stop() {
  if (worker.joinable()) {
    worker.request_stop();
    worker.join();
  }
}

void BufferedStreamDecoder::checkInterrupted() const {
  if (workerToken.stop_requested() || seekSignal.getValue())
    throw Interrupted{};
}

std::optional<long> BufferedStreamDecoder::streamReadPosition() const {
  const auto width = frameBytes.load();
  return startFrame.load() + (width ? bytesRead.load() / width : 0);
}

void BufferedStreamDecoder::finishIfDrained() {
  const auto state = getState();
  if (buffer.isEof() && !buffer.size() &&
      state.state == AudioGraphNodeState::STREAMING)
    setState({AudioGraphNodeState::FINISHED, *streamReadPosition(),
              state.streamInfo});
}

size_t BufferedStreamDecoder::read(void *dest, size_t size) {
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

size_t BufferedStreamDecoder::waitForData(std::stop_token token, size_t size) {
  auto combined = combineStopTokens(token, seekSignal.getStopToken());
  const auto count = buffer.waitForData(combined.get_token(), size);
  finishIfDrained();
  return count;
}

size_t BufferedStreamDecoder::waitForDataFor(std::stop_token token,
                                             std::chrono::milliseconds timeout,
                                             size_t size) {
  auto combined = combineStopTokens(token, seekSignal.getStopToken());
  const auto count = buffer.waitForDataFor(combined.get_token(), timeout, size);
  finishIfDrained();
  return count;
}

size_t BufferedStreamDecoder::seekTo(size_t frame) {
  if (!worker.joinable() || getState().state == AudioGraphNodeState::ERROR ||
      seekSignal.getValue())
    return size_t(-1);
  auto token = worker.get_stop_token();
  if (!ready.waitValue(token).value_or(false) || !canSeek())
    return size_t(-1);
  seekSignal.sendValue(frame);
  return seekSignal.getResponse(token);
}

void BufferedStreamDecoder::write(std::span<const uint8_t> frames) {
  auto combined = interruption();
  size_t written = 0;
  while (written < frames.size()) {
    auto space = buffer.waitForSpace(combined.get_token(), frameBytes);
    if (combined.get_token().stop_requested())
      throw Interrupted{};
    space -= space % frameBytes;
    written += buffer.write(frames.data() + written,
                            std::min(space, frames.size() - written));
  }
}

size_t BufferedStreamDecoder::readSource(AudioGraphOutputNode &source,
                                         void *dest, size_t bytes) {
  auto combined = interruption();
  auto *p = static_cast<uint8_t *>(dest);
  size_t done = 0;
  while (done < bytes) {
    const auto available = source.waitForData(
        combined.get_token(), std::min(bytes - done, size_t(4096)));
    if (combined.get_token().stop_requested())
      throw Interrupted{};
    const auto state = source.getState();
    if (state.state == AudioGraphNodeState::ERROR)
      throw SourceFailed{state.error.value_or(
          StreamError{StreamErrorSource::DECODER, name() + " input failed"})};
    const auto count =
        available ? source.read(p + done, std::min(bytes - done, available))
                  : 0;
    if (!count)
      break;
    done += count;
  }
  return done;
}

void BufferedStreamDecoder::prepare() {
  info = open();
  frameBytes = info.format.channels * sampleSize(info.format.sampleFormat);
  if (!frameBytes || !info.format.sampleRate)
    throw std::runtime_error(name() + " header describes no audio format");
  if (frameBytes > buffer.max_size())
    throw std::runtime_error(name() + " buffer is smaller than one frame");
  if (!startOffsetMs)
    return;
  if (!canSeek())
    throw std::runtime_error(name() +
                             " start offset requires a seekable stream");
  if (startOffsetMs >
      std::numeric_limits<uint64_t>::max() / info.format.sampleRate)
    throw std::runtime_error(name() + " start offset is too large");
  const auto frame = uint64_t(startOffsetMs) * info.format.sampleRate / 1000;
  if (frame >= info.streamSize)
    throw std::runtime_error(name() + " start offset is past the end");
  seek(frame);
}

void BufferedStreamDecoder::seek(size_t frame) {
  frame = std::min(frame, size_t(info.streamSize));
  setState(StreamState{AudioGraphNodeState::PREPARING});
  buffer.clear();
  buffer.resetEof();
  seekSource(frame);
  startFrame = frame;
  bytesRead = 0;
  setState({AudioGraphNodeState::STREAMING, long(frame), info});
  seekSignal.respond(frame);
}

void BufferedStreamDecoder::run(std::stop_token token) {
  workerToken = token;
  try {
    prepare();
    setState({AudioGraphNodeState::STREAMING, *streamReadPosition(), info});
    ready.sendValue(true);
    while (!token.stop_requested()) {
      try {
        if (auto target = seekSignal.getValue())
          seek(*target);
        if (buffer.isEof())
          seekSignal.waitValue(token);
        else if (!decodeNext())
          buffer.setEof();
      } catch (const Interrupted &) {
      }
    }
  } catch (const Interrupted &) {
  } catch (const SourceFailed &failed) {
    if (!token.stop_requested())
      setState({AudioGraphNodeState::ERROR, failed.error});
  } catch (const std::exception &error) {
    if (!token.stop_requested())
      setState({AudioGraphNodeState::ERROR,
                StreamError{StreamErrorSource::DECODER, error.what()}});
  }
  close();
  seekSignal.close(size_t(-1));
  ready.close(false);
  buffer.setEof();
}
