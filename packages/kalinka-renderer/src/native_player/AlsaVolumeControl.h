#ifndef ALSA_VOLUME_CONTROL_H
#define ALSA_VOLUME_CONTROL_H

#include <condition_variable>
#include <functional>
#include <list>
#include <memory>
#include <mutex>
#include <queue>
#include <string>
#include <thread>
#include <utility>
#include <vector>

#include <alsa/asoundlib.h>

/// Which backend currently provides volume. Surfaced to Python so it knows
/// whether external-change monitoring is meaningful (hardware only).
enum class VolumeBackend { None = 0, Hardware = 1, Software = 2 };

/// User-selected policy, from `output.volume_mode`.
enum class VolumeMode { Auto, Hardware, Software, Fixed };

VolumeMode parseVolumeMode(const std::string &mode);

/// dB at each percentage, 0..100, of a volume that scales amplitude by the
/// cube of the percentage, as software volume and PipeWire do. 0 is -200 dB.
std::vector<double> cubicVolumeDb();

/// Whether a control device is the PipeWire or PulseAudio plugin, whose
/// volume is cubic but carries no dB scale for ALSA to report.
bool isSoundServerMixer(snd_ctl_type_t type, const std::string &cardId);

/// Whether PCM @p pcm is the card's own device, plain or converted, which
/// no software volume stage such as softvol sits in front of.
bool reachesTheCardItself(const std::string &pcm);

/// Whether the simple mixer element @p element sets its playback volume
/// through one of the card controls named in @p controls.
bool playbackVolumeAmong(const std::string &element,
                         const std::vector<std::string> &controls);

/// Percent-based snapshot of the active volume backend.
///
/// `current`/`max` are a 0..100 scale. `backend` is a `VolumeBackend` value so
/// the Python side can decide whether to run the external-change listener.
struct VolumeState {
  bool supported = false;
  int current = 0;
  int max = 100;
  int backend = static_cast<int>(VolumeBackend::None);
  std::vector<double> dbValues;
  std::string controlMode;
};

/// Where external volume changes arrive, whichever mixer is behind them at the
/// time.
///
/// One per player and never replaced, unlike the mixer handle: configureVolume()
/// drops and remakes that on every mode change, and whoever listens has to
/// survive it. Subscribers hold a share of this, so there is nothing to outlive.
class VolumeEvents {
public:
  /// Subscribe to external changes. The callback runs on the mixer's monitor
  /// thread with the new 0..100 value. Returns an id for unsubscribe().
  int subscribe(std::function<void(int)> callback);
  void unsubscribe(int id);

  void publish(int percent);

private:
  std::mutex mutex_;
  std::list<std::pair<int, std::function<void(int)>>> subscribers_;
  int nextSubscriberId_ = 0;
};

/// Hardware ALSA mixer control for a single card, plus a background thread that
/// watches for *external* changes (alsamixer, amixer, a hardware knob, another
/// app) and reports them — the local analogue of MusicCast pushing volume
/// events back to the UI.
///
/// All mixer access is serialized; get/set are safe to call from any thread.
class AlsaVolumeControl {
public:
  /// @param deviceName the PCM name from `output.alsa.device` (e.g. "default"
  ///        or "hw:CARD=sndrpihifiberry,DEV=0"); the mixer card is derived from
  ///        it.
  /// @param controlName overrides the simple-mixer element to use; empty means
  ///        auto-pick (Master → PCM → … → first element with playback volume).
  /// @param onExternalChange where changes go, given at construction because
  ///        the monitor thread starts here and a listener attached afterwards
  ///        would miss whatever came first.
  AlsaVolumeControl(const std::string &deviceName,
                    const std::string &controlName,
                    std::function<void(int)> onExternalChange = {});
  ~AlsaVolumeControl();

  AlsaVolumeControl(const AlsaVolumeControl &) = delete;
  AlsaVolumeControl &operator=(const AlsaVolumeControl &) = delete;

  /// True when a usable playback-volume mixer element was found.
  bool available() const { return available_; }

  /// Current volume in 0..100, or -1 when unavailable.
  int getVolume();

  /// Set volume, clamped to 0..100. False when unavailable or the write fails.
  bool setVolume(int percent);

  /// dB at each integer percentage, from the card's mixer calibration or, for
  /// a sound server, its cubic scale. Empty when neither is known.
  std::vector<double> dbValues();

private:
  bool openMixer(const std::string &deviceName, const std::string &controlName);
  void closeMixer();
  int rawToPercent(long raw) const;
  long percentToRaw(int percent) const;
  int readPercentLocked();
  void monitorLoop(std::stop_token token);
  void notify(int percent);

  std::mutex mixerMutex_;
  snd_mixer_t *mixer_ = nullptr;
  snd_mixer_elem_t *elem_ = nullptr;
  long rawMin_ = 0;
  long rawMax_ = 0;
  bool soundServer_ = false;
  bool available_ = false;
  int lastNotified_ = -1;

  /// Set once, before the monitor thread starts; read only by that thread.
  const std::function<void(int)> onExternalChange_;

  int wakePipe_[2] = {-1, -1}; // self-pipe to break poll() on shutdown
  std::jthread monitorThread_;
};

/// Blocks until the hardware mixer changes — the volume analogue of
/// StateMonitor.
///
/// Subscribed to the player's VolumeEvents rather than to a mixer, so it keeps
/// working across a mode change and cannot be left holding a mixer that has
/// been remade. It shares ownership of what it listens to, and stops reporting
/// only when stop() says so.
class VolumeMonitor {
public:
  explicit VolumeMonitor(std::shared_ptr<VolumeEvents> events);
  ~VolumeMonitor();

  VolumeMonitor(const VolumeMonitor &) = delete;
  VolumeMonitor &operator=(const VolumeMonitor &) = delete;

  /// Waits for and returns the next external change as a VolumeState. Once
  /// stopped, returns a state with supported=false.
  VolumeState wait();
  bool hasData();
  bool isRunning() const;
  void stop();

private:
  const std::shared_ptr<VolumeEvents> events_;
  int subscriptionId_ = -1;
  std::mutex mutex_;
  std::condition_variable cv_;
  std::queue<int> queue_;
  bool stopped_ = false;
};

#endif // ALSA_VOLUME_CONTROL_H
