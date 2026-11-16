#include "Measure.h"

#include "DsdToPcm.h"

#include <cstdio>
#include <random>
#include <string>
#include <vector>

namespace bench {
namespace {
struct Conversion {
  unsigned dsdMultiple;
  unsigned pcmMultiple;
  DsdFilter filter;
};

const char *filterName(DsdFilter filter) {
  switch (filter) {
  case DsdFilter::Cut30k:
    return "30 kHz";
  case DsdFilter::Cut50k:
    return "50 kHz";
  case DsdFilter::Wide:
    return "wide";
  }
  return "";
}

/// Nanoseconds per stereo output frame of converting random DSD.
double nsPerOutputFrame(const Conversion &c, DsdKernel kernel) {
  constexpr size_t kFramesPerCall = 1024;
  DsdToPcm converter({44100 * c.dsdMultiple, 44100 * c.pcmMultiple, 2, c.filter,
                      DsdBitOrder::MsbFirst, kernel});
  const size_t bytes = kFramesPerCall * converter.ratio() / 8;
  std::vector<uint8_t> in(2 * bytes);
  std::mt19937 random(1);
  for (auto &byte : in)
    byte = uint8_t(random());
  std::vector<double> left(converter.maxFrames(bytes)), right(left.size());
  double *out[] = {left.data(), right.data()};
  const auto process = [&](size_t) {
    converter.process(in.data(), bytes, out);
  };
  return nsPerFrame(process, kFramesPerCall,
                    std::max<size_t>(44100 * c.pcmMultiple / 4, 32768));
}
} // namespace

void runDsd() {
  std::printf("DSD to PCM, stereo, random DSD bytes, 1024 output frames a "
              "call. Per output frame, and in brackets of one core in real "
              "time at the output rate.\n\n");
  std::printf("The FIRs are vectorised in every column but the reference; "
              "the columns differ in stage 1.\n\n");
  std::printf("| Conversion | Filter | Scalar reference | Four sums | Channel "
              "pairs | Nibble lookups |\n|---|---|---|---|---|---|\n");
  const Conversion conversions[] = {
      {64, 2, DsdFilter::Cut30k},   {64, 4, DsdFilter::Cut30k},
      {64, 4, DsdFilter::Cut50k},   {64, 4, DsdFilter::Wide},
      {64, 8, DsdFilter::Cut30k},   {128, 4, DsdFilter::Cut50k},
      {128, 8, DsdFilter::Cut50k},  {256, 8, DsdFilter::Cut30k},
      {256, 8, DsdFilter::Cut50k},  {512, 8, DsdFilter::Cut50k},
      {1024, 8, DsdFilter::Cut50k},
  };
  for (const auto &c : conversions) {
    const unsigned rate = 44100 * c.pcmMultiple;
    std::printf("| DSD%u to %.1f kHz | %s |", c.dsdMultiple, rate / 1000.0,
                filterName(c.filter));
    for (const auto kernel : {DsdKernel::Reference, DsdKernel::Sums,
                              DsdKernel::ChannelPair, DsdKernel::Nibbles})
      std::printf(" %s |", cost(nsPerOutputFrame(c, kernel), rate).c_str());
    std::printf("\n");
    std::fflush(stdout);
  }
}

} // namespace bench
