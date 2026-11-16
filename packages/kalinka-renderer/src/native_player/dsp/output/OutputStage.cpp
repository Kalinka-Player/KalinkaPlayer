#include "dsp/output/OutputStage.h"

#include "dsp/output/PlanarPcm.h"

#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <string>

namespace {
constexpr size_t kChunkFrames = 1024;
constexpr unsigned kMaxChannels = 8;
// Long enough that a slider step is no click, short enough to follow a drag.
constexpr std::chrono::milliseconds kRamp{25};

bool nothingPlays(AudioGraphNodeState state) {
  return state == AudioGraphNodeState::STOPPED ||
         state == AudioGraphNodeState::FINISHED ||
         state == AudioGraphNodeState::ERROR;
}

size_t frameBytes(AudioSampleFormat format, unsigned channels) {
  return sampleSize(format) * channels;
}
} // namespace

OutputStage::OutputStage(std::vector<StreamAudioFormat> device, uint64_t seed)
    : device(std::move(device)), ramp(kRamp),
      quantiser(kChunkFrames, kMaxChannels, seed),
      input(kChunkFrames * frameBytes(PCM_FLOAT64_LE, kMaxChannels)),
      planar(kMaxChannels, std::vector<double>(kChunkFrames)) {
  for (auto &channel : planar)
    planes.push_back(channel.data());
}

OutputStage::~OutputStage() {
  if (upstream)
    disconnect(upstream);
}

void OutputStage::connectTo(std::shared_ptr<AudioGraphOutputNode> source) {
  if (source == nullptr)
    throw std::runtime_error("Input node cannot be nullptr");
  if (upstream == source)
    return;
  if (upstream != nullptr)
    throw std::runtime_error("OutputStage is already connected");
  upstream = std::move(source);
  upstreamCallback = upstream->onStateChange(
      [this](AudioGraphNode *, StreamState state) -> bool {
        onUpstreamState(state);
        return true;
      });
}

void OutputStage::disconnect(std::shared_ptr<AudioGraphOutputNode> source) {
  if (source == nullptr || source != upstream)
    return;
  upstream->removeStateChangeCallback(upstreamCallback);
  upstreamCallback = -1;
  upstream = nullptr;
  {
    std::lock_guard lock(presentedMutex);
    presented.reset();
  }
  settlePending = true;
  setState(StreamState(AudioGraphNodeState::STOPPED));
}

void OutputStage::onUpstreamState(const StreamState &state) {
  StreamState shown = state;
  if (state.streamInfo && state.streamInfo->streamType == StreamType::FRAMES) {
    const StreamAudioFormat &source = state.streamInfo->format;
    std::lock_guard lock(presentedMutex);
    if (!presented || presented->source != source)
      presented =
          Presented{source, chooseOutput(source, device, kPcmDeviceChannels)};
    auto &format = shown.streamInfo->format;
    format.sampleFormat = presented->output.format;
    // Quantised, float keeps the bits it is dithered at.
    if (isFloat(source.sampleFormat) && !isFloat(format.sampleFormat))
      format.bitsPerSample = presented->output.bits;
  }
  if (nothingPlays(state.state))
    settlePending = true;
  setState(shown);
}

std::optional<OutputStage::Presented> OutputStage::current() const {
  std::lock_guard lock(presentedMutex);
  return presented;
}

void OutputStage::setGain(double linear) { target = linear; }

void OutputStage::follow(unsigned rate) {
  const double gain = target.load();
  if (settlePending.exchange(false))
    ramp.settle(gain);
  else
    ramp.aim(gain, rate);
}

size_t OutputStage::read(void *data, size_t size) {
  if (!upstream)
    return 0;
  const auto now = current();
  if (!now)
    return upstream->read(data, size);
  const StreamAudioFormat &source = now->source;
  if (isDsd(source.sampleFormat)) {
    if (target.load() != 1.0)
      throw std::runtime_error(
          "DSD requires fixed volume; software gain cannot alter DSD");
    return upstream->read(data, size);
  }
  if (source.channels == 0 || source.channels > kMaxChannels)
    throw std::runtime_error("No output stage for " +
                             std::to_string(source.channels) + " channels");
  follow(source.sampleRate);
  if (ramp.holding(1.0) && now->output.format == source.sampleFormat)
    return upstream->read(data, size);

  const size_t inFrame = frameBytes(source.sampleFormat, source.channels);
  const size_t outFrame = frameBytes(now->output.format, source.channels);
  auto *out = static_cast<uint8_t *>(data);
  const size_t wanted = size / outFrame;
  size_t done = 0;
  while (done < wanted) {
    const size_t ask = std::min(wanted - done, kChunkFrames);
    const size_t got = upstream->read(input.data(), ask * inFrame) / inFrame;
    if (got == 0)
      break;
    produce(*now, input.data(), got, out + done * outFrame);
    done += got;
    if (got < ask)
      break;
  }
  return done * outFrame;
}

void OutputStage::produce(const Presented &now, const uint8_t *in,
                          size_t frames, uint8_t *out) {
  const AudioSampleFormat from = now.source.sampleFormat;
  const AudioSampleFormat to = now.output.format;
  const unsigned channels = now.source.channels;
  const size_t inFrame = frameBytes(from, channels);
  const size_t outFrame = frameBytes(to, channels);
  // Integer to a wider container is exact; float to integer is quantised.
  const bool exact = from == to || (!isFloat(from) && !isFloat(to));
  for (size_t at = 0; at < frames;) {
    const auto span = ramp.take(frames - at);
    const uint8_t *source = in + at * inFrame;
    uint8_t *dest = out + at * outFrame;
    if (span.from == 1.0 && span.to == 1.0 && exact) {
      if (from == to)
        std::memcpy(dest, source, span.frames * inFrame);
      else
        convertSampleFormat(source, from, span.frames * channels, dest, to,
                            span.frames * outFrame);
    } else {
      readPlanar(source, from, channels, span.frames, planes.data());
      quantiser.pack(planes.data(), channels, span.frames, span, to,
                     now.output.bits, dest);
    }
    at += span.frames;
  }
  clipped = quantiser.clipped();
}

size_t OutputStage::toSource(const Presented &now, size_t bytes) const {
  const unsigned channels = now.source.channels;
  const size_t inFrame = frameBytes(now.source.sampleFormat, channels);
  const size_t outFrame = frameBytes(now.output.format, channels);
  if (inFrame == outFrame)
    return bytes;
  return (bytes + outFrame - 1) / outFrame * inFrame;
}

size_t OutputStage::toOutput(const Presented &now, size_t bytes) const {
  const unsigned channels = now.source.channels;
  const size_t inFrame = frameBytes(now.source.sampleFormat, channels);
  const size_t outFrame = frameBytes(now.output.format, channels);
  if (inFrame == outFrame)
    return bytes;
  return bytes / inFrame * outFrame;
}

size_t OutputStage::waitForData(std::stop_token stopToken, size_t size) {
  if (!upstream)
    return 0;
  const auto now = current();
  if (!now || now->source.channels == 0)
    return upstream->waitForData(stopToken, size);
  return toOutput(*now, upstream->waitForData(stopToken, toSource(*now, size)));
}

size_t OutputStage::waitForDataFor(std::stop_token stopToken,
                                   std::chrono::milliseconds timeout,
                                   size_t size) {
  if (!upstream)
    return 0;
  const auto now = current();
  if (!now || now->source.channels == 0)
    return upstream->waitForDataFor(stopToken, timeout, size);
  return toOutput(
      *now, upstream->waitForDataFor(stopToken, timeout, toSource(*now, size)));
}

size_t OutputStage::seekTo(size_t absolutePosition) {
  const size_t reached =
      upstream ? upstream->seekTo(absolutePosition) : size_t(-1);
  if (reached != size_t(-1))
    settlePending = true;
  return reached;
}

size_t OutputStage::seekStream(StreamId stream, size_t position) {
  const size_t reached =
      upstream ? upstream->seekStream(stream, position) : size_t(-1);
  if (reached != size_t(-1))
    settlePending = true;
  return reached;
}

std::optional<long> OutputStage::streamReadPosition() const {
  return upstream ? upstream->streamReadPosition() : std::nullopt;
}

std::optional<uint64_t> OutputStage::liveSourceFrame() const {
  return upstream ? upstream->liveSourceFrame() : std::nullopt;
}

void OutputStage::acceptSourceChange() {
  if (upstream)
    upstream->acceptSourceChange();
}
