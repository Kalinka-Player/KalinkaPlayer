#include "AlsaVolumeControl.h"
#include "Log.h"

#ifdef __PYTHON__
#include <pybind11/pybind11.h>
#endif

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <vector>

#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <unistd.h>

VolumeMode parseVolumeMode(const std::string &mode) {
  if (mode == "hardware") {
    return VolumeMode::Hardware;
  }
  if (mode == "software") {
    return VolumeMode::Software;
  }
  if (mode == "fixed") {
    return VolumeMode::Fixed;
  }
  return VolumeMode::Auto;
}

std::vector<double> cubicVolumeDb() {
  std::vector<double> values{-200.0};
  for (int percent = 1; percent <= 100; ++percent) {
    values.push_back(60.0 * std::log10(percent / 100.0));
  }
  return values;
}

bool isSoundServerMixer(snd_ctl_type_t type, const std::string &cardId) {
  return type == SND_CTL_TYPE_EXT && (cardId == "pipewire" || cardId == "pulse");
}

bool reachesTheCardItself(const std::string &pcm) {
  return pcm.starts_with("hw:") || pcm.starts_with("plughw:");
}

bool playbackVolumeAmong(const std::string &element,
                         const std::vector<std::string> &controls) {
  return std::ranges::any_of(controls, [&element](const std::string &control) {
    return control == element + " Playback Volume" ||
           control == element + " Volume";
  });
}

namespace {

// Derive the ALSA *mixer* card name from a PCM device name. The mixer attaches
// to a control device (e.g. "hw:CARD=foo" or "default"), not to the PCM string.
std::string deriveMixerCard(const std::string &pcm) {
  auto cardPos = pcm.find("CARD=");
  if (cardPos != std::string::npos) {
    std::string rest = pcm.substr(cardPos + 5);
    auto comma = rest.find(',');
    if (comma != std::string::npos) {
      rest = rest.substr(0, comma);
    }
    return "hw:CARD=" + rest;
  }
  // Index forms like "hw:0,0" / "plughw:0,0" → "hw:0".
  for (const std::string &prefix : {std::string("plughw:"), std::string("hw:")}) {
    if (pcm.rfind(prefix, 0) == 0) {
      std::string rest = pcm.substr(prefix.size());
      auto comma = rest.find(',');
      if (comma != std::string::npos) {
        rest = rest.substr(0, comma);
      }
      return "hw:" + rest;
    }
  }
  // "default", "pulse", "pipewire", … — hand to the mixer as-is.
  return pcm.empty() ? "default" : pcm;
}

bool soundServerCard(snd_mixer_t *mixer, const std::string &card) {
  snd_hctl_t *hctl = nullptr;
  if (snd_mixer_get_hctl(mixer, card.c_str(), &hctl) < 0) {
    return false;
  }
  snd_ctl_t *ctl = snd_hctl_ctl(hctl);
  snd_ctl_card_info_t *info = nullptr;
  snd_ctl_card_info_alloca(&info);
  return snd_ctl_card_info(ctl, info) >= 0 &&
         isSoundServerMixer(snd_ctl_type(ctl), snd_ctl_card_info_get_id(info));
}

// Controls software added to the card, softvol's among them.
std::vector<std::string> userControls(snd_mixer_t *mixer,
                                      const std::string &card) {
  std::vector<std::string> names;
  snd_hctl_t *hctl = nullptr;
  if (snd_mixer_get_hctl(mixer, card.c_str(), &hctl) < 0) {
    return names;
  }
  snd_ctl_elem_info_t *info = nullptr;
  snd_ctl_elem_info_alloca(&info);
  for (snd_hctl_elem_t *e = snd_hctl_first_elem(hctl); e != nullptr;
       e = snd_hctl_elem_next(e)) {
    if (snd_hctl_elem_info(e, info) >= 0 && snd_ctl_elem_info_is_user(info)) {
      names.emplace_back(snd_hctl_elem_get_name(e));
    }
  }
  return names;
}

// Preference order when no explicit control name is configured.
const char *const kPreferredControls[] = {"Master",    "PCM",     "Speaker",
                                           "Headphone", "Digital", "Playback",
                                           "Line Out"};

} // namespace

AlsaVolumeControl::AlsaVolumeControl(const std::string &deviceName,
                                     const std::string &controlName,
                                     std::function<void(int)> onExternalChange)
    : onExternalChange_(std::move(onExternalChange)) {
  available_ = openMixer(deviceName, controlName);
  if (!available_) {
    closeMixer();
    return;
  }

  {
    std::lock_guard<std::mutex> lock(mixerMutex_);
    lastNotified_ = readPercentLocked();
  }

  if (pipe(wakePipe_) != 0) {
    wakePipe_[0] = wakePipe_[1] = -1;
    spdlog::warn("ALSA volume: pipe() failed; external-change monitor disabled");
    return;
  }
  fcntl(wakePipe_[0], F_SETFL, O_NONBLOCK);
  fcntl(wakePipe_[0], F_SETFD, FD_CLOEXEC);
  fcntl(wakePipe_[1], F_SETFD, FD_CLOEXEC);

  monitorThread_ =
      std::jthread(std::bind_front(&AlsaVolumeControl::monitorLoop, this));
}

AlsaVolumeControl::~AlsaVolumeControl() {
  if (monitorThread_.joinable()) {
    monitorThread_.request_stop();
    if (wakePipe_[1] >= 0) {
      const char c = 1;
      ssize_t ignored = write(wakePipe_[1], &c, 1);
      (void)ignored;
    }
    monitorThread_.join();
  }
  if (wakePipe_[0] >= 0) {
    close(wakePipe_[0]);
  }
  if (wakePipe_[1] >= 0) {
    close(wakePipe_[1]);
  }
  closeMixer();
}

bool AlsaVolumeControl::openMixer(const std::string &deviceName,
                                  const std::string &controlName) {
  const std::string card = deriveMixerCard(deviceName);

  int err = snd_mixer_open(&mixer_, 0);
  if (err < 0) {
    spdlog::warn("ALSA volume: snd_mixer_open failed: {}", snd_strerror(err));
    mixer_ = nullptr;
    return false;
  }
  if ((err = snd_mixer_attach(mixer_, card.c_str())) < 0) {
    spdlog::info("ALSA volume: cannot attach mixer for card '{}': {}", card,
                 snd_strerror(err));
    return false;
  }
  if ((err = snd_mixer_selem_register(mixer_, nullptr, nullptr)) < 0) {
    spdlog::warn("ALSA volume: snd_mixer_selem_register failed: {}",
                 snd_strerror(err));
    return false;
  }
  if ((err = snd_mixer_load(mixer_)) < 0) {
    spdlog::warn("ALSA volume: snd_mixer_load failed: {}", snd_strerror(err));
    return false;
  }

  // softvol adds its control to the card, but hw: and plughw: bypass softvol.
  const std::vector<std::string> unheard =
      reachesTheCardItself(deviceName) ? userControls(mixer_, card)
                                       : std::vector<std::string>();
  auto usable = [&unheard](snd_mixer_elem_t *e) {
    return snd_mixer_selem_has_playback_volume(e) &&
           !playbackVolumeAmong(snd_mixer_selem_get_name(e), unheard);
  };

  snd_mixer_selem_id_t *sid = nullptr;
  snd_mixer_selem_id_alloca(&sid);

  auto findByName = [&](const char *name) -> snd_mixer_elem_t * {
    snd_mixer_selem_id_set_index(sid, 0);
    snd_mixer_selem_id_set_name(sid, name);
    snd_mixer_elem_t *e = snd_mixer_find_selem(mixer_, sid);
    return e && usable(e) ? e : nullptr;
  };

  if (!controlName.empty()) {
    elem_ = findByName(controlName.c_str());
    if (!elem_) {
      spdlog::info("ALSA volume: configured control '{}' not found on '{}'",
                   controlName, card);
    }
  }
  if (!elem_) {
    for (const char *name : kPreferredControls) {
      elem_ = findByName(name);
      if (elem_) {
        break;
      }
    }
  }
  if (!elem_) {
    for (snd_mixer_elem_t *e = snd_mixer_first_elem(mixer_); e != nullptr;
         e = snd_mixer_elem_next(e)) {
      if (snd_mixer_selem_is_active(e) && usable(e)) {
        elem_ = e;
        break;
      }
    }
  }
  if (!elem_) {
    spdlog::info("ALSA volume: no playback-volume mixer element on '{}'", card);
    return false;
  }

  snd_mixer_selem_get_playback_volume_range(elem_, &rawMin_, &rawMax_);
  if (rawMax_ <= rawMin_) {
    spdlog::info("ALSA volume: degenerate volume range on '{}'", card);
    return false;
  }

  soundServer_ = soundServerCard(mixer_, card);
  spdlog::info("ALSA volume: using mixer control '{}' on '{}' (raw {}..{}{})",
               snd_mixer_selem_get_name(elem_), card, rawMin_, rawMax_,
               soundServer_ ? ", sound server" : "");
  return true;
}

void AlsaVolumeControl::closeMixer() {
  if (mixer_ != nullptr) {
    snd_mixer_close(mixer_);
    mixer_ = nullptr;
  }
  elem_ = nullptr;
}

int AlsaVolumeControl::rawToPercent(long raw) const {
  if (rawMax_ <= rawMin_) {
    return 0;
  }
  const long span = rawMax_ - rawMin_;
  long p = std::lround((raw - rawMin_) * 100.0 / static_cast<double>(span));
  return static_cast<int>(std::clamp<long>(p, 0, 100));
}

long AlsaVolumeControl::percentToRaw(int percent) const {
  percent = std::clamp(percent, 0, 100);
  return rawMin_ +
         std::lround((rawMax_ - rawMin_) * (percent / 100.0));
}

// Requires mixerMutex_ held and a valid element. Returns 0..100 or -1.
int AlsaVolumeControl::readPercentLocked() {
  if (!elem_) {
    return -1;
  }
  long raw = rawMin_;
  int err = snd_mixer_selem_get_playback_volume(
      elem_, SND_MIXER_SCHN_FRONT_LEFT, &raw);
  if (err < 0) {
    err = snd_mixer_selem_get_playback_volume(elem_, SND_MIXER_SCHN_MONO, &raw);
  }
  if (err < 0) {
    return -1;
  }
  return rawToPercent(raw);
}

int AlsaVolumeControl::getVolume() {
  std::lock_guard<std::mutex> lock(mixerMutex_);
  if (!available_ || !elem_) {
    return -1;
  }
  snd_mixer_handle_events(mixer_);
  return readPercentLocked();
}

bool AlsaVolumeControl::setVolume(int percent) {
  std::lock_guard<std::mutex> lock(mixerMutex_);
  if (!available_ || !elem_) {
    return false;
  }
  const long raw = percentToRaw(percent);
  int err = snd_mixer_selem_set_playback_volume_all(elem_, raw);
  if (err < 0) {
    spdlog::warn("ALSA volume: set_playback_volume_all failed: {}",
                 snd_strerror(err));
    return false;
  }
  // Record our own change so the monitor thread doesn't echo it back as if it
  // were external.
  lastNotified_ = std::clamp(percent, 0, 100);
  return true;
}

std::vector<double> AlsaVolumeControl::dbValues() {
  std::lock_guard<std::mutex> lock(mixerMutex_);
  if (!available_ || !elem_) {
    return {};
  }
  std::vector<double> values;
  for (int percent = 0; percent <= 100; ++percent) {
    long db = 0;
    if (snd_mixer_selem_ask_playback_vol_dB(elem_, percentToRaw(percent), &db) < 0) {
      return soundServer_ ? cubicVolumeDb() : std::vector<double>{};
    }
    values.push_back(std::max(-200.0, db / 100.0));
  }
  return values;
}

void AlsaVolumeControl::notify(int percent) {
  if (onExternalChange_) {
    onExternalChange_(percent);
  }
}

int VolumeEvents::subscribe(std::function<void(int)> callback) {
  std::lock_guard<std::mutex> lock(mutex_);
  const int id = nextSubscriberId_++;
  subscribers_.emplace_back(id, std::move(callback));
  return id;
}

void VolumeEvents::unsubscribe(int id) {
  std::lock_guard<std::mutex> lock(mutex_);
  subscribers_.remove_if([id](const auto &p) { return p.first == id; });
}

void VolumeEvents::publish(int percent) {
  std::lock_guard<std::mutex> lock(mutex_);
  for (auto &[id, callback] : subscribers_) {
    (void)id;
    callback(percent);
  }
}

void AlsaVolumeControl::monitorLoop(std::stop_token token) {
  pthread_setname_np(pthread_self(), "AlsaVolume");

  while (!token.stop_requested()) {
    std::vector<pollfd> fds;
    int nfds = 0;
    {
      std::lock_guard<std::mutex> lock(mixerMutex_);
      if (!mixer_) {
        break;
      }
      nfds = snd_mixer_poll_descriptors_count(mixer_);
      if (nfds < 0) {
        break;
      }
      fds.resize(static_cast<size_t>(nfds) + 1);
      snd_mixer_poll_descriptors(mixer_, fds.data(), nfds);
    }
    fds[nfds].fd = wakePipe_[0];
    fds[nfds].events = POLLIN;
    fds[nfds].revents = 0;

    int ret = poll(fds.data(), fds.size(), -1);
    if (ret < 0) {
      if (errno == EINTR) {
        continue;
      }
      break;
    }
    if (token.stop_requested()) {
      break;
    }
    if (fds[nfds].revents & POLLIN) {
      char buf[64];
      while (read(wakePipe_[0], buf, sizeof(buf)) > 0) {
        // drain
      }
      if (token.stop_requested()) {
        break;
      }
    }

    int changed = -1;
    {
      std::lock_guard<std::mutex> lock(mixerMutex_);
      if (!mixer_) {
        break;
      }
      snd_mixer_handle_events(mixer_);
      const int percent = readPercentLocked();
      if (percent >= 0 && percent != lastNotified_) {
        lastNotified_ = percent;
        changed = percent;
      }
    }
    if (changed >= 0) {
      notify(changed);
    }
  }
}

VolumeMonitor::VolumeMonitor(std::shared_ptr<VolumeEvents> events)
    : events_(std::move(events)) {
  subscriptionId_ = events_->subscribe([this](int percent) {
    std::lock_guard<std::mutex> lock(mutex_);
    queue_.push(percent);
    cv_.notify_all();
  });
}

VolumeMonitor::~VolumeMonitor() { stop(); }

VolumeState VolumeMonitor::wait() {
#ifdef __PYTHON__
  pybind11::gil_scoped_release release;
#endif
  std::unique_lock<std::mutex> lock(mutex_);
  cv_.wait(lock, [this] { return !queue_.empty() || stopped_; });

  VolumeState state;
  if (!queue_.empty()) {
    state.current = queue_.front();
    queue_.pop();
    state.supported = true;
    state.max = 100;
    state.backend = static_cast<int>(VolumeBackend::Hardware);
    return state;
  }

  state.supported = false;
  state.backend = static_cast<int>(VolumeBackend::None);
  return state;
}

bool VolumeMonitor::hasData() {
  std::lock_guard<std::mutex> lock(mutex_);
  return !queue_.empty();
}

bool VolumeMonitor::isRunning() const { return !stopped_; }

void VolumeMonitor::stop() {
  // Never while holding this monitor's own lock: the callback takes it, from
  // whichever thread is publishing.
  if (subscriptionId_ >= 0) {
    events_->unsubscribe(subscriptionId_);
    subscriptionId_ = -1;
  }
  {
    std::lock_guard<std::mutex> lock(mutex_);
    stopped_ = true;
  }
  cv_.notify_all();
}
