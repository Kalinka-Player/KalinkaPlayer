#pragma once

#include "SourceStreamDecoder.h"
#include "ContainerFormat.h"

#include <memory>

/**
 * @brief Plays an uncompressed container, laid out by a ContainerFormat, from
 * a byte source.
 *
 * Owns the format and the source it is connected to: the worker reads the
 * header once, then repacks block after block into the buffer. Seeking and a
 * start offset need a source that can seek; a seek or shutdown interrupts a
 * wait on the source. A malformed file, a failed source or a format that
 * throws ends the stream in the ERROR state, carrying the source's error or
 * the format's message.
 */
class ContainerStreamDecoder final : public SourceStreamDecoder,
                                     private ByteReader {
public:
  /// @param bufferSize Output buffer in bytes; it must hold a frame.
  ContainerStreamDecoder(std::optional<StreamId> id,
                         std::unique_ptr<ContainerFormat> format,
                         size_t bufferSize, size_t startOffsetMs = 0);
  ~ContainerStreamDecoder() override;

private:
  const std::unique_ptr<ContainerFormat> format;
  uint64_t dataOffset = 0;

  void readExact(void *dest, size_t bytes) override {
    SourceStreamDecoder::readExact(dest, bytes);
  }
  uint64_t position() const override { return sourcePosition(); }
  std::string name() const override { return format->name(); }
  void forget() override { dataOffset = 0; }
  StreamInfo open() override;
  bool decodeNext() override;
  void seekSource(uint64_t frame) override;
};
