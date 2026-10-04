#include "DsdStreamDecoder.h"
#include <algorithm>
#include <array>
#include <cstring>
#include <limits>
#include <stdexcept>

namespace {
struct Interrupted {};
uint64_t number(const uint8_t *p, unsigned n, bool little = false) {
  uint64_t result = 0;
  for (unsigned i = 0; i < n; ++i)
    result = (result << 8) | p[little ? n - 1 - i : i];
  return result;
}
bool id(const uint8_t *p, const char *name) {
  return std::memcmp(p, name, 4) == 0;
}
void require(bool ok, const char *message) {
  if (!ok)
    throw std::runtime_error(message);
}
uint8_t reverse(uint8_t b) {
  b = ((b & 0x55) << 1) | ((b >> 1) & 0x55);
  b = ((b & 0x33) << 2) | ((b >> 2) & 0x33);
  return (b << 4) | (b >> 4);
}
} // namespace

DsdStreamDecoder::DsdStreamDecoder(std::optional<StreamId> id,
                                   SelectOutput select, size_t offset)
    : AudioGraphNode(id), select(std::move(select)), startOffsetMs(offset) {}
DsdStreamDecoder::~DsdStreamDecoder() {
  if (worker.joinable()) {
    worker.request_stop();
    worker.join();
  }
}
void DsdStreamDecoder::connectTo(std::shared_ptr<AudioGraphOutputNode> source) {
  if (!source || input)
    throw std::invalid_argument("Invalid DSD input connection");
  input = std::move(source);
  sourcePosition = dataOffset = sampleCount = nextByte = 0;
  channels = rate = blockSize = 0;
  dsf = reverseBits = false;
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
void DsdStreamDecoder::disconnect(
    std::shared_ptr<AudioGraphOutputNode> source) {
  if (source != input)
    return;
  if (worker.joinable()) {
    worker.request_stop();
    worker.join();
  }
  input.reset();
}
std::optional<long> DsdStreamDecoder::streamReadPosition() const {
  const auto width = frameBytes.load();
  return startFrame.load() + (width ? bytesRead.load() / width : 0);
}
void DsdStreamDecoder::finishIfDrained() {
  if (buffer.isEof() && buffer.size() == 0 &&
      getState().state == AudioGraphNodeState::STREAMING)
    setState({AudioGraphNodeState::FINISHED, *streamReadPosition(), info});
}
size_t DsdStreamDecoder::read(void *dest, size_t size) {
  const auto width = frameBytes.load();
  if (!width)
    return 0;
  // Never expose half a channel or half a DoP marker frame to ALSA.
  size = std::min(size, buffer.size());
  const auto count =
      buffer.read(static_cast<uint8_t *>(dest), size - size % width);
  bytesRead += count;
  finishIfDrained();
  return count;
}
size_t DsdStreamDecoder::waitForData(std::stop_token token, size_t size) {
  auto combined = combineStopTokens(token, seekSignal.getStopToken());
  const auto count = buffer.waitForData(combined.get_token(), size);
  finishIfDrained();
  return count;
}
size_t DsdStreamDecoder::waitForDataFor(std::stop_token token,
                                        std::chrono::milliseconds timeout,
                                        size_t size) {
  auto combined = combineStopTokens(token, seekSignal.getStopToken());
  const auto count = buffer.waitForDataFor(combined.get_token(), timeout, size);
  finishIfDrained();
  return count;
}
size_t DsdStreamDecoder::seekTo(size_t frame) {
  if (!worker.joinable() || getState().state == AudioGraphNodeState::ERROR ||
      seekSignal.getValue())
    return size_t(-1);
  auto token = worker.get_stop_token();
  if (!ready.waitValue(token).value_or(false) || !seekable)
    return size_t(-1);
  seekSignal.sendValue(frame);
  return seekSignal.getResponse(token);
}
void DsdStreamDecoder::readExact(void *dest, size_t bytes) {
  auto combined = combineStopTokens(workerToken, seekSignal.getStopToken());
  auto *p = static_cast<uint8_t *>(dest);
  while (bytes) {
    const auto available =
        input->waitForData(combined.get_token(), std::min(bytes, size_t(4096)));
    if (combined.get_token().stop_requested())
      throw Interrupted{};
    const auto state = input->getState();
    if (state.state == AudioGraphNodeState::ERROR) {
      setState({AudioGraphNodeState::ERROR,
                state.error.value_or(StreamError{StreamErrorSource::DECODER,
                                                 "DSD input failed"})});
      throw std::runtime_error(state.error ? state.error->message
                                           : "DSD input failed");
    }
    require(available != 0, "Truncated DSD file");
    const auto count = input->read(p, std::min(bytes, available));
    require(count != 0, "Truncated DSD file");
    sourcePosition += count;
    p += count;
    bytes -= count;
  }
}
std::vector<uint8_t> DsdStreamDecoder::readBytes(size_t bytes) {
  require(bytes <= 1024 * 1024, "DSD metadata chunk too large");
  std::vector<uint8_t> result(bytes);
  readExact(result.data(), bytes);
  return result;
}
void DsdStreamDecoder::skip(uint64_t bytes) {
  require(bytes <= 16 * 1024 * 1024, "DSD header is too large");
  std::array<uint8_t, 4096> scratch;
  while (bytes) {
    const auto n = std::min<uint64_t>(bytes, scratch.size());
    readExact(scratch.data(), n);
    bytes -= n;
  }
}
void DsdStreamDecoder::parseHeader() {
  auto header = readBytes(12);
  if (id(header.data(), "DSD ")) {
    dsf = true;
    require(number(header.data() + 4, 8, true) == 28,
            "Invalid DSF header size");
    auto rest = readBytes(16);
    const auto fileSize = number(rest.data(), 8, true);
    require(fileSize >= 92, "Invalid DSF file size");
    auto fmt = readBytes(52);
    require(id(fmt.data(), "fmt ") && number(fmt.data() + 4, 8, true) == 52,
            "Invalid DSF format chunk");
    require(number(fmt.data() + 12, 4, true) == 1 &&
                number(fmt.data() + 16, 4, true) == 0,
            "Unsupported DSF version or encoding");
    channels = number(fmt.data() + 24, 4, true);
    require(channels >= 1 && channels <= 2 &&
                number(fmt.data() + 20, 4, true) == channels,
            "DSF supports mono/stereo channel layouts only");
    rate = number(fmt.data() + 28, 4, true);
    const auto order = number(fmt.data() + 32, 4, true);
    require(order == 1 || order == 8, "Invalid DSF bit order");
    reverseBits = order == 1;
    sampleCount = number(fmt.data() + 36, 8, true);
    blockSize = number(fmt.data() + 44, 4, true);
    require(blockSize == 4096, "Unsupported DSF block size");
    auto data = readBytes(12);
    const auto length = number(data.data() + 4, 8, true);
    require(id(data.data(), "data") && length >= 12 && length <= fileSize - 80,
            "Invalid DSF data chunk");
    require(sampleCount > 0 && sampleCount <= (uint64_t(1) << 48),
            "Invalid DSF sample count");
    const auto groups = ((sampleCount + 7) / 8 + blockSize - 1) / blockSize;
    // Some encoders write blocks past the sample count; those are never read.
    require(groups * blockSize * channels <= length - 12,
            "DSF sample count exceeds data size");
  } else {
    require(id(header.data(), "FRM8"), "Not a DSF or DSDIFF file");
    const auto formSize = number(header.data() + 4, 8);
    require(formSize >= 4 && formSize <= (uint64_t(1) << 50),
            "Invalid DSDIFF form size");
    auto form = readBytes(4);
    require(id(form.data(), "DSD "), "Unsupported DSDIFF form");
    bool uncompressed = false;
    while (true) {
      require(sourcePosition <= formSize &&
                  formSize + 12 - sourcePosition >= 12,
              "Missing DSDIFF audio data");
      auto chunk = readBytes(12);
      const auto size = number(chunk.data() + 4, 8);
      require(size <= formSize + 12 - sourcePosition,
              "Invalid DSDIFF chunk size");
      if (id(chunk.data(), "DST "))
        throw std::runtime_error("DST-compressed DSDIFF is not supported");
      if (id(chunk.data(), "DSD ")) {
        require(uncompressed && channels >= 1 && channels <= 2 && size > 0 &&
                    size % channels == 0,
                "Invalid DSDIFF audio properties");
        sampleCount = size / channels * 8;
        break;
      }
      if (id(chunk.data(), "PROP")) {
        const auto prop = readBytes(size);
        require(prop.size() >= 4 && id(prop.data(), "SND "),
                "Invalid DSDIFF sound properties");
        size_t at = 4;
        while (at < prop.size()) {
          require(prop.size() - at >= 12, "Truncated DSDIFF property");
          const auto n = number(prop.data() + at + 4, 8);
          require(n <= prop.size() - at - 12, "Invalid DSDIFF property size");
          const auto *p = prop.data() + at + 12;
          if (id(prop.data() + at, "FS  ")) {
            require(n == 4, "Invalid DSDIFF sample rate");
            rate = number(p, 4);
          } else if (id(prop.data() + at, "CHNL")) {
            require(n >= 2, "Invalid DSDIFF channels");
            channels = number(p, 2);
            require(channels >= 1 && channels <= 2 && n == 2 + 4 * channels,
                    "DSDIFF supports mono/stereo only");
            require(channels == 1 || (id(p + 2, "SLFT") && id(p + 6, "SRGT")),
                    "Unsupported DSDIFF channel order");
          } else if (id(prop.data() + at, "CMPR")) {
            require(n >= 4 && id(p, "DSD "),
                    "Compressed DSDIFF (DST) is not supported");
            uncompressed = true;
          }
          at += 12 + n + (n & 1);
        }
        require(at == prop.size(), "Invalid DSDIFF property padding");
      } else
        skip(size);
      if (size & 1)
        skip(1);
    }
  }
  require(rate >= 2822400 && rate <= 49152000 && rate % 8 == 0,
          "Unsupported DSD sample rate");
  dataOffset = sourcePosition;
  const auto inputState = input->getState();
  if (inputState.streamInfo && inputState.streamInfo->streamSize)
    require(inputState.streamInfo->streamSize >=
                dataOffset + (sampleCount + 7) / 8 * channels,
            "Truncated DSD audio data");
  const auto format = select(rate, channels);
  require(isDsd(format.sampleFormat) && format.channels == channels &&
              format.dsdSampleRate == rate,
          "Invalid DSD transport selection");
  info = {format, StreamType::FRAMES,
          static_cast<unsigned long>(
              (sampleCount + dsdBitsPerFrame(format.sampleFormat) - 1) /
              dsdBitsPerFrame(format.sampleFormat))};
  frameBytes = channels * sampleSize(format.sampleFormat);
  // Probe without moving to any other part of the file.
  seekable = input->seekTo(dataOffset) == dataOffset;
}
void DsdStreamDecoder::seek(size_t frame) {
  frame = std::min(frame, size_t(info.streamSize));
  setState(StreamState{AudioGraphNodeState::PREPARING});
  buffer.clear();
  buffer.resetEof();
  nextByte = uint64_t(frame) * dsdBitsPerFrame(info.format.sampleFormat) / 8;
  const auto byte = dsf ? (nextByte / blockSize) * blockSize : nextByte;
  const auto offset = dataOffset + byte * channels;
  require(input->seekTo(offset) == offset, "DSD stream seek failed");
  sourcePosition = offset;
  startFrame = frame;
  bytesRead = 0;
  setState({AudioGraphNodeState::STREAMING, long(frame), info});
  seekSignal.respond(frame);
}
std::vector<uint8_t> DsdStreamDecoder::readBlock() {
  const auto totalBytes = (sampleCount + 7) / 8;
  const auto count = std::min<uint64_t>(4096, totalBytes - nextByte);
  std::vector<uint8_t> raw;
  if (dsf) {
    auto blocked = readBytes(blockSize * channels);
    const auto skipBytes = nextByte % blockSize;
    const auto valid =
        std::min<uint64_t>(blockSize - skipBytes, totalBytes - nextByte);
    raw.resize(valid * channels);
    for (size_t b = 0; b < valid; ++b)
      for (unsigned c = 0; c < channels; ++c) {
        auto v = blocked[c * blockSize + skipBytes + b];
        raw[b * channels + c] = reverseBits ? reverse(v) : v;
      }
  } else
    raw = readBytes(count * channels);
  if (sampleCount % 8 && nextByte + raw.size() / channels == totalBytes) {
    const uint8_t mask = 0xff << (8 - sampleCount % 8);
    for (unsigned c = 0; c < channels; ++c)
      raw[raw.size() - channels + c] =
          (raw[raw.size() - channels + c] & mask) | (0x69 & ~mask);
  }
  return raw;
}
void DsdStreamDecoder::run(std::stop_token token) {
  workerToken = token;
  try {
    parseHeader();
    if (startOffsetMs) {
      require(seekable, "DSD start offset requires a seekable stream");
      require(startOffsetMs <=
                  std::numeric_limits<uint64_t>::max() / info.format.sampleRate,
              "DSD start offset is too large");
      const auto frame =
          uint64_t(startOffsetMs) * info.format.sampleRate / 1000;
      require(frame < info.streamSize, "DSD start offset is past the end");
      seek(frame);
    }
    setState({AudioGraphNodeState::STREAMING, *streamReadPosition(), info});
    ready.sendValue(true);
    while (!token.stop_requested()) {
      try {
        if (auto target = seekSignal.getValue())
          seek(*target);
        if (nextByte >= (sampleCount + 7) / 8) {
          buffer.setEof();
          seekSignal.waitValue(token);
          continue;
        }
        const auto raw = readBlock();
        auto packed =
            packDsd(raw, channels, info.format.sampleFormat,
                    nextByte * 8 / dsdBitsPerFrame(info.format.sampleFormat));
        nextByte += raw.size() / channels;
        size_t written = 0;
        auto combined = combineStopTokens(token, seekSignal.getStopToken());
        while (written < packed.size()) {
          auto space = buffer.waitForSpace(combined.get_token(), frameBytes);
          if (combined.get_token().stop_requested())
            throw Interrupted{};
          space -= space % frameBytes;
          written += buffer.write(packed.data() + written,
                                  std::min(space, packed.size() - written));
        }
      } catch (const Interrupted &) { /* seek or shutdown; worker owns input */
      }
    }
  } catch (const Interrupted &) {
  } catch (const std::exception &ex) {
    if (!token.stop_requested() &&
        getState().state != AudioGraphNodeState::ERROR)
      setState({AudioGraphNodeState::ERROR,
                StreamError{StreamErrorSource::DECODER, ex.what()}});
  }
  seekSignal.close(size_t(-1));
  ready.close(false);
  buffer.setEof();
}
