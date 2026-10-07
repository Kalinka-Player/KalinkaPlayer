#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <string>

#include "../Identity.h"
#include "../RendererServices.h"
#include "../session/SessionEventSink.h"
#include "../session/SessionTransport.h"

/**
 * @brief What the bytes on one Core link mean — and no sockets anywhere.
 *
 * Runs the Hello/Welcome handshake, gates and routes session commands,
 * answers config traffic, and decides when a connection is not worth
 * retrying (protocol version rejected, renderer_id replaced). It hears about
 * the link through onUp/onMessage/onDown and talks back through the Wire it
 * is bound to; what carries the bytes is not its business — WsTransport in
 * production, a test's vector otherwise.
 *
 * While this link carries the Core that owns the session, it holds that
 * session and is attached to it: commands are gated here and routed in
 * through SessionEventSink, state comes back out through SessionTransport.
 * This class parses and frames; the session decides.
 *
 * Every link, owner or not, is told what the renderer can play: in Hello, and
 * again whenever it changes.
 *
 * @note Lives on the io_context thread; no locking.
 */
class ProtocolSession : public SessionTransport,
                        public CapabilityListener,
                        public std::enable_shared_from_this<ProtocolSession> {
public:
  /// The way down to whatever carries the bytes. All calls made on the
  /// io_context thread.
  struct Wire {
    /// @return false when the link is down; the message is then dropped.
    std::function<bool(std::string)> send;
    /// Drop the link and never retry.
    std::function<void()> giveUp;
  };

  /**
   * @param name     The endpoint's display name, for logs only.
   * @param identity Outlives this session; re-announced on every onUp().
   * @param services The session and config planes, shared with every other
   *                 connection.
   */
  ProtocolSession(std::string name, const Identity &identity,
                  std::string friendlyName, RendererServices services);

  /// Must be called once, before any event arrives, on a session already
  /// owned by a shared_ptr; from then on it hears of capability changes.
  void bind(Wire wire);

  /// The link is up: announce this renderer with Hello, session claim
  /// included.
  void onUp();

  /// One message from the Core; unparseable ones are dropped with a log.
  void onMessage(const std::string &data);

  /// The link is lost: detach from the session (arming its release rules) and
  /// forget the handshake — the next onUp() starts clean.
  void onDown();

  /**
   * @brief The Goodbye to flush on a graceful shutdown.
   *
   * @return A serialized Goodbye(REASON_SHUTDOWN) envelope once welcomed;
   *         empty before that, when there is nobody to say goodbye to.
   */
  std::string shutdownFrame();

  /// SessionTransport: false unless this link is up and past Welcome.
  bool sendSessionMessage(kalinka::renderer::v1::Envelope &env) override;

  /// SessionTransport: the session ended; forget it.
  void onSessionClosed() override;

  /// CapabilityListener: a welcomed Core is told now, one mid-handshake as
  /// soon as its Welcome arrives.
  void onCapabilitiesChanged(
      const kalinka::renderer::v1::Capabilities &capabilities) override;

private:
  void handleWelcome(const kalinka::renderer::v1::Welcome &welcome);
  void handleSessionOpen(const kalinka::renderer::v1::SessionOpen &open);
  void handleSessionClose(const std::string &sessionId);
  void handleCommand(const kalinka::renderer::v1::Envelope &env);
  void handleGoodbye(const kalinka::renderer::v1::Goodbye &goodbye);
  void handleUpgrade(const kalinka::renderer::v1::Envelope &env);
  void adoptSession();
  void detachSession();
  void
  sendCapabilities(const kalinka::renderer::v1::Capabilities &capabilities);
  void sendEnvelope(kalinka::renderer::v1::Envelope &env);
  void sendReply(kalinka::renderer::v1::Envelope &out, uint64_t inReplyTo);

  const std::string name_;
  const Identity &identity_;
  const std::string friendlyName_;
  RendererServices services_;
  Wire wire_;

  uint64_t nextMessageId_ = 1;
  std::string serverId_;  // from Welcome; owner id for sessions this Core opens
  // The session this link carries, while its Core owns one. Held so commands
  // route without a lookup; the gate is sessionId().
  std::shared_ptr<SessionEventSink> session_;
  bool welcomed_ = false;
  // Changed after Hello stated them and before Welcome let anything follow it.
  bool capabilitiesMoved_ = false;
  // Whether the Core's protocol version is one this binary speaks. A Core that
  // has moved past us keeps the connection — that is how it can tell us to
  // upgrade — but nothing it asks us to play is acted on.
  bool coreSpeaksOurProtocol_ = true;
};
