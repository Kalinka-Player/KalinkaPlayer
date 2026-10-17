#include "DashStreamDecoder.h"
#include "AudioGraphHttpStream.h"
#include "DashManifest.h"
#include "FragmentedMp4.h"
#include "TrackDecoder.h"

#include <algorithm>
#include <array>
#include <future>
#include <stdexcept>

namespace {
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
  FragmentedMp4 init;
  std::unique_ptr<TrackDecoder> track;
  unsigned rate = 0;
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
  demux->init = FragmentedMp4::parseInitialization(
      fetch(demux->manifest.initialization, 1024 * 1024));
  demux->track =
      std::make_unique<TrackDecoder>(sampleDecoder(demux->init.track));
  const auto format = demux->track->format();
  demux->rate = format.sampleRate;
  for (const auto &segment : demux->manifest.segments)
    demux->starts.push_back(
        samplesAt(segment.time, demux->manifest.timescale, demux->rate));
  const auto &last = demux->manifest.segments.back();
  const auto end = samplesAt(last.time + last.duration,
                             demux->manifest.timescale, demux->rate);
  if (end <= demux->track->delay())
    throw std::runtime_error("DASH audio is too short to play");
  const StreamInfo info{
      format, StreamType::FRAMES,
      static_cast<unsigned long>(end - demux->track->delay())};
  restartAt(0, info.streamSize);
  return info;
}

void DashStreamDecoder::seekSource(uint64_t frame) {
  demux->prefetch.reset();
  restartAt(frame, streamInfo().streamSize);
}

void DashStreamDecoder::restartAt(uint64_t frame, uint64_t length) {
  // A delayed codec's output for a frame comes from the unit that much later,
  // so decoding starts at the unit holding `frame` and keeps from `frame +
  // delay` on.
  demux->segment = frame == length
                       ? demux->starts.size()
                       : std::upper_bound(demux->starts.begin(),
                                          demux->starts.end(), frame) -
                             demux->starts.begin() - 1;
  demux->track->restart(frame + demux->track->delay());
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
    next->bytes = std::async(std::launch::async,
                             [this, url = demux->manifest.segmentUrl(index + 1),
                              cancel = next->cancel.get_token()] {
                               return fetch(url, maxSegmentBytes, cancel);
                             });
    demux->prefetch = std::move(next);
  }
  const auto timescale = demux->init.track.timescale;
  uint64_t position = demux->starts[index];
  for (const auto &fragment : demux->init.parseSegments(bytes)) {
    if (samplesAt(fragment.time, timescale, demux->rate) != position)
      throw std::runtime_error(
          "DASH audio segment has an unexpected timestamp");
    std::span<const uint8_t> data = fragment.data;
    for (size_t i = 0; i < fragment.sizes.size(); ++i) {
      checkInterrupted();
      const auto frames =
          samplesAt(fragment.durations[i], timescale, demux->rate);
      write(demux->track->decode(data.first(fragment.sizes[i]), position,
                                 frames));
      data = data.subspan(fragment.sizes[i]);
      position += frames;
    }
  }
  const auto end = index + 1 < demux->starts.size()
                       ? demux->starts[index + 1]
                       : streamInfo().streamSize + demux->track->delay();
  if (position != end)
    throw std::runtime_error(
        "DASH audio segment length does not match its timeline");
  ++demux->segment;
  return true;
}
