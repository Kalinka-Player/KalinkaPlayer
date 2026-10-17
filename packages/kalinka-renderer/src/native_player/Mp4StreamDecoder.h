#pragma once

#include "SourceStreamDecoder.h"
#include "FragmentedMp4.h"
#include "TrackDecoder.h"

#include <memory>
#include <optional>
#include <span>
#include <string>
#include <vector>

/**
 * @brief Plays the audio track of an MP4 or M4A file, AAC or FLAC, from a
 * byte source.
 *
 * Reads progressive files through their sample tables, wherever the index
 * lies, and fragmented ones fragment by fragment. Seeking and a start offset
 * need a source that can seek; an index after the audio needs one too.
 * Positions are output frames from the first frame the codec plays. A
 * malformed or unsupported file, or a failed source, ends the stream in the
 * ERROR state.
 */
class Mp4StreamDecoder final : public SourceStreamDecoder {
public:
  /// @param bufferSize Output buffer in bytes; it must hold a frame.
  Mp4StreamDecoder(std::optional<StreamId> id, size_t bufferSize,
                   size_t startOffsetMs = 0);
  ~Mp4StreamDecoder() override;

private:
  class Layout;
  class Progressive;
  class Fragmented;
  struct BoxHeader {
    std::string type;
    uint64_t offset = 0, size = 0, header = 0;
    /// The header as a box of `size` bytes, ready to parse again.
    std::vector<uint8_t> bytes() const;
  };
  /// A unit's bytes and its place on the timeline, in output frames.
  struct Unit {
    std::span<const uint8_t> bytes;
    uint64_t first = 0, frames = 0;
  };

  std::unique_ptr<TrackDecoder> track;
  std::unique_ptr<Layout> layout;
  bool sourceSeeks = false;
  uint64_t sourceSize = 0;
  /// A box header read ahead of where the source now stands.
  std::optional<BoxHeader> lookahead;

  std::string name() const override { return "MP4"; }
  StreamInfo open() override;
  bool decodeNext() override;
  void seekSource(uint64_t frame) override;
  void close() override;

  std::vector<uint8_t> readPayload(const BoxHeader &box, uint64_t limit);
  /// The header of the box at the current position; nothing at the end.
  std::optional<BoxHeader> nextBox();
  /// Moves the source to `offset`, reading forward where it cannot seek.
  void moveTo(uint64_t offset);
  /// The fragments a trailing `mfra` box lists, with the last one's length;
  /// empty when the file has none it can use.
  std::vector<FragmentedMp4::Reference> randomAccess(const FragmentedMp4 &init);
};
