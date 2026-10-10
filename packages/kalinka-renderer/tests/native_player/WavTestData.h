#pragma once

#include <bit>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <vector>

namespace wav_test {
using Bytes = std::vector<uint8_t>;
inline void number(Bytes &out, uint64_t value, unsigned width) {
  for (unsigned i = 0; i < width; ++i)
    out.push_back(value >> (8 * i));
}
inline void chunk(Bytes &out, const char *name, const Bytes &data) {
  out.insert(out.end(), name, name + 4);
  number(out, data.size(), 4);
  out.insert(out.end(), data.begin(), data.end());
  if (data.size() & 1)
    out.push_back(0);
}
// Low bits, sign, channel order, full-scale values, and nonzero trailing bits
// all matter: a tone alone can miss truncation or byte-order mistakes.
inline Bytes samples(unsigned frames, unsigned channels, unsigned bits,
                     unsigned width, bool floating = false) {
  const uint32_t values[] = {0,          1,          0xffffffff, 0x80000000,
                             0x7fffffff, 0x12345679, 0xffffff,   0x800000,
                             0x7fffff,   0x123456};
  const double floats[] = {0.0, -0.0, 0.125,   -0.5,
                           1.0, -1.0, 0x1p-60, 0x1.0000000000001p-1};
  Bytes out;
  for (size_t i = 0; i < size_t(frames) * channels; ++i) {
    if (floating) {
      const auto value = floats[i % 8];
      number(out,
             bits == 32 ? std::bit_cast<uint32_t>(float(value))
                        : std::bit_cast<uint64_t>(value),
             width / 8);
      continue;
    }
    auto value = values[i % 10] & (bits == 16   ? 0xffff
                                   : bits == 24 ? 0xffffff
                                                : 0xffffffff);
    if (width > bits)
      value <<= width - bits;
    number(out, value, width / 8);
  }
  return out;
}
inline Bytes file(unsigned bits = 24, unsigned rate = 96000,
                  unsigned channels = 2, unsigned frames = 9600,
                  bool extensible = false, unsigned width = 0,
                  bool floating = false) {
  if (!width)
    width = bits;
  Bytes fmt;
  number(fmt, extensible ? 0xfffe : floating ? 3 : 1, 2);
  number(fmt, channels, 2);
  number(fmt, rate, 4);
  number(fmt, rate * channels * width / 8, 4);
  number(fmt, channels * width / 8, 2);
  number(fmt, width, 2);
  if (extensible) {
    number(fmt, 22, 2);
    number(fmt, bits, 2);
    number(fmt, channels == 1 ? 4 : 3, 4);
    const Bytes guid{uint8_t(floating ? 3 : 1),
                     0,
                     0,
                     0,
                     0,
                     0,
                     0x10,
                     0,
                     0x80,
                     0,
                     0,
                     0xaa,
                     0,
                     0x38,
                     0x9b,
                     0x71};
    fmt.insert(fmt.end(), guid.begin(), guid.end());
  }
  Bytes body{'W', 'A', 'V', 'E'};
  chunk(body, "fmt ", fmt);
  chunk(body, "JUNK", {1, 2, 3}); // Odd-sized metadata before the audio.
  chunk(body, "data", samples(frames, channels, bits, width, floating));
  chunk(body, "LIST", {'I', 'N', 'F', 'O'}); // Must never become audio.
  Bytes result;
  chunk(result, "RIFF", body);
  return result;
}
inline void write(const std::filesystem::path &path, const Bytes &bytes) {
  std::ofstream out(path, std::ios::binary);
  out.write(reinterpret_cast<const char *>(bytes.data()), bytes.size());
}
} // namespace wav_test
