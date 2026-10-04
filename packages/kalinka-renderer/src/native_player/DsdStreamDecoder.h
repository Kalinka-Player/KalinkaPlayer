#pragma once
#include "AudioGraphNode.h"
#include "DsdFormat.h"
#include "Utils.h"
#include <functional>

// Container extraction and lossless transport packing only; no PCM decoder.
class DsdStreamDecoder : public AudioGraphOutputNode,
                         public AudioGraphInputNode {
public:
  using SelectOutput = std::function<StreamAudioFormat(unsigned, unsigned)>;
  DsdStreamDecoder(std::optional<StreamId> id, SelectOutput select,
                   size_t startOffsetMs = 0);
  ~DsdStreamDecoder() override;
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
  Buffer<uint8_t> buffer{1536000};
  const SelectOutput select;
  const size_t startOffsetMs;
  std::shared_ptr<AudioGraphOutputNode> input;
  Signal<size_t> seekSignal;
  Signal<bool> ready;
  std::atomic<long> startFrame{0}, bytesRead{0};
  std::atomic<unsigned> frameBytes{0};
  std::atomic<bool> seekable{false};
  StreamInfo info{};
  std::stop_token workerToken;
  uint64_t sourcePosition = 0, dataOffset = 0, sampleCount = 0, nextByte = 0;
  unsigned channels = 0, rate = 0, blockSize = 0;
  bool dsf = false, reverseBits = false;
  std::jthread worker;
  void run(std::stop_token token);
  void readExact(void *dest, size_t bytes);
  std::vector<uint8_t> readBytes(size_t bytes);
  void skip(uint64_t bytes);
  void parseHeader();
  void seek(size_t frame);
  std::vector<uint8_t> readBlock();
  void finishIfDrained();
};
