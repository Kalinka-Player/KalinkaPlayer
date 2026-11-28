#include "SignalPath.h"

#include <format>
#include <utility>

namespace {
unsigned significantBits(const StreamAudioFormat &format) {
  return format.bitsPerSample ? format.bitsPerSample
                              : unsigned(sampleBits(format.sampleFormat));
}

std::string described(const StreamAudioFormat &format) {
  return std::format("{}-bit {}", significantBits(format),
                     isFloat(format.sampleFormat) ? "float" : "integer");
}
} // namespace

namespace signal_step {
SignalStep toFloat(const StreamAudioFormat &from, std::string_view purpose) {
  std::string summary = "From " + described(from);
  if (!purpose.empty())
    summary += std::format(", {}", purpose);
  return {kFloat, "64-bit float", std::move(summary), true};
}

std::optional<SignalStep> container(const StreamAudioFormat &from,
                                    const StreamAudioFormat &to) {
  if (from.sampleFormat == to.sampleFormat || isFloat(from.sampleFormat) ||
      isFloat(to.sampleFormat) || isDsd(from.sampleFormat) ||
      isDsd(to.sampleFormat))
    return std::nullopt;
  const unsigned bits = significantBits(from);
  const auto room = unsigned(sampleBits(to.sampleFormat));
  if (room > bits)
    return SignalStep{kZeroPadding, "Zero-padding",
                      std::format("{}-bit into {}-bit", bits, room), true};
  return SignalStep{kRepack, "Repacked",
                    std::format("{}-bit, {} bytes to {}", bits,
                                sampleSize(from.sampleFormat),
                                sampleSize(to.sampleFormat)),
                    true};
}
} // namespace signal_step
