#ifndef AUDIO_GRAPH_HTTP_H
#define AUDIO_GRAPH_HTTP_H

#include "AudioGraphNode.h"
#include "Buffer.h"

#include "Utils.h"
#include <chrono>
#include <curlpp/Easy.hpp>

class AudioGraphHttpStream : public AudioGraphOutputNode {
public:
  static constexpr std::chrono::seconds DEFAULT_STALL_TIMEOUT{15};

  /**
   * @param chunkSize Bytes asked for per request; 0 asks for the rest of the
   * stream in one.
   * @param stallTimeout How long a connected transfer may run below 1 KB/s
   * before it is dropped and resumed from where it stopped; 0 or less waits
   * for as long as the connection lasts. Time spent waiting for room in the
   * buffer, as while paused, does not count.
   */
  AudioGraphHttpStream(std::optional<StreamId> streamId, const std::string &url,
                       size_t bufferSize, size_t chunkSize = 0,
                       std::chrono::seconds stallTimeout = DEFAULT_STALL_TIMEOUT);
  virtual size_t read(void *data, size_t size) override;
  virtual size_t waitForData(std::stop_token stopToken, size_t size) override;
  virtual size_t waitForDataFor(std::stop_token stopToken,
                                std::chrono::milliseconds timeout,
                                size_t size) override;
  virtual size_t seekTo(size_t absolutePosition) override;

  virtual ~AudioGraphHttpStream();

private:
  std::jthread readerThread;
  std::string url;
  Buffer<uint8_t> buffer;
  size_t contentLength = 1;
  size_t offset = 0;
  Signal<size_t> seekRequestSignal;
  size_t chunkSize = 0;
  std::chrono::seconds stallTimeout;
  bool acceptRange = true;
  bool hasReadHeader = false;
  bool setStreamingState = true;

  void reader(std::stop_token token);
  void readContentChunks(std::stop_token token);
  int readSingleChunk(std::stop_token stopToken);
  size_t WriteCallback(void *contents, size_t size, size_t nmemb);
  void emptyBufferCallback(Buffer<uint8_t> &buffer);
  size_t headerCallback(char *buffer, size_t size, size_t nitems);

  void handleSeekSignal(size_t position);

  curlpp::Easy request;
};

#endif