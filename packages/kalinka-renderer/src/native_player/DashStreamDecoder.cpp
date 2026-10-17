#include "DashStreamDecoder.h"
#include "AudioGraphHttpStream.h"
#include "DashManifest.h"
#include "FragmentedFlac.h"

#include <FLAC++/decoder.h>

#include <algorithm>
#include <array>
#include <cstring>
#include <exception>
#include <functional>
#include <future>
#include <stdexcept>

namespace {
class FragmentDecoder final : private FLAC::Decoder::Stream {
public:
  using Output =
      std::function<void(const FLAC__Frame &, const FLAC__int32 *const[])>;
  FragmentDecoder(const FragmentedFlac &config,
                  const FragmentedFlac::Fragment &fragment, Output output)
      : output(std::move(output)) {
    input = {'f', 'L', 'a', 'C', 0x80, 0, 0, 34};
    input.insert(input.end(), config.streamInfo.begin(),
                 config.streamInfo.end());
    input.insert(input.end(), fragment.frames.begin(), fragment.frames.end());
    set_md5_checking(false);
    if (init() != FLAC__STREAM_DECODER_INIT_STATUS_OK)
      throw std::runtime_error("Could not initialize the FLAC decoder");
  }
  void decode() {
    const auto ok = process_until_end_of_stream();
    if (exception)
      std::rethrow_exception(exception);
    if (!ok || failed)
      throw std::runtime_error("Invalid FLAC audio in DASH segment");
  }

private:
  std::vector<uint8_t> input;
  size_t position = 0;
  Output output;
  bool failed = false;
  std::exception_ptr exception;
  FLAC__StreamDecoderReadStatus read_callback(FLAC__byte *data,
                                              size_t *count) override {
    *count = std::min(*count, input.size() - position);
    if (!*count)
      return FLAC__STREAM_DECODER_READ_STATUS_END_OF_STREAM;
    std::memcpy(data, input.data() + position, *count);
    position += *count;
    return FLAC__STREAM_DECODER_READ_STATUS_CONTINUE;
  }
  FLAC__StreamDecoderWriteStatus
  write_callback(const FLAC__Frame *frame,
                 const FLAC__int32 *const samples[]) override {
    if (failed)
      return FLAC__STREAM_DECODER_WRITE_STATUS_ABORT;
    try {
      output(*frame, samples);
    } catch (...) {
      exception = std::current_exception();
      return FLAC__STREAM_DECODER_WRITE_STATUS_ABORT;
    }
    return FLAC__STREAM_DECODER_WRITE_STATUS_CONTINUE;
  }
  void error_callback(FLAC__StreamDecoderErrorStatus) override {
    failed = true;
  }
};

constexpr size_t maxSegmentBytes = 32 * 1024 * 1024;
} // namespace

/// @brief A segment downloading while the one before it plays.
/// @note Destruction cancels the download and waits for its thread.
struct DashStreamDecoder::Prefetch {
  size_t segment;
  std::stop_source cancel;
  std::future<std::vector<uint8_t>> bytes;
  ~Prefetch() { cancel.request_stop(); }
};

struct DashStreamDecoder::Demux {
  DashManifest manifest;
  FragmentedFlac config;
  std::vector<uint64_t> starts;
  size_t segment = 0;
  std::unique_ptr<Prefetch> prefetch;
};

DashStreamDecoder::DashStreamDecoder(std::optional<StreamId> id,
                                     std::string url, size_t bufferSize,
                                     size_t startOffsetMs,
                                     std::chrono::milliseconds timeout,
                                     long maxRedirects)
    : AudioGraphNode(id), BufferedStreamDecoder(bufferSize, startOffsetMs),
      url(std::move(url)), timeout(timeout), maxRedirects(maxRedirects) {
  start();
}

DashStreamDecoder::~DashStreamDecoder() { stop(); }

std::vector<uint8_t> DashStreamDecoder::fetch(const std::string &source,
                                              size_t limit,
                                              std::stop_token cancel) {
  if (!source.starts_with("http://") && !source.starts_with("https://"))
    throw std::runtime_error("DASH requires HTTP or HTTPS media URLs");
  if (timeout <= std::chrono::milliseconds::zero())
    throw std::runtime_error("DASH requires a positive network timeout");
  AudioGraphHttpStream input(std::nullopt, source, 65536, 0,
                             std::chrono::ceil<std::chrono::seconds>(timeout),
                             maxRedirects);
  auto combined = interruption(cancel);
  auto deadline = std::chrono::steady_clock::now() + timeout;
  std::vector<uint8_t> result;
  std::array<uint8_t, 16384> chunk;
  while (true) {
    checkInterrupted();
    if (cancel.stop_requested())
      throw Interrupted{};
    const auto remaining = std::chrono::ceil<std::chrono::milliseconds>(
        deadline - std::chrono::steady_clock::now());
    const auto available = input.waitForDataFor(
        combined.get_token(),
        std::max(remaining, std::chrono::milliseconds::zero()), 1);
    const auto state = input.getState();
    if (state.state == AudioGraphNodeState::ERROR)
      throw SourceFailed{state.error.value_or(StreamError{
          StreamErrorSource::HTTP_STREAM, "Could not fetch DASH media"})};
    if (available) {
      const auto count = input.read(chunk.data(), chunk.size());
      if (count > limit - result.size())
        throw std::runtime_error("DASH media exceeds the size limit");
      result.insert(result.end(), chunk.begin(), chunk.begin() + count);
      deadline = std::chrono::steady_clock::now() + timeout;
    } else if (state.state == AudioGraphNodeState::FINISHED)
      break;
    else if (std::chrono::steady_clock::now() >= deadline)
      throw std::runtime_error("DASH media request timed out");
  }
  checkInterrupted();
  return result;
}

StreamInfo DashStreamDecoder::open() {
  demux = std::make_unique<Demux>();
  demux->manifest = DashManifest::parse(fetch(url, 1024 * 1024), url);
  demux->config = FragmentedFlac::parseInitialization(
      fetch(demux->manifest.initialization, 1024 * 1024));
  const auto &format = demux->config.format;
  for (const auto &segment : demux->manifest.segments)
    demux->starts.push_back(samplesAt(
        segment.time, demux->manifest.timescale, format.sampleRate));
  const auto &last = demux->manifest.segments.back();
  const auto total = samplesAt(
      last.time + last.duration, demux->manifest.timescale, format.sampleRate);
  return {format, StreamType::FRAMES, static_cast<unsigned long>(total)};
}

void DashStreamDecoder::seekSource(uint64_t frame) {
  demux->prefetch.reset();
  demux->segment =
      frame == streamInfo().streamSize
          ? demux->starts.size()
          : std::upper_bound(demux->starts.begin(), demux->starts.end(),
                             frame) -
                demux->starts.begin() - 1;
  discardBefore = nextFrame = frame;
}

void DashStreamDecoder::close() { demux.reset(); }

std::vector<uint8_t> DashStreamDecoder::segmentBytes(size_t index) {
  if (auto pending = std::move(demux->prefetch);
      pending && pending->segment == index)
    return pending->bytes.get();
  return fetch(demux->manifest.segmentUrl(index), maxSegmentBytes);
}

bool DashStreamDecoder::decodeNext() {
  if (demux->segment == demux->starts.size())
    return false;
  const auto index = demux->segment;
  const auto bytes = segmentBytes(index);
  if (index + 1 < demux->starts.size()) {
    auto next = std::make_unique<Prefetch>();
    next->segment = index + 1;
    next->bytes = std::async(
        std::launch::async,
        [this, url = demux->manifest.segmentUrl(index + 1),
         cancel = next->cancel.get_token()] {
          return fetch(url, maxSegmentBytes, cancel);
        });
    demux->prefetch = std::move(next);
  }
  const auto fragments = demux->config.parseSegments(bytes);
  uint64_t position = demux->starts[index];
  for (const auto &fragment : fragments) {
    if (demux->config.samplePosition(fragment.time) != position)
      throw std::runtime_error(
          "DASH audio segment has an unexpected timestamp");
    size_t packet = 0;
    FragmentDecoder decoder(
        demux->config, fragment,
        [&](const FLAC__Frame &frame, const FLAC__int32 *const samples[]) {
          checkInterrupted();
          if (packet >= fragment.durations.size() ||
              frame.header.blocksize !=
                  demux->config.samplePosition(fragment.durations[packet++]))
            throw std::runtime_error("DASH audio sample duration is invalid");
          writeFrame(frame, samples, position);
          position += frame.header.blocksize;
        });
    decoder.decode();
    if (packet != fragment.durations.size())
      throw std::runtime_error("DASH audio segment is incomplete");
  }
  const auto end = index + 1 < demux->starts.size() ? demux->starts[index + 1]
                                                    : streamInfo().streamSize;
  if (position != end)
    throw std::runtime_error(
        "DASH audio segment length does not match its timeline");
  ++demux->segment;
  return true;
}

void DashStreamDecoder::writeFrame(const FLAC__Frame &frame,
                                   const FLAC__int32 *const samples[],
                                   uint64_t first) {
  const auto &header = frame.header;
  const auto &format = streamInfo().format;
  if (header.sample_rate != format.sampleRate ||
      header.channels != format.channels ||
      header.bits_per_sample != format.bitsPerSample ||
      !header.blocksize || header.blocksize > 65536)
    throw std::runtime_error("DASH audio format changed during playback");
  const auto skip = std::min(discardBefore > first ? discardBefore - first : 0,
                             uint64_t(header.blocksize));
  if (first + skip > nextFrame)
    throw std::runtime_error("A DASH audio segment is missing");
  if (skip == header.blocksize)
    return;
  const auto count = header.blocksize - skip;
  const size_t width = sampleSize(format.sampleFormat);
  // A padded container leaves its spare high byte clear, as the FLAC path does.
  const uint32_t mask =
      format.bitsPerSample < 32 ? (1u << format.bitsPerSample) - 1 : ~0u;
  pcm.resize(count * format.channels * width);
  for (size_t i = 0; i < count; ++i)
    for (unsigned channel = 0; channel < format.channels; ++channel) {
      const auto sample = uint32_t(samples[channel][i + skip]) & mask;
      auto *dest = pcm.data() + (i * format.channels + channel) * width;
      for (size_t byte = 0; byte < width; ++byte)
        dest[byte] = sample >> (8 * byte);
    }
  write(pcm);
  nextFrame = first + header.blocksize;
  discardBefore = nextFrame;
}
