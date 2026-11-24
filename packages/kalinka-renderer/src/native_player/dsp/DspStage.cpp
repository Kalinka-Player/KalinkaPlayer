#include "dsp/DspStage.h"

#include <algorithm>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <utility>

namespace {
// Room for what the sink asks for at once, and the read-ahead around it.
constexpr size_t kFifoFrames = 32768;
constexpr size_t kChunkFrames = 4096;
// Discards waiting for the pulling thread, held without allocating.
constexpr size_t kDiscardRoom = 32;

bool endsInput(AudioGraphNodeState state) {
  return state == AudioGraphNodeState::SOURCE_CHANGED ||
         state == AudioGraphNodeState::FINISHED ||
         state == AudioGraphNodeState::ERROR;
}

StreamState about(AudioGraphNodeState state, std::optional<StreamId> id) {
  StreamState stamped{state};
  stamped.streamId = id;
  return stamped;
}
} // namespace

DspStage::DspStage(std::shared_ptr<DspProcessor> processor,
                   std::unique_ptr<DspProcessor> fallback)
    : processor(processor ? std::move(processor) : std::move(fallback)),
      fallback(std::move(fallback)) {
  shared.discards.reserve(kDiscardRoom);
  discardsTaken.reserve(kDiscardRoom);
}

DspStage::~DspStage() {
  if (upstream) {
    disconnect(upstream);
  }
}

void DspStage::connectTo(std::shared_ptr<AudioGraphOutputNode> source) {
  if (source == nullptr) {
    throw std::runtime_error("Input node cannot be nullptr");
  }
  if (upstream == source) {
    return;
  }
  if (upstream != nullptr) {
    throw std::runtime_error("DspStage is already connected");
  }
  upstream = std::move(source);
  upstreamCallback = upstream->onStateChange(
      [this](AudioGraphNode *, StreamState state) -> bool {
        onUpstreamState(state);
        return true;
      });
}

void DspStage::disconnect(std::shared_ptr<AudioGraphOutputNode> source) {
  if (source == nullptr || source != upstream) {
    return;
  }
  upstream->removeStateChangeCallback(upstreamCallback);
  upstreamCallback = -1;
  upstream = nullptr;
  dropEverything();
  upstreamSeen = StreamState(AudioGraphNodeState::STOPPED);
  updateState([this](const StreamState &) -> std::optional<StreamState> {
    shared.upstream = StreamState(AudioGraphNodeState::STOPPED);
    shared.follow = Follow::Upstream;
    shared.playing.reset();
    shared.afterPlaying.reset();
    shared.discards.clear();
    shared.discardEverything = false;
    return shown(shared);
  });
}

void DspStage::onUpstreamState(const StreamState &state) {
  if (!processor) {
    setState(state);
    return;
  }
  updateState([this, &state](const StreamState &) -> std::optional<StreamState> {
    shared.upstream = state;
    ++shared.upstreamChanges;
    return shown(shared);
  });
}

StreamState DspStage::shown(const Shared &shared) const {
  const bool fixed =
      shared.follow == Follow::Fixed ||
      (shared.follow == Follow::UntilInputEnds && endsInput(shared.upstream.state));
  StreamState state =
      presented(fixed ? shared.fixed : shared.upstream, !fixed);
  // Always now: a waiter only takes a state newer than the one it last saw.
  state.timestamp = getTimestampNs();
  return state;
}

StreamState DspStage::presented(StreamState state,
                                bool upstreamPosition) const {
  const auto *selected =
      state.streamInfo ? processorFor(state.streamInfo->format) : nullptr;
  if (!selected) {
    return state;
  }
  const auto out = selected->output(state.streamInfo->format);
  if (out.format != state.streamInfo->format &&
      !state.streamInfo->decodedFormat)
    state.streamInfo->decodedFormat = state.streamInfo->format;
  state.streamInfo->format = out.format;
  state.streamInfo->streamSize /= out.ratio;
  if (upstreamPosition && state.position > 0) {
    state.position /= long(out.ratio);
  }
  return state;
}

DspProcessor *DspStage::processorFor(const StreamAudioFormat &format) const {
  if (processor && processor->handles(format))
    return processor.get();
  return fallback && fallback->handles(format) ? fallback.get() : nullptr;
}

SignalPath DspStage::describe(const StreamAudioFormat &format) const {
  const auto *selected = processorFor(format);
  return selected ? selected->describe(format) : SignalPath{};
}

StreamAudioFormat DspStage::handsOn(const StreamAudioFormat &format) const {
  const auto *selected = processorFor(format);
  return selected ? selected->output(format).format : format;
}

void DspStage::takeUpstream() {
  updateState([this](const StreamState &) -> std::optional<StreamState> {
    upstreamSeen = shared.upstream;
    upstreamChangesSeen = shared.upstreamChanges;
    return std::nullopt;
  });
}

void DspStage::sync() {
  bool everything = false;
  updateState([this, &everything](const StreamState &)
                  -> std::optional<StreamState> {
    upstreamSeen = shared.upstream;
    upstreamChangesSeen = shared.upstreamChanges;
    discardsTaken.swap(shared.discards);
    everything = std::exchange(shared.discardEverything, false);
    return std::nullopt;
  });
  if (!everything && discardsTaken.empty()) {
    return;
  }
  if (everything) {
    dropEverything();
  }
  for (const StreamId id : discardsTaken) {
    drop(id);
  }
  discardsTaken.clear();
  // What a discard showed was decided without what this thread did since.
  publish();
}

void DspStage::publish() {
  Follow follow = Follow::Upstream;
  StreamState fixed{AudioGraphNodeState::STOPPED};
  std::optional<StreamId> playing;
  std::optional<StreamState> afterPlaying;
  if (!segments.empty()) {
    const Segment &head = segments.front();
    const Segment *next = nextAfterHead();
    std::optional<StreamState> announced;
    if (next) {
      announced = about(AudioGraphNodeState::SOURCE_CHANGED, next->id);
    } else if (boundary) {
      announced = about(AudioGraphNodeState::SOURCE_CHANGED, *boundary);
    }
    if (!headExhausted()) {
      follow = head.open ? Follow::UntilInputEnds : Follow::Fixed;
      fixed = StreamState(AudioGraphNodeState::STREAMING,
                          head.start.value_or(0) + long(handed - head.begin),
                          head.info);
      fixed.streamId = head.id;
      playing = head.id;
      afterPlaying = announced;
    } else if (announced) {
      follow = Follow::Fixed;
      fixed = *announced;
    } else {
      segments.clear();
      endRun();
    }
  } else if (boundary) {
    follow = Follow::Fixed;
    fixed = about(AudioGraphNodeState::SOURCE_CHANGED, *boundary);
  }
  updateState([&, this](const StreamState &) -> std::optional<StreamState> {
    // A discard not yet taken showed its outcome already; that stands.
    if (!shared.discards.empty() || shared.discardEverything) {
      return std::nullopt;
    }
    shared.follow = follow;
    shared.fixed = fixed;
    shared.playing = playing;
    shared.afterPlaying = afterPlaying;
    return shown(shared);
  });
}

size_t DspStage::passRead(void *data, size_t size) {
  return upstream ? upstream->read(data, size) : 0;
}

uint64_t DspStage::produced() const {
  return fedOut > latency ? fedOut - latency : 0;
}

size_t DspStage::fifoFrames() const {
  return produced() > handed ? produced() - handed : 0;
}

size_t DspStage::readable() const {
  if (segments.empty()) {
    return 0;
  }
  const uint64_t limit = std::min(segments.front().end, produced());
  return limit > handed ? limit - handed : 0;
}

bool DspStage::headExhausted() const {
  return !segments.empty() && !segments.front().open &&
         handed >= segments.front().end;
}

const DspStage::Segment *DspStage::nextAfterHead() const {
  for (auto it = std::next(segments.begin()); it < segments.end(); ++it) {
    if (!it->dropped) {
      return &*it;
    }
  }
  return nullptr;
}

void DspStage::dropTo(uint64_t frame) {
  const uint64_t limit = std::min(frame, produced());
  if (limit > handed) {
    fifoOffset += (limit - handed) * outFrameBytes;
  }
  handed = std::max(handed, frame);
  if (fifoFrames() == 0) {
    fifoOffset = 0;
  }
}

void DspStage::popHead(uint64_t nextBegin) {
  dropTo(nextBegin);
  size_t offset = 0;
  bool cut = false;
  do {
    const Segment &head = segments.front();
    cut |= head.cut;
    offset += head.prefixBytes;
    segments.pop_front();
  } while (segments.front().dropped);
  if (cut && keepSuccessorInput)
    rebuildAfterCut(offset);
  offset += std::exchange(segments.front().prefixBytes, 0);
  readAhead.erase(readAhead.begin(), readAhead.begin() + offset);
}

void DspStage::rebuildAfterCut(size_t offset) {
  fedIn = fedOut = handed = fifoOffset = 0;
  prepareProcessor(segments.front().info.format);
  for (auto &segment : segments) {
    if (!continueIn(segment.info.format))
      throw std::logic_error("Cannot replay previously compatible DSP input");
    segment.begin = segment.end = fedIn / ratio;
    segment.partialEnd = false;
    const size_t end = offset + segment.prefixBytes;
    while (offset < end) {
      const size_t frames =
          std::min(inputChunkFrames, (end - offset) / inFrameBytes);
      std::memcpy(input.data(), readAhead.data() + offset,
                  frames * inFrameBytes);
      feed(segment, input.data(), frames);
      offset += frames * inFrameBytes;
    }
  }
  flushLeft = segments.back().open ? 0 : latency;
}

void DspStage::drop(StreamId id) {
  if (boundary && *boundary == id) {
    boundary.reset();
  }
  if (joinPending && *joinPending == id) {
    joinPending.reset();
    flushLeft = latency;
  }
  if (leadIn && leadIn->id == id) {
    leadIn.reset();
  }
  for (auto it = segments.begin(); it != segments.end(); ++it) {
    if (it->id != id) {
      continue;
    }
    if (it->open || it->partialEnd || it->end > std::max(handed, it->begin))
      it->cut = true;
    if (it->open) {
      it->open = false;
      flushLeft = latency;
    }
    if (it == segments.begin()) {
      // What is playing ends here, as a removed source does upstream.
      it->end = std::max(handed, it->begin);
    } else {
      it->dropped = true;
    }
  }
}

void DspStage::dropEverything() {
  segments.clear();
  boundary.reset();
  leadIn.reset();
  endRun();
}

void DspStage::endRun() {
  joinPending.reset();
  readAhead.clear();
  fedIn = fedOut = handed = 0;
  flushLeft = 0;
  fifoOffset = 0;
}

bool DspStage::startRun(std::optional<long> position) {
  const bool afterSeek = leadIn && leadIn->id == upstreamSeen.streamId;
  auto info = upstreamSeen.streamInfo;
  if (!info && afterSeek)
    info = leadIn->info;
  if (!info && upstreamSeen.state == AudioGraphNodeState::PREPARING &&
      upstreamSeen.streamId)
    info = upstream->seekStreamInfo(*upstreamSeen.streamId);
  // A source still buffering may already hold frames, and they are processed.
  const bool live = upstreamSeen.state == AudioGraphNodeState::STREAMING ||
                    upstreamSeen.state == AudioGraphNodeState::PREPARING;
  if (!live || !info || !processorFor(info->format)) {
    return false;
  }
  endRun();
  prepareProcessor(info->format);
  std::optional<long> start =
      position ? std::optional(*position / long(ratio)) : std::nullopt;
  // Frames before a seek's target only build history: handed passes them.
  uint64_t lead = 0;
  if (afterSeek) {
    lead = leadIn->frames;
    start = leadIn->target;
  }
  leadIn.reset();
  segments.push_back({upstreamSeen.streamId, *info, start, lead, lead});
  handed = lead;
  return true;
}

void DspStage::prepareProcessor(const StreamAudioFormat &format) {
  runProcessor = processorFor(format);
  const auto out = runProcessor->output(format);
  ratio = out.ratio;
  inFrameBytes = format.channels * sampleSize(format.sampleFormat);
  outFrameBytes = out.format.channels * sampleSize(out.format.sampleFormat);
  latency = runProcessor->start(format);
  runFormat = format;
  fifo.resize(kFifoFrames * outFrameBytes);
  inputChunkFrames = kChunkFrames;
  input.resize(inputChunkFrames * inFrameBytes);
  output.resize((kChunkFrames + 1) * outFrameBytes);
  keepSuccessorInput = ratio > 1 || runProcessor->needsSuccessorInput();
  if (keepSuccessorInput)
    readAhead.reserve((latency + 1) * ratio *
                      std::max(inFrameBytes, outFrameBytes));
}

void DspStage::feed(Segment &segment, const uint8_t *data, size_t frames) {
  const size_t made = runProcessor->process(data, output.data(), frames);
  fedIn += frames;
  segment.end = std::max(segment.end, fedIn / ratio);
  segment.partialEnd = fedIn % ratio != 0;
  append(output.data(), made);
}

void DspStage::append(const uint8_t *frames, size_t count) {
  // Priming outputs come before the run, and a discard may have passed more.
  const uint64_t priming = fedOut < latency ? latency - fedOut : 0;
  size_t skip = std::min<uint64_t>(count, priming);
  fedOut += skip;
  if (produced() < handed) {
    const size_t passed =
        std::min<uint64_t>(count - skip, handed - produced());
    fedOut += passed;
    skip += passed;
  }
  const size_t bytes = (count - skip) * outFrameBytes;
  const size_t used = fifoFrames() * outFrameBytes;
  if (fifoOffset + used + bytes > fifo.size()) {
    std::memmove(fifo.data(), fifo.data() + fifoOffset, used);
    fifoOffset = 0;
  }
  std::memcpy(fifo.data() + fifoOffset + used, frames + skip * outFrameBytes,
              bytes);
  fedOut += count - skip;
}

bool DspStage::continueIn(const StreamAudioFormat &format) {
  if (format == runFormat)
    return true;
  const size_t width = format.channels * sampleSize(format.sampleFormat);
  if (!width || width > input.size() || processorFor(format) != runProcessor ||
      !runProcessor->changeInputFormat(format))
    return false;
  inFrameBytes = width;
  inputChunkFrames = std::min(kChunkFrames, input.size() / width);
  runFormat = format;
  return true;
}

void DspStage::join(std::optional<StreamId> id, std::optional<long> position,
                    bool idle) {
  const StreamState &now = upstreamSeen;
  const Segment &tail = segments.back();
  // A next source still buffering continues the run as a dry one does.
  const bool playing = now.state == AudioGraphNodeState::STREAMING ||
                       now.state == AudioGraphNodeState::PREPARING;
  // Ended inside a seek's lead-in: the next source's frames would be passed.
  const bool leadingIn = fedIn / ratio < tail.begin;
  if (!idle && playing && !leadingIn && !tail.cut &&
      now.streamInfo.has_value() && now.streamInfo->live == tail.info.live &&
      continueIn(now.streamInfo->format)) {
    segments.push_back(
        {id, *now.streamInfo,
         position ? std::optional(*position / long(ratio)) : std::nullopt,
         fedIn / ratio, fedIn / ratio});
    return;
  }
  flushLeft = latency;
  if (!idle) {
    boundary = id;
  }
}

DspStage::Step DspStage::plan(size_t frames, size_t &count) {
  while (true) {
    if (joinPending) {
      if (upstreamSeen.state == AudioGraphNodeState::PREPARING &&
          !upstreamSeen.streamInfo.has_value()) {
        return Step::Done;
      }
      const auto id = *joinPending;
      joinPending.reset();
      join(id, upstream->streamReadPosition(), false);
      publish();
      continue;
    }
    if (segments.empty() || headExhausted())
      return Step::Done;
    // Only read ahead far enough to finish the outgoing segment.
    if (!segments.front().open)
      frames = std::min<uint64_t>(frames, segments.front().end - handed);
    if (readable() >= frames)
      return Step::Done;
    Segment &tail = segments.back();
    const size_t room = kFifoFrames - fifoFrames();
    if (!tail.open) {
      // Output frames, each a ratio's worth of silence fed in.
      count = std::min({flushLeft, std::max<size_t>(1, kChunkFrames / ratio),
                        room});
      return count ? Step::Flush : Step::Done;
    }
    if (upstreamSeen.state == AudioGraphNodeState::SOURCE_CHANGED) {
      return Step::Accept;
    }
    if (endsInput(upstreamSeen.state)) {
      tail.open = false;
      flushLeft = latency;
      publish();
      continue;
    }
    const uint64_t priming = fedOut < latency ? latency - fedOut : 0;
    const uint64_t passed = handed > produced() ? handed - produced() : 0;
    // Input frames: a ratio's worth for each output wanted.
    count = std::min<uint64_t>(
        {(frames - readable() + priming + passed) * ratio, inputChunkFrames,
         room * ratio});
    return count ? Step::Read : Step::Done;
  }
}

bool DspStage::perform(Step step, size_t count) {
  switch (step) {
  case Step::Read: {
    const size_t frames =
        upstream->read(input.data(), count * inFrameBytes) / inFrameBytes;
    if (frames == 0) {
      return false;
    }
    Segment &tail = segments.back();
    if (segments.size() > 1 && keepSuccessorInput) {
      const size_t bytes = frames * inFrameBytes;
      readAhead.insert(readAhead.end(), input.begin(), input.begin() + bytes);
      tail.prefixBytes += bytes;
    }
    feed(tail, input.data(), frames);
    publish();
    return true;
  }
  case Step::Flush: {
    const size_t made =
        runProcessor->process(nullptr, output.data(), count * ratio);
    flushLeft -= std::min(flushLeft, made);
    append(output.data(), made);
    publish();
    return true;
  }
  case Step::Accept: {
    upstream->acceptSourceChange();
    const auto position = upstream->streamReadPosition();
    // Not sync(): a discard taken here would close the tail unrecorded.
    takeUpstream();
    Segment &tail = segments.back();
    const StreamState &now = upstreamSeen;
    tail.open = false;
    const bool idle = (now.state == AudioGraphNodeState::FINISHED ||
                       now.state == AudioGraphNodeState::ERROR) &&
                      !now.streamId.has_value();
    tail.left = !idle;
    if (!idle && now.state == AudioGraphNodeState::PREPARING &&
        !now.streamInfo.has_value()) {
      // Its format decides whether the run carries on: wait for it.
      joinPending = now.streamId;
    } else {
      join(now.streamId, position, idle);
    }
    publish();
    return true;
  }
  case Step::Done:
    break;
  }
  return false;
}

size_t DspStage::fill(std::stop_token token, size_t bytes, bool wait,
                      std::optional<std::chrono::steady_clock::time_point>
                          deadline) {
  if (!upstream) {
    return 0;
  }
  bool waited = false;
  while (true) {
    sync();
    if (segments.empty()) {
      if (boundary) {
        return 0;
      }
      if (!startRun(upstream->streamReadPosition())) {
        return kPassThrough;
      }
      continue;
    }
    // A wait asks for every frame it names; a read takes whole frames only.
    const size_t frames = std::clamp<size_t>(
        wait ? (bytes + outFrameBytes - 1) / outFrameBytes
             : bytes / outFrameBytes,
        1,
        kFifoFrames / 2);
    size_t count = 0;
    const Step step = plan(frames, count);
    if (step == Step::Done && wait && joinPending && readable() == 0 &&
        !token.stop_requested() &&
        (!deadline || std::chrono::steady_clock::now() < *deadline)) {
      // Upstream's next source shows its format once it has data.
      upstream->waitForDataFor(token, std::chrono::milliseconds(10), 1);
      sync();
      continue;
    }
    if (step == Step::Done) {
      return readable();
    }
    const uint64_t changes = upstreamChangesSeen;
    if (perform(step, count)) {
      waited = false;
      continue;
    }
    // Upstream said it had data and then gave none: let the caller retry.
    if (!wait || waited || token.stop_requested() ||
        (deadline && std::chrono::steady_clock::now() >= *deadline)) {
      return readable();
    }
    const size_t needed = count * inFrameBytes;
    const size_t available =
        deadline ? upstream->waitForDataFor(
                       token,
                       std::chrono::duration_cast<std::chrono::milliseconds>(
                           *deadline - std::chrono::steady_clock::now()),
                       needed)
                 : upstream->waitForData(token, needed);
    sync();
    if (available == 0 && upstreamChangesSeen == changes) {
      return readable();
    }
    waited = available > 0;
  }
}

size_t DspStage::read(void *data, size_t size) {
  lastUnsettled = 0;
  if (!processor) {
    return passRead(data, size);
  }
  const size_t ready = fill(std::stop_token(), size, false, std::nullopt);
  if (ready == kPassThrough) {
    return passRead(data, size);
  }
  if (ready == 0) {
    return 0;
  }
  const size_t frames = std::min(ready, size / outFrameBytes);
  std::memcpy(data, fifo.data() + fifoOffset, frames * outFrameBytes);
  fifoOffset += frames * outFrameBytes;
  handed += frames;
  const auto settled = runProcessor->settledOutputFrames();
  const auto buffered = fifoFrames();
  lastUnsettled =
      frames - (settled > buffered ? std::min(frames, settled - buffered) : 0);
  if (fifoFrames() == 0) {
    fifoOffset = 0;
  }
  publish();
  return frames * outFrameBytes;
}

size_t DspStage::waitForData(std::stop_token stopToken, size_t size) {
  if (!processor) {
    return upstream ? upstream->waitForData(stopToken, size) : 0;
  }
  const size_t ready = fill(stopToken, size, true, std::nullopt);
  if (ready == kPassThrough) {
    return upstream->waitForData(stopToken, size);
  }
  return ready * outFrameBytes;
}

size_t DspStage::waitForDataFor(std::stop_token stopToken,
                                std::chrono::milliseconds timeout,
                                size_t size) {
  if (!processor) {
    return upstream ? upstream->waitForDataFor(stopToken, timeout, size) : 0;
  }
  const size_t ready = fill(stopToken, size, true,
                            std::chrono::steady_clock::now() + timeout);
  if (ready == kPassThrough) {
    return upstream->waitForDataFor(stopToken, timeout, size);
  }
  return ready * outFrameBytes;
}

size_t DspStage::seekTo(size_t absolutePosition) {
  return seek(std::nullopt, absolutePosition);
}

size_t DspStage::seekStream(StreamId stream, size_t position) {
  return seek(stream, position);
}

size_t DspStage::seek(std::optional<StreamId> stream, size_t position) {
  if (!upstream)
    return size_t(-1);
  const auto upstreamSeek = [&](size_t frame) {
    return stream ? upstream->seekStream(*stream, frame)
                  : upstream->seekTo(frame);
  };
  if (!processor)
    return upstreamSeek(position);
  sync();
  if (!stream && (boundary || (!segments.empty() && segments.front().left)))
    return size_t(-1);

  const auto knownInfo =
      [&](std::optional<StreamId> id) -> std::optional<StreamInfo> {
    if (id == upstreamSeen.streamId && upstreamSeen.streamInfo)
      return upstreamSeen.streamInfo;
    if (leadIn && leadIn->id == id)
      return leadIn->info;
    const auto segment =
        std::find_if(segments.rbegin(), segments.rend(),
                     [&](const Segment &s) { return s.id == id; });
    if (segment != segments.rend())
      return segment->info;
    return std::nullopt;
  };

  const auto id = stream ? upstream->seekTargetId(*stream) : upstreamSeen.streamId;
  auto info = knownInfo(id);
  if (!info) {
    info = upstream->seekStreamInfo(id, true);
    takeUpstream();
  }
  if (!info && ((stream && position) ||
                upstreamSeen.state == AudioGraphNodeState::PREPARING))
    return size_t(-1);

  const unsigned r = ratioOf(info);
  const bool atEnd = info && info->streamType == StreamType::FRAMES &&
                     info->streamSize && position >= info->streamSize / r;
  if (atEnd)
    position = info->streamSize / r;
  size_t early = 0;
  const auto *filtered = info ? processorFor(info->format) : nullptr;
  if (filtered && !atEnd) {
    const size_t history = info->format == runFormat
                               ? filtered->history()
                               : filtered->historyFor(info->format);
    early = std::min(position, history);
  }
  if (position - early > std::numeric_limits<size_t>::max() / r)
    return size_t(-1);
  const auto changes = upstreamChangesSeen;
  // At EOF even a trailing partial output frame belongs to the old source.
  const size_t reached =
      upstreamSeek(atEnd ? info->streamSize : (position - early) * r);
  sync();
  if (upstreamSeen.state == AudioGraphNodeState::ERROR &&
      (!upstreamSeen.streamId || upstreamSeen.streamId == id) &&
      upstreamChangesSeen != changes) {
    dropEverything();
    publish();
    return size_t(-1);
  }
  if (reached == size_t(-1))
    return reached;

  dropEverything();
  const size_t from = reached / r;
  // A decoder that lands short of the requested preroll plays from there.
  const size_t target =
      from <= position && position - from <= early ? position : from;
  if (filtered && upstreamSeen.state != AudioGraphNodeState::SOURCE_CHANGED)
    leadIn = LeadIn{upstreamSeen.streamId, *info, long(target), target - from};
  publish();
  return target;
}

unsigned DspStage::ratioOf(const std::optional<StreamInfo> &info) const {
  const auto *selected = info ? processorFor(info->format) : nullptr;
  return selected ? selected->output(info->format).ratio : 1;
}

std::optional<long> DspStage::streamReadPosition() const {
  if (processor) {
    for (const Segment &segment : segments) {
      if (!segment.dropped) {
        if (!segment.start) {
          return std::nullopt;
        }
        const uint64_t within =
            std::clamp(handed, segment.begin, segment.end) - segment.begin;
        return *segment.start + long(within);
      }
    }
  }
  if (leadIn) {
    return leadIn->target;
  }
  const auto position =
      upstream ? upstream->streamReadPosition() : std::nullopt;
  // No run yet, as straight after a seek: upstream counts its own frames, in
  // the format it has now, which an accepted source shows before a sync.
  if (processor && position)
    return *position / long(ratioOf(upstream->getState().streamInfo));
  return position;
}

std::optional<uint64_t> DspStage::liveSourceFrame() const {
  const auto frame = upstream ? upstream->liveSourceFrame() : std::nullopt;
  if (!processor || !frame || segments.empty() || !segments.back().open) {
    return frame;
  }
  const Segment &tail = segments.back();
  const uint64_t held =
      (tail.end - std::clamp(handed, tail.begin, tail.end)) * ratio;
  return *frame > held ? *frame - held : 0;
}

void DspStage::acceptSourceChange() {
  if (!processor) {
    if (upstream) {
      upstream->acceptSourceChange();
    }
    return;
  }
  sync();
  if (segments.empty()) {
    if (boundary || upstreamSeen.state == AudioGraphNodeState::SOURCE_CHANGED)
      leadIn.reset();
    if (boundary) {
      boundary.reset();
      publish();
      return;
    }
    if (upstream) {
      upstream->acceptSourceChange();
    }
    return;
  }
  if (!headExhausted()) {
    return;
  }
  if (const auto *next = nextAfterHead()) {
    popHead(next->begin);
  } else if (boundary) {
    boundary.reset();
    dropEverything();
  }
  publish();
}

void DspStage::discard(StreamId id) {
  if (!processor) {
    return;
  }
  updateState([this, id](const StreamState &) -> std::optional<StreamState> {
    shared.discards.push_back(id);
    if (shared.playing == id) {
      shared.follow =
          shared.afterPlaying ? Follow::Fixed : Follow::Upstream;
      if (shared.afterPlaying) {
        shared.fixed = *shared.afterPlaying;
      }
      shared.playing.reset();
      shared.afterPlaying.reset();
    }
    return shown(shared);
  });
}

void DspStage::discardAll() {
  if (!processor) {
    return;
  }
  updateState([this](const StreamState &) -> std::optional<StreamState> {
    shared.discards.clear();
    shared.discardEverything = true;
    shared.follow = Follow::Upstream;
    shared.playing.reset();
    shared.afterPlaying.reset();
    return shown(shared);
  });
}
