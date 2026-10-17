#include "DashManifest.h"

#include <libxml/parser.h>
#include <libxml/uri.h>

#include <charconv>
#include <cmath>
#include <memory>
#include <regex>
#include <stdexcept>
#include <string_view>

namespace {
constexpr size_t maxSegments = 100000;
[[noreturn]] void invalid() {
  throw std::runtime_error("Unsupported or invalid DASH audio manifest");
}
std::string attribute(const xmlNode *node, const char *name) {
  std::unique_ptr<xmlChar, decltype(xmlFree)> value(
      xmlGetProp(node, BAD_CAST name), xmlFree);
  return value ? std::string(reinterpret_cast<const char *>(value.get())) : "";
}
bool named(const xmlNode *node, const char *name) {
  return node->type == XML_ELEMENT_NODE &&
         xmlStrEqual(node->name, BAD_CAST name);
}
const xmlNode *first(const xmlNode *node, const char *name) {
  for (auto *item = node->children; item; item = item->next)
    if (named(item, name))
      return item;
  return nullptr;
}
const xmlNode *child(const xmlNode *node, const char *name) {
  const auto *result = first(node, name);
  if (result)
    for (auto *item = result->next; item; item = item->next)
      if (named(item, name))
        invalid();
  return result;
}
uint64_t number(std::string_view text) {
  uint64_t value = 0;
  auto [end, error] =
      std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc{} || end != text.data() + text.size() ||
      value > 1000000000000ULL)
    invalid();
  return value;
}
std::string resolve(const std::string &reference, const std::string &base) {
  std::unique_ptr<xmlChar, decltype(xmlFree)> value(
      xmlBuildURI(BAD_CAST reference.c_str(), BAD_CAST base.c_str()), xmlFree);
  if (!value)
    invalid();
  std::string url(reinterpret_cast<const char *>(value.get()));
  if (url.size() > 16384 ||
      (!url.starts_with("https://") && !url.starts_with("http://")))
    throw std::runtime_error("DASH requires HTTP or HTTPS media URLs");
  return url;
}
std::string expand(const std::string &text, const DashManifest &manifest,
                   uint64_t time, uint64_t index) {
  std::string output;
  for (size_t i = 0; i < text.size();) {
    if (text[i] != '$') {
      output += text[i++];
      continue;
    }
    const auto end = text.find('$', i + 1);
    if (end == std::string::npos)
      invalid();
    const auto token = text.substr(i + 1, end - i - 1);
    if (token.empty())
      output += '$';
    else if (token == "RepresentationID")
      output += manifest.representation;
    else {
      const auto percent = token.find('%');
      const auto name = token.substr(0, percent);
      const auto value = name == "Number"      ? index
                         : name == "Time"      ? time
                         : name == "Bandwidth" ? manifest.bandwidth
                                               : (invalid(), 0);
      auto digits = std::to_string(value);
      if (percent != std::string::npos) {
        const auto format = token.substr(percent);
        if (format.size() < 4 || !format.starts_with("%0") ||
            format.back() != 'd')
          invalid();
        const auto width =
            number(std::string_view(format).substr(2, format.size() - 3));
        if (!width || width > 20)
          invalid();
        if (width > digits.size())
          digits.insert(0, width - digits.size(), '0');
      }
      output += digits;
    }
    i = end + 1;
    if (output.size() > 16384)
      invalid();
  }
  return resolve(output, manifest.baseUrl);
}
double seconds(const std::string &text) {
  if (text.size() > 64)
    invalid();
  static const std::regex pattern(
      R"(^P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$)");
  std::smatch match;
  if (!std::regex_match(text, match, pattern))
    invalid();
  double result = 0;
  const double units[] = {86400, 3600, 60, 1};
  for (size_t i = 1; i <= 4; ++i)
    if (match[i].matched)
      result += std::stod(match[i].str()) * units[i - 1];
  if (!std::isfinite(result) || result < 0 || result > 7 * 86400)
    invalid();
  return result;
}
} // namespace

DashManifest DashManifest::parse(std::span<const uint8_t> bytes,
                                 const std::string &url) {
  if (bytes.empty() || bytes.size() > 1024 * 1024)
    invalid();
  // Older libxml2 initializes lazily and races when two streams parse at once.
  [[maybe_unused]] static const bool initialized = (xmlInitParser(), true);
  const std::string xml(bytes.begin(), bytes.end());
  if (xml.find("<!DOCTYPE") != std::string::npos ||
      xml.find("<!ENTITY") != std::string::npos)
    invalid();
  std::unique_ptr<xmlDoc, decltype(&xmlFreeDoc)> doc(
      xmlReadMemory(xml.data(), int(xml.size()), nullptr, nullptr,
                    XML_PARSE_NONET | XML_PARSE_NOERROR | XML_PARSE_NOWARNING),
      xmlFreeDoc);
  if (!doc || doc->intSubset || doc->extSubset)
    invalid();
  const auto *root = xmlDocGetRootElement(doc.get());
  if (!root || !named(root, "MPD") || !root->ns ||
      !xmlStrEqual(root->ns->href, BAD_CAST "urn:mpeg:dash:schema:mpd:2011") ||
      (!attribute(root, "type").empty() && attribute(root, "type") != "static"))
    invalid();
  std::vector<const xmlNode *> pending{root};
  while (!pending.empty()) {
    const auto *node = pending.back();
    pending.pop_back();
    if (named(node, "ContentProtection"))
      throw std::runtime_error("Encrypted DASH audio is not supported");
    for (auto *item = node->children; item; item = item->next)
      if (item->type == XML_ELEMENT_NODE)
        pending.push_back(item);
  }
  const auto duration = seconds(attribute(root, "mediaPresentationDuration"));
  const auto *period = child(root, "Period");
  if (!period || duration <= 0)
    invalid();
  if (!attribute(period, "start").empty() &&
      seconds(attribute(period, "start")) != 0)
    invalid();
  const xmlNode *adaptation = nullptr, *representation = nullptr;
  uint64_t best = 0;
  for (auto *set = period->children; set; set = set->next) {
    if (!named(set, "AdaptationSet"))
      continue;
    for (auto *rep = set->children; rep; rep = rep->next) {
      if (!named(rep, "Representation"))
        continue;
      auto codec = attribute(rep, "codecs");
      if (codec.empty())
        codec = attribute(set, "codecs");
      if (codec != "flac" && codec != "fLaC")
        continue;
      const auto bw = attribute(rep, "bandwidth");
      const auto bitrate = bw.empty() ? 0 : number(bw);
      if (!representation || bitrate > best) {
        adaptation = set;
        representation = rep;
        best = bitrate;
      }
    }
  }
  if (!representation)
    throw std::runtime_error("DASH currently supports FLAC audio only");
  DashManifest result;
  result.baseUrl = resolve(url, url);
  result.representation = attribute(representation, "id");
  result.bandwidth = best;
  const xmlNode *timeline = nullptr;
  uint64_t start = 1;
  for (const auto *node : {root, period, adaptation, representation}) {
    // Alternative BaseURLs name mirrors of the same media; the first is enough.
    if (const auto *base = first(node, "BaseURL")) {
      std::unique_ptr<xmlChar, decltype(xmlFree)> value(xmlNodeGetContent(base),
                                                        xmlFree);
      if (!value)
        invalid();
      result.baseUrl =
          resolve(reinterpret_cast<const char *>(value.get()), result.baseUrl);
    }
    if (child(node, "SegmentList") || child(node, "SegmentBase"))
      invalid();
    if (const auto *templ = child(node, "SegmentTemplate")) {
      if (const auto value = attribute(templ, "initialization"); !value.empty())
        result.initialization = value;
      if (const auto value = attribute(templ, "media"); !value.empty())
        result.media = value;
      if (const auto value = attribute(templ, "timescale"); !value.empty())
        result.timescale = number(value);
      if (const auto value = attribute(templ, "startNumber"); !value.empty())
        start = number(value);
      if (const auto value = attribute(templ, "presentationTimeOffset");
          !value.empty() && number(value) != 0)
        invalid();
      if (const auto *value = child(templ, "SegmentTimeline"))
        timeline = value;
    }
  }
  if (!timeline || result.initialization.empty() || result.media.empty() ||
      !result.timescale || result.timescale > 1000000000 || !start)
    invalid();
  uint64_t time = 0;
  const uint64_t endTime = std::llround(duration * result.timescale);
  for (auto *item = timeline->children; item; item = item->next) {
    if (!named(item, "S"))
      continue;
    if (const auto t = attribute(item, "t"); !t.empty() && number(t) != time)
      invalid();
    const auto d = number(attribute(item, "d"));
    if (!d || d > result.timescale * 3600)
      invalid();
    const auto repeat = attribute(item, "r");
    uint64_t count = 1;
    if (repeat == "-1") {
      auto limit = endTime;
      for (auto *next = item->next; next; next = next->next)
        if (named(next, "S")) {
          limit = number(attribute(next, "t"));
          break;
        }
      if (limit <= time)
        invalid();
      count = (limit - time + d - 1) / d;
    } else if (!repeat.empty())
      count += number(repeat);
    if (count > maxSegments - result.segments.size() ||
        d * count > result.timescale * 7 * 86400 - time)
      invalid();
    for (uint64_t i = 0; i < count; ++i) {
      result.segments.push_back({time, d, start++});
      time += d;
    }
  }
  if (result.segments.empty() ||
      std::abs(double(time) - endTime) > result.timescale * 0.02)
    invalid();
  result.initialization =
      expand(result.initialization, result, 0, result.segments.front().number);
  for (size_t i = 0; i < result.segments.size(); ++i)
    result.segmentUrl(i);
  return result;
}

std::string DashManifest::segmentUrl(size_t index) const {
  const auto &segment = segments.at(index);
  return expand(media, *this, segment.time, segment.number);
}
