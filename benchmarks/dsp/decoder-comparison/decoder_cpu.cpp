// Steady-state decoder CPU and a render of the same mono reference in stereo.
// Input: the benchmark's mono LSB-first DSD64 DSF, on stdin. Render output:
// stereo interleaved float64, little-endian at 176400 Hz, on stdout.
#include "dsp/dsd/DsdToPcm.h"
#include "pcm/Dsd2Pcm.hxx"
extern "C" {
#include <libavcodec/avcodec.h>
#include <libavutil/avutil.h>
#include <libswresample/swresample.h>
#include <samplerate.h>
}
#include <algorithm>
#include <array>
#include <bit>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
constexpr int channels = 2, block = 2048, pcm = 176400;
constexpr int capacity = block + 256;
using Output = std::array<double, channels * capacity>;
struct PacketDeleter {
  void operator()(AVPacket *p) const { av_packet_free(&p); }
};
using Packet = std::unique_ptr<AVPacket, PacketDeleter>;

void require(bool ok, const char *message) {
  if (!ok)
    throw std::runtime_error(message);
}

std::vector<Packet> source() {
  static_assert(std::endian::native == std::endian::little);
  std::array<uint8_t, 92> h{};
  std::cin.read(reinterpret_cast<char *>(h.data()), h.size());
  auto u32 = [&](int offset) {
    uint32_t n;
    std::memcpy(&n, h.data() + offset, 4);
    return n;
  };
  uint64_t bits;
  std::memcpy(&bits, h.data() + 64, 8);
  require(std::cin && !std::memcmp(h.data(), "DSD ", 4) &&
              !std::memcmp(h.data() + 80, "data", 4) && u32(52) == 1 &&
              u32(56) == 2822400 && u32(60) == 1 && u32(72) == 4096 &&
              bits % 16 == 0 && bits > 0 && bits < 2822400ULL * 60,
          "expected the mono DSD64 reference DSF");
  std::array<uint8_t, 256> reverse{};
  for (int n = 0; n < 256; ++n)
    for (int b = 0; b < 8; ++b)
      reverse[n] |= ((n >> b) & 1) << (7 - b);
  std::vector<Packet> packets;
  std::array<uint8_t, block> data{};
  for (size_t remaining = bits / 8; remaining;) {
    int n = std::min<size_t>(block, remaining);
    std::cin.read(reinterpret_cast<char *>(data.data()), n);
    require(bool(std::cin), "short DSF input");
    Packet packet(av_packet_alloc());
    require(packet && av_new_packet(packet.get(), channels * n) >= 0,
            "cannot allocate DSD packet");
    for (int i = 0; i < n; ++i)
      packet->data[2 * i] = packet->data[2 * i + 1] = reverse[data[i]];
    packets.push_back(std::move(packet));
    remaining -= n;
  }
  return packets;
}

struct Converter {
  virtual int process(const AVPacket &p, Output &out) = 0;
  // Only steady-state frames enter the CPU measurement. The tone analyser
  // uses an interior excerpt and ignores filter start-up and end transients.
  virtual ~Converter() = default;
};

class Kalinka final : public Converter {
  DsdToPcm decoder;
  std::array<double, capacity> left{}, right{};

public:
  explicit Kalinka(DsdFilter filter)
      : decoder({2822400, pcm, channels, filter, DsdBitOrder::MsbFirst}) {}
  int process(const AVPacket &p, Output &out) override {
    double *planes[]{left.data(), right.data()};
    int n = decoder.process(p.data, p.size / channels, planes);
    for (int i = 0; i < n; ++i) {
      out[2 * i] = left[i];
      out[2 * i + 1] = right[i];
    }
    return n;
  }
};

class FFmpeg final : public Converter {
  AVCodecContext *decoder = nullptr;
  AVFrame *frame = nullptr;
  SwrContext *swr = nullptr;

public:
  FFmpeg() {
    auto *codec = avcodec_find_decoder(AV_CODEC_ID_DSD_MSBF);
    require(codec, "FFmpeg DSD decoder unavailable");
    decoder = avcodec_alloc_context3(codec);
    frame = av_frame_alloc();
    require(decoder && frame, "cannot allocate FFmpeg decoder");
    decoder->sample_rate = 352800;
    decoder->thread_count = 1;
    av_channel_layout_default(&decoder->ch_layout, channels);
    require(avcodec_open2(decoder, codec, nullptr) >= 0,
            "cannot open DSD decoder");
    require(swr_alloc_set_opts2(&swr, &decoder->ch_layout, AV_SAMPLE_FMT_DBL,
                                pcm, &decoder->ch_layout, AV_SAMPLE_FMT_FLTP,
                                352800, 0, nullptr) >= 0 &&
                swr_init(swr) >= 0,
            "cannot start FFmpeg swr");
  }
  ~FFmpeg() override {
    swr_free(&swr);
    av_frame_free(&frame);
    avcodec_free_context(&decoder);
  }
  int process(const AVPacket &p, Output &out) override {
    require(avcodec_send_packet(decoder, &p) >= 0 &&
                avcodec_receive_frame(decoder, frame) >= 0,
            "DSD packet decode failed");
    require(frame->format == AV_SAMPLE_FMT_FLTP &&
                frame->nb_samples == p.size / channels &&
                frame->sample_rate == 352800,
            "unexpected FFmpeg intermediate format");
    const uint8_t *in[]{frame->extended_data[0], frame->extended_data[1]};
    auto *dst = reinterpret_cast<uint8_t *>(out.data());
    int n = swr_convert(swr, &dst, capacity, in, frame->nb_samples);
    require(n >= 0, "swr failed");
    av_frame_unref(frame);
    return n;
  }
};

class Mpd final : public Converter {
  MultiDsd2Pcm decoder;
  SRC_STATE *src = nullptr;
  std::array<float, channels * block> intermediate{}, converted{};

public:
  Mpd() {
    int error = 0;
    src = src_new(SRC_SINC_FASTEST, channels, &error);
    require(src && src_set_ratio(src, .5) == 0, "cannot start libsamplerate");
  }
  ~Mpd() override { src_delete(src); }
  int process(const AVPacket &p, Output &out) override {
    const int n = p.size / channels;
    decoder.Translate(channels, n, reinterpret_cast<const std::byte *>(p.data),
                      intermediate.data());
    SRC_DATA d{};
    d.data_in = intermediate.data();
    d.input_frames = n;
    d.data_out = converted.data();
    d.output_frames = n / 2;
    d.src_ratio = .5;
    require(src_process(src, &d) == 0 && d.input_frames_used == n,
            "libsamplerate failed to consume packet");
    std::copy_n(converted.data(), channels * d.output_frames_gen, out.data());
    return d.output_frames_gen;
  }
};

std::unique_ptr<Converter> create(const std::string &name) {
  if (name == "kalinka-30k")
    return std::make_unique<Kalinka>(DsdFilter::Cut30k);
  if (name == "kalinka-wide")
    return std::make_unique<Kalinka>(DsdFilter::Wide);
  if (name == "ffmpeg")
    return std::make_unique<FFmpeg>();
  if (name == "mpd")
    return std::make_unique<Mpd>();
  throw std::runtime_error("unknown converter");
}

double seconds(clockid_t clock) {
  timespec t{};
  require(clock_gettime(clock, &t) == 0, "clock_gettime failed");
  return t.tv_sec + t.tv_nsec * 1e-9;
}
} // namespace

int main(int argc, char **argv) try {
  require(argc == 2 || argc == 3,
          "usage: decoder-cpu time | render NAME < dsd64.dsf");
  auto packets = source();
  Output out{};
  std::string command = argv[1];
  if (command == "render" && argc == 3) {
    auto decoder = create(argv[2]);
    for (auto &packet : packets) {
      int n = decoder->process(*packet, out);
      std::cout.write(reinterpret_cast<char *>(out.data()),
                      n * channels * sizeof(double));
    }
    require(bool(std::cout), "failed to write PCM");
    return 0;
  }
  require(command == "time" && argc == 2, "expected time or render NAME");
  // Full blocks exclusively: deterministic input and exactly 1024 output
  // frames per measured call after priming each converter.
  if (packets.back()->size != channels * block)
    packets.pop_back();
  require(!packets.empty(), "fixture too short");
  const std::array<std::string, 4> names{"kalinka-30k", "ffmpeg", "mpd",
                                         "kalinka-wide"};
  // Balanced order, each path follows every other path once per four rounds.
  constexpr int order[4][4] = {
      {0, 1, 3, 2}, {1, 2, 0, 3}, {2, 3, 1, 0}, {3, 0, 2, 1}};
  constexpr int warm = 512, measured = 4096;
  std::cerr << "FFmpeg " << av_version_info() << ", libavcodec "
            << avcodec_version() << ", swr " << swresample_version() << ", "
            << src_get_version() << ", GCC " << __VERSION__ << "\n";
  std::cout << "round,position,converter,frames,cpu_seconds,wall_seconds,"
               "percent_one_core\n";
  for (int round = 0; round < 12; ++round) {
    for (int pos = 0; pos < 4; ++pos) {
      auto &name = names[order[round % 4][pos]];
      auto decoder = create(name);
      for (int i = 0; i < warm; ++i)
        decoder->process(*packets[i % packets.size()], out);
      size_t frames = 0;
      double wall = seconds(CLOCK_MONOTONIC),
             cpu = seconds(CLOCK_PROCESS_CPUTIME_ID);
      for (int i = 0; i < measured; ++i)
        frames += decoder->process(*packets[(warm + i) % packets.size()], out);
      cpu = seconds(CLOCK_PROCESS_CPUTIME_ID) - cpu;
      wall = seconds(CLOCK_MONOTONIC) - wall;
      require(frames == size_t(measured) * block / 2 && std::isfinite(out[0]),
              "unexpected output frame count or level");
      std::cout << round + 1 << ',' << pos + 1 << ',' << name << ',' << frames
                << ',' << std::fixed << std::setprecision(9) << cpu << ','
                << wall << ',' << 100 * cpu * pcm / frames << std::endl;
    }
  }
} catch (const std::exception &e) {
  std::cerr << e.what() << '\n';
  return 1;
}
