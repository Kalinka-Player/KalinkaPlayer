#pragma once

#include "AudioGraphNode.h"
#include "Utils.h"

#include <vorbis/vorbisfile.h>

#include <exception>

// Ogg/Vorbis input, interleaved signed 16-bit little-endian PCM output.
// All libvorbisfile calls and compressed input access belong to the worker.
class VorbisStreamDecoder : public AudioGraphOutputNode,
                            public AudioGraphInputNode {
public:
  VorbisStreamDecoder(std::optional<StreamId> streamId, size_t bufferSize,
                      size_t startOffsetMs = 0);
  ~VorbisStreamDecoder() override;

  void connectTo(std::shared_ptr<AudioGraphOutputNode> inputNode) override;
  void disconnect(std::shared_ptr<AudioGraphOutputNode> inputNode) override;
  size_t read(void *data, size_t size) override;
  size_t waitForData(std::stop_token stopToken = {}, size_t size = 1) override;
  size_t waitForDataFor(std::stop_token stopToken,
                        std::chrono::milliseconds timeout,
                        size_t size) override;
  /** @brief Accept a frame seek, returning the clamped target or size_t(-1).
   * @note Completion reports STREAMING; a later seek failure reports ERROR.
   */
  size_t seekTo(size_t absolutePosition) override;
  std::optional<long> streamReadPosition() const override;

private:
  Buffer<uint8_t> buffer;
  const size_t startOffsetMs;
  std::shared_ptr<AudioGraphOutputNode> inputNode;
  Signal<size_t> seekSignal;
  Signal<bool> initCompleteSignal;
  std::atomic<bool> seekable = false;
  std::atomic<long> runStartFrame = 0;
  std::atomic<long> bytesReadInRun = 0;
  std::atomic<unsigned int> frameSizeBytes = 0;

  // Worker-only state, including the token passed to the worker (the jthread
  // member itself may still be being assigned when the worker first runs).
  std::stop_token workerToken;
  bool interruptReadForSeek = false;
  bool resetBeforeSeek = false;
  size_t sourcePosition = 0;
  std::optional<size_t> sourceLength;
  std::exception_ptr callbackException;
  StreamInfo streamInfo{};
  std::jthread decodingThread;

  void threadRun(std::stop_token token);
  size_t readInput(void *data, size_t size, size_t count);
  int seekInput(ogg_int64_t offset, int whence);
  static size_t readCallback(void *data, size_t size, size_t count,
                             void *source) noexcept;
  static int seekCallback(void *source, ogg_int64_t offset,
                          int whence) noexcept;
  static long tellCallback(void *source) noexcept;
  void checkResult(long result, const char *operation);
  void validateFormat(const vorbis_info *info) const;
  void seek(OggVorbis_File &file, size_t position);
  long framesRead() const;
  void finishIfDrained();
};
