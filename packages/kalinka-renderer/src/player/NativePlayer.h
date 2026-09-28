#pragma once

#include <boost/asio.hpp>

#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <thread>
#include <vector>

#include "../native_player/AudioPlayer.h"
#include "../native_player/StateMonitor.h"
#include "Player.h"

/**
 * @brief The real Player: the native audio graph behind the protocol seam.
 *
 * Wraps AudioPlayer — the same graph the server's play queue drives — and
 * adds the two things it deliberately does not have:
 *
 * - **Token vocabulary.** The graph names streams by StreamId, the protocol
 *   by the Core's opaque source_token. Which stream a state is about comes
 *   stamped on the state itself, never inferred from ordering.
 * - **A thread boundary.** Graph states arrive on blocking monitors; two pump
 *   threads post them onto the io_context, so everything protocol-facing
 *   stays single-threaded.
 *
 * Configuration: output driver, device (enumerated from ALSA), volume mode,
 * and the numbers the graph is built with — how far ahead of the card it
 * buffers, how much of a stream it holds in memory, how long it waits on a
 * stream that has stopped arriving. Changing any of the latter rebuilds the
 * graph — APPLY_COST_INTERRUPTS_PLAYBACK — and anything that was playing is
 * gone, as declared. Overrides that differ from the defaults are persisted to
 * the state directory (see SettingsPersistence) and loaded on construction; an
 * applied change updates memory and the file in one step.
 *
 * The config plane's paths are the renderer's own; the keys the graph is built
 * with are the backend's, and stay behind them.
 *
 * If the graph cannot be built (no ALSA), the renderer stays up: commands
 * that need audio answer with PLAYBACK_STATE_ERROR, exactly like a track
 * that failed, and the next device change retries.
 *
 * @note All Player methods on the io_context thread, per the contract.
 */
class NativePlayer : public Player,
                     public std::enable_shared_from_this<NativePlayer> {
public:
  explicit NativePlayer(boost::asio::io_context &ioc);
  ~NativePlayer() override;

  void setStateSink(StateSink sink) override;
  void setSource(const kalinka::renderer::v1::Source &source) override;
  void enqueueSource(const kalinka::renderer::v1::Source &source) override;
  void removeSource(const std::string &sourceToken) override;
  void clearQueue() override;
  void pause() override;
  void resume() override;
  void stop() override;
  void setVolume(uint32_t percent) override;
  bool beginSessionVolume(const SessionVolumePolicy &policy,
                          std::string &error) override;
  void endSessionVolume() override;
  void seek(uint64_t positionMs) override;
  void fillConfig(kalinka::renderer::v1::ConfigSection &out) const override;
  bool applyConfig(const std::string &path, const std::string &value,
                   std::string &error) override;
  void fillSnapshot(kalinka::renderer::v1::StateSnapshot &out) const override;

  /**
   * @brief The buffering section, declared apart from the sink it feeds.
   *
   * Register it beside the player. The values are the player's, so what comes
   * back holds a share of the player and can be registered for as long as it
   * likes. Requires the player to be owned by a shared_ptr already.
   */
  std::shared_ptr<ConfigContributor> bufferSettings();

  /// The network section, which is held and registered as bufferSettings() is.
  std::shared_ptr<ConfigContributor> networkSettings();

private:
  /// Contributes one section of knobs; writes land back on the player.
  class KnobSection;

  struct TrackedSource {
    kalinka::renderer::v1::Source source;
    StreamId streamId;
  };

  bool ensurePlayer();
  void rebuildPlayer();
  void startPumps();
  void stopPumps();
  StreamId appendSource(const kalinka::renderer::v1::Source &source);
  void onStreamState(const StreamState &state);
  void emit(kalinka::renderer::v1::Envelope &env);
  void emitVolume(const VolumeState &volume, bool external);
  void reportUnavailable(const char *command);

  /// The Source a stamped state is about, or nullptr when it names none.
  const kalinka::renderer::v1::Source *
  sourceFor(const std::optional<StreamId> &streamId) const;
  std::optional<std::string>
  tokenFor(const std::optional<StreamId> &streamId) const;
  std::optional<std::string> currentToken() const;
  /// Ids rise with append order, so becoming current retires earlier streams.
  void forgetSourcesBefore(StreamId streamId);

  static const std::map<std::string, std::string> &defaultSettings();
  void persistOverrides() const;
  /// Whatever section declared the path, the values live here.
  bool applySetting(const std::string &path, const std::string &value,
                    std::string &error);
  /// The settings the graph is built with, under the keys it reads them by.
  Config graphConfig() const;
  /// What the graph should run: fixed for a downstream session, else configured.
  const std::string &effectiveVolumeMode() const;

  boost::asio::io_context &ioc_;
  StateSink sink_;
  std::map<std::string, std::string> settings_ = defaultSettings();
  // Never merged into settings_: that is the configured value, which the
  // config plane reports and persistOverrides() writes.
  bool sessionForcedFixed_ = false;
  std::optional<std::string> sessionRestoreMode_;
  std::optional<int> sessionRestoreVolume_;

  std::unique_ptr<AudioPlayer> player_;
  // shared_ptr: each pump thread keeps its monitor alive; stop() is what
  // unblocks the thread, from the io_context side.
  std::shared_ptr<StateMonitor> stateMonitor_;
  std::shared_ptr<VolumeMonitor> volumeMonitor_;
  std::thread statePump_;
  std::thread volumePump_;

  StreamId nextStreamId_ = 1;
  // Every stream the graph still knows about, in append (= switch) order.
  std::vector<TrackedSource> sources_;
  std::optional<StreamId> currentId_;
};
