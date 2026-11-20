// Render the reference DSF files in this comparison with Kalinka's release DSP.
// Output is raw little-endian float64, interleaved by channel, at 176400 Hz.
#include "dsp/dsd/DsdToPcm.h"
#include <algorithm>
#include <array>
#include <bit>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

int main(int argc, char **argv) try {
  if (argc != 4)
    throw std::runtime_error(
        "Usage: render SOURCE.dsf 30k|50k|wide OUTPUT.f64");
  static_assert(std::endian::native == std::endian::little);
  std::ifstream input(argv[1], std::ios::binary);
  input.exceptions(std::ios::failbit | std::ios::badbit);
  std::array<unsigned char, 92> header{};
  input.read(reinterpret_cast<char *>(header.data()), header.size());
  auto read32 = [&](size_t offset) {
    uint32_t x;
    std::memcpy(&x, header.data() + offset, sizeof(x));
    return x;
  };
  uint64_t count;
  std::memcpy(&count, header.data() + 64, sizeof(count));
  const auto channels = read32(52);
  const size_t block = read32(72);
  if (std::memcmp(header.data(), "DSD ", 4) ||
      std::memcmp(header.data() + 28, "fmt ", 4) ||
      std::memcmp(header.data() + 80, "data", 4) || read32(56) != 2822400 ||
      read32(60) != 1 || block != 4096 || channels < 1 || channels > 2 ||
      count % 16)
    throw std::runtime_error(
        "Expected comparison-format mono/stereo LSB-first DSD64 DSF");
  const std::string preset = argv[2];
  if (preset != "30k" && preset != "50k" && preset != "wide")
    throw std::runtime_error("Unknown filter");
  const auto filter = preset == "30k"   ? DsdFilter::Cut30k
                      : preset == "50k" ? DsdFilter::Cut50k
                                        : DsdFilter::Wide;
  DsdToPcm dsp({2822400, 176400, channels, filter, DsdBitOrder::LsbFirst});
  std::ofstream output(argv[3], std::ios::binary);
  output.exceptions(std::ios::failbit | std::ios::badbit);
  std::vector<unsigned char> planar(block * channels),
      interleaved(planar.size());
  std::vector<double> left(dsp.maxFrames(block)), right(left.size());
  std::vector<double> packed(left.size() * channels);
  double *result[]{left.data(), right.data()};
  for (auto remaining = count / 8; remaining > 0;) {
    input.read(reinterpret_cast<char *>(planar.data()), planar.size());
    const size_t n = std::min<uint64_t>(remaining, block);
    for (size_t i = 0; i < n; ++i)
      for (unsigned c = 0; c < channels; ++c)
        interleaved[i * channels + c] = planar[c * block + i];
    const size_t frames = dsp.process(interleaved.data(), n, result);
    for (size_t i = 0; i < frames; ++i)
      for (unsigned c = 0; c < channels; ++c)
        packed[i * channels + c] = result[c][i];
    output.write(reinterpret_cast<const char *>(packed.data()),
                 frames * channels * sizeof(double));
    remaining -= n;
  }
  return 0;
} catch (const std::exception &error) {
  std::cerr << error.what() << '\n';
  return 1;
}
