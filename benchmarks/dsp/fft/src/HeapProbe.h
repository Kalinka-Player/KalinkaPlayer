#ifndef HEAP_PROBE_H
#define HEAP_PROBE_H

#include <cstddef>

/// Heap traffic over some stretch of the program.
struct HeapUse {
  /// Calls that obtained memory: malloc, calloc, realloc and the aligned ones.
  size_t allocations = 0;
  /// Usable bytes those calls returned.
  size_t allocatedBytes = 0;
  /// Bytes obtained minus bytes freed: what the stretch left held.
  std::ptrdiff_t heldBytes = 0;
};

/**
 * @brief Measures the heap traffic of the whole process from its construction
 * on.
 *
 * The program defines malloc, calloc, realloc, free and the aligned
 * allocators itself, counting and forwarding to glibc's own entry points, so
 * the counts include every shared library and operator new, which libstdc++
 * builds on malloc. glibc only.
 *
 * @note Not thread-safe: the counters are plain integers, and the benchmark
 * runs on one thread.
 */
class HeapProbe {
public:
  HeapProbe();

  HeapUse use() const;

private:
  const HeapUse start;
};

#endif
