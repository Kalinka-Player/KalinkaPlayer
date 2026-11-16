#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <mutex>
#include <stop_token>

/**
 * @brief A bounded queue of bytes from one writer thread to one reader thread.
 *
 * Each side moves its own position with an atomic store and copies with at
 * most two memcpy calls; neither takes a lock unless the other is waiting for
 * it, and a waiter is woken only once what it waits for is there. One thread at
 * a time may use the writer's calls and one the reader's; size(), capacity()
 * and isEof() may be asked from any thread. Destroying the ring while a call
 * is in progress is undefined.
 */
class ByteRing {
public:
  explicit ByteRing(size_t capacity);

  ByteRing(const ByteRing &) = delete;
  ByteRing &operator=(const ByteRing &) = delete;

  /// Reader: moves up to @p bytes into @p dest; returns how many it moved.
  size_t read(void *dest, size_t bytes);
  /**
   * @brief Reader: waits until @p bytes are held, or the whole capacity when
   * that is less, or the end is marked or @p token stops.
   * @return The bytes held.
   */
  size_t waitForData(std::stop_token token = {}, size_t bytes = 1);
  /// Reader: waitForData() that gives up after @p timeout.
  size_t waitForDataFor(std::stop_token token,
                        std::chrono::milliseconds timeout, size_t bytes);

  /// Writer: appends up to @p bytes; returns how many there was room for.
  size_t write(const void *source, size_t bytes);
  /**
   * @brief Writer: waits until there is room for @p bytes, or the whole
   * capacity when that is less, or @p token stops.
   * @return The room there is.
   */
  size_t waitForSpace(std::stop_token token = {}, size_t bytes = 1);
  /**
   * @brief Writer: drops everything written so far. The reader never sees it,
   * and its room is free when this returns.
   * @note Waits for a read in progress to finish its copy.
   */
  void discard();
  /**
   * @brief Writer: resizes the ring, which must hold nothing.
   * @throws std::invalid_argument for 0 bytes or a ring that holds data.
   */
  void setCapacity(size_t bytes);
  /// Writer: marks the end, after the last byte written so far.
  void setEof();
  /// Writer: clears the end mark, for a writer about to write again.
  void resetEof();

  size_t size() const;
  size_t capacity() const { return limit.load(std::memory_order_relaxed); }
  bool empty() const { return size() == 0; }
  /// Whether the end is marked; when it is, size() counts every byte left.
  bool isEof() const { return eof.load(std::memory_order_seq_cst); }

private:
  std::unique_ptr<uint8_t[]> storage;
  size_t allocated;
  std::atomic<size_t> limit;
  /// Bytes ever written, read and dropped; they only grow, and never wrap.
  std::atomic<uint64_t> written{0}, consumed{0}, dropped{0};
  /// Set while read() may be copying, so discard() can wait for it.
  std::atomic<bool> reading{false};
  std::atomic<bool> eof{false};
  /// What a waiting side waits for; 0 when it is not waiting.
  std::atomic<size_t> readerWants{0}, writerWants{0};
  std::mutex mutex;
  std::condition_variable_any hasData, hasSpace;

  uint64_t readPosition() const;
  size_t space() const;
  bool holds(size_t bytes) const;
  bool hasRoomFor(size_t bytes) const;
  void wakeReader();
  void wakeWriter();
  void awaitIdleReader() const;
};
