#include "AlacDecoder.h"

#include <algorithm>
#include <array>
#include <bit>
#include <cstring>
#include <stdexcept>
#include <vector>

namespace {
enum Element { SCE, CPE, CCE, LFE, DSE, PCE, FIL, END };

/// Nine ones in a row escape a value to its raw bits.
constexpr unsigned ESCAPE_PREFIX = 9;
constexpr uint32_t MEAN_CLAMP = 0xffff;
constexpr uint32_t ZERO_RUN_MEAN = 128;
constexpr uint32_t LONGEST_ZERO_RUN = 65535;

[[noreturn]] void invalidAudio() {
  throw std::runtime_error("Invalid ALAC audio in MP4");
}

int32_t signExtend(uint32_t value, unsigned bits) {
  const auto shift = 32 - bits;
  return int32_t(value << shift) >> shift;
}

int sign(int32_t value) { return (value > 0) - (value < 0); }

/// Big-endian bits of one access unit, every read checked against its end.
class Reader {
public:
  explicit Reader(std::span<const uint8_t> bytes) : bytes(bytes) {}

  /// The next @p count bits, at most 32.
  uint32_t read(unsigned count) {
    const auto value = count ? uint32_t(window() >> (64 - count)) : 0;
    skip(count);
    return value;
  }

  /// At least the next 57 bits, most significant first, zero past the end;
  /// whatever a caller decodes from them it must then skip().
  uint64_t window() const {
    const auto first = position / 8;
    uint64_t value = 0;
    if (bytes.size() - first >= sizeof value) {
      std::memcpy(&value, bytes.data() + first, sizeof value);
      if constexpr (std::endian::native == std::endian::little)
        value = std::byteswap(value);
    } else
      for (size_t i = first; i < bytes.size(); ++i)
        value |= uint64_t(bytes[i]) << (56 - 8 * (i - first));
    return value << (position % 8);
  }

  void skip(size_t count) {
    if (count > bytes.size() * 8 - position)
      invalidAudio();
    position += count;
  }

  void align() { skip((8 - position % 8) % 8); }

private:
  std::span<const uint8_t> bytes;
  size_t position = 0;
};

/// The entropy coder's parameters for a whole stream.
struct Rice {
  uint32_t initialMean;
  unsigned meanRate, limit;
};

/// One channel's adaptive linear predictor.
struct Predictor {
  unsigned mode, quantization, meanFactor, order;
  std::array<int16_t, 31> coefficients;
};

/// One Golomb-Rice value: a unary count of @p modulus and a remainder in at
/// most @p k bits, or @p raw bits after an escape. The longest, 9 + 32 bits,
/// fits in one window.
uint32_t riceValue(Reader &bits, uint32_t modulus, unsigned k, unsigned raw) {
  const auto window = bits.window();
  const auto prefix = unsigned(std::countl_one(window));
  if (prefix >= ESCAPE_PREFIX) {
    bits.skip(ESCAPE_PREFIX + raw);
    return uint32_t(window << ESCAPE_PREFIX >> (64 - raw));
  }
  auto value = prefix * modulus;
  auto used = prefix + 1;
  if (k > 1) {
    // A remainder below 2 is sent in k - 1 bits.
    const auto remainder = uint32_t(window << used >> (64 - k));
    if (remainder >= 2) {
      value += remainder - 1;
      used += k;
    } else
      used += k - 1;
  }
  bits.skip(used);
  return value;
}

/// One channel's residuals: Golomb-Rice values whose parameter follows a
/// running mean, with runs of zeros where the mean falls low.
void readResiduals(Reader &bits, std::span<int32_t> out, unsigned chanBits,
                   const Rice &rice, unsigned meanFactor) {
  const uint32_t rate = rice.meanRate * meanFactor / 4;
  const uint32_t limitMask = (1u << rice.limit) - 1;
  uint32_t mean = rice.initialMean, afterRun = 0;
  for (size_t i = 0; i < out.size();) {
    const auto k =
        std::min(unsigned(std::bit_width((mean >> 9) + 3)) - 1, rice.limit);
    const auto n = riceValue(bits, (1u << k) - 1, k, chanBits);
    const uint32_t folded = n + afterRun;
    const uint32_t magnitude = (folded + 1) >> 1;
    out[i++] = int32_t(folded & 1 ? 0u - magnitude : magnitude);
    // Wraps in 32 bits, as the reference coder's mean does.
    mean = n > MEAN_CLAMP ? MEAN_CLAMP
                          : rate * folded + mean - ((rate * mean) >> 9);
    afterRun = 0;
    if (mean < ZERO_RUN_MEAN && i < out.size()) {
      const auto runK =
          unsigned(std::countl_zero(mean)) - 24 + ((mean + 16) >> 6);
      const auto run =
          riceValue(bits, ((1u << runK) - 1) & limitMask, runK, 16);
      if (run > out.size() - i)
        invalidAudio();
      std::fill_n(out.begin() + i, run, 0);
      i += run;
      afterRun = run < LONGEST_ZERO_RUN;
      mean = 0;
    }
  }
}

void firstOrder(std::span<const int32_t> in, std::span<int32_t> out,
                unsigned bits) {
  out[0] = in[0];
  for (size_t j = 1; j < in.size(); ++j)
    out[j] = signExtend(uint32_t(in[j]) + uint32_t(out[j - 1]), bits);
}

/// Rebuilds samples from residuals with the adaptive linear predictor,
/// whose coefficients learn from each residual's sign.
void predict(std::span<const int32_t> in, std::span<int32_t> out,
             Predictor predictor, unsigned bits) {
  if (!predictor.order) {
    std::copy(in.begin(), in.end(), out.begin());
    return;
  }
  if (predictor.order == 31) {
    firstOrder(in, out, bits);
    return;
  }
  const size_t order = predictor.order;
  const auto quantization = predictor.quantization;
  auto &coefficients = predictor.coefficients;
  const auto warmUp = std::min(order + 1, in.size());
  firstOrder(in.first(warmUp), out.first(warmUp), bits);
  const uint32_t half = 1u << (quantization - 1);
  for (size_t j = order + 1; j < in.size(); ++j) {
    const auto top = uint32_t(out[j - order - 1]);
    uint32_t sum = 0;
    for (size_t k = 0; k < order; ++k)
      sum += uint32_t(coefficients[k]) * (uint32_t(out[j - 1 - k]) - top);
    const auto residual = in[j];
    out[j] = signExtend(uint32_t(residual) + top +
                            uint32_t(int32_t(sum + half) >> quantization),
                        bits);
    const auto direction = sign(residual);
    auto error = residual;
    for (size_t k = order; direction && k-- > 0;) {
      const auto difference = int32_t(top - uint32_t(out[j - 1 - k]));
      const auto step = sign(difference) * direction;
      coefficients[k] = int16_t(coefficients[k] - step);
      const auto change =
          int32_t(uint32_t(step) * uint32_t(difference)) >> quantization;
      error = int32_t(uint32_t(error) - uint32_t(order - k) * uint32_t(change));
      if (direction > 0 ? error <= 0 : error >= 0)
        break;
    }
  }
}

/// Recovers left and right, in place, from the encoder's weighted mix.
void unmix(std::span<int32_t> u, std::span<int32_t> v, unsigned shift,
           int8_t weight) {
  for (size_t i = 0; i < u.size(); ++i) {
    const auto weighted =
        int32_t(uint32_t(int32_t(weight)) * uint32_t(v[i])) >> shift;
    const auto left = uint32_t(u[i]) + uint32_t(v[i]) - uint32_t(weighted);
    u[i] = int32_t(left);
    v[i] = int32_t(left - uint32_t(v[i]));
  }
}

Predictor predictor(Reader &bits) {
  Predictor result{};
  result.mode = bits.read(4);
  result.quantization = bits.read(4);
  result.meanFactor = bits.read(3);
  result.order = bits.read(5);
  for (unsigned i = 0; i < result.order; ++i)
    result.coefficients[i] = int16_t(bits.read(16));
  // Mode 15 runs a first-order pass before the predictor.
  if ((result.mode != 0 && result.mode != 15) ||
      (result.order && result.order < 31 && !result.quantization))
    invalidAudio();
  return result;
}

void skipData(Reader &bits) {
  bits.read(4);
  const bool aligned = bits.read(1);
  auto count = bits.read(8);
  if (count == 255)
    count += bits.read(8);
  if (aligned)
    bits.align();
  bits.skip(size_t(count) * 8);
}

void skipFill(Reader &bits) {
  auto count = bits.read(4);
  if (count == 15)
    count += bits.read(8) - 1;
  bits.skip(size_t(count) * 8);
}
} // namespace

/// The configuration, and the buffers every frame decodes through.
class AlacDecoder::Frames {
public:
  explicit Frames(std::span<const uint8_t> config) {
    if (config.size() != 24)
      mp4::invalid();
    frameLength = mp4::integer(config, 0, 4);
    const unsigned bits = config[5], channels = config[9];
    const auto rate = unsigned(mp4::integer(config, 20, 4));
    // The mean keeps the parameter far below 31, so a larger limit is moot.
    rice = {config[7], config[6], std::min(config[8], uint8_t(31))};
    if (config[4] || !frameLength || frameLength > 65536 ||
        (bits != 16 && bits != 24 && bits != 32) ||
        (channels != 1 && channels != 2) || !rate || rate > 768000)
      mp4::invalid();
    audio = {rate, channels, bits,
             bits == 16   ? PCM16_LE
             : bits == 24 ? PCM24_LE
                          : PCM32_LE};
    residuals.resize(frameLength);
    for (auto &channel : samples)
      channel.resize(frameLength);
  }

  const StreamAudioFormat &format() const { return audio; }

  std::span<const uint8_t> decode(std::span<const uint8_t> unit) {
    Reader bits(unit);
    for (;;)
      switch (const auto element = bits.read(3)) {
      case DSE:
        skipData(bits);
        break;
      case FIL:
        skipFill(bits);
        break;
      case SCE:
      case CPE: {
        const bool stereo = element == CPE;
        if (audio.channels != (stereo ? 2u : 1u))
          invalidAudio();
        const auto frames = audioElement(bits, stereo);
        const int32_t *const channels[] = {samples[0].data(),
                                           samples[1].data()};
        interleave(audio, channels, frames, pcm);
        return pcm;
      }
      default:
        invalidAudio();
      }
  }

private:
  StreamAudioFormat audio{};
  size_t frameLength = 0;
  Rice rice{};
  std::vector<int32_t> residuals;
  std::array<std::vector<int32_t>, 2> samples;
  std::vector<uint8_t> pcm;

  /// Decodes a mono or stereo element into `samples`.
  /// @return Its frames.
  size_t audioElement(Reader &bits, bool stereo) {
    bits.read(4);
    if (bits.read(12))
      invalidAudio();
    const bool partial = bits.read(1);
    const unsigned shift = 8 * bits.read(2);
    const bool verbatim = bits.read(1);
    size_t frames = frameLength;
    if (partial) {
      frames = bits.read(32);
      if (!frames || frames > frameLength)
        invalidAudio();
    }
    const unsigned channels = stereo ? 2 : 1;
    const auto depth = audio.bitsPerSample;
    if (verbatim) {
      if (shift)
        invalidAudio();
      for (size_t i = 0; i < frames; ++i)
        for (unsigned channel = 0; channel < channels; ++channel)
          samples[channel][i] = signExtend(bits.read(depth), depth);
      return frames;
    }
    // Verbatim encoders may leave the coder unconfigured; only this needs it.
    if (shift > 16 || shift >= depth || !rice.limit)
      invalidAudio();
    // Stereo's side channel needs one bit more than the samples.
    const unsigned chanBits = depth - shift + (stereo ? 1 : 0);
    if (chanBits > 32)
      invalidAudio();
    const auto mixShift = bits.read(8);
    const auto mixWeight = int8_t(bits.read(8));
    std::array<Predictor, 2> predictors{};
    for (unsigned channel = 0; channel < channels; ++channel)
      predictors[channel] = predictor(bits);
    // The low bytes the shift took off come before the residuals.
    auto low = bits;
    bits.skip(size_t(shift) * channels * frames);
    const std::span residual(residuals.data(), frames);
    for (unsigned channel = 0; channel < channels; ++channel) {
      const auto &channelPredictor = predictors[channel];
      readResiduals(bits, residual, chanBits, rice,
                    channelPredictor.meanFactor);
      if (channelPredictor.mode == 15)
        firstOrder(residual, residual, chanBits);
      predict(residual, std::span(samples[channel].data(), frames),
              channelPredictor, chanBits);
    }
    if (stereo && mixWeight) {
      if (mixShift > 31)
        invalidAudio();
      unmix(std::span(samples[0].data(), frames),
            std::span(samples[1].data(), frames), mixShift, mixWeight);
    }
    if (shift)
      for (size_t i = 0; i < frames; ++i)
        for (unsigned channel = 0; channel < channels; ++channel)
          samples[channel][i] =
              int32_t(uint32_t(samples[channel][i]) << shift | low.read(shift));
    return frames;
  }
};

AlacDecoder::AlacDecoder(std::span<const uint8_t> config)
    : frames(std::make_unique<Frames>(config)) {}

AlacDecoder::~AlacDecoder() = default;

StreamAudioFormat AlacDecoder::format() const { return frames->format(); }

std::span<const uint8_t> AlacDecoder::decode(std::span<const uint8_t> unit) {
  return frames->decode(unit);
}
