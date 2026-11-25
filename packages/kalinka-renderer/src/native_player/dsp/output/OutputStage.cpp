#include "dsp/output/OutputStage.h"

#include "dsp/fir/FirFilterBank.h"
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
                    const StreamAudioFormat &source,
                    const std::shared_ptr<const FirFilterBank> &fir) {
  auto plan = isDsd(source.sampleFormat)
                  ? peq::Plan{}
                  : peq::prepare(settings.profile.get(), source.sampleRate,
                                 outputChannels(source));
  if (!plan.applicable)
    spdlog::warn("Skipping room profile '{}' at {} Hz", settings.profile->name,
                 source.sampleRate);
  if (fir && source.channels == 2 && !isDsd(source.sampleFormat)) {
    if (const auto variant = fir->at(source.sampleRate))
      plan.automaticPreampDb += variant->automaticPreampDb;
  }
  return plan;
}
} // namespace

OutputStage::OutputStage(std::vector<StreamAudioFormat> device, uint64_t seed,
                         std::vector<unsigned> firRates)
    : device(std::move(device)), firRates(std::move(firRates)), ramp(kRamp),
      fade(kFadeLength), quantiser(kPlanarFrames, kPlanarChannels, seed),
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
    const bool fromInteger =
        state.streamInfo->decodedFormat && !fromDsd &&
        !isFloat(state.streamInfo->decodedFormat->sampleFormat) &&
        isFloat(source.sampleFormat);
    // Keep the stream's hardware format fixed across live FIR changes.
    // For installed profiles, reserve the DAC's processing precision. Other
    // rates keep their decoded precision, including a first WAV added mid-song.
    const bool useDecoded =
        !fromDsd && state.streamInfo->decodedFormat &&
        std::ranges::find(firRates, source.sampleRate) == firRates.end();
    const auto &preferred =
        useDecoded ? *state.streamInfo->decodedFormat : source;
    const auto now = present(source, fromDsd, fromInteger, preferred);
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
    if (isFloat(format.sampleFormat))
      format.bitsPerSample = sampleBits(format.sampleFormat);
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

OutputStage::Presented
OutputStage::present(const StreamAudioFormat &source, bool fromDsd,
                     bool fromInteger, const StreamAudioFormat &preferred) {
  const auto matches = [&](const std::optional<Presented> &p) {
    return p && p->source == source && p->fromDsd == fromDsd &&
           p->fromInteger == fromInteger && p->preferred == preferred;
  };
  if (const auto existing = current(); matches(existing))
    return *existing;
  std::lock_guard prepareLock(eqPreparationMutex);
  const auto before = current();
  if (matches(before))
    return *before;
  Presented next{source,
                 chooseOutput(preferred, device, kPcmDeviceChannels),
                 outputChannels(source),
                 fromDsd,
                 fromInteger,
                 preferred};
  const bool layoutChanged =
      !before || before->source.sampleRate != source.sampleRate ||
      before->channels != next.channels ||
      isDsd(before->source.sampleFormat) != isDsd(source.sampleFormat);
  // Mono expands to stereo after FIR. A gapless join can change FIR headroom
  // while both PEQ channels still carry the preceding stream's history.
  const bool replan =
      layoutChanged ||
      (automaticFir && before->source.channels != source.channels);
  const auto plan =
      replan ? prepareEq(eqSettings, source, automaticFir) : peq::Plan{};
  std::lock_guard lock(presentedMutex);
  if (replan) {
    eqPlan = plan;
    ++eqRevision;
    resetEq |= layoutChanged;
  }
  presented = next;
  return next;
}

void OutputStage::resetClipping() {
  ++clippingRevision;
  if (std::exchange(windowClips, 0) && events)
    events->post({reading, Clipping{0}});
}

void OutputStage::setRoomEq(peq::Settings settings,
                            std::shared_ptr<const FirFilterBank> fir) {
  if (settings.preampDb)
    fir.reset();
  std::lock_guard prepareLock(eqPreparationMutex);
  if (settings.profile == eqSettings.profile && fir == automaticFir) {
    std::lock_guard lock(presentedMutex);
    // The same effective PEQ plan can accompany a FIR or bypass change.
    resetClipping();
    if (settings.preampDb != eqSettings.preampDb) {
      eqSettings.preampDb = settings.preampDb;
      ++eqRevision;
    }
    return;
  }
  const auto now = current();
  const auto plan = now ? prepareEq(settings, now->source, fir) : peq::Plan{};
  std::lock_guard lock(presentedMutex);
  resetClipping();
  eqSettings = std::move(settings);
  automaticFir = std::move(fir);
  eqPlan = plan;
  ++eqRevision;
}

std::vector<SignalStep> OutputStage::describe(const StreamAudioFormat &decoded,
                                              const StreamAudioFormat &source,
                                              const StreamAudioFormat &output,
                                              bool firDry) const {
  std::vector<SignalStep> steps;
  if (isDsd(source.sampleFormat))
    return steps;
  peq::Settings settings;
  std::shared_ptr<const FirFilterBank> fir;
  bool active;
  double preampDb;
  bool prepared;
  {
    std::lock_guard lock(presentedMutex);
    settings = eqSettings;
    fir = automaticFir;
    prepared = presented && presented->source.sampleRate == source.sampleRate &&
               presented->source.channels == source.channels &&
               !isDsd(presented->source.sampleFormat);
    active = eqPlan.active;
    preampDb = settings.preampDb.value_or(eqPlan.automaticPreampDb);
  }
  if (!prepared) {
    const auto plan = prepareEq(settings, source, fir);
    active = plan.active;
    preampDb = settings.preampDb.value_or(plan.automaticPreampDb);
  }
  if (active)
    steps.push_back(
        {signal_step::kPeq, "Parametric EQ", settings.profile->name});
  if (preampDb != 0)
    steps.push_back(
        {signal_step::kPreamp, "Headroom", std::format("{:+.2f} dB", preampDb)});
  const double gain = dsdGain.load();
  if (convertedFromDsd(decoded, source) && gain != 1.0)
    steps.push_back({signal_step::kDsdGain, "DSD gain",
                     std::format("{:+.1f} dB", 20 * std::log10(gain))});
  if (((isFloat(source.sampleFormat) &&
        !(firDry && !isFloat(decoded.sampleFormat) &&
          output.bitsPerSample >= decoded.bitsPerSample)) ||
       active || preampDb != 0) &&
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
  std::optional<uint64_t> meteringRevision;
  {
    std::lock_guard lock(presentedMutex);
    now = presented;
    if (now && !isDsd(now->source.sampleFormat)) {
      follow(*now);
      if (appliedRevision == eqRevision)
        meteringRevision = clippingRevision;
    }
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
  if (eq.bypassed() && ramp.holding(1.0) && fade.holding(1.0) &&
      now->output.format == source.sampleFormat) {
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
  uint64_t newClips = 0;
  while (done < wanted) {
    const size_t ask = std::min(wanted - done, kPlanarFrames);
    const size_t got = upstream->read(input.data(), ask * inFrame) / inFrame;
    if (got == 0)
      break;
    if (source.channels != now->channels)
      expandMono(input.data(), got, now->channels,
                 sampleSize(source.sampleFormat));
    const size_t unsettled = unsettledFrames ? unsettledFrames() : 0;
    newClips +=
        produce(*now, input.data(), got, out + done * outFrame, unsettled);
    done += got;
    if (got < ask)
      break;
  }
  clipped = quantiser.clipped();
  if (newClips && events) {
    std::lock_guard lock(presentedMutex);
    if (meteringRevision == clippingRevision) {
      windowClips += newClips;
      events->post({reading, Clipping{windowClips}});
    }
  }
  return done * outFrame;
}

uint64_t OutputStage::produce(const Presented &now, const uint8_t *in,
                              size_t frames, uint8_t *out, size_t unsettled) {
  uint64_t newClips = 0;
  const AudioSampleFormat from = now.source.sampleFormat;
  const AudioSampleFormat to = now.output.format;
  const unsigned channels = now.channels;
  const size_t inFrame = frameBytes(from, channels);
  const size_t outFrame = frameBytes(to, channels);
  // Integer to a wider container is exact; float to integer is quantised.
  const bool exact = from == to || (!isFloat(from) && !isFloat(to));
  for (size_t at = 0; at < frames;) {
    size_t n = eq.span(frames - at);
    if (at < unsettled)
      n = std::min(n, unsettled - at);
    for (const auto *r : {&ramp, &fade})
      if (r->left())
        n = std::min(n, r->left());
    const auto volume = ramp.take(n), faded = fade.take(n),
               preamp = eq.takePreamp(n);
    const bool countClips =
        at >= unsettled && !eq.transitioning() && preamp.from == preamp.to;
    const int movingRamps = (volume.from != volume.to) +
                            (faded.from != faded.to) +
                            (preamp.from != preamp.to);
    const bool overlapping = movingRamps > 1;
    GainRamp::Span span{volume.from * faded.from * preamp.from,
                        volume.to * faded.to * preamp.to, n};
    const uint8_t *source = in + at * inFrame;
    uint8_t *dest = out + at * outFrame;
    if (!overlapping && !eq.filtering() && span.from == 1.0 && span.to == 1.0 &&
        exact) {
      if (from == to)
        std::memcpy(dest, source, span.frames * inFrame);
      else
        convertSampleFormat(source, from, span.frames * channels, dest, to,
                            span.frames * outFrame);
    } else {
      readPlanar(source, from, channels, span.frames, planar.planes());
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
      const uint64_t before = quantiser.clipped();
      quantiser.pack(planar.planes(), channels, span.frames, span, to,
                     now.output.bits, dest,
                     now.fromInteger && !eq.filtering() && !overlapping);
      if (countClips)
        newClips += quantiser.clipped() - before;
    }
    at += span.frames;
  }
  return newClips;
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
  {
    // The successor can even reuse the discarded stream ID.
    std::lock_guard lock(presentedMutex);
    ++clippingRevision;
    windowClips = 0;
    if (std::exchange(discardAtBoundary, false))
      resetEq = true;
  }
  if (upstream)
    upstream->acceptSourceChange();
}
