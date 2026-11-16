#define MINIMP3_IMPLEMENTATION
#define MINIMP3_ONLY_SIMD

#include "Mp3StreamDecoder.h"
#include "Log.h"
#include "PerfMon.h"

#include "minimp3/minimp3_ex.h"

#include <span>

namespace {
bool atId3Trailer(const mp3dec_ex_t &decoder, uint64_t sourcePosition,
                  uint64_t sourceSize) {
  // A failed frame search may consume the trailer and the last audio frame
  // together. Locate that search's input, which minimp3 still holds, rather
  // than trusting its sample estimate (the scan can omit frames near tags).
  if (sourcePosition < decoder.input_filled)
    return false;
  const auto bufferStart = sourcePosition - decoder.input_filled;
  if (decoder.offset < bufferStart || decoder.offset >= sourcePosition)
    return false;
  auto offset = decoder.offset;
  auto tail = std::span(decoder.file.buffer, decoder.input_filled)
                  .subspan(offset - bufferStart);
  if (tail.size() >= HDR_SIZE && hdr_valid(tail.data())) {
    const auto *header = tail.data();
    if (hdr_sample_rate_hz(header) != unsigned(decoder.info.hz) ||
        4 - HDR_GET_LAYER(header) != decoder.info.layer ||
        (HDR_IS_MONO(header) ? 1 : 2) != decoder.info.channels)
      return false;
    const auto bytes = hdr_frame_bytes(header, decoder.free_format_bytes) +
                       hdr_padding(header);
    if (bytes <= 0 || size_t(bytes) > tail.size())
      return false;
    tail = tail.subspan(bytes);
    offset += bytes;
  }
  if (tail.size() < MINIMP3_ID3_DETECT_SIZE || tail[3] < 2 || tail[3] > 4 ||
      tail[4] == 0xff || (tail[3] != 4 && (tail[5] & 0x10)))
    return false;
  const auto tagBytes = mp3dec_skip_id3v2(tail.data(), tail.size());
  return tagBytes && offset <= sourceSize && tagBytes == sourceSize - offset;
}

struct GaplessInfo {
  uint64_t remainingFrames = 0;
  unsigned delay = 0;
  unsigned padding = 0;

  std::pair<size_t, size_t> trim(size_t frames) {
    const auto skip = std::min<size_t>(delay, frames);
    delay -= skip;
    auto end = frames;
    if (remainingFrames) {
      end = std::min<uint64_t>(
          frames, remainingFrames - std::min<uint64_t>(remainingFrames, padding));
      remainingFrames -= std::min<uint64_t>(remainingFrames, frames);
    }
    // The tag describes this encoded track, not the lifetime of a broadcast.
    // If audio continues beyond it, keep playing without trimming that audio.
    return {skip, end > skip ? end - skip : 0};
  }
};

// The same Xing/Info and LAME/Lavc fields used by minimp3's file API, read
// within this frame's bounds. No duration scan, rewind or PCM tail buffer.
std::optional<GaplessInfo> readGaplessTag(std::span<const uint8_t> frame) {
  if (frame.size() < HDR_SIZE)
    return std::nullopt;
  const auto *header = frame.data();
  size_t at = HDR_SIZE + (HDR_IS_CRC(header) ? 2 : 0) +
              (HDR_TEST_MPEG1(header) ? (HDR_IS_MONO(header) ? 17 : 32)
                                      : (HDR_IS_MONO(header) ? 9 : 17));
  if (frame.size() < at + 8 ||
      (std::memcmp(header + at, "Xing", 4) &&
       std::memcmp(header + at, "Info", 4)))
    return std::nullopt;
  const auto flags = frame[at + 7];
  if (!(flags & FRAMES_FLAG))
    return GaplessInfo{}; // A tag without a length still isn't audio.
  at += 8;
  if (frame.size() < at + 4)
    return std::nullopt;
  const auto count = uint32_t(frame[at]) << 24 | uint32_t(frame[at + 1]) << 16 |
                     uint32_t(frame[at + 2]) << 8 | frame[at + 3];
  GaplessInfo info{uint64_t(count) * hdr_frame_samples(header)};
  at += 4 + (flags & BYTES_FLAG ? 4 : 0) + (flags & TOC_FLAG ? 100 : 0) +
        (flags & VBR_SCALE_FLAG ? 4 : 0);
  if (at >= frame.size())
    return std::nullopt;
  if (frame[at]) {
    if (frame.size() - at < 36)
      return std::nullopt;
    at += 21;
    // minimp3's synthesis delay is 529 samples in addition to the encoder's.
    info.delay = (unsigned(frame[at]) << 4 | frame[at + 1] >> 4) + 529;
    info.padding = std::max(0, ((frame[at + 1] & 15) << 8 | frame[at + 2]) - 529);
  }
  if (uint64_t(info.delay) + info.padding > info.remainingFrames)
    return GaplessInfo{}; // Ignore inconsistent lengths, but skip the tag.
  return info;
}
} // namespace

Mp3StreamDecoder::Mp3StreamDecoder(std::optional<StreamId> streamId,
                                   size_t bufferSize, size_t startOffsetMs)
    : AudioGraphNode(streamId),
      buffer(bufferSize),
      startOffsetMs(startOffsetMs) {}

Mp3StreamDecoder::~Mp3StreamDecoder() {
  if (decodingThread.joinable()) {
    decodingThread.request_stop();
    decodingThread.join();
    decodingThread = std::jthread();
  }
}

size_t Mp3StreamDecoder::read(void *data, size_t size) {
  auto readBytes = buffer.read(static_cast<uint8_t *>(data), size);
  bytesReadInRun += readBytes;
  finishIfDrained();
  return readBytes;
}

long Mp3StreamDecoder::framesRead() const {
  const auto frameSize = frameSizeBytes.load();
  if (frameSize == 0) {
    return runStartFrame.load(); // not open yet, so nothing has been read
  }
  return runStartFrame.load() + bytesReadInRun.load() / frameSize;
}

std::optional<long> Mp3StreamDecoder::streamReadPosition() const {
  return framesRead();
}

void Mp3StreamDecoder::restartRunAt(long frame) {
  runStartFrame = frame;
  bytesReadInRun = 0;
}

size_t Mp3StreamDecoder::waitForData(std::stop_token stopToken, size_t size) {
  auto combinedToken = combineStopTokens(stopToken, seekSignal.getStopToken());
  auto availableSize = buffer.waitForData(combinedToken.get_token(), size);
  finishIfDrained();
  return availableSize;
}

size_t Mp3StreamDecoder::waitForDataFor(std::stop_token stopToken,
                                        std::chrono::milliseconds timeout,
                                        size_t size) {
  auto combinedToken = combineStopTokens(stopToken, seekSignal.getStopToken());
  const auto availableSize =
      buffer.waitForDataFor(combinedToken.get_token(), timeout, size);
  finishIfDrained();
  return availableSize;
}

size_t Mp3StreamDecoder::seekTo(size_t absolutePosition) {
  if (getState().state == AudioGraphNodeState::ERROR || seekSignal.getValue()) {
    return -1;
  }
  auto token = decodingThread.get_stop_token();

  if (!initCompleteSignal.waitValue(token).value_or(false) || !seekable) {
    return -1;
  }
  spdlog::trace("Mp3StreamDecoder::seekTo({})", absolutePosition);
  seekSignal.sendValue(absolutePosition);
  auto retVal = seekSignal.getResponse(token);
  spdlog::trace("Mp3StreamDecoder::seekTo({}) -> {}", absolutePosition, retVal);
  return retVal;
}

void Mp3StreamDecoder::connectTo(
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
    initCompleteSignal.reopen();
    decodingThread =
        std::jthread(std::bind_front(&Mp3StreamDecoder::threadRun, this));
  }
}

void Mp3StreamDecoder::disconnect(
    std::shared_ptr<AudioGraphOutputNode> outputNode) {
  if (outputNode != this->inputNode) {
    return;
  }

  if (decodingThread.joinable()) {
    decodingThread.request_stop();
    decodingThread.join();
  }

  this->inputNode = nullptr;
}

void Mp3StreamDecoder::threadRun(std::stop_token token) {

  mp3dec_ex_t mp3{};
  mp3dec_io_t io = {
      .read =
          [](void *buf, size_t size, void *user_data) {
            return static_cast<Mp3StreamDecoder *>(user_data)->readCallback(
                buf, size);
          },
      .read_data = this,
      .seek =
          [](uint64_t position, void *user_data) {
            return static_cast<Mp3StreamDecoder *>(user_data)->seekCallback(
                position);
          },
      .seek_data = this};

  try {

    // HTTP headers are only available once the source has delivered bytes.
    inputNode->waitForData(token, 1);
    const auto source = inputNode->getState();
    if (source.state == AudioGraphNodeState::ERROR) {
      setState({AudioGraphNodeState::ERROR, *source.error});
    } else if (!token.stop_requested() &&
               (!source.streamInfo || !source.streamInfo->streamSize)) {
      decodeStream(token);
    } else if (!token.stop_requested()) {
      seekable = true;

      int ret = mp3dec_ex_open_cb(&mp3, &io, MP3D_SEEK_TO_SAMPLE);
      spdlog::trace("mp3dec_ex_open_cb ret: {}", ret);

      if (ret != 0) {
        initCompleteSignal.sendValue(false);
        spdlog::error("Failed to open MP3 decoder: {}", ret);
        throw std::runtime_error("Failed to open MP3 decoder");
      };

      if (!mp3.info.channels || !mp3.info.hz)
        throw std::runtime_error("No MP3 audio found");
      frameSizeBytes = sizeof(mp3d_sample_t) * mp3.info.channels;
      initCompleteSignal.sendValue(true);

      constexpr size_t frameBufferSize = MINIMP3_MAX_SAMPLES_PER_FRAME;
      std::array<mp3d_sample_t, frameBufferSize> frameBuffer;

      if (startOffsetMs > 0) {
        auto startOffsetSamples =
            static_cast<size_t>(startOffsetMs * mp3.info.hz / 1000);
        if (startOffsetSamples > mp3.samples / mp3.info.channels) {
          throw std::runtime_error(
              "Start offset is greater than the total number of samples");
        }
        handleSeekSignal(&mp3, startOffsetSamples);
      }

      while (!token.stop_requested()) {
        while (!token.stop_requested()) {
          if (seekSignal.getValue().has_value()) {
            handleSeekSignal(&mp3, seekSignal.getValue().value());
            continue;
          }
          size_t size =
              mp3dec_ex_read(&mp3, frameBuffer.data(), frameBufferSize);

          if (seekSignal.getValue().has_value()) {
            continue;
          }

          if (size == 0) {
            // minimp3 also reports E_DECODE for non-audio trailers such as
            // ID3v2.4. Accept a tag covering the remainder of the file, while
            // preserving actual audio decoding and source failures.
            const bool trailer = mp3.last_error == MP3D_E_DECODE &&
                                 atId3Trailer(mp3, sourcePosition,
                                               source.streamInfo->streamSize);
            if (mp3.last_error && !trailer && !token.stop_requested()) {
              throw std::runtime_error("MP3 decode failure: " +
                                       std::to_string(mp3.last_error));
            }
            buffer.setEof();
            finishIfDrained();
            break;
          }

          setState(StreamState{
              AudioGraphNodeState::STREAMING, framesRead(),
              StreamInfo{
                  .format =
                      StreamAudioFormat{
                          .sampleRate = static_cast<unsigned int>(mp3.info.hz),
                          .channels =
                              static_cast<unsigned int>(mp3.info.channels),
                          .bitsPerSample = 16,
                          .sampleFormat = AudioSampleFormat::PCM16_LE},
                  .streamType = StreamType::FRAMES,
                  .streamSize = mp3.samples / mp3.info.channels}});

          writePcm(frameBuffer.data(), size * sizeof(mp3d_sample_t), token);
        }
        seekSignal.waitValue(token);
      }
    }
  } catch (std::exception &ex) {
    if (getState().state != AudioGraphNodeState::ERROR) {
      std::string message =
          std::string("Mp3 decoder thread exception: ") + ex.what();
      spdlog::error(message);
      setState({AudioGraphNodeState::ERROR,
                StreamError{StreamErrorSource::DECODER, message}});
    }
  }

  mp3dec_ex_close(&mp3);
  buffer.setEof();
  finishIfDrained();
  seekSignal.close(static_cast<size_t>(-1));
  initCompleteSignal.close(false);
  if (token.stop_requested() && getState().state != AudioGraphNodeState::ERROR)
    setState(StreamState{AudioGraphNodeState::STOPPED});
  spdlog::trace("Mp3StreamDecoder::threadRun finished");
}

void Mp3StreamDecoder::writePcm(const void *data, size_t size,
                                std::stop_token token) {
  auto combined = combineStopTokens(token, seekSignal.getStopToken());
  size_t written = 0;
  while (written < size) {
    buffer.waitForSpace(combined.get_token());
    if (combined.get_token().stop_requested())
      return;
    written += buffer.write(static_cast<const uint8_t *>(data) + written,
                            size - written);
  }
}

void Mp3StreamDecoder::decodeStream(std::stop_token token) {
  if (startOffsetMs)
    throw std::runtime_error("MP3 start offset requires a seekable stream");

  // The extended file API scans for duration and rewinds even with
  // MP3D_DO_NOT_SCAN. Decode sequentially with minimp3's frame API instead.
  // Its recommended lookahead holds ten frames even at the largest bitrate.
  std::array<uint8_t, MINIMP3_BUF_SIZE> encoded;
  std::array<uint8_t, HDR_SIZE> previousHeader{};
  std::array<mp3d_sample_t, MINIMP3_MAX_SAMPLES_PER_FRAME> pcm;
  mp3dec_t decoder{};
  mp3dec_init(&decoder);
  size_t held = 0;
  bool eof = false;
  bool firstFrame = true;
  std::optional<GaplessInfo> gapless;
  std::optional<StreamAudioFormat> format;
  while (!token.stop_requested()) {
    // Once synchronized, decode complete buffered frames before asking for
    // more network data. In particular, skipping a Xing frame must not add
    // another full lookahead wait before the first audible samples.
    const auto completeFrameBytes = [&]() -> size_t {
      if (firstFrame || held < HDR_SIZE ||
          !hdr_compare(previousHeader.data(), encoded.data()))
        return 0;
      const auto bytes =
          hdr_frame_bytes(encoded.data(), decoder.free_format_bytes) +
          hdr_padding(encoded.data());
      return bytes > 0 && held >= size_t(bytes) ? bytes : 0;
    };
    while (!completeFrameBytes() && held < encoded.size() && !eof &&
           !token.stop_requested()) {
      const auto available = inputNode->waitForData(token, 1);
      if (token.stop_requested())
        return;
      const auto state = inputNode->getState();
      if (state.state == AudioGraphNodeState::ERROR) {
        setState({AudioGraphNodeState::ERROR, *state.error});
        return;
      }
      const auto count = inputNode->read(
          encoded.data() + held, std::min(available, encoded.size() - held));
      held += count;
      eof = count == 0;
    }
    if (token.stop_requested() || !held)
      break;
    mp3dec_frame_info_t info{};
    // With a matching header at the expected boundary, submit exactly one
    // frame. Otherwise minimp3 can reject the final audio frame because an
    // ID3 trailer, rather than another MPEG header, follows it.
    const auto frameBytes = completeFrameBytes();
    const auto frames = mp3dec_decode_frame(
        &decoder, encoded.data(), frameBytes ? frameBytes : held, pcm.data(), &info);
    if (!info.frame_bytes)
      break;
    bool tag = false;
    unsigned encodedFrames = 0;
    if (info.hz) {
      const auto frame = std::span(encoded).subspan(
          info.frame_offset, info.frame_bytes - info.frame_offset);
      std::copy_n(frame.data(), HDR_SIZE, previousHeader.data());
      encodedFrames = hdr_frame_samples(frame.data());
      if (firstFrame) {
        firstFrame = false;
        if (info.layer == 3)
          gapless = readGaplessTag(frame);
        tag = gapless.has_value();
      }
    }
    held -= info.frame_bytes;
    std::memmove(encoded.data(), encoded.data() + info.frame_bytes, held);
    if (tag) {
      mp3dec_init(&decoder); // File decoding also starts after the tag frame.
      continue;
    }
    const auto [skip, count] = gapless ? gapless->trim(encodedFrames)
                                     : std::pair<size_t, size_t>{0, frames};
    if (!frames)
      continue; // Skip junk or prime a stream joined mid-reservoir.
    const StreamAudioFormat decoded{unsigned(info.hz), unsigned(info.channels),
                                    16, AudioSampleFormat::PCM16_LE};
    if (format && *format != decoded)
      throw std::runtime_error("MP3 audio format changed during playback");
    if (!format) {
      format = decoded;
      frameSizeBytes = sizeof(mp3d_sample_t) * info.channels;
      setState({AudioGraphNodeState::STREAMING, framesRead(),
                StreamInfo{.format = decoded,
                           .streamType = StreamType::FRAMES,
                           .prebufferMs = inputNode->getState()
                                              .streamInfo.value_or(StreamInfo{})
                                              .prebufferMs}});
      initCompleteSignal.sendValue(true);
    }
    writePcm(pcm.data() + skip * info.channels,
             count * info.channels * sizeof(mp3d_sample_t), token);
  }
  if (!format && !token.stop_requested())
    throw std::runtime_error("No MP3 audio found");
}

void Mp3StreamDecoder::finishIfDrained() {
  // A read may empty the buffer just before the worker appends its final
  // samples and marks EOF. Recheck the current size before reporting FINISHED.
  if (buffer.isEof() && buffer.empty() &&
      getState().state == AudioGraphNodeState::STREAMING) {
    setState(StreamState{AudioGraphNodeState::FINISHED});
  }
}

size_t Mp3StreamDecoder::readCallback(void *buf, size_t size) {
  spdlog::trace("Mp3StreamDecoder::readCallback({})", size);

  auto stopToken = decodingThread.get_stop_token();
  auto combinedToken = combineStopTokens(stopToken, seekSignal.getStopToken());
  auto token = initCompleteSignal.getValue().value_or(false)
                   ? combinedToken.get_token()
                   : stopToken;
  // minimp3 treats a short read as EOF, including while counting CBR samples.
  // Drain partial source buffers until the request is full or truly at EOF;
  // waiting for the whole request can also exceed the HTTP producer's reserve.
  size_t filled = 0;
  while (filled < size && !token.stop_requested()) {
    const auto available = inputNode->waitForData(token, 1);
    if (!available || token.stop_requested()) {
      if (!token.stop_requested()) {
        const auto state = inputNode->getState();
        if (state.state == AudioGraphNodeState::ERROR)
          setState({AudioGraphNodeState::ERROR, *state.error});
      }
      break;
    }
    const auto count = inputNode->read(static_cast<uint8_t *>(buf) + filled,
                                       std::min(available, size - filled));
    if (!count)
      break;
    filled += count;
    sourcePosition += count;
  }
  spdlog::trace("Mp3StreamDecoder::readCallback({}) -> {}", size, filled);
  return filled;
}

int Mp3StreamDecoder::seekCallback(uint64_t position) {
  spdlog::trace("Mp3StreamDecoder::seekCallback({})", position);
  auto actualPosition = inputNode->seekTo(position);
  if (actualPosition != size_t(-1))
    sourcePosition = actualPosition;
  spdlog::trace("Mp3StreamDecoder::seekCallback({}) -> {}", position,
                actualPosition);
  return position - actualPosition;
}

void Mp3StreamDecoder::handleSeekSignal(void *mp3dec_ex, size_t seekPosFrames) {
  mp3dec_ex_t *mp3 = static_cast<mp3dec_ex_t *>(mp3dec_ex);
  // minimp3 returns interleaved data and counts
  // every sample for each channel,so we need
  // to multiply the position by the number of channels.
  auto seekPosSamples = seekPosFrames * mp3->info.channels;
  const auto totalFrames = mp3->samples / mp3->info.channels;

  spdlog::trace("Mp3StreamDecoder::handleSeekSignal seek to {}/{}",
                seekPosFrames, totalFrames);
  setState(StreamState{AudioGraphNodeState::PREPARING});

  buffer.clear();
  if (seekPosSamples >= mp3->samples) {
    spdlog::trace(
        "Mp3StreamDecoder::handleSeekSignal seekPos is too large {} -> {}",
        seekPosSamples, mp3->samples);
    seekPosSamples = mp3->samples;
    seekSignal.respond(seekPosSamples / mp3->info.channels);
    buffer.setEof();
    setState(StreamState{AudioGraphNodeState::FINISHED});
    return;
  }

  if (!mp3->indexes_built) {
    spdlog::trace("Index is not built, seeking to {}", seekPosFrames);
  }

  seekSignal.respond(seekPosFrames);
  buffer.resetEof();
  int ret = mp3dec_ex_seek(mp3, seekPosSamples);
  if (ret != 0) {
    spdlog::error("MP3 decoder seek failure: {}", ret);
    throw std::runtime_error("MP3 decoder seek failure");
  }

  restartRunAt(seekPosFrames);
  spdlog::trace("Mp3StreamDecoder::handleSeekSignal seek to {} -> {}",
                seekPosFrames, framesRead());
}
