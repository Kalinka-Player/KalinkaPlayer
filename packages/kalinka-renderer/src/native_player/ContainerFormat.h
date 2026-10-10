#pragma once

#include "AudioInfo.h"

#include <algorithm>
#include <array>
#include <bit>
#include <cstdint>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

/**
 * @brief A container's bytes, read forward by the format that understands
 * them.
 *
 * A read either completes or throws: the source ended early, failed, or
 * playback is seeking or shutting down. A format never catches what a read
 * throws; the decoder driving it does.
 */
class ByteReader {
public:
  virtual ~ByteReader() = default;
  virtual void readExact(void *dest, size_t bytes) = 0;
  /// Bytes read so far, which is where the next read starts.
  virtual uint64_t position() const = 0;
  /// Reads and discards `bytes`, in bounded memory however many.
  void skip(uint64_t bytes) {
    std::array<uint8_t, 4096> scratch;
    while (bytes) {
      const auto count = std::min<uint64_t>(bytes, scratch.size());
      readExact(scratch.data(), count);
      bytes -= count;
    }
  }
};

/**
 * @brief The layout of one uncompressed container: how its header describes
 * the audio, where a frame lies in it, and how a block of it leaves as output
 * frames.
 *
 * Driven by one ContainerStreamDecoder on its worker thread, which owns the
 * format and reads the source for it, so a format needs no locking; only
 * name() is asked from other threads. It keeps its own cursor and refuses a
 * file by throwing std::runtime_error; the message is what the listener sees.
 */
class ContainerFormat {
public:
  /// What the header said.
  struct Header {
    /// The audio as it leaves: its output format and length in frames.
    StreamInfo info;
    /// How much of the source the audio occupies after the header, so a
    /// source known to be shorter is refused before anything plays.
    uint64_t audioBytes = 0;
  };
  virtual ~ContainerFormat() = default;
  /// Names the format in errors, as in "WAV start offset is past the end".
  virtual std::string name() const = 0;
  /// Reads the header and leaves `source` at the first audio byte, with the
  /// cursor on the first frame. Runs once per connected source.
  virtual Header parseHeader(ByteReader &source) = 0;
  /// Moves the cursor to `frame`, which is never past the end.
  /// @return Where reading resumes, in bytes from the first audio byte; the
  /// decoder positions the source there before asking for the next block.
  virtual uint64_t seek(uint64_t frame) = 0;
  /// Reads the block at the cursor and advances past it.
  /// @return The block as whole output frames; empty once the audio is
  /// exhausted.
  virtual std::vector<uint8_t> nextBlock(ByteReader &source) = 0;
};

/// What a format says about the bytes it parses.
namespace container {
/// How deep into a source the audio may start. Everything before it is read
/// through, so a corrupt chunk size would otherwise cost the whole file.
constexpr uint64_t MAX_HEADER_BYTES = uint64_t(64) << 20;
/// Refuses the file with `message` unless `ok`.
inline void require(bool ok, const char *message) {
  if (!ok)
    throw std::runtime_error(message);
}
/// Whether the four bytes at `p` spell the chunk id `name`.
inline bool tagged(const uint8_t *p, const char *name) {
  return std::memcmp(p, name, 4) == 0;
}
/// The `n`-byte unsigned integer at `p`.
inline uint64_t number(const uint8_t *p, unsigned n,
                       std::endian order = std::endian::little) {
  uint64_t result = 0;
  for (unsigned i = 0; i < n; ++i)
    result = (result << 8) | p[order == std::endian::little ? n - 1 - i : i];
  return result;
}
} // namespace container
