#ifndef MP3STREAMDECODER_H
#define MP3STREAMDECODER_H

#include "AudioGraphNode.h"

#include "Utils.h"
#include <thread>

class Mp3StreamDecoder : public AudioGraphOutputNode,
                         public AudioGraphInputNode {
public:
  // startOffsetMs begins decoding partway into the stream instead of at 0.
  Mp3StreamDecoder(std::optional<StreamId> streamId, size_t bufferSize,
                   size_t startOffsetMs = 0);
  virtual ~Mp3StreamDecoder();

  size_t read(void *data, size_t size) override;

  size_t waitForData(std::stop_token stopToken = std::stop_token(),
                     size_t size = 1) override;

  size_t waitForDataFor(std::stop_token stopToken,
                        std::chrono::milliseconds timeout,
                        size_t size) override;

  size_t seekTo(size_t absolutePosition) override;

  std::optional<long> streamReadPosition() const override;

  void connectTo(std::shared_ptr<AudioGraphOutputNode> inputNode) override;

  void disconnect(std::shared_ptr<AudioGraphOutputNode> inputNode) override;

private:
  std::jthread decodingThread;
  /// The worker's own stop token, which its callbacks read: the thread object
  /// may still be being assigned when they first run.
  std::stop_token workerToken;
  std::shared_ptr<AudioGraphOutputNode> inputNode;
  Buffer<uint8_t> buffer;
  const size_t startOffsetMs;

  Signal<size_t> seekSignal;
  Signal<bool> initCompleteSignal;

  // Bytes, not frames: a read that ends mid-frame would round down, forever.
  std::atomic<long> runStartFrame = 0;
  std::atomic<long> bytesReadInRun = 0;
  std::atomic<unsigned int> frameSizeBytes = 0;
  std::atomic<bool> seekable = false;
  uint64_t sourcePosition = 0; // Worker-only cursor for minimp3's buffered input.

  void threadRun(std::stop_token token);
  void decodeStream(std::stop_token token);
  void writePcm(const void *data, size_t size, std::stop_token token);
  long framesRead() const;
  void restartRunAt(long frame);
  void finishIfDrained();

  typedef size_t (*MP3D_READ_CB)(void *buf, size_t size, void *user_data);
  typedef int (*MP3D_SEEK_CB)(uint64_t position, void *user_data);

  size_t readCallback(void *buf, size_t size);
  int seekCallback(uint64_t position);
  void handleSeekSignal(void *mp3dec_ex, size_t seekPosFrames);
};

#endif // MP3STREAMDECODER_H
