#include "Identity.h"

#include <spdlog/spdlog.h>

#include <cstdint>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <random>

namespace fs = std::filesystem;

std::string generateUuid() {
  // UUIDv4 from std::random_device (urandom-backed on Linux).
  std::random_device rd;
  std::uniform_int_distribution<uint32_t> dist;
  uint32_t r[4] = {dist(rd), dist(rd), dist(rd), dist(rd)};
  r[1] = (r[1] & 0xffff0fffu) | 0x00004000u;  // version 4
  r[2] = (r[2] & 0x3fffffffu) | 0x80000000u;  // variant 10xx
  char buf[37];
  std::snprintf(buf, sizeof(buf), "%08x-%04x-%04x-%04x-%04x%08x", r[0],
                r[1] >> 16, r[1] & 0xffff, r[2] >> 16, r[2] & 0xffff, r[3]);
  return buf;
}

namespace {

fs::path identityFile() {
  const char *prefix = std::getenv("KALINKA_PREFIX");
  return fs::path(prefix && *prefix ? prefix : "/") /
         "var/lib/kalinka-renderer/renderer_id";
}

std::string loadOrCreateRendererId() {
  const fs::path file = identityFile();
  std::error_code ec;

  std::ifstream in(file);
  std::string id;
  if (in && std::getline(in, id) && id.size() == 36) {
    return id;
  }

  id = generateUuid();
  fs::create_directories(file.parent_path(), ec);
  // Atomic write so a crash can't leave a truncated id behind.
  const fs::path tmp = file.string() + ".tmp";
  std::ofstream out(tmp, std::ios::trunc);
  out << id << '\n';
  out.close();
  if (out) {
    fs::rename(tmp, file, ec);
  }
  if (!out || ec) {
    spdlog::warn("Could not persist renderer id at {}; using ephemeral id",
                 file.string());
  }
  return id;
}

}  // namespace

Identity Identity::load() {
  std::ifstream in("/etc/machine-id");
  std::string machineId;
  in >> machineId;
  if (machineId.size() != 32 ||
      machineId.find_first_not_of("0123456789abcdef") != std::string::npos ||
      machineId == std::string(32, '0')) {
    machineId.clear();
  }
  return Identity{loadOrCreateRendererId(), generateUuid(), machineId};
}
