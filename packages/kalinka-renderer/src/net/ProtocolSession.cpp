#include "ProtocolSession.h"

#include <spdlog/spdlog.h>
#include <sys/utsname.h>

#include <chrono>

#include "../Protocol.h"
#include "kalinka/renderer/v1/renderer.pb.h"

namespace pb = kalinka::renderer::v1;

namespace {
int64_t nowUnixMs() {
  return std::chrono::duration_cast<std::chrono::milliseconds>(
             std::chrono::system_clock::now().time_since_epoch())
      .count();
}

pb::ControlKind kindOf(const pb::Command &command) {
  switch (command.op_case()) {
  case pb::Command::kSetSource:
    return pb::CONTROL_KIND_SET_SOURCE;
  case pb::Command::kEnqueueSource:
    return pb::CONTROL_KIND_ENQUEUE_SOURCE;
  case pb::Command::kRemoveSource:
    return pb::CONTROL_KIND_REMOVE_SOURCE;
  case pb::Command::kClearQueue:
    return pb::CONTROL_KIND_CLEAR_QUEUE;
  case pb::Command::kPause:
    return pb::CONTROL_KIND_PAUSE;
  case pb::Command::kResume:
    return pb::CONTROL_KIND_RESUME;
  case pb::Command::kStop:
    return pb::CONTROL_KIND_STOP;
  case pb::Command::kSetVolume:
    return pb::CONTROL_KIND_SET_VOLUME;
  case pb::Command::kRequestSnapshot:
    return pb::CONTROL_KIND_REQUEST_SNAPSHOT;
  case pb::Command::kSeek:
    return pb::CONTROL_KIND_SEEK;
  case pb::Command::OP_NOT_SET:
    return pb::CONTROL_KIND_UNSPECIFIED;
  }
  return pb::CONTROL_KIND_UNSPECIFIED;
}

// The rescue messages every version carries, plus SessionOpen — refused, but
// refused out loud, since dropping it would leave the Core waiting for a
// result that never comes. Anything else means whatever the two versions
// agreed it meant, which is exactly what no longer holds.
bool handledFromACoreWeCannotFollow(pb::Envelope::PayloadCase payload) {
  switch (payload) {
  case pb::Envelope::kWelcome:
  case pb::Envelope::kGoodbye:
  case pb::Envelope::kUpgrade:
  case pb::Envelope::kSessionOpen:
    return true;
  default:
    return false;
  }
}
}  // namespace

ProtocolSession::ProtocolSession(std::string name, const Identity &identity,
                                 std::string friendlyName,
                                 RendererServices services)
    : name_(std::move(name)), identity_(identity),
      friendlyName_(std::move(friendlyName)), services_(std::move(services)) {}

void ProtocolSession::bind(Wire wire) {
  wire_ = std::move(wire);
  if (services_.capabilities) {
    services_.capabilities->addListener(weak_from_this());
  }
}

void ProtocolSession::onUp() {
  pb::Envelope env;
  pb::Hello *hello = env.mutable_hello();
  hello->mutable_protocol_versions()->set_min(kMinRendererProtocolVersion);
  hello->mutable_protocol_versions()->set_max(kMaxRendererProtocolVersion);
  hello->set_upgrade_supported(services_.upgrade &&
                               services_.upgrade->supported());
  hello->set_renderer_id(identity_.rendererId);
  hello->set_instance_id(identity_.instanceId);
  hello->set_friendly_name(friendlyName_);
  hello->set_software_version(KALINKA_RENDERER_VERSION);
  hello->set_kind(pb::RENDERER_KIND_NATIVE);
  capabilitiesMoved_ = false;
  if (services_.capabilities) {
    *hello->mutable_capabilities() = services_.capabilities->current();
  }
  if (const auto session = services_.sessions->current()) {
    // Reported to every Core; only its owner acts on it.
    hello->set_active_session_id(session->sessionId());
    hello->set_session_owner_server_id(session->ownerServerId());
  }

  utsname u{};
  hello->mutable_platform()->set_machine_id(identity_.machineId);
  if (uname(&u) == 0) {
    pb::Platform *platform = hello->mutable_platform();
    platform->set_os("linux");
    platform->set_os_version(u.release);
    platform->set_arch(u.machine);
    platform->set_hostname(u.nodename);
    platform->set_audio_backend("alsa");
  }
  sendEnvelope(env);
}

void ProtocolSession::onMessage(const std::string &data) {
  pb::Envelope env;
  if (!env.ParseFromString(data)) {
    spdlog::warn("[{}] Dropping unparseable {}-byte message", name_,
                 data.size());
    return;
  }
  if (welcomed_ && !coreSpeaksOurProtocol_ &&
      !handledFromACoreWeCannotFollow(env.payload_case())) {
    spdlog::debug("[{}] Ignoring payload case {} from a Core whose protocol "
                  "this renderer does not speak",
                  name_, static_cast<int>(env.payload_case()));
    return;
  }
  switch (env.payload_case()) {
  case pb::Envelope::kWelcome:
    handleWelcome(env.welcome());
    break;
  case pb::Envelope::kSessionOpen:
    handleSessionOpen(env.session_open());
    break;
  case pb::Envelope::kSessionClose:
    handleSessionClose(env.session_close().session_id());
    break;
  case pb::Envelope::kCommand:
    handleCommand(env);
    break;
  case pb::Envelope::kConfigRequest: {
    pb::Envelope out;
    services_.config->fillSnapshot(*out.mutable_config_snapshot());
    sendReply(out, env.message_id());
    break;
  }
  case pb::Envelope::kConfigUpdate: {
    pb::Envelope out;
    services_.config->apply(env.config_update(), *out.mutable_config_result());
    sendReply(out, env.message_id());
    break;
  }
  case pb::Envelope::kUpgrade:
    handleUpgrade(env);
    break;
  case pb::Envelope::kGoodbye:
    handleGoodbye(env.goodbye());
    break;
  default:
    spdlog::debug("[{}] Ignoring message with payload case {}", name_,
                  static_cast<int>(env.payload_case()));
    break;
  }
}

void ProtocolSession::handleWelcome(const pb::Welcome &welcome) {
  if (welcomed_) {
    spdlog::warn("[{}] Ignoring a second Welcome on one connection", name_);
    return;
  }
  welcomed_ = true;
  serverId_ = welcome.server_id();
  coreSpeaksOurProtocol_ =
      rendererProtocolSupported(static_cast<int>(welcome.protocol_version()));
  spdlog::info("[{}] Registered: server '{}' version {} (api {}, protocol v{})",
               name_, welcome.server_name(), welcome.server_version(),
               welcome.api_version(), welcome.protocol_version());
  if (capabilitiesMoved_) {
    sendCapabilities(services_.capabilities->current());
  }
  if (!coreSpeaksOurProtocol_) {
    spdlog::warn(
        "[{}] Server speaks protocol v{}, this renderer speaks {}-{}; staying "
        "connected so it can upgrade us, but playback is refused until then",
        name_, welcome.protocol_version(), kMinRendererProtocolVersion,
        kMaxRendererProtocolVersion);
    // A session this Core opened before it moved on cannot be driven by it
    // any more; ending it beats leaving audio running with no route back.
    if (auto orphan = services_.sessions->ownedBy(serverId_)) {
      orphan->close("server speaks a protocol this renderer does not");
    }
    return;
  }
  adoptSession();
}

void ProtocolSession::handleUpgrade(const pb::Envelope &env) {
  // Answered whatever the Core speaks: one we cannot follow is exactly the
  // one that needs to replace this binary.
  pb::Envelope out;
  pb::UpgradeResult *result = out.mutable_upgrade_result();
  const std::string &target = env.upgrade().target_version();

  if (!services_.upgrade || !services_.upgrade->supported()) {
    result->set_accepted(false);
    result->set_detail(
        "this renderer was not installed in a way that can upgrade itself");
  } else if (services_.sessions->current() != nullptr) {
    // The restart would cut the audio off mid-track. A Core asks idle
    // renderers first; this is the renderer's own last word on it.
    result->set_accepted(false);
    result->set_detail("a playback session is running");
  } else if (std::string error = services_.upgrade->request(target);
             !error.empty()) {
    result->set_accepted(false);
    result->set_detail(error);
  } else {
    result->set_accepted(true);
    result->set_detail(target.empty() ? "upgrading to the latest release"
                                      : "upgrading to " + target);
    spdlog::info("[{}] Upgrade requested by the Core (target '{}')", name_,
                 target.empty() ? "latest" : target);
  }
  if (!result->accepted()) {
    spdlog::warn("[{}] Upgrade request refused: {}", name_, result->detail());
  }
  sendReply(out, env.message_id());
}

void ProtocolSession::handleSessionOpen(const pb::SessionOpen &open) {
  const std::string &sessionId = open.session_id();
  pb::Envelope env;
  pb::SessionOpenResult *result = env.mutable_session_open_result();
  result->set_session_id(sessionId);
  bool accepted = false;

  if (!welcomed_) {
    result->set_accepted(false);
    result->set_error(pb::SessionOpenResult::ERROR_INTERNAL);
    result->set_detail("session opened before the handshake completed");
  } else if (!coreSpeaksOurProtocol_) {
    result->set_accepted(false);
    result->set_error(pb::SessionOpenResult::ERROR_INTERNAL);
    result->set_detail("renderer does not speak this server's protocol");
  } else {
    std::string busyOwner;
    std::string openError;
    SessionVolumePolicy volume{open.force_fixed_output()};
    if (auto session = services_.sessions->open(sessionId, serverId_, busyOwner,
                                                volume, &openError)) {
      result->set_accepted(true);
      session_ = std::move(session);
      accepted = true;
    } else if (!openError.empty()) {
      result->set_accepted(false);
      result->set_error(pb::SessionOpenResult::ERROR_INTERNAL);
      result->set_detail(openError);
    } else {
      result->set_accepted(false);
      result->set_error(pb::SessionOpenResult::ERROR_BUSY);
      result->set_detail("another playback session is running");
      result->set_owner_server_id(busyOwner);
    }
  }
  sendEnvelope(env);
  if (accepted) {
    // Attaching answers with the snapshot, after the result above: the Core
    // starts with the renderer's state rather than having to ask.
    session_->attach(weak_from_this());
  }
}

void ProtocolSession::handleSessionClose(const std::string &sessionId) {
  // Closing notifies every attached connection through onSessionClosed(),
  // which is what clears session_ here.
  services_.sessions->close(sessionId, serverId_);
  pb::Envelope env;
  pb::SessionClosed *closed = env.mutable_session_closed();
  closed->set_session_id(sessionId);
  closed->set_reason(pb::SessionClosed::REASON_ACK);
  sendEnvelope(env);
}

void ProtocolSession::handleCommand(const pb::Envelope &in) {
  // The gate: a command must name the session this link is attached to.
  // session_ is only ever set while this Core owns it, so the owner check is
  // implied. Past the gate nothing is refused — what a command did is
  // reported as state.
  if (session_ && in.session_id() == session_->sessionId()) {
    session_->onCommand(in.command());
    return;
  }
  spdlog::warn("[{}] Rejecting command from server {}: session '{}' is not "
               "the one being run",
               name_, serverId_, in.session_id());
  pb::Envelope out;
  pb::CommandRejected *rejection = out.mutable_command_rejected();
  rejection->set_session_id(in.session_id());
  rejection->set_command(kindOf(in.command()));
  rejection->set_at_unix_ms(nowUnixMs());
  rejection->set_detail(services_.sessions->current()
                            ? "another session is running"
                            : "no session is open on this renderer");
  // The envelope's session_id stays empty: a refused command belongs to no
  // session of ours, and the one it named is inside the rejection.
  sendEnvelope(out);
}

void ProtocolSession::handleGoodbye(const pb::Goodbye &goodbye) {
  if (goodbye.reason() == pb::Goodbye::REASON_VERSION_UNSUPPORTED) {
    spdlog::error("[{}] Server rejected protocol version; giving up: {}",
                  name_, goodbye.detail());
    wire_.giveUp();
  } else if (goodbye.reason() == pb::Goodbye::REASON_REPLACED) {
    // Duplicated renderer_id; reconnecting would displace it back and forth.
    spdlog::error("[{}] Replaced by another connection with the same "
                  "renderer_id; giving up (duplicated renderer identity?): {}",
                  name_, goodbye.detail());
    wire_.giveUp();
  } else {
    spdlog::info("[{}] Server said goodbye (reason {})", name_,
                 static_cast<int>(goodbye.reason()));
  }
}

void ProtocolSession::adoptSession() {
  // The reconnect half of session restore: a handshake that turns out to
  // carry the session's owner becomes a route to it again.
  if (auto session = services_.sessions->ownedBy(serverId_)) {
    session_ = std::move(session);
    session_->attach(weak_from_this());
  }
}

void ProtocolSession::detachSession() {
  if (!session_) {
    return;
  }
  // Moved out first: losing its last route may close the session, which calls
  // straight back into onSessionClosed().
  auto session = std::move(session_);
  session->onConnectionClosed(this);
}

void ProtocolSession::onDown() {
  detachSession();
  welcomed_ = false;
  serverId_.clear();
}

std::string ProtocolSession::shutdownFrame() {
  if (!welcomed_) {
    return {};
  }
  pb::Envelope env;
  env.set_message_id(nextMessageId_++);
  env.mutable_goodbye()->set_reason(pb::Goodbye::REASON_SHUTDOWN);
  return env.SerializeAsString();
}

bool ProtocolSession::sendSessionMessage(pb::Envelope &env) {
  if (!welcomed_) {
    return false;
  }
  env.set_message_id(nextMessageId_++);
  return wire_.send(env.SerializeAsString());
}

void ProtocolSession::onSessionClosed() { session_.reset(); }

void ProtocolSession::onCapabilitiesChanged(
    const pb::Capabilities &capabilities) {
  if (!welcomed_) {
    capabilitiesMoved_ = true;
    return;
  }
  sendCapabilities(capabilities);
}

void ProtocolSession::sendCapabilities(const pb::Capabilities &capabilities) {
  pb::Envelope env;
  *env.mutable_capabilities_changed()->mutable_capabilities() = capabilities;
  sendEnvelope(env);
}

void ProtocolSession::sendEnvelope(pb::Envelope &env) {
  env.set_message_id(nextMessageId_++);
  wire_.send(env.SerializeAsString());
}

void ProtocolSession::sendReply(pb::Envelope &out, uint64_t inReplyTo) {
  out.set_in_reply_to(inReplyTo);
  sendEnvelope(out);
}
