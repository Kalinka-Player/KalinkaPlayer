#pragma once

#include "AudioGraphNode.h"
#include "Utils.h"

#include <atomic>
#include <span>
#include <stop_token>
#include <string>

/**
 * @brief An output node whose worker thread decodes a source into a bounded
 * buffer of whole frames, which read() and waitForData() drain from another
 * thread.
 *
 * Owns the buffer, the worker, the start offset and the seek handshake; a
 * subclass supplies the source through open(), decodeNext() and seekSource(),
 * all of which run on the worker. A seek or shutdown interrupts the worker's
 * waits. A hook that throws ends the stream in the ERROR state.
 * @note A subclass calls start() once it is fully built and stop() in its own
 * destructor, so the worker never calls into a half-built or half-destroyed
 * object.
 */
class BufferedStreamDecoder : public AudioGraphOutputNode {
public:
  size_t read(void *data, size_t size) override;
  size_t waitForData(std::stop_token token = {}, size_t size = 1) override;
  size_t waitForDataFor(std::stop_token token,
                        std::chrono::milliseconds timeout,
                        size_t size) override;
  size_t seekTo(size_t frame) override;
  std::optional<long> streamReadPosition() const override;

protected:
  /// Unwinds the worker out of a wait that a seek or shutdown cut short.
  struct Interrupted {};
  /// Ends the stream in the ERROR state with an error the source reported.
  struct SourceFailed {
    StreamError error;
  };

  /// @param bufferSize Output buffer in bytes; it must hold a frame.
  BufferedStreamDecoder(size_t bufferSize, size_t startOffsetMs);

  /// Resets the stream to PREPARING and launches the worker.
  void start();
  /// Interrupts the worker and waits for it; a no-op when none runs.
  void stop();
  /// Throws Interrupted once a seek or shutdown is pending.
  void checkInterrupted() const;
  /// A token stopped by a seek request, shutdown or any of `extra`.
  template <typename... Extra> auto interruption(Extra... extra) const {
    return combineStopTokens(workerToken, seekSignal.getStopToken(),
                             extra...);
  }
  /// Appends whole frames, waiting for room; throws Interrupted.
  void write(std::span<const uint8_t> frames);
  /// The stream open() described; valid on the worker once open() returns.
  const StreamInfo &streamInfo() const { return info; }

private:
  Buffer<uint8_t> buffer;
  const size_t startOffsetMs;
  Signal<size_t> seekSignal;
  Signal<bool> ready;
  std::atomic<long> startFrame{0}, bytesRead{0};
  std::atomic<unsigned> frameBytes{0};
  StreamInfo info{};
  std::stop_token workerToken;
  std::jthread worker;

  /// Prefixes the messages of the errors raised here.
  virtual std::string name() const = 0;
  /// Reads the source up to its first frame; returns the stream it holds.
  virtual StreamInfo open() = 0;
  /// Appends the next block through write(); false once the source is done.
  virtual bool decodeNext() = 0;
  /// Repositions the source so decodeNext() resumes at `frame`, which is at
  /// most the stream's length.
  virtual void seekSource(uint64_t frame) = 0;
  /// Whether seekSource() works; asked once open() has returned.
  virtual bool canSeek() const { return true; }
  /// Releases the source on the worker's way out.
  virtual void close() {}

  void run(std::stop_token token);
  void prepare();
  void seek(size_t frame);
  void finishIfDrained();
};
