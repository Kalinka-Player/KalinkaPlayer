#ifndef FLAC_STREAM_DECODER_H
#define FLAC_STREAM_DECODER_H

#include "AudioGraphNode.h"

#include <FLAC++/decoder.h>
#include <memory>
#include <thread>
#include <vector>

#include "Buffer.h"
#include "Utils.h"

class FlacStreamDecoder : public AudioGraphOutputNode,
                          public AudioGraphInputNode,
                          public FLAC::Decoder::Stream {
public:
  // startOffsetMs begins decoding partway into the stream instead of at 0.
  FlacStreamDecoder(std::optional<StreamId> streamId, size_t bufferSize,
                    size_t startOffsetMs = 0);

  virtual void
  connectTo(std::shared_ptr<AudioGraphOutputNode> inputNode) override;
  virtual void
  disconnect(std::shared_ptr<AudioGraphOutputNode> inputNode) override;

  virtual size_t read(void *data, size_t size) override;
  virtual size_t waitForData(std::stop_token stopToken = std::stop_token(),
                             size_t size = 1) override;

  virtual size_t waitForDataFor(std::stop_token stopToken,
                                std::chrono::milliseconds timeout,
                                size_t size) override;

  virtual size_t seekTo(size_t absolutePosition) override;

  virtual std::optional<long> streamReadPosition() const override;

  virtual ~FlacStreamDecoder();

protected:
  virtual ::FLAC__StreamDecoderWriteStatus
  write_callback(const ::FLAC__Frame *frame,
                 const FLAC__int32 *const buffer[]) override;
  virtual ::FLAC__StreamDecoderReadStatus read_callback(FLAC__byte buffer[],
                                                        size_t *bytes) override;

  virtual void error_callback(::FLAC__StreamDecoderErrorStatus status) override;

  virtual void
  metadata_callback(const ::FLAC__StreamMetadata *metadata) override;

  virtual ::FLAC__StreamDecoderSeekStatus
  seek_callback(FLAC__uint64 absolute_byte_offset) override;

  virtual ::FLAC__StreamDecoderTellStatus
  tell_callback(FLAC__uint64 *absolute_byte_offset) override;

  virtual ::FLAC__StreamDecoderLengthStatus
  length_callback(FLAC__uint64 *stream_length) override;

  virtual bool eof_callback() override;

private:
  std::jthread decodingThread;
  /// The worker's own stop token, which its callbacks read: the thread object
  /// may still be being assigned when they first run.
  std::stop_token workerToken;

  std::optional<FLAC__StreamMetadata_StreamInfo> flacStreamInfo;
  std::optional<size_t> sourceStreamLength;
  size_t sourceStreamPosition = 0;

  std::vector<uint8_t> data;
  Buffer<uint8_t> buffer;
  const size_t startOffsetMs;
  // Bytes, not frames: a read that ends mid-frame would round down, forever.
  std::atomic<long> runStartFrame = 0;
  std::atomic<long> bytesReadInRun = 0;
  std::atomic<unsigned int> frameSizeBytes = 0;

  Signal<size_t> seekSignal;

  std::shared_ptr<AudioGraphOutputNode> inputNode;

  void thread_run(std::stop_token token);
  long framesRead() const;
  void restartRunAt(long frame);
  void onEmptyBuffer(Buffer<uint8_t> &buffer);
  void throwOnFlacError(bool retval);
  void setStreamingState();
  void processStartOffset();
  void handleSeekSignal(size_t position);
};

#endif