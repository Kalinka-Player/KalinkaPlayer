#include "Session.h"

#include <spdlog/spdlog.h>

#include <algorithm>

namespace asio = boost::asio;
namespace renderer_pb = kalinka::renderer::v1;

std::shared_ptr<Session> Session::create(asio::io_context &ioc,
                                         std::string sessionId,
                                         std::string ownerServerId,
                                         std::chrono::seconds ownerGrace,
                                         std::shared_ptr<Player> player,
                                         std::function<void()> onEnded,
                                         SessionVolumePolicy volume,
                                         std::string *error) {
  auto session = std::shared_ptr<Session>(
      new Session(ioc, std::move(sessionId), std::move(ownerServerId),
                  ownerGrace, std::move(player), std::move(onEnded)));
  // Weak: the player must not keep its session alive.
  session->player_->setStateSink(
      [weak = std::weak_ptr<Session>(session)](renderer_pb::Envelope &env) {
        if (auto self = weak.lock()) {
          self->publish(env);
        }
      });
  std::string ignoredError;
  std::string &openError = error ? *error : ignoredError;
  if (!session->player_->beginSessionVolume(volume, openError)) {
    if (openError.empty()) {
      openError = "session volume policy could not be applied";
    }
    session->player_->setStateSink({});
    return nullptr;
  }
  return session;
}

Session::Session(asio::io_context &ioc, std::string sessionId,
                 std::string ownerServerId, std::chrono::seconds ownerGrace,
                 std::shared_ptr<Player> player, std::function<void()> onEnded)
    : sessionId_(std::move(sessionId)),
      ownerServerId_(std::move(ownerServerId)), ownerGrace_(ownerGrace),
      graceTimer_(ioc), player_(std::move(player)),
      onEnded_(std::move(onEnded)) {}

void Session::attach(std::weak_ptr<SessionTransport> transport) {
  if (closed_) {
    return;
  }
  const auto held = transport.lock();
  std::erase_if(transports_,
                [&held](const std::weak_ptr<SessionTransport> &weak) {
                  auto existing = weak.lock();
                  return !existing || existing == held;
                });
  transports_.push_back(std::move(transport));
  graceTimer_.cancel();
  publishSnapshot();
}

void Session::onCommand(const renderer_pb::Command &command) {
  switch (command.op_case()) {
  case renderer_pb::Command::kSetSource:
    player_->setSource(command.set_source().source());
    break;
  case renderer_pb::Command::kEnqueueSource:
    player_->enqueueSource(command.enqueue_source().source());
    break;
  case renderer_pb::Command::kRemoveSource:
    player_->removeSource(command.remove_source().source_token());
    break;
  case renderer_pb::Command::kClearQueue:
    player_->clearQueue();
    break;
  case renderer_pb::Command::kPause:
    player_->pause();
    break;
  case renderer_pb::Command::kResume:
    player_->resume();
    break;
  case renderer_pb::Command::kStop:
    player_->stop();
    break;
  case renderer_pb::Command::kSetVolume:
    player_->setVolume(command.set_volume().percent());
    break;
  case renderer_pb::Command::kSeek:
    player_->seek(command.seek().position_ms());
    break;
  case renderer_pb::Command::kRequestSnapshot:
    publishSnapshot();
    break;
  case renderer_pb::Command::OP_NOT_SET:
    spdlog::warn("Dropping a command with no operation set");
    break;
  }
}

void Session::onConnectionClosed(const SessionTransport *transport) {
  std::erase_if(transports_,
                [transport](const std::weak_ptr<SessionTransport> &weak) {
                  auto held = weak.lock();
                  return !held || held.get() == transport;
                });
  if (!closed_ && transports_.empty()) {
    ownerLost();
  }
}

void Session::close(const char *reason) {
  if (closed_) {
    return;
  }
  // A connection dropping its reference inside onSessionClosed() must not be
  // the one that destroys us mid-method.
  auto self = shared_from_this();
  closed_ = true;
  spdlog::info("Closing session {} (owner {}): {}", sessionId_, ownerServerId_,
               reason);
  graceTimer_.cancel();
  // The sink goes first: the STOPPED this provokes has nowhere to matter — the
  // owner is told the session closed, not that playback stopped.
  player_->setStateSink({});
  player_->stop();
  player_->endSessionVolume();
  for (const auto &weak : transports_) {
    if (auto transport = weak.lock()) {
      transport->onSessionClosed();
    }
  }
  transports_.clear();
  if (onEnded_) {
    onEnded_();
  }
}

void Session::publishSnapshot() {
  renderer_pb::Envelope env;
  player_->fillSnapshot(*env.mutable_state_snapshot());
  publish(env);
}

bool Session::publish(renderer_pb::Envelope &env) {
  if (closed_) {
    return false;
  }
  env.set_session_id(sessionId_);
  // Newest connection first: an older one may still be open but idle.
  for (auto link = transports_.rbegin(); link != transports_.rend(); ++link) {
    if (auto transport = link->lock();
        transport && transport->sendSessionMessage(env)) {
      return true;
    }
  }
  spdlog::debug("Dropping session {} state: owner {} is unreachable",
                sessionId_, ownerServerId_);
  return false;
}

bool Session::attached() const {
  return std::ranges::any_of(
      transports_,
      [](const std::weak_ptr<SessionTransport> &weak) {
        return !weak.expired();
      });
}

void Session::ownerLost() {
  spdlog::info(
      "Session {} owner {} disconnected; closing in {}s unless it returns",
      sessionId_, ownerServerId_, ownerGrace_.count());
  graceTimer_.expires_after(ownerGrace_);
  graceTimer_.async_wait(
      [weak = weak_from_this()](const boost::system::error_code &ec) {
        if (ec) {
          return;  // cancelled: the owner came back, or we shut down
        }
        if (auto self = weak.lock(); self && !self->attached()) {
          self->close("owner did not return");
        }
      });
}
