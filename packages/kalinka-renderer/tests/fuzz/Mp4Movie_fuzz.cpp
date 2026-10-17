#include "FragmentedMp4.h"
#include "Mp4SampleTable.h"

#include <stdexcept>

extern "C" int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size) {
  const std::span<const uint8_t> file(data, size);
  try {
    const auto table = Mp4SampleTable::parse(file);
    if (table.offsets.size() != table.sizes.size() ||
        table.times.size() != table.sizes.size() ||
        (!table.times.empty() && table.times.back() >= table.end))
      __builtin_trap();
  } catch (const std::runtime_error &) {
  }
  try {
    const auto init = FragmentedMp4::parseInitialization(file);
    for (const auto &box : mp4::boxes(file)) {
      try {
        if (box.type == "sidx")
          init.parseIndex(box.payload,
                          box.offset + box.header + box.payload.size());
        else if (box.type == "mfra")
          init.parseRandomAccess(box.payload);
        else if (box.type == "moof")
          init.timing(box.payload);
      } catch (const std::runtime_error &) {
      }
    }
  } catch (const std::runtime_error &) {
  }
  return 0;
}
