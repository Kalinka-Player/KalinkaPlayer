#pragma once

#include "AudioGraphNode.h"
#include "ContainerFormat.h"
#include "Utils.h"

#include <memory>
#include <span>

/**
 * @brief Plays an uncompressed container, laid out by a ContainerFormat, from
 * a byte source.
 *
 * Owns the format, the source it is connected to and the worker thread that
 * drives both: the worker reads the header once, then repacks block after
 * block into a buffer that read() and waitForData() drain from another
 * thread. Seeking and a start offset need a source that can seek; a seek or
 * shutdown interrupts a wait on the source. A malformed file, a failed source
 * or a format that throws ends the stream in the ERROR state, carrying the
 * source's error or the format's message.
 */
class ContainerStreamDecoder final : public AudioGraphOutputNode,
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
  size_t read(void *data, size_t size) override;
  size_t waitForData(std::stop_token token = {}, size_t size = 1) override;
  size_t waitForDataFor(std::stop_token token,
                        std::chrono::milliseconds timeout,
                        size_t size) override;
  size_t seekTo(size_t frame) override;
  std::optional<long> streamReadPosition() const override;

private:
  const std::unique_ptr<ContainerFormat> format;
  Buffer<uint8_t> buffer;
  const size_t startOffsetMs;
  std::shared_ptr<AudioGraphOutputNode> input;
  Signal<size_t> seekSignal;
  Signal<bool> ready;
  std::atomic<long> startFrame{0}, bytesRead{0};
  std::atomic<unsigned> frameBytes{0};
  std::atomic<bool> seekable{false};
  StreamInfo info{};
  std::stop_token workerToken;
  uint64_t sourcePosition = 0, dataOffset = 0;
  std::jthread worker;

  void readExact(void *dest, size_t bytes) override;
  uint64_t position() const override { return sourcePosition; }
  void stop();
  void run(std::stop_token token);
  void open();
  void seek(size_t frame);
  void write(std::span<const uint8_t> block);
  void finishIfDrained();
};
