// A test device that plays in real time, which ALSA loads as `kalinka_paced`.
#include <alsa/asoundlib.h>
#include <alsa/pcm_external.h>

#include <sys/timerfd.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fstream>
#include <memory>
#include <string>

namespace {
using Clock = std::chrono::steady_clock;

/**
 * @brief The device: frames play at the rate from start() on. A frame due
 * before it was handed over is an underrun, after which what is handed over
 * is dropped until the next prepare, as a card's is. What plays is appended
 * to the capture file, when one is named.
 */
struct Paced {
  snd_pcm_ioplug_t io{};
  int timer = -1;
  std::ofstream capture;
  Clock::time_point startedAt;
  bool running = false;
  bool underrun = false;
  snd_pcm_uframes_t handedOver = 0;

  snd_pcm_uframes_t due() const {
    if (!running)
      return 0;
    const std::chrono::duration<double> elapsed = Clock::now() - startedAt;
    return snd_pcm_uframes_t(elapsed.count() * io.rate);
  }

  snd_pcm_uframes_t played() const { return std::min(due(), handedOver); }

  bool starved() {
    if (running && io.state != SND_PCM_STATE_DRAINING && due() > handedOver)
      underrun = true;
    return underrun;
  }
};

Paced &paced(snd_pcm_ioplug_t *io) {
  return *static_cast<Paced *>(io->private_data);
}

int start(snd_pcm_ioplug_t *io) {
  paced(io).startedAt = Clock::now();
  paced(io).running = true;
  return 0;
}

int stop(snd_pcm_ioplug_t *io) {
  paced(io).running = false;
  return 0;
}

int prepare(snd_pcm_ioplug_t *io) {
  Paced &device = paced(io);
  device.running = false;
  device.underrun = false;
  device.handedOver = 0;
  return 0;
}

snd_pcm_sframes_t pointer(snd_pcm_ioplug_t *io) {
  Paced &device = paced(io);
  if (device.starved())
    return -EPIPE;
  return snd_pcm_sframes_t(device.played());
}

snd_pcm_sframes_t transfer(snd_pcm_ioplug_t *io,
                           const snd_pcm_channel_area_t *areas,
                           snd_pcm_uframes_t offset, snd_pcm_uframes_t size) {
  Paced &device = paced(io);
  if (!device.starved() && device.capture.is_open()) {
    const auto *frames = static_cast<const char *>(areas[0].addr) +
                         (areas[0].first + offset * areas[0].step) / 8;
    device.capture.write(frames, std::streamsize(size * areas[0].step / 8));
    device.capture.flush();
  }
  device.handedOver += size;
  return snd_pcm_sframes_t(size);
}

int pollRevents(snd_pcm_ioplug_t *io, struct pollfd *, unsigned int,
                unsigned short *revents) {
  Paced &device = paced(io);
  uint64_t ticks = 0;
  while (read(device.timer, &ticks, sizeof ticks) > 0) {
  }
  if (device.starved()) {
    *revents = POLLOUT | POLLERR;
    return 0;
  }
  const snd_pcm_uframes_t queued = device.handedOver - device.played();
  *revents = io->buffer_size - queued >= io->period_size ? POLLOUT : 0;
  return 0;
}

int closeDevice(snd_pcm_ioplug_t *io) {
  std::unique_ptr<Paced> device(&paced(io));
  ::close(device->timer);
  return 0;
}

const snd_pcm_ioplug_callback_t kCallbacks = {
    .start = start,
    .stop = stop,
    .pointer = pointer,
    .transfer = transfer,
    .close = closeDevice,
    .prepare = prepare,
    .poll_revents = pollRevents,
};

int constrain(snd_pcm_ioplug_t *io) {
  static const unsigned int accesses[] = {SND_PCM_ACCESS_MMAP_INTERLEAVED,
                                          SND_PCM_ACCESS_RW_INTERLEAVED};
  static const unsigned int formats[] = {SND_PCM_FORMAT_S16_LE,
                                         SND_PCM_FORMAT_S32_LE};
  int err = snd_pcm_ioplug_set_param_list(io, SND_PCM_IOPLUG_HW_ACCESS, 2,
                                          accesses);
  if (err >= 0)
    err = snd_pcm_ioplug_set_param_list(io, SND_PCM_IOPLUG_HW_FORMAT, 2,
                                        formats);
  if (err >= 0)
    err = snd_pcm_ioplug_set_param_minmax(io, SND_PCM_IOPLUG_HW_CHANNELS, 1, 2);
  if (err >= 0)
    err = snd_pcm_ioplug_set_param_minmax(io, SND_PCM_IOPLUG_HW_RATE, 8000,
                                          384000);
  if (err >= 0)
    err = snd_pcm_ioplug_set_param_minmax(io, SND_PCM_IOPLUG_HW_PERIOD_BYTES,
                                          64, 1 << 20);
  if (err >= 0)
    err = snd_pcm_ioplug_set_param_minmax(io, SND_PCM_IOPLUG_HW_PERIODS, 2, 64);
  if (err >= 0)
    err = snd_pcm_ioplug_set_param_minmax(io, SND_PCM_IOPLUG_HW_BUFFER_BYTES,
                                          128, 1 << 22);
  return err;
}
} // namespace

extern "C" {
SND_PCM_PLUGIN_DEFINE_FUNC(kalinka_paced) {
  (void)root;
  std::string file;
  snd_config_iterator_t i, next;
  snd_config_for_each(i, next, conf) {
    snd_config_t *entry = snd_config_iterator_entry(i);
    const char *id = nullptr;
    if (snd_config_get_id(entry, &id) < 0)
      continue;
    if (!strcmp(id, "comment") || !strcmp(id, "type") || !strcmp(id, "hint"))
      continue;
    const char *value = nullptr;
    if (!strcmp(id, "file") && snd_config_get_string(entry, &value) >= 0) {
      file = value;
      continue;
    }
    SNDERR("Unknown field %s", id);
    return -EINVAL;
  }
  if (stream != SND_PCM_STREAM_PLAYBACK)
    return -EINVAL;

  auto device = std::make_unique<Paced>();
  if (!file.empty()) {
    device->capture.open(file, std::ios::binary | std::ios::trunc);
    if (!device->capture)
      return -EIO;
  }
  device->timer = timerfd_create(CLOCK_MONOTONIC, TFD_NONBLOCK | TFD_CLOEXEC);
  if (device->timer < 0)
    return -errno;
  // Ticks keep a poll from sleeping past the moment space frees up.
  const itimerspec tick{{0, 1000000}, {0, 1000000}};
  timerfd_settime(device->timer, 0, &tick, nullptr);

  snd_pcm_ioplug_t &io = device->io;
  io.version = SND_PCM_IOPLUG_VERSION;
  io.name = "Kalinka paced test device";
  io.callback = &kCallbacks;
  io.private_data = device.get();
  io.poll_fd = device->timer;
  io.poll_events = POLLIN;
  io.mmap_rw = 0;
  io.flags = SND_PCM_IOPLUG_FLAG_BOUNDARY_WA;
  if (const int err = snd_pcm_ioplug_create(&io, name, stream, mode); err < 0) {
    ::close(device->timer);
    return err;
  }
  // The close callback owns it from here.
  Paced *const created = device.release();
  if (const int err = constrain(&created->io); err < 0) {
    snd_pcm_ioplug_delete(&created->io);
    return err;
  }
  *pcmp = created->io.pcm;
  return 0;
}

SND_PCM_PLUGIN_SYMBOL(kalinka_paced);
}
