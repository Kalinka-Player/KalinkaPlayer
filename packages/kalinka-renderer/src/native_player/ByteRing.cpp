#include "ByteRing.h"

#include <algorithm>
#include <cstring>
#include <stdexcept>
#include <thread>

ByteRing::ByteRing(size_t capacity)
    : storage(std::make_unique_for_overwrite<uint8_t[]>(capacity)),
      allocated(capacity), limit(capacity) {}

size_t ByteRing::read(void *dest, size_t bytes) {
  // While this is set a discard waits, so nothing is written past one until
  // the copy below is done.
  reading.store(true, std::memory_order_seq_cst);
  const auto end = written.load(std::memory_order_seq_cst);
  const auto begin = std::max(consumed.load(std::memory_order_relaxed),
                              dropped.load(std::memory_order_seq_cst));
  const auto count = end > begin ? std::min<uint64_t>(bytes, end - begin) : 0;
  if (!count) {
    reading.store(false, std::memory_order_seq_cst);
    return 0;
  }
  const auto cap = capacity();
  const auto at = size_t(begin % cap);
  const auto first = std::min<size_t>(count, cap - at);
  auto *to = static_cast<uint8_t *>(dest);
  std::memcpy(to, storage.get() + at, first);
  std::memcpy(to + first, storage.get(), count - first);
  consumed.store(begin + count, std::memory_order_seq_cst);
  reading.store(false, std::memory_order_seq_cst);
  wakeWriter();
  return count;
}

size_t ByteRing::waitForData(std::stop_token token, size_t bytes) {
  if (bytes && !holds(bytes)) {
    std::unique_lock lock(mutex);
    readerWants.store(bytes, std::memory_order_seq_cst);
    hasData.wait(lock, token, [&] { return holds(bytes); });
    readerWants.store(0, std::memory_order_relaxed);
  }
  return size();
}

size_t ByteRing::waitForDataFor(std::stop_token token,
                                std::chrono::milliseconds timeout,
                                size_t bytes) {
  if (bytes && !holds(bytes)) {
    std::unique_lock lock(mutex);
    readerWants.store(bytes, std::memory_order_seq_cst);
    hasData.wait_for(lock, token, timeout, [&] { return holds(bytes); });
    readerWants.store(0, std::memory_order_relaxed);
  }
  return size();
}

size_t ByteRing::write(const void *source, size_t bytes) {
  const auto end = written.load(std::memory_order_relaxed);
  const auto cap = capacity();
  const auto count = std::min<uint64_t>(bytes, cap - (end - readPosition()));
  if (!count)
    return 0;
  const auto at = size_t(end % cap);
  const auto first = std::min<size_t>(count, cap - at);
  const auto *from = static_cast<const uint8_t *>(source);
  std::memcpy(storage.get() + at, from, first);
  std::memcpy(storage.get(), from + first, count - first);
  written.store(end + count, std::memory_order_seq_cst);
  wakeReader();
  return count;
}

size_t ByteRing::waitForSpace(std::stop_token token, size_t bytes) {
  if (bytes && !hasRoomFor(bytes)) {
    std::unique_lock lock(mutex);
    writerWants.store(bytes, std::memory_order_seq_cst);
    hasSpace.wait(lock, token, [&] { return hasRoomFor(bytes); });
    writerWants.store(0, std::memory_order_relaxed);
  }
  return space();
}

void ByteRing::discard() {
  dropped.store(written.load(std::memory_order_relaxed),
                std::memory_order_seq_cst);
  awaitIdleReader();
}

void ByteRing::setCapacity(size_t bytes) {
  if (!bytes || !empty())
    throw std::invalid_argument("Only an empty ring can be resized");
  awaitIdleReader();
  if (bytes > allocated) {
    storage = std::make_unique_for_overwrite<uint8_t[]>(bytes);
    allocated = bytes;
  }
  limit.store(bytes, std::memory_order_seq_cst);
}

void ByteRing::setEof() {
  {
    std::lock_guard lock(mutex);
    eof.store(true, std::memory_order_seq_cst);
  }
  hasData.notify_all();
}

void ByteRing::resetEof() { eof.store(false, std::memory_order_seq_cst); }

size_t ByteRing::size() const {
  // The end first: an end written past a discard comes with that discard.
  const auto end = written.load(std::memory_order_seq_cst);
  const auto begin = readPosition();
  return end > begin ? size_t(end - begin) : 0;
}

uint64_t ByteRing::readPosition() const {
  return std::max(consumed.load(std::memory_order_seq_cst),
                  dropped.load(std::memory_order_seq_cst));
}

size_t ByteRing::space() const {
  const auto cap = capacity();
  const auto held = size();
  return cap > held ? cap - held : 0;
}

bool ByteRing::holds(size_t bytes) const {
  return isEof() || size() >= std::min(bytes, capacity());
}

bool ByteRing::hasRoomFor(size_t bytes) const {
  return space() >= std::min(bytes, capacity());
}

void ByteRing::wakeReader() {
  if (const auto wanted = readerWants.load(std::memory_order_seq_cst);
      wanted && holds(wanted)) {
    std::lock_guard lock(mutex);
    hasData.notify_one();
  }
}

void ByteRing::wakeWriter() {
  if (const auto wanted = writerWants.load(std::memory_order_seq_cst);
      wanted && hasRoomFor(wanted)) {
    std::lock_guard lock(mutex);
    hasSpace.notify_one();
  }
}

void ByteRing::awaitIdleReader() const {
  while (reading.load(std::memory_order_seq_cst))
    std::this_thread::yield();
}
