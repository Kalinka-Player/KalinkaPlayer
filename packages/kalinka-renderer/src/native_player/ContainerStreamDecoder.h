#pragma once

#include "BufferedStreamDecoder.h"
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
class ContainerStreamDecoder final : public BufferedStreamDecoder,
                                     public AudioGraphInputNode,
                                     private ByteReader {
public:
  /// @param bufferSize Output buffer in bytes; it must hold a frame.
  ContainerStreamDecoder(std::optional<StreamId> id,
                         std::unique_ptr<ContainerFormat> format,
                         size_t bufferSize, size_t startOffsetMs = 0);
  ~ContainerStreamDecoder() override;
  void connectTo(std::shared_ptr<AudioGraphOutputNode> source) override;
  void disconnect(std::shared_ptr<AudioGraphOutputNode> source) override;

private:
  const std::unique_ptr<ContainerFormat> format;
  std::shared_ptr<AudioGraphOutputNode> input;
  std::atomic<bool> seekable{false};
  uint64_t sourcePosition = 0, dataOffset = 0;

  void readExact(void *dest, size_t bytes) override;
  uint64_t position() const override { return sourcePosition; }
  std::string name() const override { return format->name(); }
  StreamInfo open() override;
  bool decodeNext() override;
  void seekSource(uint64_t frame) override;
  bool canSeek() const override { return seekable; }
};
