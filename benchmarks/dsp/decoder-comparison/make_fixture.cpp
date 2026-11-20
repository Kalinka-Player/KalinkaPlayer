// Generate the fixed comparison DSF on the reference machine. For comparisons
// on other machines use the committed file, since modulator arithmetic varies.
#include "SigmaDelta.h"
#include "Signals.h"
#include <algorithm>
#include <fstream>

int main(int argc, char **argv) {
  if (argc != 2)
    return 1;
  constexpr unsigned fs = 2822400, ratio = 16, slot = 2048, edge = 768;
  constexpr unsigned toneStart = ratio * slot;
  const auto tone =
      harness::SigmaDelta(7, 64).modulate(harness::sine(1000, fs, fs / 2, .5));
  std::vector<uint8_t> bits(toneStart * ratio + tone.size() + slot * ratio);
  for (size_t i = 0; i < bits.size(); ++i)
    bits[i] = (0x69 >> (7 - i % 8)) & 1;
  for (unsigned phase = 0; phase < ratio; ++phase)
    bits[(phase * slot + edge) * ratio + phase] ^= 1;
  std::copy(tone.begin(), tone.end(), bits.begin() + toneStart * ratio);
  const auto dsf = harness::dsfFile({bits}, fs);
  std::ofstream output(argv[1], std::ios::binary);
  output.exceptions(std::ios::failbit | std::ios::badbit);
  output.write(reinterpret_cast<const char *>(dsf.data()), dsf.size());
}
