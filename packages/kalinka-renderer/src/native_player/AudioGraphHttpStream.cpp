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
const int RETRIES = 3;
// Any less, and a server that sends a few bytes before each stall never fails.
const size_t PROGRESS_THAT_RENEWS_RETRIES = CURL_MAX_WRITE_SIZE;
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
    silentSince = std::chrono::steady_clock::now();
    return totalSize;
  }
  // A 200 carries the file from its first byte, whatever range was asked for.
  if (responseCode == 200) {
    sizeWritten = std::min(bytesToSkip, totalSize);
    bytesToSkip -= sizeWritten;
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
      return 0;
    }
    auto writtenChunkSize =
        buffer.write(static_cast<uint8_t *>(contents) + sizeWritten,
                     std::min(totalSize - sizeWritten, spaceAvailable));
    sizeWritten += writtenChunkSize;
    offset += writtenChunkSize;
  }

  // Set on the way out: waiting for room is not the sender going quiet.
  silentSince = std::chrono::steady_clock::now();
  return sizeWritten;
}

int AudioGraphHttpStream::transferInfoCallback(void *stream, curl_off_t,
                                               curl_off_t, curl_off_t,
                                               curl_off_t) {
  const auto &self = *static_cast<AudioGraphHttpStream *>(stream);
  const bool abort = self.readerThread.get_stop_token().stop_requested() ||
                     self.seekRequestSignal.getStopToken().stop_requested() ||
                     self.stalled();
  return abort ? 1 : 0;
}

bool AudioGraphHttpStream::stalled() const {
  return stallTimeout > std::chrono::seconds::zero() &&
         std::chrono::steady_clock::now() - silentSince >= stallTimeout;
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
  int numRetries = RETRIES;
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

    const size_t requestedFrom = offset;
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
      if (offset - requestedFrom >= PROGRESS_THAT_RENEWS_RETRIES) {
        numRetries = RETRIES;
      }
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
      numRetries = RETRIES;
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
    } else if (responseCode >= 500 && responseCode < 600 && numRetries > 0 &&
               acceptRange) {
      --numRetries;
      spdlog::warn(
          "HTTP GET request failed with code {}, retrying {} more times",
          responseCode, numRetries);
      std::this_thread::sleep_for(std::chrono::seconds(1));
    } else {
      throw std::runtime_error("HTTP GET request failed with code " +
                               std::to_string(responseCode));
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
  // Called about once a second while nothing arrives, unlike WriteCallback.
  request.setOpt(new curlpp::options::NoProgress(false));
  curl_easy_setopt(request.getHandle(), CURLOPT_XFERINFOFUNCTION,
                   &AudioGraphHttpStream::transferInfoCallback);
  curl_easy_setopt(request.getHandle(), CURLOPT_XFERINFODATA, this);
  request.setOpt(new curlpp::options::WriteFunction(
      std::bind(&AudioGraphHttpStream::WriteCallback, this, _1, _2, _3)));
  if (!hasReadHeader) {
    request.setOpt(new curlpp::options::HeaderFunction(std::bind(
        &AudioGraphHttpStream::headerCallback, this, std::placeholders::_1,
        std::placeholders::_2, std::placeholders::_3)));
  }
  silentSince = std::chrono::steady_clock::now();
  bytesToSkip = offset;
  try {
    request.perform();
  } catch (const curlpp::LibcurlRuntimeError &ex) {
    if (ex.whatCode() == CURLE_ABORTED_BY_CALLBACK && stalled()) {
      throw curlpp::LibcurlRuntimeError(
          "Nothing received for " + std::to_string(stallTimeout.count()) +
              " s",
          CURLE_OPERATION_TIMEDOUT);
    }
    throw;
  }
  long responseCode = 0;
  curlpp::Info<CURLINFO_RESPONSE_CODE, long>::get(request, responseCode);
  // Only a success: a failed request's headers carry no Content-Range.
  if (responseCode >= 200 && responseCode < 300) {
    hasReadHeader = true;
  }
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
