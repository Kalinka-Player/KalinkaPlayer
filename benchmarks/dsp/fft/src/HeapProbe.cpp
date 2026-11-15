#include "HeapProbe.h"

#include <cerrno>
#include <cstdint>
#include <cstdlib>
#include <malloc.h>

extern "C" {
void *__libc_malloc(size_t size) noexcept;
void *__libc_calloc(size_t count, size_t size) noexcept;
void *__libc_realloc(void *ptr, size_t size) noexcept;
void *__libc_memalign(size_t alignment, size_t size) noexcept;
void *__libc_valloc(size_t size) noexcept;
void *__libc_pvalloc(size_t size) noexcept;
void __libc_free(void *ptr) noexcept;
}

namespace {

HeapUse total;

void *counted(void *ptr) {
  if (ptr) {
    const size_t bytes = malloc_usable_size(ptr);
    ++total.allocations;
    total.allocatedBytes += bytes;
    total.heldBytes += static_cast<std::ptrdiff_t>(bytes);
  }
  return ptr;
}

void released(void *ptr) {
  if (ptr) {
    total.heldBytes -= static_cast<std::ptrdiff_t>(malloc_usable_size(ptr));
  }
}

} // namespace

HeapProbe::HeapProbe() : start(total) {}

HeapUse HeapProbe::use() const {
  return {total.allocations - start.allocations,
          total.allocatedBytes - start.allocatedBytes,
          total.heldBytes - start.heldBytes};
}

extern "C" {

void *malloc(size_t size) noexcept { return counted(__libc_malloc(size)); }

void *calloc(size_t count, size_t size) noexcept {
  return counted(__libc_calloc(count, size));
}

void *realloc(void *ptr, size_t size) noexcept {
  const std::ptrdiff_t before =
      ptr ? static_cast<std::ptrdiff_t>(malloc_usable_size(ptr)) : 0;
  void *moved = __libc_realloc(ptr, size);
  // glibc frees the block on success and on realloc(ptr, 0), not on failure.
  if (moved || size == 0) {
    total.heldBytes -= before;
  }
  return counted(moved);
}

void *reallocarray(void *ptr, size_t count, size_t size) noexcept {
  if (size != 0 && count > SIZE_MAX / size) {
    errno = ENOMEM;
    return nullptr;
  }
  return realloc(ptr, count * size);
}

void free(void *ptr) noexcept {
  released(ptr);
  __libc_free(ptr);
}

void *memalign(size_t alignment, size_t size) noexcept {
  return counted(__libc_memalign(alignment, size));
}

void *aligned_alloc(size_t alignment, size_t size) noexcept {
  return counted(__libc_memalign(alignment, size));
}

int posix_memalign(void **out, size_t alignment, size_t size) noexcept {
  if (alignment == 0 || alignment % sizeof(void *) != 0 ||
      (alignment & (alignment - 1)) != 0) {
    return EINVAL;
  }
  void *ptr = counted(__libc_memalign(alignment, size));
  if (!ptr) {
    return ENOMEM;
  }
  *out = ptr;
  return 0;
}

void *valloc(size_t size) noexcept { return counted(__libc_valloc(size)); }

void *pvalloc(size_t size) noexcept { return counted(__libc_pvalloc(size)); }
}
