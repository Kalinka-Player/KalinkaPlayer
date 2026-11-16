#ifndef STALLING_FILE_INPUT_H
#define STALLING_FILE_INPUT_H

#include "FileInputNode.h"

#include <chrono>
#include <condition_variable>
#include <mutex>
#include <stdexcept>
#include <string>

/// @brief Seekable input whose reads can stall until cancelled or released.
class StallingFileInput : public AudioGraphOutputNode {
public:
  explicit StallingFileInput(const std::string &path) : source(1, path) {}

  StreamState getState() override { return source.getState(); }
  size_t read(void *data, size_t size) override {
    return source.read(data, size);
  }
  size_t seekTo(size_t position) override { return source.seekTo(position); }
  size_t waitForData(std::stop_token token, size_t size) override {
    std::unique_lock lock(mutex);
    if (stalled) {
      ++blockedReads;
      changed.notify_all();
      changed.wait(lock, token, [this] { return !stalled; });
    }
    if (token.stop_requested()) {
      return 0;
    }
    if (failed) {
      throw std::runtime_error("Test input failed during seek");
    }
    lock.unlock();
    return source.waitForData(token, size);
  }
  size_t waitForDataFor(std::stop_token token, std::chrono::milliseconds,
                        size_t size) override {
    return waitForData(token, size);
  }

  void stall() {
    std::lock_guard lock(mutex);
    stalled = true;
  }
  void release(bool fail = false) {
    std::lock_guard lock(mutex);
    stalled = false;
    failed = fail;
    changed.notify_all();
  }
  bool waitUntilStalled(size_t count = 1) {
    std::unique_lock lock(mutex);
    return changed.wait_for(lock, std::chrono::seconds(1),
                            [this, count] { return blockedReads >= count; });
  }

private:
  FileInputNode source;
  std::mutex mutex;
  std::condition_variable_any changed;
  bool stalled = false;
  bool failed = false;
  size_t blockedReads = 0;
};

#endif
