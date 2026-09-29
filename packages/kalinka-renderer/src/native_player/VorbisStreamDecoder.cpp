#include "VorbisStreamDecoder.h"

#include <algorithm>
#include <array>
#include <cerrno>
#include <climits>
#include <limits>
#include <pthread.h>
#include <stdexcept>

VorbisStreamDecoder::VorbisStreamDecoder(std::optional<StreamId> streamId,
                                         size_t bufferSize,
                                         size_t startOffsetMs)
    : AudioGraphNode(streamId), buffer(bufferSize),
      startOffsetMs(startOffsetMs) {
  if (bufferSize == 0) {
    throw std::invalid_argument("Vorbis decoder buffer must not be empty");
  }
}

VorbisStreamDecoder::~VorbisStreamDecoder() {
  if (decodingThread.joinable()) {
    decodingThread.request_stop();
    decodingThread.join();
  }
}

void VorbisStreamDecoder::connectTo(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (!source) {
    throw std::invalid_argument("Input node is null");
  }
  if (inputNode) {
    throw std::runtime_error("Input node is already connected");
  }
  inputNode = std::move(source);
  buffer.resetEof();
  buffer.clear();
  seekSignal.reopen();
  initCompleteSignal.reopen();
  seekable = false;
  runStartFrame = 0;
  bytesReadInRun = 0;
  frameSizeBytes = 0;
  sourcePosition = 0;
  sourceLength.reset();
  callbackException = nullptr;
  interruptReadForSeek = false;
  resetBeforeSeek = false;
  setState(StreamState{AudioGraphNodeState::PREPARING});
  decodingThread =
      std::jthread([this](std::stop_token token) { threadRun(token); });
}

void VorbisStreamDecoder::disconnect(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (source != inputNode) {
    return;
  }
  if (decodingThread.joinable()) {
    decodingThread.request_stop();
    decodingThread.join();
  }
  inputNode.reset();
}

long VorbisStreamDecoder::framesRead() const {
  const auto frameSize = frameSizeBytes.load();
  return runStartFrame.load() +
         (frameSize ? bytesReadInRun.load() / frameSize : 0);
}

std::optional<long> VorbisStreamDecoder::streamReadPosition() const {
  return framesRead();
}

void VorbisStreamDecoder::finishIfDrained() {
  if (buffer.isEof() && buffer.size() == 0) {
    const auto state = getState();
    if (state.state == AudioGraphNodeState::STREAMING) {
      setState({AudioGraphNodeState::FINISHED, framesRead(), state.streamInfo});
    }
  }
}

size_t VorbisStreamDecoder::read(void *data, size_t size) {
  const auto bytes = buffer.read(static_cast<uint8_t *>(data), size);
  bytesReadInRun += bytes;
  finishIfDrained();
  return bytes;
}

size_t VorbisStreamDecoder::waitForData(std::stop_token token, size_t size) {
  auto combined = combineStopTokens(token, seekSignal.getStopToken());
  const auto available = buffer.waitForData(combined.get_token(), size);
  finishIfDrained();
  return available;
}

size_t VorbisStreamDecoder::waitForDataFor(std::stop_token token,
                                           std::chrono::milliseconds timeout,
                                           size_t size) {
  auto combined = combineStopTokens(token, seekSignal.getStopToken());
  const auto available =
      buffer.waitForDataFor(combined.get_token(), timeout, size);
  finishIfDrained();
  return available;
}

size_t VorbisStreamDecoder::seekTo(size_t position) {
  if (!decodingThread.joinable() ||
      getState().state == AudioGraphNodeState::ERROR || seekSignal.getValue()) {
    return static_cast<size_t>(-1);
  }
  const auto token = decodingThread.get_stop_token();
  if (!initCompleteSignal.waitValue(token).value_or(false) || !seekable) {
    return static_cast<size_t>(-1);
  }
  seekSignal.sendValue(position);
  const auto result = seekSignal.getResponse(token);
  return token.stop_requested() ? static_cast<size_t>(-1) : result;
}

size_t VorbisStreamDecoder::readInput(void *data, size_t size, size_t count) {
  errno = 0;
  if (size == 0 || count == 0) {
    return 0;
  }
  if (count > std::numeric_limits<size_t>::max() / size) {
    throw std::runtime_error("Vorbis input read is too large");
  }
  auto combined = combineStopTokens(workerToken, seekSignal.getStopToken());
  const auto token = interruptReadForSeek ? combined.get_token() : workerToken;
  // A live producer can wait for playback before sending its next page.
  // Waiting for libvorbisfile's full read buffer would deadlock even when a
  // complete page is already available. Return available whole items; only a
  // zero-length read signals EOF to libvorbisfile.
  const auto available = inputNode->waitForData(token, size);
  if (token.stop_requested()) {
    return 0;
  }
  const auto state = inputNode->getState();
  if (state.state == AudioGraphNodeState::ERROR) {
    setState({AudioGraphNodeState::ERROR,
              state.error.value_or(StreamError{StreamErrorSource::DECODER,
                                               "Vorbis input failed"})});
    errno = EIO;
    return 0;
  }
  if (state.streamInfo && state.streamInfo->streamSize > 0) {
    sourceLength = state.streamInfo->streamSize;
  }
  if (available == 0) {
    return 0;
  }
  // libvorbisfile requests bytes (size == 1); preserve fread's item count.
  const auto bytes = inputNode->read(data, std::min(available, size * count));
  sourcePosition += bytes;
  return bytes / size;
}

size_t VorbisStreamDecoder::readCallback(void *data, size_t size, size_t count,
                                         void *source) noexcept {
  auto &self = *static_cast<VorbisStreamDecoder *>(source);
  try {
    return self.readInput(data, size, count);
  } catch (...) {
    self.callbackException = std::current_exception();
    errno = EIO;
    return 0;
  }
}

int VorbisStreamDecoder::seekInput(ogg_int64_t offset, int whence) {
  // Without a length, SEEK_END cannot be implemented. Returning failure to
  // the initial seek probe makes libvorbisfile use sequential decoding.
  if (!sourceLength || *sourceLength > LONG_MAX ||
      workerToken.stop_requested()) {
    return -1;
  }
  ogg_int64_t base;
  switch (whence) {
  case SEEK_SET:
    base = 0;
    break;
  case SEEK_CUR:
    base = sourcePosition;
    break;
  case SEEK_END:
    base = *sourceLength;
    break;
  default:
    return -1;
  }
  if (offset < -base ||
      offset > static_cast<ogg_int64_t>(*sourceLength) - base) {
    return -1;
  }
  const auto position = static_cast<size_t>(base + offset);
  if (inputNode->seekTo(position) != position) {
    return -1;
  }
  sourcePosition = position;
  return 0;
}

int VorbisStreamDecoder::seekCallback(void *source, ogg_int64_t offset,
                                      int whence) noexcept {
  auto &self = *static_cast<VorbisStreamDecoder *>(source);
  try {
    return self.seekInput(offset, whence);
  } catch (...) {
    self.callbackException = std::current_exception();
    return -1;
  }
}

long VorbisStreamDecoder::tellCallback(void *source) noexcept {
  const auto position =
      static_cast<VorbisStreamDecoder *>(source)->sourcePosition;
  return position > LONG_MAX ? -1 : static_cast<long>(position);
}

void VorbisStreamDecoder::checkResult(long result, const char *operation) {
  if (callbackException) {
    std::rethrow_exception(callbackException);
  }
  if (result < 0 || getState().state == AudioGraphNodeState::ERROR) {
    throw std::runtime_error(std::string("Vorbis ") + operation + " failed (" +
                             std::to_string(result) + ")");
  }
}

void VorbisStreamDecoder::validateFormat(const vorbis_info *info) const {
  if (!info || info->rate <= 0 || info->channels < 1 || info->channels > 2) {
    throw std::runtime_error("Vorbis decoder supports mono and stereo audio");
  }
  if (static_cast<unsigned long>(info->rate) != streamInfo.format.sampleRate ||
      static_cast<unsigned int>(info->channels) != streamInfo.format.channels) {
    throw std::runtime_error(
        "Vorbis chained streams must use the same audio format");
  }
}

void VorbisStreamDecoder::seek(OggVorbis_File &file, size_t position) {
  position = std::min(position, static_cast<size_t>(streamInfo.streamSize));
  setState(StreamState{AudioGraphNodeState::PREPARING});
  buffer.resetEof();
  buffer.clear();
  // The control thread must be free to stop playback while input is stalled.
  seekSignal.respond(position);
  interruptReadForSeek = true;
  if (resetBeforeSeek) {
    // An interrupted PCM seek can leave libvorbisfile's decode state cleared.
    const auto result = ov_raw_seek(&file, 0);
    if (workerToken.stop_requested() || seekSignal.getValue()) {
      return;
    }
    checkResult(result, "reset seek");
    resetBeforeSeek = false;
  }
  const auto result = ov_pcm_seek(&file, static_cast<ogg_int64_t>(position));
  if (workerToken.stop_requested() || seekSignal.getValue()) {
    resetBeforeSeek = true;
    return;
  }
  checkResult(result, "seek");
  runStartFrame = position;
  bytesReadInRun = 0;
  setState({AudioGraphNodeState::STREAMING, framesRead(), streamInfo});
}

void VorbisStreamDecoder::threadRun(std::stop_token token) {
  pthread_setname_np(pthread_self(), "VorbisDecoder");
  workerToken = token;
  OggVorbis_File file{};
  bool opened = false;
  try {
    // The seekability probe runs before libvorbisfile's first read. Prime the
    // input so an HTTP source has received its headers and published length.
    std::array<char, 4096> initial;
    const auto initialBytes = readInput(initial.data(), 1, initial.size());
    const ov_callbacks callbacks{readCallback, seekCallback, nullptr,
                                 tellCallback};
    const int result =
        ov_open_callbacks(this, &file, initial.data(), initialBytes, callbacks);
    opened = result == 0;
    checkResult(result, "open");
    const auto *info = ov_info(&file, -1);
    if (!info) {
      throw std::runtime_error("Vorbis stream info is missing");
    }
    seekable = ov_seekable(&file) != 0;
    const auto totalFrames = seekable ? ov_pcm_total(&file, -1) : 0;
    checkResult(totalFrames, "length");
    streamInfo = {
        .format = {.sampleRate = static_cast<unsigned int>(info->rate),
                   .channels = static_cast<unsigned int>(info->channels),
                   .bitsPerSample = 16,
                   .sampleFormat = AudioSampleFormat::PCM16_LE},
        .streamType = StreamType::FRAMES,
        .streamSize = static_cast<unsigned long>(totalFrames)};
    validateFormat(info);
    if (seekable) {
      for (int link = 0; link < ov_streams(&file); ++link) {
        validateFormat(ov_info(&file, link));
      }
    }
    frameSizeBytes = streamInfo.format.channels * sizeof(int16_t);
    if (startOffsetMs > 0) {
      if (!seekable) {
        throw std::runtime_error(
            "Vorbis start offset requires a seekable stream");
      }
      if (startOffsetMs >
          std::numeric_limits<uint64_t>::max() / streamInfo.format.sampleRate) {
        throw std::runtime_error("Vorbis start offset is too large");
      }
      const auto frame = static_cast<uint64_t>(startOffsetMs) *
                         streamInfo.format.sampleRate / 1000;
      if (frame >= streamInfo.streamSize) {
        throw std::runtime_error(
            "Vorbis start offset is past the end of the stream");
      }
      seek(file, frame);
    }
    setState({AudioGraphNodeState::STREAMING, framesRead(), streamInfo});
    initCompleteSignal.sendValue(true);

    std::array<char, 8192> pcm;
    while (!token.stop_requested()) {
      if (const auto position = seekSignal.getValue()) {
        seek(file, *position);
        continue;
      }
      interruptReadForSeek = true;
      int section = 0;
      const auto bytes =
          ov_read(&file, pcm.data(), pcm.size(), 0, 2, 1, &section);
      if (token.stop_requested() || seekSignal.getValue()) {
        continue;
      }
      // A missing/corrupt page is recoverable; libvorbisfile has advanced to
      // the next page, so continue decoding there.
      if (bytes == OV_HOLE) {
        continue;
      }
      checkResult(bytes, "decode");
      if (bytes == 0) {
        buffer.setEof();
        seekSignal.waitValue(token);
        continue;
      }
      validateFormat(ov_info(&file, -1));
      size_t written = 0;
      auto combined = combineStopTokens(token, seekSignal.getStopToken());
      while (written < static_cast<size_t>(bytes)) {
        const auto space = buffer.waitForSpace(combined.get_token());
        if (combined.get_token().stop_requested()) {
          break;
        }
        written += buffer.write(
            reinterpret_cast<const uint8_t *>(pcm.data()) + written,
            std::min(space, static_cast<size_t>(bytes) - written));
      }
    }
  } catch (const std::exception &ex) {
    if (!token.stop_requested() &&
        getState().state != AudioGraphNodeState::ERROR) {
      setState({AudioGraphNodeState::ERROR,
                StreamError{StreamErrorSource::DECODER, ex.what()}});
    }
  }
  if (opened) {
    ov_clear(&file);
  }
  seekSignal.close(static_cast<size_t>(-1));
  initCompleteSignal.close(false);
  buffer.setEof();
  if (getState().state != AudioGraphNodeState::ERROR) {
    setState(StreamState{AudioGraphNodeState::STOPPED});
  }
}
