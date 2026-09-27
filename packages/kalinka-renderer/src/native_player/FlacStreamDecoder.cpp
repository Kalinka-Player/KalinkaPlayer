#include "FlacStreamDecoder.h"
#include "AudioSampleFormat.h"
#include "Log.h"
#include "PerfMon.h"

#include "StateMonitor.h"
#include <functional>
#include <iostream>
#include <pthread.h>
#include <vector>

FlacStreamDecoder::FlacStreamDecoder(std::optional<StreamId> streamId,
                                     size_t bufferSize, size_t startOffsetMs)
    : AudioGraphNode(streamId),
      buffer(bufferSize, std::bind(&FlacStreamDecoder::onEmptyBuffer, this,
                                   std::placeholders::_1)),
      startOffsetMs(startOffsetMs) {
  init();
}

void FlacStreamDecoder::connectTo(
    std::shared_ptr<AudioGraphOutputNode> inputNode) {
  if (inputNode == nullptr) {
    throw std::runtime_error("Input node is null");
  }

  if (this->inputNode != nullptr) {
    throw std::runtime_error("Input node is already connected");
  }

  this->inputNode = inputNode;

  if (!decodingThread.joinable()) {
    setState(StreamState(AudioGraphNodeState::PREPARING));
    seekSignal.reopen();
    decodingThread =
        std::jthread(std::bind_front(&FlacStreamDecoder::thread_run, this));
  }
}

void FlacStreamDecoder::disconnect(
    std::shared_ptr<AudioGraphOutputNode> inputNode) {
  if (inputNode != this->inputNode) {
    return;
  }

  if (decodingThread.joinable()) {
    decodingThread.request_stop();
    decodingThread.join();
  }

  this->inputNode = nullptr;
}

namespace {
constexpr AudioSampleFormat bitsToFormat(int bits) {
  if (bits == 16) {
    return AudioSampleFormat::PCM16_LE;
  } else if (bits == 24) {
    return AudioSampleFormat::PCM24_LE;
  } else if (bits == 32) {
    throw std::runtime_error("32-bit PCM not supported");
  }

  throw std::runtime_error("Unsupported bits depth per sample");
}
} // namespace

::FLAC__StreamDecoderWriteStatus
FlacStreamDecoder::write_callback(const ::FLAC__Frame *frame,
                                  const FLAC__int32 *const buffer[]) {
  const auto blockSize = frame->header.blocksize;
  const auto blockNum = frame->header.channels;
  if (blockNum != 2) {
    return FLAC__STREAM_DECODER_WRITE_STATUS_ABORT;
  }

  AudioSampleFormat format = bitsToFormat(frame->header.bits_per_sample);
  auto const blockSizeInBytes = sampleSize(format) * blockSize * blockNum;

  if (blockSizeInBytes > data.size()) {
    data.resize(blockSizeInBytes);
  }
  convertToFormat(data.data(), buffer, blockSize, format);

  size_t bytesWritten = 0;
  auto combinedStopToken = combineStopTokens(decodingThread.get_stop_token(),
                                             seekSignal.getStopToken());

  while (bytesWritten < blockSizeInBytes) {
    auto spaceAvailable =
        this->buffer.waitForSpace(combinedStopToken.get_token());

    if (seekSignal.getStopToken().stop_requested()) {
      break;
    }

    if (decodingThread.get_stop_token().stop_requested()) {
      return FLAC__STREAM_DECODER_WRITE_STATUS_ABORT;
    }

    perfmon_begin("FlacStreamDecoder::write_callback-write");
    bytesWritten += this->buffer.write(
        reinterpret_cast<uint8_t *>(data.data()) + bytesWritten,
        std::min(spaceAvailable, blockSizeInBytes - bytesWritten));
    perfmon_end("FlacStreamDecoder::write_callback-write");
  }

  return FLAC__STREAM_DECODER_WRITE_STATUS_CONTINUE;
}
::FLAC__StreamDecoderReadStatus
FlacStreamDecoder::read_callback(FLAC__byte buffer[], size_t *bytes) {
  auto combinedStopToken = combineStopTokens(seekSignal.getStopToken(),
                                             decodingThread.get_stop_token());
  inputNode->waitForData(combinedStopToken.get_token(), *bytes);
  *bytes = inputNode->read(buffer, *bytes);
  sourceStreamPosition += *bytes;
  if (decodingThread.get_stop_token().stop_requested()) {
    return FLAC__STREAM_DECODER_READ_STATUS_ABORT;
  }
  auto inputNodeState = inputNode->getState();
  auto inputNodeStatus = inputNodeState.state;
  if (inputNodeStatus == AudioGraphNodeState::ERROR) {
    setState({AudioGraphNodeState::ERROR, *inputNodeState.error});
    return FLAC__STREAM_DECODER_READ_STATUS_ABORT;
  }
  if (inputNodeStatus == AudioGraphNodeState::FINISHED && *bytes == 0) {
    return FLAC__STREAM_DECODER_READ_STATUS_END_OF_STREAM;
  }

  return FLAC__STREAM_DECODER_READ_STATUS_CONTINUE;
}

void FlacStreamDecoder::error_callback(
    ::FLAC__StreamDecoderErrorStatus status) {
  setState({AudioGraphNodeState::ERROR,
            StreamError{StreamErrorSource::DECODER,
                        std::string("Flac decoder error: ") +
                            FLAC__StreamDecoderErrorStatusString[status]}});
}

void FlacStreamDecoder::metadata_callback(
    const ::FLAC__StreamMetadata *metadata) {
  if (metadata->type == FLAC__METADATA_TYPE_STREAMINFO) {
    flacStreamInfo = metadata->data.stream_info;
    frameSizeBytes = sampleSize(bitsToFormat(flacStreamInfo->bits_per_sample)) *
                     flacStreamInfo->channels;

    if (inputNode->getState().streamInfo.has_value()) {
      sourceStreamLength = inputNode->getState().streamInfo.value().streamSize;
    }
  }
}

::FLAC__StreamDecoderSeekStatus
FlacStreamDecoder::seek_callback(FLAC__uint64 absolute_byte_offset) {
  auto ret = inputNode->seekTo(absolute_byte_offset);
  if (ret == (size_t)-1) {
    return FLAC__STREAM_DECODER_SEEK_STATUS_UNSUPPORTED;
  }
  sourceStreamPosition = ret;
  return FLAC__STREAM_DECODER_SEEK_STATUS_OK;
}

::FLAC__StreamDecoderTellStatus
FlacStreamDecoder::tell_callback(FLAC__uint64 *absolute_byte_offset) {
  *absolute_byte_offset = sourceStreamPosition;
  return FLAC__STREAM_DECODER_TELL_STATUS_OK;
}

::FLAC__StreamDecoderLengthStatus
FlacStreamDecoder::length_callback(FLAC__uint64 *stream_length) {
  if (sourceStreamLength) {
    *stream_length = *sourceStreamLength;
    return FLAC__STREAM_DECODER_LENGTH_STATUS_OK;
  }
  return FLAC__STREAM_DECODER_LENGTH_STATUS_ERROR;
}

bool FlacStreamDecoder::eof_callback() {
  auto state = inputNode->getState().state;
  return state == AudioGraphNodeState::FINISHED;
}

void FlacStreamDecoder::throwOnFlacError(bool retval) {
  if (!retval) {
    State state = get_state();
    switch (state) {
    case FLAC__STREAM_DECODER_OGG_ERROR:
    case FLAC__STREAM_DECODER_SEEK_ERROR:
    case FLAC__STREAM_DECODER_MEMORY_ALLOCATION_ERROR:
      throw std::runtime_error("Flac decoder error: " +
                               std::string(state.as_cstring()));
    default:
      break;
    }
  }
}

long FlacStreamDecoder::framesRead() const {
  const auto frameSize = frameSizeBytes.load();
  if (frameSize == 0) {
    return runStartFrame.load(); // no format yet, so nothing has been read
  }
  return runStartFrame.load() + bytesReadInRun.load() / frameSize;
}

std::optional<long> FlacStreamDecoder::streamReadPosition() const {
  return framesRead();
}

void FlacStreamDecoder::restartRunAt(long frame) {
  runStartFrame = frame;
  bytesReadInRun = 0;
}

void FlacStreamDecoder::setStreamingState() {
  if (!flacStreamInfo.has_value()) {
    throw std::runtime_error("Stream info not available");
  }

  StreamInfo streamInfo = {
      .format =
          StreamAudioFormat{
              .sampleRate = flacStreamInfo.value().sample_rate,
              .channels = flacStreamInfo.value().channels,
              .bitsPerSample = flacStreamInfo.value().bits_per_sample,
              .sampleFormat =
                  bitsToFormat(flacStreamInfo.value().bits_per_sample)},
      .streamType = StreamType::FRAMES,
      .streamSize = flacStreamInfo.value().total_samples};

  setState({AudioGraphNodeState::STREAMING, framesRead(), streamInfo});
}

void FlacStreamDecoder::processStartOffset() {
  if (startOffsetMs > 0) {
    if (!flacStreamInfo.has_value()) {
      throw std::runtime_error("Stream info not available");
    }
    auto startOffsetSamples =
        (startOffsetMs * flacStreamInfo.value().sample_rate) / 1000;
    if (startOffsetSamples >= flacStreamInfo.value().total_samples) {
      throw std::runtime_error(
          "Start offset is greater than the total number of samples");
    }
    handleSeekSignal(startOffsetSamples);
  }
}

void FlacStreamDecoder::handleSeekSignal(size_t position) {
  auto state = get_state();
  if (state == FLAC__STREAM_DECODER_SEEK_ERROR) {
    if (!flush()) {
      spdlog::warn("FlacDecoder flush failed");
    }
  }
  buffer.clear();
  if (position >= flacStreamInfo.value().total_samples) {
    // Answered with where it landed, not what was asked for, as a seek
    // anywhere else is.
    restartRunAt(flacStreamInfo.value().total_samples);
    seekSignal.respond(flacStreamInfo.value().total_samples);
    return;
  }

  buffer.resetEof();
  setState(StreamState{AudioGraphNodeState::PREPARING});
  seekSignal.respond(position);
  if (!seek_absolute(position)) {
    spdlog::warn("Seek failed, offset={}, state={}", position,
                 FLAC__StreamDecoderStateString[get_state()]);
  } else {
    restartRunAt(position);
  }
  setStreamingState();
}

void FlacStreamDecoder::thread_run(std::stop_token token) {
  pthread_setname_np(pthread_self(), "FlacDecoder");
  try {
    if (get_state() == FLAC__STREAM_DECODER_UNINITIALIZED) {
      throw std::runtime_error("Flac decoder not initialized");
    }
    if (token.stop_requested()) {
      return;
    }
    bool retval = process_until_end_of_metadata();
    throwOnFlacError(retval);

    // Absent metadata, the stream never began: leave the error that says so
    // rather than replacing it with one about the metadata.
    if (flacStreamInfo.has_value()) {
      processStartOffset();
      setStreamingState();
    }

    while (!token.stop_requested()) {
      while (!token.stop_requested()) {
        State state = get_state();
        if (state == FLAC__STREAM_DECODER_END_OF_STREAM ||
            state == FLAC__STREAM_DECODER_ABORTED) {
          break;
        }
        retval = process_single();
        throwOnFlacError(retval);

        if (seekSignal.getValue().has_value()) {
          handleSeekSignal(*seekSignal.getValue());
          if (static_cast<FLAC__uint64>(framesRead()) ==
              flacStreamInfo.value().total_samples) {
            break;
          }
        }
      }

      buffer.setEof();
      auto seekValue = seekSignal.waitValue(token);
      if (!seekValue) {
        break;
      }

      handleSeekSignal(*seekValue);
    }
  } catch (std::exception &ex) {
    if (getState().state != AudioGraphNodeState::ERROR) {
      std::string message =
          std::string("Flac decoder thread exception: ") + ex.what();
      spdlog::warn(message);
      setState({AudioGraphNodeState::ERROR,
                StreamError{StreamErrorSource::DECODER, message}});
    }
  }
  seekSignal.close(static_cast<size_t>(-1));
  buffer.setEof();
  setState(StreamState{AudioGraphNodeState::STOPPED});
}

void FlacStreamDecoder::onEmptyBuffer(Buffer<uint8_t> &buffer) {
  auto currentState = getState();
  if (buffer.isEof() && currentState.state != AudioGraphNodeState::ERROR) {
    setState(StreamState{AudioGraphNodeState::FINISHED, framesRead(),
                         currentState.streamInfo});
  }
}

FlacStreamDecoder::~FlacStreamDecoder() {
  if (decodingThread.joinable()) {
    decodingThread.request_stop();
    decodingThread.join();
    decodingThread = std::jthread();
  }
}

size_t FlacStreamDecoder::read(void *data, size_t size) {
  perfmon_begin("FlacStreamDecoder::read");
  auto readBytes = buffer.read(static_cast<uint8_t *>(data), size);
  bytesReadInRun += readBytes;
  perfmon_end("FlacStreamDecoder::read");
  return readBytes;
}
size_t FlacStreamDecoder::waitForData(std::stop_token stopToken, size_t size) {
  auto combinedToken = combineStopTokens(stopToken, seekSignal.getStopToken());
  return buffer.waitForData(combinedToken.get_token(), size);
}

size_t FlacStreamDecoder::waitForDataFor(std::stop_token stopToken,
                                         std::chrono::milliseconds timeout,
                                         size_t size) {
  auto combinedToken = combineStopTokens(stopToken, seekSignal.getStopToken());
  return buffer.waitForDataFor(combinedToken.get_token(), timeout, size);
}

size_t FlacStreamDecoder::seekTo(size_t absolutePosition) {
  if (getState().state == AudioGraphNodeState::ERROR || seekSignal.getValue()) {
    return -1;
  }

  spdlog::trace("FlacStreamDecoder::seekTo({})", absolutePosition);
  seekSignal.sendValue(absolutePosition);
  auto retVal = seekSignal.getResponse(decodingThread.get_stop_token());
  spdlog::trace("FlacStreamDecoder::seekTo({}) -> {}", absolutePosition,
                retVal);
  return retVal;
}
