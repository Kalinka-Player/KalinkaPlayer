#include "DspStage.h"

#include <algorithm>
#include <cstring>
#include <stdexcept>

namespace {
// Room for what the sink asks for at once, and the read-ahead around it.
constexpr size_t kFifoFrames = 32768;
constexpr size_t kChunkFrames = 4096;

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

DspStage::DspStage(std::unique_ptr<DspProcessor> processor)
    : processor(std::move(processor)) {}

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
  // Unlocked: the callback takes our mutex under the upstream node's.
  upstream->removeStateChangeCallback(upstreamCallback);
  upstreamCallback = -1;
  std::lock_guard lock(mutex);
  segments.clear();
  boundary.reset();
  endRun();
  upstream = nullptr;
  upstreamState = StreamState(AudioGraphNodeState::STOPPED);
  setState(upstreamState);
}

void DspStage::onUpstreamState(const StreamState &state) {
  if (!processor) {
    setState(state);
    return;
  }
  std::lock_guard lock(mutex);
  upstreamState = state;
  ++upstreamChanges;
  present();
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
    fifoOffset += (limit - handed) * frameBytes;
  }
  handed = std::max(handed, frame);
  if (fifoFrames() == 0) {
    fifoOffset = 0;
  }
}

void DspStage::popHead() {
  const Segment *next = nextAfterHead();
  const uint64_t to = next ? next->begin : segments.front().end;
  segments.pop_front();
  dropTo(to);
  skipDropped();
}

void DspStage::skipDropped() {
  while (!segments.empty() && segments.front().dropped) {
    const uint64_t to = segments.front().end;
    segments.pop_front();
    dropTo(to);
  }
  if (segments.empty()) {
    endRun();
  }
}

void DspStage::endRun() {
  ++generation;
  fedIn = fedOut = handed = 0;
  flushLeft = 0;
  fifoOffset = 0;
}

bool DspStage::startRun(std::optional<long> position) {
  const auto &info = upstreamState.streamInfo;
  // A source still buffering may already hold frames, and they are processed.
  const bool live = upstreamState.state == AudioGraphNodeState::STREAMING ||
                    upstreamState.state == AudioGraphNodeState::PREPARING;
  if (!live || !info.has_value() || !processor->handles(info->format)) {
    return false;
  }
  endRun();
  frameBytes = info->format.channels * sampleSize(info->format.sampleFormat);
  latency = processor->start(info->format);
  fifo.resize(kFifoFrames * frameBytes);
  input.resize(kChunkFrames * frameBytes);
  output.resize(kChunkFrames * frameBytes);
  segments.push_back({upstreamState.streamId, *info, position, 0, 0});
  return true;
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
  const size_t bytes = (count - skip) * frameBytes;
  const size_t used = fifoFrames() * frameBytes;
  if (fifoOffset + used + bytes > fifo.size()) {
    std::memmove(fifo.data(), fifo.data() + fifoOffset, used);
    fifoOffset = 0;
  }
  std::memcpy(fifo.data() + fifoOffset + used, frames + skip * frameBytes,
              bytes);
  fedOut += count - skip;
}

void DspStage::present() {
  skipDropped();
  StreamState shown = upstreamState;
  if (!segments.empty()) {
    const Segment &head = segments.front();
    if (!headExhausted()) {
      if (!head.open || endsInput(upstreamState.state)) {
        shown = StreamState(AudioGraphNodeState::STREAMING,
                            head.start.value_or(0) + long(handed - head.begin),
                            head.info);
        shown.streamId = head.id;
      }
    } else if (const Segment *next = nextAfterHead()) {
      shown = about(AudioGraphNodeState::SOURCE_CHANGED, next->id);
    } else if (boundary) {
      shown = about(AudioGraphNodeState::SOURCE_CHANGED, *boundary);
    } else {
      segments.clear();
      endRun();
    }
  } else if (boundary) {
    shown = about(AudioGraphNodeState::SOURCE_CHANGED, *boundary);
  }
  // Always now: a waiter only takes a state newer than the one it last saw.
  shown.timestamp = getTimestampNs();
  setState(shown);
}

DspStage::Step DspStage::plan(size_t frames, size_t &count) {
  while (true) {
    if (segments.empty() || readable() >= frames || headExhausted()) {
      return Step::Done;
    }
    Segment &tail = segments.back();
    const size_t room = kFifoFrames - fifoFrames();
    if (!tail.open) {
      count = std::min({flushLeft, kChunkFrames, room});
      return count ? Step::Flush : Step::Done;
    }
    if (upstreamState.state == AudioGraphNodeState::SOURCE_CHANGED) {
      return Step::Accept;
    }
    if (endsInput(upstreamState.state)) {
      tail.open = false;
      flushLeft = latency;
      present();
      continue;
    }
    const uint64_t priming = fedOut < latency ? latency - fedOut : 0;
    const uint64_t passed = handed > produced() ? handed - produced() : 0;
    count = std::min<uint64_t>(
        {frames - readable() + priming + passed, kChunkFrames, room});
    return count ? Step::Read : Step::Done;
  }
}

bool DspStage::perform(Step step, size_t count, uint64_t run) {
  switch (step) {
  case Step::Read: {
    const size_t frames = upstream->read(input.data(), count * frameBytes) /
                          frameBytes;
    if (frames == 0) {
      return false;
    }
    processor->process(input.data(), output.data(), frames);
    std::lock_guard lock(mutex);
    if (run == generation && !segments.empty() && segments.back().open) {
      segments.back().end += frames;
      fedIn += frames;
      append(output.data(), frames);
      present();
    }
    return true;
  }
  case Step::Flush: {
    processor->process(nullptr, output.data(), count);
    std::lock_guard lock(mutex);
    if (run == generation) {
      flushLeft -= std::min(flushLeft, count);
      append(output.data(), count);
      present();
    }
    return true;
  }
  case Step::Accept: {
    upstream->acceptSourceChange();
    const auto position = upstream->streamReadPosition();
    std::lock_guard lock(mutex);
    if (run != generation || segments.empty() || !segments.back().open) {
      return true;
    }
    Segment &tail = segments.back();
    const StreamState &now = upstreamState;
    tail.open = false;
    const bool idle = (now.state == AudioGraphNodeState::FINISHED ||
                       now.state == AudioGraphNodeState::ERROR) &&
                      !now.streamId.has_value();
    tail.left = !idle;
    if (!idle && now.state == AudioGraphNodeState::STREAMING &&
        now.streamInfo.has_value() &&
        now.streamInfo->format == tail.info.format &&
        now.streamInfo->live == tail.info.live) {
      segments.push_back(
          {now.streamId, *now.streamInfo, position, fedIn, fedIn});
    } else {
      flushLeft = latency;
      if (!idle) {
        boundary = now.streamId;
      }
    }
    present();
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
    std::unique_lock lock(mutex);
    skipDropped();
    if (segments.empty()) {
      if (boundary) {
        return 0;
      }
      lock.unlock();
      const auto position = upstream->streamReadPosition();
      lock.lock();
      skipDropped();
      if (segments.empty() && !boundary && !startRun(position)) {
        return kPassThrough;
      }
      continue;
    }
    // A wait asks for every frame it names; a read takes whole frames only.
    const size_t frames = std::clamp<size_t>(
        wait ? (bytes + frameBytes - 1) / frameBytes : bytes / frameBytes, 1,
        kFifoFrames / 2);
    size_t count = 0;
    const Step step = plan(frames, count);
    if (step == Step::Done) {
      return readable();
    }
    const uint64_t run = generation;
    const uint64_t changes = upstreamChanges;
    const size_t needed = count * frameBytes;
    lock.unlock();
    if (perform(step, count, run)) {
      waited = false;
      continue;
    }
    // Upstream said it had data and then gave none: let the caller retry.
    if (!wait || waited || token.stop_requested() ||
        (deadline && std::chrono::steady_clock::now() >= *deadline)) {
      lock.lock();
      return readable();
    }
    const size_t available =
        deadline ? upstream->waitForDataFor(
                       token,
                       std::chrono::duration_cast<std::chrono::milliseconds>(
                           *deadline - std::chrono::steady_clock::now()),
                       needed)
                 : upstream->waitForData(token, needed);
    lock.lock();
    if (available == 0 && upstreamChanges == changes) {
      return readable();
    }
    waited = available > 0;
  }
}

size_t DspStage::read(void *data, size_t size) {
  if (!processor) {
    return passRead(data, size);
  }
  const size_t ready = fill(std::stop_token(), size, false, std::nullopt);
  if (ready == kPassThrough) {
    return passRead(data, size);
  }
  std::lock_guard lock(mutex);
  const size_t frames = std::min(readable(), size / frameBytes);
  std::memcpy(data, fifo.data() + fifoOffset, frames * frameBytes);
  fifoOffset += frames * frameBytes;
  handed += frames;
  if (fifoFrames() == 0) {
    fifoOffset = 0;
  }
  present();
  return frames * frameBytes;
}

size_t DspStage::waitForData(std::stop_token stopToken, size_t size) {
  if (!processor) {
    return upstream ? upstream->waitForData(stopToken, size) : 0;
  }
  const size_t ready = fill(stopToken, size, true, std::nullopt);
  if (ready == kPassThrough) {
    return upstream->waitForData(stopToken, size);
  }
  return ready * frameBytes;
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
  return ready * frameBytes;
}

size_t DspStage::seekTo(size_t absolutePosition) {
  if (!processor) {
    return upstream ? upstream->seekTo(absolutePosition) : size_t(-1);
  }
  {
    std::lock_guard lock(mutex);
    skipDropped();
    // Upstream has moved on: what it would seek is not what is playing.
    if (boundary || (!segments.empty() && segments.front().left)) {
      return size_t(-1);
    }
    segments.clear();
    endRun();
    present();
  }
  return upstream ? upstream->seekTo(absolutePosition) : size_t(-1);
}

std::optional<long> DspStage::streamReadPosition() const {
  if (processor) {
    std::lock_guard lock(mutex);
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
  return upstream ? upstream->streamReadPosition() : std::nullopt;
}

std::optional<uint64_t> DspStage::liveSourceFrame() const {
  const auto frame = upstream ? upstream->liveSourceFrame() : std::nullopt;
  if (!processor || !frame) {
    return frame;
  }
  std::lock_guard lock(mutex);
  if (segments.empty() || !segments.back().open) {
    return frame;
  }
  const Segment &tail = segments.back();
  const uint64_t held =
      tail.end - std::clamp(handed, tail.begin, tail.end);
  return *frame > held ? *frame - held : 0;
}

void DspStage::acceptSourceChange() {
  if (!processor) {
    if (upstream) {
      upstream->acceptSourceChange();
    }
    return;
  }
  std::unique_lock lock(mutex);
  skipDropped();
  if (segments.empty()) {
    if (boundary) {
      boundary.reset();
      present();
      return;
    }
    lock.unlock();
    if (upstream) {
      upstream->acceptSourceChange();
    }
    return;
  }
  if (!headExhausted()) {
    return;
  }
  if (nextAfterHead()) {
    popHead();
  } else if (boundary) {
    boundary.reset();
    segments.clear();
    endRun();
  }
  present();
}

void DspStage::discard(StreamId id) {
  if (!processor) {
    return;
  }
  std::lock_guard lock(mutex);
  if (boundary && *boundary == id) {
    boundary.reset();
  }
  for (auto it = segments.begin(); it != segments.end(); ++it) {
    if (it->id != id) {
      continue;
    }
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
  present();
}

void DspStage::discardAll() {
  if (!processor) {
    return;
  }
  std::lock_guard lock(mutex);
  segments.clear();
  boundary.reset();
  endRun();
  present();
}
