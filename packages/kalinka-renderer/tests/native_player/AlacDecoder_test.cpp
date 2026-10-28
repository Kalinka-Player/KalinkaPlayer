#include "AlacDecoder.h"

#include <gtest/gtest.h>

#include <optional>

namespace {
using Bytes = std::vector<uint8_t>;

enum Element { SCE, CPE, CCE, LFE, DSE, PCE, FIL, END };

/// Bits most significant first, as ALAC reads them.
class BitWriter {
public:
  BitWriter &put(uint64_t value, unsigned count) {
    for (unsigned i = count; i-- > 0; ++bit) {
      if (bit % 8 == 0)
        bytes.push_back(0);
      bytes.back() |= ((value >> i) & 1) << (7 - bit % 8);
    }
    return *this;
  }

  /// A mono or stereo element's header; `frames` only for a partial frame.
  BitWriter &header(Element element, unsigned shift, bool verbatim,
                    std::optional<uint32_t> frames = {}) {
    put(element, 3).put(0, 4).put(0, 12).put(frames.has_value(), 1);
    put(shift, 2).put(verbatim, 1);
    if (frames)
      put(*frames, 32);
    return *this;
  }

  /// One channel's predictor, with `order` zero coefficients.
  BitWriter &predictor(unsigned mode = 0, unsigned order = 0,
                       unsigned quantization = 9, unsigned meanFactor = 4) {
    put(mode, 4).put(quantization, 4).put(meanFactor, 3).put(order, 5);
    return put(0, 16 * order);
  }

  /// A residual of `n` too large for a unary prefix.
  BitWriter &escaped(uint32_t n, unsigned bits) {
    return put(0x1ff, 9).put(n, bits);
  }

  /// A residual of `n` while the coder's parameter is 1.
  BitWriter &unary(unsigned n) { return put((1u << n) - 1, n).put(0, 1); }

  Bytes done() const { return bytes; }

private:
  Bytes bytes;
  size_t bit = 0;
};

struct Config {
  uint32_t frameLength = 4;
  uint8_t version = 0, bits = 16, riceLimit = 14, channels = 2, meanRate = 40;
  uint32_t rate = 44100;

  Bytes bytes() const {
    return BitWriter()
        .put(frameLength, 32)
        .put(version, 8)
        .put(bits, 8)
        .put(meanRate, 8)
        .put(10, 8)
        .put(riceLimit, 8)
        .put(channels, 8)
        .put(255, 16)
        .put(0, 64)
        .put(rate, 32)
        .done();
  }
};

/// Interleaved `samples` as little-endian PCM in `width` bytes, masked to
/// `bits`.
Bytes pcm(const std::vector<int64_t> &samples, unsigned bits, unsigned width) {
  Bytes result;
  const auto mask = bits < 32 ? (uint32_t(1) << bits) - 1 : ~uint32_t(0);
  for (const auto sample : samples)
    for (unsigned byte = 0; byte < width; ++byte)
      result.push_back((uint32_t(sample) & mask) >> (8 * byte));
  return result;
}

Bytes decode(AlacDecoder &decoder, const Bytes &unit) {
  const auto out = decoder.decode(unit);
  return {out.begin(), out.end()};
}

/// A verbatim frame of interleaved `samples` at `bits`.
Bytes verbatim(Element element, unsigned bits,
               const std::vector<int64_t> &samples,
               std::optional<uint32_t> frames = {}) {
  BitWriter frame;
  frame.header(element, 0, true, frames);
  for (const auto sample : samples)
    frame.put(uint64_t(sample), bits);
  return frame.put(END, 3).done();
}

/// A one-frame compressed element whose residuals are all 0: valid unless a
/// field says otherwise.
struct Compressed {
  Element element = CPE;
  unsigned shift = 0, mode = 0, order = 0, quantization = 9, mixShift = 0;
  uint8_t mixWeight = 0;

  Bytes bytes() const {
    const unsigned channels = element == CPE ? 2 : 1;
    BitWriter frame;
    frame.header(element, shift, false).put(mixShift, 8).put(mixWeight, 8);
    for (unsigned channel = 0; channel < channels; ++channel)
      frame.predictor(mode, order, quantization);
    frame.put(0, 8 * shift * channels);
    for (unsigned channel = 0; channel < channels; ++channel)
      frame.unary(0);
    return frame.put(END, 3).done();
  }
};

/// Mono residuals 1, then a run of four zeros, then 3.
BitWriter zeroRunResiduals() {
  BitWriter frame;
  frame.header(SCE, 0, false, 6).put(0, 16);
  return frame;
}
} // namespace

TEST(AlacDecoder, DecodesVerbatimFramesAtEveryDepth) {
  for (const unsigned bits : {16u, 24u, 32u})
    for (const unsigned channels : {1u, 2u}) {
      SCOPED_TRACE(std::to_string(bits) + " bits, " + std::to_string(channels) +
                   " channels");
      AlacDecoder decoder(
          Config{.bits = uint8_t(bits), .channels = uint8_t(channels)}.bytes());
      const auto width = bits == 16 ? 2u : 4u;
      EXPECT_EQ(decoder.format(), (StreamAudioFormat{44100, channels, bits,
                                                     bits == 16   ? PCM16_LE
                                                     : bits == 24 ? PCM24_LE
                                                                  : PCM32_LE}));
      const int64_t top = (int64_t(1) << (bits - 1)) - 1;
      std::vector<int64_t> samples;
      for (const auto value : {int64_t(0), int64_t(-1), top, -top - 1})
        samples.insert(samples.end(), channels, value);
      EXPECT_EQ(
          decode(decoder, verbatim(channels == 2 ? CPE : SCE, bits, samples)),
          pcm(samples, bits, width));
    }
}

TEST(AlacDecoder, APartialFrameSaysHowManyFramesItHolds) {
  AlacDecoder decoder(Config{.frameLength = 8}.bytes());
  EXPECT_EQ(decode(decoder, verbatim(CPE, 16, {1, 2, 3, 4, 5, 6}, 3)),
            pcm({1, 2, 3, 4, 5, 6}, 16, 2));
  EXPECT_EQ(
      decode(decoder, verbatim(CPE, 16, std::vector<int64_t>(16, 7))).size(),
      8u * 4);
}

TEST(AlacDecoder, SkipsDataAndFillElements) {
  AlacDecoder decoder(Config{.frameLength = 1, .channels = 1}.bytes());
  BitWriter frame;
  // Three bytes of fill, two of data aligned one bit on, then 15 + 2 - 1
  // bytes of fill.
  frame.put(FIL, 3).put(3, 4).put(0xffffff, 24);
  frame.put(DSE, 3).put(0, 4).put(1, 1).put(2, 8).put(0, 1).put(0xffff, 16);
  frame.put(FIL, 3).put(15, 4).put(2, 8).put(0, 8 * 16);
  frame.header(SCE, 0, true).put(0x1234, 16).put(END, 3);
  EXPECT_EQ(decode(decoder, frame.done()), pcm({0x1234}, 16, 2));
}

TEST(AlacDecoder, DecodesEscapedAndUnaryResiduals) {
  AlacDecoder decoder(Config{.channels = 1}.bytes());
  BitWriter frame;
  frame.header(SCE, 0, false).put(0, 16).predictor();
  frame.escaped(10, 16).unary(1).unary(0).unary(4).put(END, 3);
  EXPECT_EQ(decode(decoder, frame.done()), pcm({5, -1, 0, 2}, 16, 2));
}

TEST(AlacDecoder, DecodesRunsOfZerosAndFirstOrderPrediction) {
  AlacDecoder decoder(Config{.frameLength = 6, .channels = 1}.bytes());
  // After the first residual the mean is low enough for a run: four zeros in
  // a parameter of 2, then 3 coded one less, the run having stood for it.
  const auto residuals = [](BitWriter frame) {
    return frame.unary(2).put(0b1010, 4).unary(5).put(END, 3).done();
  };
  EXPECT_EQ(decode(decoder, residuals(zeroRunResiduals().predictor())),
            pcm({1, 0, 0, 0, 0, 3}, 16, 2));
  EXPECT_EQ(decode(decoder, residuals(zeroRunResiduals().predictor(15))),
            pcm({1, 1, 1, 1, 1, 4}, 16, 2));
  EXPECT_EQ(decode(decoder, residuals(zeroRunResiduals().predictor(0, 31))),
            pcm({1, 1, 1, 1, 1, 4}, 16, 2));
}

TEST(AlacDecoder, WrapsTheRunningMeanAsTheReferenceCoderDoes) {
  // At the fastest rate a header can name, rate * mean passes 32 bits on the
  // second residual, and the reference keeps only its low bits.
  AlacDecoder decoder(
      Config{.frameLength = 3, .riceLimit = 16, .channels = 1, .meanRate = 255}
          .bytes());
  BitWriter frame;
  frame.header(SCE, 0, false).put(0, 16).predictor(0, 0, 9, 7);
  frame.escaped(0xffff, 16).escaped(0xffff, 16);
  // A parameter of 16 reads 2 from these bits, where 15 would read 0.
  frame.put(0, 1).put(1, 15).put(1, 1).put(END, 3);
  EXPECT_EQ(decode(decoder, frame.done()), pcm({-32768, -32768, 1}, 16, 2));
}

TEST(AlacDecoder, UnmixesStereo) {
  AlacDecoder decoder(Config{.frameLength = 1}.bytes());
  BitWriter frame;
  // Weight 1 over 2^1: left = u + v - v / 2, right = left - v.
  frame.header(CPE, 0, false).put(1, 8).put(1, 8).predictor().predictor();
  frame.escaped(20, 17).unary(8).put(END, 3);
  EXPECT_EQ(decode(decoder, frame.done()), pcm({12, 8}, 16, 2));
}

TEST(AlacDecoder, RestoresTheBytesItShiftedOff) {
  AlacDecoder decoder(Config{.frameLength = 1, .bits = 24}.bytes());
  BitWriter frame;
  frame.header(CPE, 1, false).put(0, 16).predictor().predictor();
  frame.put(0xab, 8).put(0xcd, 8).unary(5).unary(8).put(END, 3);
  EXPECT_EQ(decode(decoder, frame.done()),
            pcm({-3 * 256 + 0xab, 4 * 256 + 0xcd}, 24, 4));
}

TEST(AlacDecoder, RefusesFramesItCannotDecode) {
  AlacDecoder stereo(Config{}.bytes());
  AlacDecoder mono(Config{.channels = 1}.bytes());
  AlacDecoder single(Config{.frameLength = 1}.bytes());
  AlacDecoder singleMono(Config{.frameLength = 1, .channels = 1}.bytes());
  AlacDecoder deep(Config{.frameLength = 1, .bits = 32}.bytes());
  AlacDecoder uncoded(Config{.frameLength = 1, .riceLimit = 0}.bytes());
  const auto good = verbatim(CPE, 16, {1, 2, 3, 4, 5, 6, 7, 8});
  // Each refusal below differs from one of these in the one thing it tests.
  const std::pair<AlacDecoder *, Bytes> valid[] = {
      {&stereo, good},
      {&single, Compressed{}.bytes()},
      {&single, Compressed{.mode = 15}.bytes()},
      {&single, Compressed{.order = 4}.bytes()},
      {&single, Compressed{.mixShift = 31, .mixWeight = 1}.bytes()},
      {&singleMono, Compressed{.element = SCE, .shift = 1}.bytes()},
      {&deep, Compressed{.shift = 2}.bytes()},
  };
  for (const auto &[decoder, unit] : valid)
    EXPECT_NO_THROW(decoder->decode(unit));

  BitWriter shiftedVerbatim;
  shiftedVerbatim.header(CPE, 1, true).put(0, 8 * 16).put(END, 3);
  BitWriter unused;
  unused.put(CPE, 3).put(0, 4).put(1, 12).put(1, 4);
  unused.put(0, 8 * 16).put(END, 3);
  BitWriter overrun;
  overrun.header(SCE, 0, false, 3).put(0, 16).predictor();
  overrun.unary(2).put(0b1011, 4).put(END, 3);
  BitWriter shortEscape;
  shortEscape.header(SCE, 0, false).put(0, 16).predictor().put(0x1ff, 9);
  const std::tuple<AlacDecoder *, Bytes, const char *> cases[] = {
      {&stereo, Bytes(), "empty"},
      {&stereo, Bytes(good.begin(), good.end() - 2), "truncated"},
      {&stereo, shiftedVerbatim.done(), "shifted verbatim"},
      {&stereo, unused.done(), "unused header bits set"},
      {&stereo, BitWriter().put(CCE, 3).put(0, 64).done(), "coupling"},
      {&stereo, BitWriter().put(LFE, 3).put(0, 64).done(), "LFE"},
      {&stereo, BitWriter().put(PCE, 3).put(0, 64).done(), "PCE"},
      {&stereo, BitWriter().put(END, 3).done(), "nothing but END"},
      {&stereo, verbatim(SCE, 16, {1, 2, 3, 4}), "mono in stereo"},
      {&mono, good, "stereo in mono"},
      {&stereo, verbatim(CPE, 16, {}, 0), "no frames"},
      {&stereo, verbatim(CPE, 16, std::vector<int64_t>(10), 5),
       "more frames than the configuration allows"},
      {&mono, overrun.done(), "a run of zeros past the frame"},
      {&mono, shortEscape.done(), "an escape cut short"},
      {&single, Compressed{.mode = 1}.bytes(), "unknown prediction mode"},
      {&single, Compressed{.order = 4, .quantization = 0}.bytes(),
       "a predictor with no quantization"},
      {&single, Compressed{.mixShift = 32, .mixWeight = 1}.bytes(),
       "a mix shifted by 32"},
      {&singleMono, Compressed{.element = SCE, .shift = 2}.bytes(),
       "every bit shifted off"},
      {&deep, Compressed{.shift = 3}.bytes(), "three bytes shifted"},
      {&deep, Compressed{}.bytes(), "a 33-bit side channel"},
      {&uncoded, Compressed{}.bytes(), "compressed with no Rice parameter"},
  };
  for (const auto &[decoder, unit, name] : cases) {
    SCOPED_TRACE(name);
    EXPECT_THROW(decoder->decode(unit), std::runtime_error);
  }
  stereo.reset();
  EXPECT_EQ(decode(stereo, good), pcm({1, 2, 3, 4, 5, 6, 7, 8}, 16, 2));
}

TEST(AlacDecoder, RefusesConfigsItCannotPlay) {
  const std::pair<Bytes, const char *> cases[] = {
      {Config{.bits = 20}.bytes(), "20 bits"},
      {Config{.channels = 6}.bytes(), "six channels"},
      {Config{.version = 1}.bytes(), "a later version"},
      {Bytes(23, 0), "too short"},
      {Config{.frameLength = 0}.bytes(), "empty frames"},
      {Config{.frameLength = 65537}.bytes(), "overlong frames"},
      {Config{.rate = 0}.bytes(), "no rate"},
      {Config{.rate = 768001}.bytes(), "too fast"},
  };
  for (const auto &[config, name] : cases) {
    SCOPED_TRACE(name);
    EXPECT_THROW(AlacDecoder{config}, std::runtime_error);
  }
  EXPECT_NO_THROW(
      AlacDecoder(Config{.frameLength = 65536, .rate = 768000}.bytes()));
}

TEST(AlacDecoder, PlaysVerbatimFramesWithoutARiceParameter) {
  // ffmpeg's uncompressed encodes leave the coder's parameters at zero.
  AlacDecoder decoder(Config{.frameLength = 1, .riceLimit = 0}.bytes());
  EXPECT_EQ(decode(decoder, verbatim(CPE, 16, {-5, 5})), pcm({-5, 5}, 16, 2));
}

TEST(AlacDecoder, IsTheDecoderForAnAlacTrack) {
  mp4::AudioTrack track;
  track.codec = mp4::AudioTrack::Codec::ALAC;
  track.config = Config{.bits = 24, .rate = 96000}.bytes();
  const auto decoder = sampleDecoder(track);
  ASSERT_NE(dynamic_cast<AlacDecoder *>(decoder.get()), nullptr);
  EXPECT_EQ(decoder->format(), (StreamAudioFormat{96000, 2, 24, PCM24_LE}));
  EXPECT_EQ(decoder->delay(), 0u);
  EXPECT_FALSE(decoder->padded());
}
