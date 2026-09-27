#ifndef BUFFER_H
#define BUFFER_H

#include <atomic>
#include <cassert>
#include <condition_variable>
#include <deque>
#include <functional>
#include <iostream>
#include <mutex>
#include <stop_token>

template <class T> class Buffer {
  std::deque<T> data;
  mutable std::mutex m;
  std::condition_variable_any hasDataCon;
  std::condition_variable_any hasSpaceCon;

  size_t capacity;
  std::atomic<bool> eof;
  std::function<void(Buffer<T> &)> onEmptyCallback;
  bool done = false;

public:
  Buffer(
      size_t capacity,
      std::function<void(Buffer<T> &)> onEmptyCallback = [](Buffer<T> &) {})
      : capacity(capacity), eof(false), onEmptyCallback(onEmptyCallback) {}

  ~Buffer() {
    {
      std::lock_guard<std::mutex> lock(m);
      done = true;
    }
    hasDataCon.notify_all();
    hasSpaceCon.notify_all();
  }

  Buffer(const Buffer &) = delete;

  size_t max_size() const { return capacity; }

  size_t read(T *dest, size_t size) {
    size_t sizeToCopy = 0;
    size_t remainingSize = 0;
    {
      std::lock_guard<std::mutex> lock(m);
      auto availableData = data.size();
      if (availableData > 0) {
        sizeToCopy = std::min(availableData, size);
        std::copy_n(data.begin(), sizeToCopy, dest);
        data.erase(data.begin(), data.begin() + sizeToCopy);
      }
      remainingSize = data.size();
    }

    if (remainingSize == 0) {
      onEmptyCallback(*this);
    }

    if (sizeToCopy != 0) {
      hasSpaceCon.notify_one();
    }

    return sizeToCopy;
  }

  size_t write(const T *source, size_t size) {
    size_t sizeToCopy = 0;
    {
      std::lock_guard<std::mutex> lock(m);
      sizeToCopy = std::min(size, capacity - data.size());
      data.insert(data.end(), source, source + sizeToCopy);
      assert(data.size() <= capacity);
    }

    if (sizeToCopy != 0) {
      hasDataCon.notify_one();
    }
    return sizeToCopy;
  }

  size_t waitForData(std::stop_token stopToken = std::stop_token(),
                     size_t size = 1) {
    if (size > 0) {
      std::unique_lock<std::mutex> lock(m);
      hasDataCon.wait(lock, stopToken, [this, size] {
        return done || data.size() >= std::min(size, capacity) || eof.load();
      });
    }

    return data.size();
  }

  size_t waitForDataFor(std::stop_token stopToken = std::stop_token(),
                        std::chrono::milliseconds timeout = {},
                        size_t size = 1) {
    if (size > 0) {
      std::unique_lock<std::mutex> lock(m);
      hasDataCon.wait_for(lock, stopToken, timeout, [this, size] {
        return done || data.size() >= std::min(size, capacity) || eof.load();
      });
    }

    return data.size();
  }

  size_t waitForSpace(std::stop_token stopToken = std::stop_token(),
                      size_t size = 1) {
    size = std::min(size, capacity);
    if (size > 0) {
      std::unique_lock<std::mutex> lock(m);
      hasSpaceCon.wait(lock, stopToken, [this, size]() {
        return done || (capacity - data.size()) >= size;
      });
    }
    return capacity - data.size();
  }

  size_t size() const {
    std::lock_guard lock(m);
    return data.size();
  }

  size_t availableSpace() const {
    std::lock_guard lock(m);
    return capacity - data.size();
  }

  bool empty() const { return data.empty(); }

  void setEof() {
    {
      std::lock_guard<std::mutex> lock(m);
      // Under the lock, or a waiter could see the end before the callback ran.
      eof.store(true);
      if (data.empty()) {
        onEmptyCallback(*this);
      }
    }
    hasDataCon.notify_all();
  }

  bool isEof() { return eof.load(); }

  void resetEof() { eof.store(false); }

  void clear() {
    {
      std::lock_guard<std::mutex> lock(m);
      data.clear();
      if (data.empty()) {
        onEmptyCallback(*this);
      }
    }
    hasSpaceCon.notify_all();
  }
};

#endif