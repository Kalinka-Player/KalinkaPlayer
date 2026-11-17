#include "DsdBlockKernels.h"

#include "ContainerTestSupport.h"

#include <gtest/gtest.h>

#include <random>
#include <string>

using container_test::bitsReversed;
using container_test::Bytes;

TEST(DsdBlockKernels, EveryLengthMatchesByteByByteWithinItsBounds) {
  constexpr uint8_t kGuard = 0xa5;
  constexpr size_t kGuards = 64;
  std::mt19937 random(1);
  for (unsigned channels : {1u, 2u})
    for (bool reverse : {false, true})
      for (size_t bytes = 0; bytes <= 200; ++bytes) {
        SCOPED_TRACE(std::to_string(channels) + " channels, " +
                     std::to_string(bytes) + " bytes" +
                     (reverse ? ", reversed" : ""));
        const size_t stride = bytes + 5;
        // Ends at the last byte it may read, so a sanitiser sees an over-read.
        Bytes blocks(stride * (channels - 1) + bytes);
        for (auto &byte : blocks)
          byte = uint8_t(random());
        Bytes expected;
        for (size_t b = 0; b < bytes; ++b)
          for (unsigned c = 0; c < channels; ++c) {
            const auto byte = blocks[c * stride + b];
            expected.push_back(reverse ? bitsReversed(byte) : byte);
          }
        Bytes out(expected.size() + kGuards, kGuard);
        dsd_block_kernels::interleave(blocks.data(), stride, channels, bytes,
                                      reverse, out.data());
        EXPECT_EQ(Bytes(out.begin(), out.begin() + expected.size()), expected);
        EXPECT_EQ(Bytes(out.begin() + expected.size(), out.end()),
                  Bytes(kGuards, kGuard))
            << "wrote past the frames";
      }
}
