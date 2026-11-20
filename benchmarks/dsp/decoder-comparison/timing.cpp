// Only the entry point and formatting live here. The revision's benchmark
// supplies the random input, warm-up, repetition, kernel and time measurement.
#include "Measure.h"
#include <cstdio>
namespace bench {
std::string cost(double ns, unsigned rate) {
  char text[80];
  std::snprintf(text, sizeof(text), "%.2f ns (%.3f %%)", ns,
                percentOfCore(ns, rate));
  return text;
}
} // namespace bench
int main() { bench::runDsd(); }
