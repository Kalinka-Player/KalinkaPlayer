#include "DspProfileCache.h"
#include "DspProfile.h"
#include "SettingsPersistence.h"

#include <curl/curl.h>
#include <openssl/sha.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <format>
#include <fstream>
#include <memory>
#include <stdexcept>

namespace fs = std::filesystem;
namespace dsp_profiles {
namespace {
constexpr size_t kMaxFirSize = 32u << 20;
struct Download {
  std::string bytes;
  size_t limit;
};

kalinka::renderer::v1::FirProfile parseFir(const std::string &bytes) {
  kalinka::renderer::v1::FirProfile profile;
  if (!profile.ParseFromString(bytes) || profile.name().empty() ||
      profile.name().size() > 120 ||
      profile.name().find_first_of("\r\n") != std::string::npos ||
      profile.impulse_wav_size() < 1 || profile.impulse_wav_size() > 8)
    throw std::runtime_error("Invalid FIR filter profile");
  return profile;
}

constexpr uintmax_t kMaxCacheBytes = 256ull << 20;

size_t append(char *src, size_t size, size_t count, void *dest) {
  auto &[bytes, limit] = *static_cast<Download *>(dest);
  if (size && count > (limit - bytes.size()) / size)
    return 0;
  bytes.append(src, size * count);
  return size * count;
}

std::string digest(std::string_view bytes) {
  unsigned char sum[SHA256_DIGEST_LENGTH];
  SHA256(reinterpret_cast<const unsigned char *>(bytes.data()), bytes.size(),
         sum);
  std::string result;
  for (auto byte : sum)
    std::format_to(std::back_inserter(result), "{:02x}", byte);
  return result;
}

void trimCache(const fs::path &dir, const std::string &incoming, bool fir) {
  const auto saved = loadSettingsOverrides();
  const auto selected =
      saved.find(fir ? "output.rew_fir_profile" : "dsp.profile");
  std::vector<std::pair<fs::file_time_type, fs::path>> candidates;
  uintmax_t total = 0;
  for (const auto &file : fs::directory_iterator(dir)) {
    const auto id = file.path().filename().string();
    if (!validId(id) || !file.is_regular_file())
      continue;
    total += file.file_size();
    if (id != incoming && (selected == saved.end() || id != selected->second))
      candidates.emplace_back(file.last_write_time(), file.path());
  }
  if (total <= kMaxCacheBytes)
    return;
  std::ranges::sort(candidates);
  for (const auto &[time, file] : candidates) {
    const auto size = fs::file_size(file);
    if (fs::remove(file))
      total -= size;
    if (total <= kMaxCacheBytes)
      break;
  }
}
} // namespace

fs::path directory() {
  const char *prefix = std::getenv("KALINKA_PREFIX");
  return fs::path(prefix && *prefix ? prefix : "/") /
         "var/lib/kalinka-renderer/dsp";
}

bool validId(std::string_view id) {
  return id.size() == 64 && id.find_first_not_of("0123456789abcdef") == id.npos;
}

static std::string cached(const std::string &id, bool fir) {
  if (!validId(id))
    throw std::runtime_error("Invalid profile SHA-256");
  const auto path = (fir ? directory() / "fir-profiles" : directory()) / id;
  std::ifstream file(path, std::ios::binary | std::ios::ate);
  const auto size = file.tellg();
  if (size <= 0 || size > std::streamoff(fir ? kMaxFirSize : kMaxSize))
    throw std::runtime_error("Profile is unavailable in the cache");
  std::string bytes(size_t(size), '\0');
  file.seekg(0);
  if (!file.read(bytes.data(), size) || digest(bytes) != id)
    throw std::runtime_error("Cached profile SHA-256 mismatch");
  return bytes;
}

std::shared_ptr<const peq::Profile> load(const std::string &id) {
  return std::make_shared<const peq::Profile>(parse(cached(id, false)));
}

kalinka::renderer::v1::FirProfile loadFir(const std::string &id) {
  return parseFir(cached(id, true));
}

void fetch(const std::string &id, const std::string &url, bool fir) {
  if (!validId(id))
    throw std::runtime_error("Invalid profile SHA-256");
  const auto path = (fir ? directory() / "fir-profiles" : directory()) / id;
  if (fs::exists(path)) {
    try {
      if (fir)
        loadFir(id);
      else
        load(id);
      fs::last_write_time(path, fs::file_time_type::clock::now());
      return;
    } catch (const std::exception &) {
      if (url.empty())
        throw;
    }
  }
  if (url.empty())
    throw std::runtime_error("Profile is unavailable on this renderer");
  std::unique_ptr<CURL, decltype(&curl_easy_cleanup)> curl(curl_easy_init(),
                                                           curl_easy_cleanup);
  if (!curl)
    throw std::runtime_error("Could not initialise profile download");
  Download download{{}, fir ? kMaxFirSize : kMaxSize};
  auto &bytes = download.bytes;
  curl_easy_setopt(curl.get(), CURLOPT_URL, url.c_str());
  curl_easy_setopt(curl.get(), CURLOPT_PROTOCOLS_STR, "http,https");
  curl_easy_setopt(curl.get(), CURLOPT_FAILONERROR, 1L);
  curl_easy_setopt(curl.get(), CURLOPT_NOSIGNAL, 1L);
  curl_easy_setopt(curl.get(), CURLOPT_TIMEOUT_MS, fir ? 30000L : 2000L);
  curl_easy_setopt(curl.get(), CURLOPT_WRITEFUNCTION, append);
  curl_easy_setopt(curl.get(), CURLOPT_WRITEDATA, &download);
  if (curl_easy_perform(curl.get()) != CURLE_OK)
    throw std::runtime_error("Could not download DSP profile");
  if (digest(bytes) != id)
    throw std::runtime_error("Downloaded profile SHA-256 mismatch");
  if (fir)
    parseFir(bytes);
  else
    parse(bytes);
  fs::create_directories(path.parent_path());
  auto temp = path;
  temp += ".tmp";
  std::ofstream file(temp, std::ios::binary | std::ios::trunc);
  file.write(bytes.data(), std::streamsize(bytes.size()));
  file.close();
  if (!file)
    throw std::runtime_error("Could not cache DSP profile");
  fs::rename(temp, path);
  trimCache(path.parent_path(), id, fir);
}
} // namespace dsp_profiles
