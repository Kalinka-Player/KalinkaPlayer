#include "dsp/output/OutputStage.h"

#include "dsp/output/PlanarPcm.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <format>
#include <stdexcept>
#include <string>
#include <utility>

namespace {
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

bool convertedFromDsd(const StreamAudioFormat &decoded,
                      const StreamAudioFormat &source) {
  return isDsd(decoded.sampleFormat) && !isDsd(source.sampleFormat);
}

bool convertedFromDsd(const StreamInfo &info) {
  return info.decodedFormat &&
         convertedFromDsd(*info.decodedFormat, info.format);
}
} // namespace

OutputStage::OutputStage(std::vector<StreamAudioFormat> device, uint64_t seed)
    : device(std::move(device)), ramp(kRamp), fade(kFadeLength),
      quantiser(kPlanarFrames, kPlanarChannels, seed),
      truePeak(kPlanarFrames, kPlanarChannels),
      input(kPlanarFrames * frameBytes(PCM_FLOAT64_LE, kPlanarChannels)) {}

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
    const bool fromDsd = convertedFromDsd(*state.streamInfo);
    std::lock_guard lock(presentedMutex);
    if (!presented || presented->source != source ||
        presented->fromDsd != fromDsd)
      presented = Presented{
          source, chooseOutput(source, device, kPcmDeviceChannels), fromDsd};
    auto &format = shown.streamInfo->format;
    format.sampleFormat = presented->output.format;
    // Quantised, float keeps the bits it is dithered at.
    if (isFloat(source.sampleFormat) && !isFloat(format.sampleFormat)) {
      format.bitsPerSample = presented->output.bits;
      if (!shown.streamInfo->decodedFormat)
        shown.streamInfo->decodedFormat = source;
    }
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

void OutputStage::setDsdGain(double linear) { dsdGain = linear; }

std::vector<SignalStep>
OutputStage::describe(const StreamAudioFormat &decoded,
                      const StreamAudioFormat &source,
                      const StreamAudioFormat &output) const {
  std::vector<SignalStep> steps;
  const double gain = dsdGain.load();
  if (convertedFromDsd(decoded, source) && gain != 1.0)
    steps.push_back({"dsd_gain", "DSD gain",
                     std::format("{:+.1f} dB", 20 * std::log10(gain))});
  if (isFloat(source.sampleFormat) && !isFloat(output.sampleFormat))
    steps.push_back({"dither", "Dither",
                     std::format("TPDF at {} bits", output.bitsPerSample)});
  return steps;
}

void OutputStage::fadeOut() { fadeTarget = 0.0; }

void OutputStage::fadeIn() { fadeTarget = 1.0; }

void OutputStage::settle(double level) { fadeSettle = fadeTarget = level; }

bool OutputStage::silent() const {
  return fadeTarget == 0.0 &&
         (fadeSettle ? *fadeSettle == 0.0 : fade.holding(0.0));
}

void OutputStage::follow(const Presented &now) {
  const unsigned rate = now.source.sampleRate;
  const double gain = target.load() * (now.fromDsd ? dsdGain.load() : 1.0);
  if (settlePending.exchange(false))
    ramp.settle(gain);
  else
    ramp.aim(gain, rate);
  if (fadeSettle)
    fade.settle(*std::exchange(fadeSettle, std::nullopt));
  fade.aim(fadeTarget, rate);
}

GainRamp::Span OutputStage::take(size_t frames) {
  for (const GainRamp *turning : {&ramp, &fade})
    if (turning->left())
      frames = std::min(frames, turning->left());
  const auto volume = ramp.take(frames);
  const auto faded = fade.take(frames);
  return {volume.from * faded.from, volume.to * faded.to, frames};
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
  if (source.channels == 0 || source.channels > kPlanarChannels)
    throw std::runtime_error("No output stage for " +
                             std::to_string(source.channels) + " channels");
  follow(*now);
  if (!now->fromDsd && ramp.holding(1.0) && fade.holding(1.0) &&
      now->output.format == source.sampleFormat)
    return upstream->read(data, size);

  const size_t inFrame = frameBytes(source.sampleFormat, source.channels);
  const size_t outFrame = frameBytes(now->output.format, source.channels);
  auto *out = static_cast<uint8_t *>(data);
  const size_t wanted = size / outFrame;
  size_t done = 0;
  while (done < wanted) {
    const size_t ask = std::min(wanted - done, kPlanarFrames);
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
  // Converted DSD is metered, so it is quantised even onto float.
  const bool exact =
      !now.fromDsd && (from == to || (!isFloat(from) && !isFloat(to)));
  const unsigned rate = now.source.sampleRate;
  for (size_t at = 0; at < frames;) {
    const auto span = take(frames - at);
    const uint8_t *source = in + at * inFrame;
    uint8_t *dest = out + at * outFrame;
    if (span.from == 1.0 && span.to == 1.0 && exact) {
      if (from == to)
        std::memcpy(dest, source, span.frames * inFrame);
      else
        convertSampleFormat(source, from, span.frames * channels, dest, to,
                            span.frames * outFrame);
    } else {
      const auto began = std::chrono::steady_clock::now();
      readPlanar(source, from, channels, span.frames, planar.planes());
      truePeak.feed(planar.planes(), channels, span.frames, rate,
                    std::max(std::abs(span.from), std::abs(span.to)));
      quantiser.pack(planar.planes(), channels, span.frames, span, to,
                     now.output.bits, dest);
      load.add(std::chrono::steady_clock::now() - began, span.frames, rate);
    }
    at += span.frames;
  }
  clipped = quantiser.clipped();
  peak = truePeak.peak();
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
  clippedBefore = quantiser.clipped();
  truePeak.resetPeak();
  peak = 0.0;
  if (upstream)
    upstream->acceptSourceChange();
}
