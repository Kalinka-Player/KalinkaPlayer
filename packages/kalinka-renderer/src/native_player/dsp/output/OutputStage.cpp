#include "dsp/output/OutputStage.h"

#include "dsp/output/PlanarPcm.h"

#include <spdlog/spdlog.h>

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

unsigned outputChannels(const StreamAudioFormat &source) {
  return source.channels == 1 && !isDsd(source.sampleFormat)
             ? kPcmDeviceChannels
             : source.channels;
}

bool convertedFromDsd(const StreamAudioFormat &decoded,
                      const StreamAudioFormat &source) {
  return isDsd(decoded.sampleFormat) && !isDsd(source.sampleFormat);
}

bool convertedFromDsd(const StreamInfo &info) {
  return info.decodedFormat &&
         convertedFromDsd(*info.decodedFormat, info.format);
}

peq::Plan prepareEq(const peq::Settings &settings,
                    const StreamAudioFormat &source) {
  auto plan = isDsd(source.sampleFormat)
                  ? peq::Plan{}
                  : peq::prepare(settings.profile.get(), source.sampleRate,
                                 outputChannels(source));
  if (!plan.applicable)
    spdlog::warn("Skipping room profile '{}' at {} Hz", settings.profile->name,
                 source.sampleRate);
  return plan;
}
} // namespace

OutputStage::OutputStage(std::vector<StreamAudioFormat> device, uint64_t seed)
    : device(std::move(device)), ramp(kRamp), fade(kFadeLength),
      quantiser(kPlanarFrames, kPlanarChannels, seed),
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
    std::lock_guard prepareLock(eqPreparationMutex);
    std::lock_guard lock(presentedMutex);
    presented.reset();
    reading.reset();
    discardAtBoundary = false;
  }
  settlePending = true;
  setState(StreamState(AudioGraphNodeState::STOPPED));
}

void OutputStage::onUpstreamState(const StreamState &state) {
  StreamState shown = state;
  // A source change names the stream to come, not the one still being read.
  if (state.streamId && state.state != AudioGraphNodeState::SOURCE_CHANGED) {
    std::lock_guard lock(presentedMutex);
    if (reading != state.streamId && discardAtBoundary) {
      resetEq = true;
      discardAtBoundary = false;
    }
    reading = state.streamId;
  }
  if (state.streamInfo && state.streamInfo->streamType == StreamType::FRAMES) {
    const StreamAudioFormat &source = state.streamInfo->format;
    const bool fromDsd = convertedFromDsd(*state.streamInfo);
    const auto now = present(source, fromDsd);
    auto &format = shown.streamInfo->format;
    format.sampleFormat = now.output.format;
    format.channels = now.channels;
    // EQ and fades can start during a run, so its device precision stays fixed.
    if (!isDsd(source.sampleFormat) && !isFloat(format.sampleFormat)) {
      format.bitsPerSample =
          isFloat(source.sampleFormat)
              ? now.output.bits
              : std::max(now.output.bits, source.bitsPerSample);
    }
    if ((format.bitsPerSample != source.bitsPerSample ||
         isFloat(source.sampleFormat) != isFloat(format.sampleFormat) ||
         format.channels != source.channels) &&
        !shown.streamInfo->decodedFormat)
      shown.streamInfo->decodedFormat = source;
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

void OutputStage::discardRoomEq(std::optional<StreamId> id) {
  std::lock_guard lock(presentedMutex);
  if (!id)
    resetEq = true;
  if (reading && (!id || reading == id))
    discardAtBoundary = true;
}

OutputStage::Presented OutputStage::present(const StreamAudioFormat &source,
                                            bool fromDsd) {
  const auto matches = [&](const std::optional<Presented> &p) {
    return p && p->source == source && p->fromDsd == fromDsd;
  };
  if (const auto existing = current(); matches(existing))
    return *existing;
  std::lock_guard prepareLock(eqPreparationMutex);
  const auto before = current();
  if (matches(before))
    return *before;
  Presented next{source, chooseOutput(source, device, kPcmDeviceChannels),
                 outputChannels(source), fromDsd};
  const bool changed =
      !before || before->source.sampleRate != source.sampleRate ||
      before->channels != next.channels ||
      isDsd(before->source.sampleFormat) != isDsd(source.sampleFormat);
  const auto plan = changed ? prepareEq(eqSettings, source) : peq::Plan{};
  std::lock_guard lock(presentedMutex);
  if (changed) {
    eqPlan = plan;
    ++eqRevision;
    resetEq = true;
  }
  presented = next;
  return next;
}

void OutputStage::setRoomEq(peq::Settings settings) {
  std::lock_guard prepareLock(eqPreparationMutex);
  if (settings.profile == eqSettings.profile) {
    std::lock_guard lock(presentedMutex);
    if (settings.preampDb != eqSettings.preampDb) {
      eqSettings.preampDb = settings.preampDb;
      ++eqRevision;
    }
    return;
  }
  const auto now = current();
  const auto plan = now ? prepareEq(settings, now->source) : peq::Plan{};
  std::lock_guard lock(presentedMutex);
  eqSettings = std::move(settings);
  eqPlan = plan;
  ++eqRevision;
}

std::vector<SignalStep>
OutputStage::describe(const StreamAudioFormat &decoded,
                      const StreamAudioFormat &source,
                      const StreamAudioFormat &output) const {
  std::vector<SignalStep> steps;
  if (isDsd(source.sampleFormat))
    return steps;
  peq::Settings settings;
  bool active;
  double preampDb;
  bool prepared;
  {
    std::lock_guard lock(presentedMutex);
    settings = eqSettings;
    prepared = presented && presented->source.sampleRate == source.sampleRate &&
               presented->channels == outputChannels(source) &&
               !isDsd(presented->source.sampleFormat);
    active = eqPlan.active;
    preampDb = settings.preampDb.value_or(eqPlan.automaticPreampDb);
  }
  if (!prepared) {
    const auto plan = peq::prepare(settings.profile.get(), source.sampleRate,
                                   outputChannels(source));
    active = plan.active;
    preampDb = settings.preampDb.value_or(plan.automaticPreampDb);
  }
  const auto level = expansionTarget.load();
  const bool expanded = source.channels == 2 && level != ExpansionLevel::Off;
  if (expanded)
    steps.push_back({signal_step::kStereoExpansion, "Stereo expansion",
                     level == ExpansionLevel::Strong ? "Strong" : "Normal"});
  if (active)
    steps.push_back(
        {signal_step::kPeq, "Parametric EQ", settings.profile->name});
  if (preampDb != 0)
    steps.push_back(
        {signal_step::kPreamp, "Preamp", std::format("{:+.2f} dB", preampDb)});
  const double gain = dsdGain.load();
  if (convertedFromDsd(decoded, source) && gain != 1.0)
    steps.push_back({signal_step::kDsdGain, "DSD gain",
                     std::format("{:+.1f} dB", 20 * std::log10(gain))});
  if ((isFloat(source.sampleFormat) || expanded || active || preampDb != 0) &&
      !isFloat(output.sampleFormat)) {
    const auto choice = chooseOutput(source, device, kPcmDeviceChannels);
    const auto bits = choice.format == output.sampleFormat
                          ? choice.bits
                          : output.bitsPerSample;
    steps.push_back(
        {signal_step::kDither, "Dither", std::format("TPDF at {} bits", bits)});
  }
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
  const bool reset = settlePending.exchange(false);
  const bool clearHistory = std::exchange(resetEq, false) || reset;
  expansion.follow(now.source.channels == 2 ? rate : 0, expansionTarget.load(),
                   clearHistory);
  if (clearHistory || (eqRevision != appliedRevision && !eq.transitioning())) {
    eq.follow(eqPlan, eqSettings.preampDb.value_or(eqPlan.automaticPreampDb),
              rate, clearHistory);
    appliedRevision = eqRevision;
  }
  const double gain = target.load() * (now.fromDsd ? dsdGain.load() : 1.0);
  if (reset)
    ramp.settle(gain);
  else
    ramp.aim(gain, rate);
  if (fadeSettle)
    fade.settle(*std::exchange(fadeSettle, std::nullopt));
  fade.aim(fadeTarget, rate);
}

size_t OutputStage::read(void *data, size_t size) {
  if (!upstream)
    return 0;
  std::optional<Presented> now;
  {
    std::lock_guard lock(presentedMutex);
    now = presented;
    if (now && !isDsd(now->source.sampleFormat))
      follow(*now);
  }
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
  if (!expansion.active() && eq.bypassed() && ramp.holding(1.0) &&
      fade.holding(1.0) && now->output.format == source.sampleFormat) {
    if (source.channels == now->channels)
      return upstream->read(data, size);
    const auto width = sampleSize(source.sampleFormat);
    const auto frames =
        upstream->read(data, size / (now->channels * width) * width) / width;
    expandMono(static_cast<uint8_t *>(data), frames, now->channels, width);
    return frames * now->channels * width;
  }

  const size_t inFrame = frameBytes(source.sampleFormat, source.channels);
  const size_t outFrame = frameBytes(now->output.format, now->channels);
  auto *out = static_cast<uint8_t *>(data);
  const size_t wanted = size / outFrame;
  size_t done = 0;
  while (done < wanted) {
    const size_t ask = std::min(wanted - done, kPlanarFrames);
    const size_t got = upstream->read(input.data(), ask * inFrame) / inFrame;
    if (got == 0)
      break;
    if (source.channels != now->channels)
      expandMono(input.data(), got, now->channels,
                 sampleSize(source.sampleFormat));
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
  const unsigned channels = now.channels;
  const size_t inFrame = frameBytes(from, channels);
  const size_t outFrame = frameBytes(to, channels);
  // Integer to a wider container is exact; float to integer is quantised.
  const bool exact = from == to || (!isFloat(from) && !isFloat(to));
  for (size_t at = 0; at < frames;) {
    size_t n = expansion.span(eq.span(frames - at));
    for (const auto *r : {&ramp, &fade})
      if (r->left())
        n = std::min(n, r->left());
    const auto volume = ramp.take(n), faded = fade.take(n),
               preamp = eq.takePreamp(n);
    const int movingRamps = (volume.from != volume.to) +
                            (faded.from != faded.to) +
                            (preamp.from != preamp.to);
    const bool overlapping = movingRamps > 1;
    GainRamp::Span span{volume.from * faded.from * preamp.from,
                        volume.to * faded.to * preamp.to, n};
    const uint8_t *source = in + at * inFrame;
    uint8_t *dest = out + at * outFrame;
    if (!overlapping && !expansion.active() && !eq.filtering() &&
        span.from == 1.0 && span.to == 1.0 && exact) {
      if (from == to)
        std::memcpy(dest, source, span.frames * inFrame);
      else
        convertSampleFormat(source, from, span.frames * channels, dest, to,
                            span.frames * outFrame);
    } else {
      readPlanar(source, from, channels, span.frames, planar.planes());
      if (expansion.active())
        expansion.process(planar.planes()[0], planar.planes()[1], n);
      if (eq.filtering())
        eq.process(planar.planes(), channels, span.frames);
      // The product of simultaneous linear ramps is curved between the
      // endpoints.
      if (overlapping) {
        for (size_t i = 0; i < n; ++i) {
          const double t = double(i + 1) / double(n);
          const double gain = std::lerp(faded.from, faded.to, t) *
                              std::lerp(preamp.from, preamp.to, t);
          for (unsigned ch = 0; ch < channels; ++ch)
            planar.planes()[ch][i] *= gain;
        }
        span = volume;
      }
      quantiser.pack(planar.planes(), channels, span.frames, span, to,
                     now.output.bits, dest);
    }
    at += span.frames;
  }
  const uint64_t total = quantiser.clipped();
  clipped = total;
  if (events && total > clippedPosted) {
    clippedPosted = total;
    std::optional<StreamId> stream;
    {
      std::lock_guard lock(presentedMutex);
      stream = reading;
    }
    events->post({stream, Clipping{total - clippedBefore}});
  }
}

size_t OutputStage::toSource(const Presented &now, size_t bytes) const {
  const unsigned channels = now.source.channels;
  const size_t inFrame = frameBytes(now.source.sampleFormat, channels);
  const size_t outFrame = frameBytes(now.output.format, now.channels);
  if (inFrame == outFrame)
    return bytes;
  return (bytes + outFrame - 1) / outFrame * inFrame;
}

size_t OutputStage::toOutput(const Presented &now, size_t bytes) const {
  const unsigned channels = now.source.channels;
  const size_t inFrame = frameBytes(now.source.sampleFormat, channels);
  const size_t outFrame = frameBytes(now.output.format, now.channels);
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
  clippedBefore = clippedPosted = quantiser.clipped();
  {
    // The successor can even reuse the discarded stream ID.
    std::lock_guard lock(presentedMutex);
    if (std::exchange(discardAtBoundary, false))
      resetEq = true;
  }
  if (upstream)
    upstream->acceptSourceChange();
}
