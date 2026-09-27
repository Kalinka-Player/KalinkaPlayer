#include "AudioGraphHttpStream.h"

#include <curlpp/Info.hpp>
#include <curlpp/Options.hpp>
#include <curlpp/Types.hpp>
#include <curlpp/cURLpp.hpp>

#include <boost/algorithm/string.hpp>
#include <functional>
#include <pthread.h>

#include "Log.h"

namespace {
const long STALL_BYTES_PER_SECOND = 1024;
} // namespace

AudioGraphHttpStream::AudioGraphHttpStream(std::optional<StreamId> streamId,
                                           const std::string &url,
                                           size_t bufferSize, size_t chunkSize,
                                           std::chrono::seconds stallTimeout)
    : AudioGraphNode(streamId), url(url),
      buffer(std::max(bufferSize, static_cast<size_t>(CURL_MAX_WRITE_SIZE)),
             std::bind(&AudioGraphHttpStream::emptyBufferCallback, this,
                       std::placeholders::_1)),
      chunkSize(chunkSize),
      stallTimeout(std::max(stallTimeout, std::chrono::seconds::zero())) {
  readerThread =
      std::jthread(std::bind_front(&AudioGraphHttpStream::reader, this));
}

AudioGraphHttpStream::~AudioGraphHttpStream() {
  if (readerThread.joinable()) {
    readerThread.request_stop();
    readerThread.join();
    readerThread = std::jthread();
  }
}

size_t AudioGraphHttpStream::WriteCallback(void *contents, size_t size,
                                           size_t nmemb) {
  size_t sizeWritten = 0;
  size_t totalSize = size * nmemb;

  long responseCode = 0;
  curlpp::Info<CURLINFO_RESPONSE_CODE, long>::get(request, responseCode);
  if (responseCode != 200 && responseCode != 206) {
    spdlog::trace("Skipping data for response code {}", responseCode);
    return totalSize;
  }

  if (setStreamingState) {
    setState(StreamState(AudioGraphNodeState::STREAMING, offset,
                         StreamInfo{.streamType = StreamType::BYTES,
                                    .streamSize = contentLength}));
    setStreamingState = false;
  }
  auto combinedStopToken = combineStopTokens(seekRequestSignal.getStopToken(),
                                             readerThread.get_stop_token());
  while (sizeWritten < totalSize) {
    auto spaceAvailable = buffer.waitForSpace(combinedStopToken.get_token());
    if (combinedStopToken.get_token().stop_requested()) {
      if (seekRequestSignal.getStopToken().stop_requested()) {
        return chunkSize ? totalSize : 0;
      }
      return 0;
    }
    auto writtenChunkSize =
        buffer.write(static_cast<uint8_t *>(contents) + sizeWritten,
                     std::min(totalSize - sizeWritten, spaceAvailable));
    sizeWritten += writtenChunkSize;
    offset += writtenChunkSize;
  }

  return sizeWritten;
}

void AudioGraphHttpStream::emptyBufferCallback(Buffer<uint8_t> &buffer) {
  if (buffer.isEof() && getState().state != AudioGraphNodeState::ERROR) {
    setState(StreamState(AudioGraphNodeState::FINISHED));
  }
}

size_t AudioGraphHttpStream::headerCallback(char *buffer, size_t size,
                                            size_t nitems) {
  size_t totalSize = size * nitems;
  std::string header(buffer, totalSize);

  size_t separator = header.find(": ");
  if (separator != std::string::npos) {
    std::string key = header.substr(0, separator);
    std::string value = header.substr(separator + 2);
    boost::algorithm::to_lower(key);
    boost::algorithm::to_lower(value);
    boost::algorithm::trim(key);
    boost::algorithm::trim(value);
    if (!value.empty() && value.back() == '\r') {
      value.pop_back();
    }
    if (key == "content-range") {
      size_t separator = value.find('/');
      if (separator != std::string::npos) {
        value = value.substr(separator + 1);
        contentLength = std::stoul(value);
      }
    }
    if (key == "accept-ranges") {
      acceptRange = (value == "bytes");
    }
  }
  return totalSize;
}

void AudioGraphHttpStream::handleSeekSignal(size_t position) {
  if (!acceptRange && position != 0) {
    seekRequestSignal.respond(offset);
    return;
  }

  buffer.clear();
  if (position >= contentLength) {
    offset = contentLength;
  } else {
    buffer.resetEof();
    offset = position;
    setStreamingState = true;
    setState(StreamState(AudioGraphNodeState::PREPARING));
  }
  seekRequestSignal.respond(offset);
}

void AudioGraphHttpStream::reader(std::stop_token stopToken) {
  pthread_setname_np(pthread_self(), "HttpStream");
  try {
    setState(StreamState(AudioGraphNodeState::PREPARING));
    while (!stopToken.stop_requested()) {
      readContentChunks(stopToken);
      buffer.setEof();
      spdlog::debug("Finished reading content");
      auto seekValue = seekRequestSignal.waitValue(stopToken);
      if (!seekValue) {
        break;
      }
      handleSeekSignal(*seekValue);
    }
  } catch (curlpp::LibcurlRuntimeError &ex) {
    if (!stopToken.stop_requested()) {
      std::string message = std::string("Libcurl exception: ") +
                            curl_easy_strerror(ex.whatCode()) + ": " +
                            ex.what();
      spdlog::error(message);
      setState({AudioGraphNodeState::ERROR, StreamError{StreamErrorSource::HTTP_STREAM, message}});
    }
  } catch (std::runtime_error &ex) {
    spdlog::error(ex.what());
    setState({AudioGraphNodeState::ERROR, StreamError{StreamErrorSource::HTTP_STREAM, ex.what()}});
  }
  seekRequestSignal.close(static_cast<size_t>(-1));
  buffer.setEof();
  spdlog::debug("Reader thread is finished");
}

void AudioGraphHttpStream::readContentChunks(std::stop_token stopToken) {
  using namespace std::placeholders;
  int numRetries = 3;
  while (offset < contentLength) {
    auto seekToPos = seekRequestSignal.getValue();
    if (seekToPos) {
      handleSeekSignal(seekToPos.value());
      continue;
    }

    auto combinedStopToken =
        combineStopTokens(stopToken, seekRequestSignal.getStopToken());

    buffer.waitForSpace(combinedStopToken.get_token(), buffer.max_size() / 2);
    long responseCode = 0;
    if (stopToken.stop_requested()) {
      break;
    }

    if (seekRequestSignal.getStopToken().stop_requested()) {
      continue;
    }

    try {
      responseCode = readSingleChunk(stopToken);
    } catch (const curlpp::LibcurlRuntimeError &ex) {
      if (stopToken.stop_requested()) {
        break;
      }
      if (seekRequestSignal.getStopToken().stop_requested()) {
        continue;
      }
      responseCode = -1;
      spdlog::warn("Libcurl exception: {}", ex.what());
      if (numRetries == 0 || !acceptRange) {
        spdlog::error("Request failed at offset {}/{} - aborting", offset,
                      contentLength);
        throw;
      } else {
        spdlog::warn("Request failed at offset {}/{} - retrying {} more times",
                     offset, contentLength, numRetries);
        --numRetries;
        continue;
      }
    }

    if (responseCode >= 200 && responseCode < 300) {
      numRetries = 3;
    }

    if (responseCode == 200) {
      break;
    } else if (responseCode == 206) {
      continue;
    } else if (responseCode == 416) {
      throw std::runtime_error("Request range not satisfiable, " +
                               std::to_string(offset) + "/" +
                               std::to_string(contentLength) +
                               ", chunk=" + std::to_string(chunkSize));
    } else if (responseCode >= 400 && responseCode < 500) {
      throw std::runtime_error("HTTP GET request failed with code " +
                               std::to_string(responseCode));
    } else if (responseCode >= 500 && responseCode < 600) {
      if (numRetries == 0 || !acceptRange) {
        std::string message =
            "HTTP GET request failed with code " + std::to_string(responseCode);
        throw std::runtime_error(message);
      } else {
        --numRetries;
        spdlog::warn(
            "HTTP GET request failed with code {}, retrying {} more times",
            responseCode, numRetries);
        std::this_thread::sleep_for(std::chrono::seconds(1));
      }
    }
  }
}

int AudioGraphHttpStream::readSingleChunk(std::stop_token stopToken) {
  using namespace std::placeholders;

  request.reset();
  curlpp::options::Url myUrl(url);
  request.setOpt(myUrl);

  if (acceptRange) {
    std::ostringstream range;
    range << offset << "-";
    if (chunkSize) {
      range << chunkSize + offset - 1;
    }
    request.setOpt(new curlpp::options::Range(range.str()));

    spdlog::trace("Requesting range: {}, contentLength={}, offset={}",
                  range.str(), contentLength, offset);
  }

  request.setOpt(new curlpp::options::ConnectTimeout(10));
  // A sender gone quiet would otherwise hold perform() until TCP gives up.
  request.setOpt(new curlpp::options::LowSpeedLimit(STALL_BYTES_PER_SECOND));
  request.setOpt(new curlpp::options::LowSpeedTime(stallTimeout.count()));
  request.setOpt(new curlpp::options::WriteFunction(
      std::bind(&AudioGraphHttpStream::WriteCallback, this, _1, _2, _3)));
  if (!hasReadHeader) {
    request.setOpt(new curlpp::options::HeaderFunction(std::bind(
        &AudioGraphHttpStream::headerCallback, this, std::placeholders::_1,
        std::placeholders::_2, std::placeholders::_3)));
  }
  request.perform();
  // Only now: a request that failed before its headers left them unread.
  hasReadHeader = true;

  long responseCode = 0;
  curlpp::Info<CURLINFO_RESPONSE_CODE, long>::get(request, responseCode);
  return responseCode;
}

size_t AudioGraphHttpStream::read(void *data, size_t size) {
  return buffer.read(static_cast<uint8_t *>(data), size);
}

size_t AudioGraphHttpStream::waitForData(std::stop_token stopToken,
                                         size_t size) {
  auto combinedToken =
      combineStopTokens(stopToken, readerThread.get_stop_token());
  return buffer.waitForData(combinedToken.get_token(), size);
}

size_t AudioGraphHttpStream::waitForDataFor(std::stop_token stopToken,
                                            std::chrono::milliseconds timeout,
                                            size_t size) {
  auto combinedToken =
      combineStopTokens(stopToken, readerThread.get_stop_token());
  return buffer.waitForDataFor(combinedToken.get_token(), timeout, size);
}

size_t AudioGraphHttpStream::seekTo(size_t absolutePosition) {
  if (getState().state == AudioGraphNodeState::ERROR ||
      seekRequestSignal.getValue()) {
    return -1;
  }

  spdlog::trace("AudioGraphHttpStream::seekTo({})", absolutePosition);
  seekRequestSignal.sendValue(absolutePosition);
  auto retVal = seekRequestSignal.getResponse(readerThread.get_stop_token());
  spdlog::trace("AudioGraphHttpStream::seekTo({}) -> {}", absolutePosition,
                retVal);
  return retVal;
}
