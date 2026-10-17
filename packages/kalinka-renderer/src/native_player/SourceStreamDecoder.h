#pragma once

#include "BufferedStreamDecoder.h"

#include <atomic>
#include <memory>

/**
 * @brief A BufferedStreamDecoder that decodes the byte source it is connected
 * to.
 *
 * Owns the connection: connectTo() starts the worker on a fresh source and
 * disconnect() stops it. The source position stays exact even when a seek or
 * shutdown cuts a read short, so a subclass may read on from where it stands.
 * Everything but connecting runs on the worker.
 */
class SourceStreamDecoder : public BufferedStreamDecoder,
                            public AudioGraphInputNode {
public:
  void connectTo(std::shared_ptr<AudioGraphOutputNode> source) final;
  void disconnect(std::shared_ptr<AudioGraphOutputNode> source) final;

protected:
  SourceStreamDecoder(size_t bufferSize, size_t startOffsetMs);

  /// Reads up to `bytes`, fewer only where the source ends. Throws
  /// Interrupted, or SourceFailed when the source fails.
  size_t readSome(void *dest, size_t bytes);
  /// Reads exactly `bytes`; throws "Truncated <name> file" at the end.
  void readExact(void *dest, size_t bytes);
  /// Moves the source to `offset`; false where it cannot.
  bool reposition(uint64_t offset);
  /// Where the next read starts.
  uint64_t sourcePosition() const { return position; }
  /// The source's length in bytes, or 0 while unknown; an HTTP source knows
  /// it once its first bytes have arrived.
  uint64_t sourceLength() const;
  /// Whether playback may seek; reported by canSeek().
  void setSeekable(bool value) { seekable = value; }

private:
  std::shared_ptr<AudioGraphOutputNode> input;
  std::atomic<bool> seekable{false};
  uint64_t position = 0;

  bool canSeek() const final { return seekable; }
  /// Forgets the previous source, before the worker starts on a new one.
  virtual void forget() {}
};
