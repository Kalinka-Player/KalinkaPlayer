#include <gtest/gtest.h>

#include <boost/asio.hpp>

#include <chrono>
#include <memory>
#include <string>
#include <vector>

#include "Protocol.h"
#include "fakes.h"
#include "net/ProtocolSession.h"
#include "session/SessionManager.h"
#include "upgrade/UpgradeService.h"

namespace pb = kalinka::renderer::v1;
using namespace std::chrono_literals;

namespace {

/// Captures everything the protocol sends, already parsed.
struct FakeWire {
  std::vector<pb::Envelope> sent;
  bool up = true;
  bool gaveUp = false;

  ProtocolSession::Wire wire() {
    return {
        [this](std::string data) {
          if (!up) {
            return false;
          }
          pb::Envelope env;
          EXPECT_TRUE(env.ParseFromString(data));
          sent.push_back(env);
          return true;
        },
        [this] { gaveUp = true; },
    };
  }
};

/// Records what was asked of it instead of touching the filesystem.
class FakeUpgradeService : public UpgradeService {
public:
  bool available = true;
  std::string failWith;
  std::vector<std::string> requested;

  bool supported() const override { return available; }

  std::string request(const std::string &targetVersion) override {
    if (!failWith.empty()) {
      return failWith;
    }
    requested.push_back(targetVersion);
    return {};
  }
};

}  // namespace

class ProtocolSessionTest : public ::testing::Test {
protected:
  std::shared_ptr<ProtocolSession> makeProtocol(FakeWire &wire) {
    auto protocol = std::make_shared<ProtocolSession>("test", identity,
                                                      "Unit Renderer",
                                                      services);
    protocol->bind(wire.wire());
    return protocol;
  }

  void welcome(ProtocolSession &protocol, const std::string &serverId,
               uint32_t protocolVersion = kMaxRendererProtocolVersion) {
    pb::Envelope env;
    env.set_message_id(1);
    env.mutable_welcome()->set_server_id(serverId);
    env.mutable_welcome()->set_protocol_version(protocolVersion);
    protocol.onMessage(env.SerializeAsString());
  }

  void feed(ProtocolSession &protocol, const pb::Envelope &env) {
    protocol.onMessage(env.SerializeAsString());
  }

  std::shared_ptr<Session> openSession(ProtocolSession &protocol,
                                       const std::string &sessionId) {
    pb::Envelope env;
    env.mutable_session_open()->set_session_id(sessionId);
    feed(protocol, env);
    return services.sessions->current();
  }

  boost::asio::io_context ioc;
  std::shared_ptr<FakePlayer> player = std::make_shared<FakePlayer>();
  Identity identity{"rid-1", "iid-1"};
  std::shared_ptr<FakeUpgradeService> upgrade =
      std::make_shared<FakeUpgradeService>();
  std::shared_ptr<FakeCapabilitySource> capabilities =
      std::make_shared<FakeCapabilitySource>();
  RendererServices services{
      std::make_shared<SessionManager>(ioc, 60s, player),
      std::make_shared<ConfigService>(
          std::vector<std::shared_ptr<ConfigContributor>>{player}),
      upgrade,
      std::make_shared<CapabilityService>(capabilities),
  };
};

TEST_F(ProtocolSessionTest, HelloGoesOutWhenTheLinkComesUp) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);

  protocol->onUp();

  ASSERT_EQ(wire.sent.size(), 1u);
  const pb::Hello &hello = wire.sent[0].hello();
  EXPECT_EQ(hello.renderer_id(), "rid-1");
  EXPECT_EQ(hello.instance_id(), "iid-1");
  EXPECT_EQ(hello.friendly_name(), "Unit Renderer");
  EXPECT_EQ(hello.protocol_versions().min(), kMinRendererProtocolVersion);
  EXPECT_EQ(hello.protocol_versions().max(), kMaxRendererProtocolVersion);
  EXPECT_TRUE(hello.active_session_id().empty());
}

TEST_F(ProtocolSessionTest, HelloBroadcastsTheRunningSession) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  std::string busyOwner;
  services.sessions->open("sid-1", "owner-1", busyOwner);

  protocol->onUp();

  const pb::Hello &hello = wire.sent[0].hello();
  EXPECT_EQ(hello.active_session_id(), "sid-1");
  EXPECT_EQ(hello.session_owner_server_id(), "owner-1");
}

TEST_F(ProtocolSessionTest, HelloSaysWhatTheRendererCanPlay) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  capabilities->dsd = true;

  protocol->onUp();

  const pb::Hello &hello = wire.sent[0].hello();
  ASSERT_TRUE(hello.has_capabilities());
  EXPECT_TRUE(hello.capabilities().dsd());
}

TEST_F(ProtocolSessionTest, WithoutACapabilityPlaneHelloSaysNothingOfThem) {
  services.capabilities.reset();
  FakeWire wire;
  auto protocol = makeProtocol(wire);

  protocol->onUp();

  EXPECT_FALSE(wire.sent[0].hello().has_capabilities());
}

TEST_F(ProtocolSessionTest, EveryWelcomedCoreIsToldWhenCapabilitiesChange) {
  FakeWire first;
  FakeWire second;
  auto toFirst = makeProtocol(first);
  auto toSecond = makeProtocol(second);
  toFirst->onUp();
  welcome(*toFirst, "server-1");
  toSecond->onUp();
  welcome(*toSecond, "server-2");

  capabilities->dsd = true;
  services.capabilities->refresh();
  services.capabilities->refresh();

  for (const FakeWire *wire : {&first, &second}) {
    ASSERT_EQ(wire->sent.size(), 2u);
    ASSERT_TRUE(wire->sent[1].has_capabilities_changed());
    EXPECT_TRUE(wire->sent[1].capabilities_changed().capabilities().dsd());
  }
}

TEST_F(ProtocolSessionTest, AChangeDuringTheHandshakeFollowsTheWelcome) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  protocol->onUp();

  capabilities->dsd = true;
  services.capabilities->refresh();
  ASSERT_EQ(wire.sent.size(), 1u);
  welcome(*protocol, "server-1");

  ASSERT_EQ(wire.sent.size(), 2u);
  EXPECT_TRUE(wire.sent[1].capabilities_changed().capabilities().dsd());
}

TEST_F(ProtocolSessionTest, ALinkThatWasDownHearsOfAChangeInItsNextHello) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  protocol->onUp();
  welcome(*protocol, "server-1");
  protocol->onDown();
  wire.up = false;

  capabilities->dsd = true;
  services.capabilities->refresh();
  wire.up = true;
  protocol->onUp();
  welcome(*protocol, "server-1");

  ASSERT_EQ(wire.sent.size(), 2u);
  EXPECT_TRUE(wire.sent[1].hello().capabilities().dsd());
}

TEST_F(ProtocolSessionTest, SessionOpenBeforeWelcomeIsRefused) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);

  openSession(*protocol, "sid-1");

  ASSERT_EQ(wire.sent.size(), 1u);
  const pb::SessionOpenResult &result = wire.sent[0].session_open_result();
  EXPECT_FALSE(result.accepted());
  EXPECT_EQ(result.error(), pb::SessionOpenResult::ERROR_INTERNAL);
  EXPECT_EQ(services.sessions->current(), nullptr);
}

TEST_F(ProtocolSessionTest, SessionOpenAnswersResultThenSnapshot) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");

  auto session = openSession(*protocol, "sid-1");

  ASSERT_NE(session, nullptr);
  EXPECT_EQ(session->ownerServerId(), "server-a");
  ASSERT_EQ(wire.sent.size(), 2u);
  EXPECT_TRUE(wire.sent[0].session_open_result().accepted());
  EXPECT_TRUE(wire.sent[1].has_state_snapshot());
  EXPECT_EQ(wire.sent[1].session_id(), "sid-1");
}

TEST_F(ProtocolSessionTest, FixedOutputOverrideReachesThePlayer) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");

  pb::Envelope env;
  pb::SessionOpen *open = env.mutable_session_open();
  open->set_session_id("sid-1");
  open->set_force_fixed_output(true);
  feed(*protocol, env);

  ASSERT_EQ(player->sessionVolumes.size(), 1u);
  EXPECT_TRUE(player->sessionVolumes[0].forceFixedOutput);
}

TEST_F(ProtocolSessionTest, FailedVolumeSafetyRefusesTheSession) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");
  player->sessionVolumeError = "safe starting volume unavailable";

  openSession(*protocol, "sid-1");

  ASSERT_EQ(wire.sent.size(), 1u);
  const pb::SessionOpenResult &result = wire.sent[0].session_open_result();
  EXPECT_FALSE(result.accepted());
  EXPECT_EQ(result.error(), pb::SessionOpenResult::ERROR_INTERNAL);
  EXPECT_EQ(result.detail(), "safe starting volume unavailable");
  EXPECT_EQ(services.sessions->current(), nullptr);
}

TEST_F(ProtocolSessionTest, ASecondCoreIsToldWhoOwnsTheSession) {
  FakeWire wireA, wireB;
  auto a = makeProtocol(wireA);
  auto b = makeProtocol(wireB);
  welcome(*a, "server-a");
  welcome(*b, "server-b");
  openSession(*a, "sid-1");

  openSession(*b, "sid-2");

  const pb::SessionOpenResult &result = wireB.sent[0].session_open_result();
  EXPECT_FALSE(result.accepted());
  EXPECT_EQ(result.error(), pb::SessionOpenResult::ERROR_BUSY);
  EXPECT_EQ(result.owner_server_id(), "server-a");
}

TEST_F(ProtocolSessionTest, TheGateRejectsCommandsForForeignSessions) {
  FakeWire wireA, wireB;
  auto a = makeProtocol(wireA);
  auto b = makeProtocol(wireB);
  welcome(*a, "server-a");
  welcome(*b, "server-b");
  openSession(*a, "sid-1");

  pb::Envelope env;
  env.set_session_id("sid-1");  // the other Core's session, replayed
  env.mutable_command()->mutable_resume();
  feed(*b, env);

  ASSERT_EQ(wireB.sent.size(), 1u);
  const pb::CommandRejected &rejected = wireB.sent[0].command_rejected();
  EXPECT_EQ(rejected.session_id(), "sid-1");
  EXPECT_EQ(rejected.command(), pb::CONTROL_KIND_RESUME);
  EXPECT_EQ(rejected.detail(), "another session is running");
  EXPECT_TRUE(player->calls.empty());
}

TEST_F(ProtocolSessionTest, NoSessionGetsItsOwnRejectionDetail) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");

  pb::Envelope env;
  env.set_session_id("sid-unknown");
  env.mutable_command()->mutable_resume();
  feed(*protocol, env);

  EXPECT_EQ(wire.sent[0].command_rejected().detail(),
            "no session is open on this renderer");
}

TEST_F(ProtocolSessionTest, GatedCommandsReachThePlayer) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");
  openSession(*protocol, "sid-1");
  wire.sent.clear();

  pb::Envelope env;
  env.set_session_id("sid-1");
  env.mutable_command()->mutable_resume();
  feed(*protocol, env);

  EXPECT_EQ(player->calls, (std::vector<std::string>{"resume"}));
  EXPECT_TRUE(wire.sent.empty());  // outcome arrives as state, not as an ack
}

TEST_F(ProtocolSessionTest, SessionCloseIsAcknowledged) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");
  openSession(*protocol, "sid-1");
  wire.sent.clear();

  pb::Envelope env;
  env.mutable_session_close()->set_session_id("sid-1");
  feed(*protocol, env);

  ASSERT_EQ(wire.sent.size(), 1u);
  const pb::SessionClosed &closed = wire.sent[0].session_closed();
  EXPECT_EQ(closed.session_id(), "sid-1");
  EXPECT_EQ(closed.reason(), pb::SessionClosed::REASON_ACK);
  EXPECT_EQ(services.sessions->current(), nullptr);
}

TEST_F(ProtocolSessionTest, TheLinkGoingDownDetachesFromTheSession) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");
  openSession(*protocol, "sid-1");

  protocol->onDown();

  // The link is gone but the session is not: the owner has its grace period.
  ASSERT_NE(services.sessions->current(), nullptr);
  EXPECT_TRUE(player->calls.empty());
}

TEST_F(ProtocolSessionTest, ReconnectingOwnerGetsTheSessionBack) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");
  openSession(*protocol, "sid-1");

  protocol->onDown();  // the session survives on its grace period
  ASSERT_NE(services.sessions->current(), nullptr);
  wire.sent.clear();

  protocol->onUp();
  welcome(*protocol, "server-a");

  // Hello with the claim, then the snapshot the reattachment pushes.
  ASSERT_EQ(wire.sent.size(), 2u);
  EXPECT_EQ(wire.sent[0].hello().active_session_id(), "sid-1");
  EXPECT_TRUE(wire.sent[1].has_state_snapshot());
}

TEST_F(ProtocolSessionTest, ConfigIsAnsweredInReplyWithoutASession) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");

  pb::Envelope request;
  request.set_message_id(7);
  request.mutable_config_request();
  feed(*protocol, request);

  ASSERT_EQ(wire.sent.size(), 1u);
  EXPECT_TRUE(wire.sent[0].has_config_snapshot());
  EXPECT_EQ(wire.sent[0].in_reply_to(), 7u);

  pb::Envelope update;
  update.set_message_id(8);
  pb::ConfigUpdate::Setting *setting =
      update.mutable_config_update()->add_settings();
  setting->set_path("output.buffer_ms");
  setting->set_value("250");
  feed(*protocol, update);

  ASSERT_EQ(wire.sent.size(), 2u);
  EXPECT_EQ(wire.sent[1].in_reply_to(), 8u);
  EXPECT_TRUE(wire.sent[1].config_result().outcomes(0).applied());
}

TEST_F(ProtocolSessionTest, FatalGoodbyesGiveUpTheLink) {
  for (const auto reason : {pb::Goodbye::REASON_VERSION_UNSUPPORTED,
                            pb::Goodbye::REASON_REPLACED}) {
    FakeWire wire;
    auto protocol = makeProtocol(wire);
    pb::Envelope env;
    env.mutable_goodbye()->set_reason(reason);
    feed(*protocol, env);
    EXPECT_TRUE(wire.gaveUp);
  }

  FakeWire wire;
  auto protocol = makeProtocol(wire);
  pb::Envelope env;
  env.mutable_goodbye()->set_reason(pb::Goodbye::REASON_SHUTDOWN);
  feed(*protocol, env);
  EXPECT_FALSE(wire.gaveUp);
}

TEST_F(ProtocolSessionTest, ShutdownFrameExistsOnlyOnceWelcomed) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  EXPECT_TRUE(protocol->shutdownFrame().empty());

  welcome(*protocol, "server-a");
  pb::Envelope env;
  ASSERT_TRUE(env.ParseFromString(protocol->shutdownFrame()));
  EXPECT_EQ(env.goodbye().reason(), pb::Goodbye::REASON_SHUTDOWN);
}

TEST_F(ProtocolSessionTest, GarbageIsDroppedWithoutSideEffects) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");

  protocol->onMessage("not a protobuf");

  EXPECT_TRUE(wire.sent.empty());
  EXPECT_FALSE(wire.gaveUp);
}

TEST(RendererProtocolRange, SpansAtLeastOneVersion) {
  EXPECT_LE(kMinRendererProtocolVersion, kMaxRendererProtocolVersion);
}

TEST(RendererProtocolRange, AcceptsEveryVersionInsideItAndNothingOutside) {
  for (auto version = kMinRendererProtocolVersion;
       version <= kMaxRendererProtocolVersion; ++version) {
    EXPECT_TRUE(rendererProtocolSupported(static_cast<int>(version)))
        << "refused a Core speaking " << version;
  }
  EXPECT_FALSE(rendererProtocolSupported(
      static_cast<int>(kMinRendererProtocolVersion) - 1));
  EXPECT_FALSE(rendererProtocolSupported(
      static_cast<int>(kMaxRendererProtocolVersion) + 1));
}

TEST_F(ProtocolSessionTest, AWelcomeFromACoreWeCannotFollowKeepsTheLink) {
  // Dropping the link here would leave this renderer unreachable by anything
  // but a shell on its own machine — the connection is what an upgrade rides.
  FakeWire wire;
  auto protocol = makeProtocol(wire);

  welcome(*protocol, "server-a", kMaxRendererProtocolVersion + 1);

  EXPECT_FALSE(wire.gaveUp);
  EXPECT_TRUE(wire.sent.empty());
}

TEST_F(ProtocolSessionTest, ACoreWeCannotFollowIsRefusedPlayback) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a", kMaxRendererProtocolVersion + 1);

  openSession(*protocol, "sid-1");

  ASSERT_EQ(wire.sent.size(), 1u);
  const pb::SessionOpenResult &result = wire.sent[0].session_open_result();
  EXPECT_FALSE(result.accepted());
  EXPECT_EQ(result.error(), pb::SessionOpenResult::ERROR_INTERNAL);
  EXPECT_EQ(services.sessions->current(), nullptr);
}

TEST_F(ProtocolSessionTest, ASessionIsNotLeftPlayingForACoreWeCannotFollow) {
  // The Core that opened it has upgraded past us: it can no longer stop it,
  // so the renderer ends it rather than playing on unreachably.
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  std::string busyOwner;
  services.sessions->open("sid-1", "server-a", busyOwner);
  ASSERT_NE(services.sessions->current(), nullptr);

  welcome(*protocol, "server-a", kMaxRendererProtocolVersion + 1);

  EXPECT_EQ(services.sessions->current(), nullptr);
}

TEST_F(ProtocolSessionTest, AnUpgradeRequestIsPassedToTheUpgradePlane) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");

  pb::Envelope env;
  env.set_message_id(7);
  env.mutable_upgrade()->set_target_version("0.4.0");
  feed(*protocol, env);

  EXPECT_EQ(upgrade->requested, std::vector<std::string>{"0.4.0"});
  ASSERT_EQ(wire.sent.size(), 1u);
  EXPECT_TRUE(wire.sent[0].upgrade_result().accepted());
  EXPECT_EQ(wire.sent[0].in_reply_to(), 7u);
}

TEST_F(ProtocolSessionTest, ACoreWeCannotFollowCanStillUpgradeUs) {
  // The whole point of the version-free set: the Core that has left us behind
  // is the one that has to be able to replace this binary.
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a", kMaxRendererProtocolVersion + 1);

  pb::Envelope env;
  env.mutable_upgrade()->set_target_version("0.4.0");
  feed(*protocol, env);

  EXPECT_EQ(upgrade->requested, std::vector<std::string>{"0.4.0"});
  ASSERT_EQ(wire.sent.size(), 1u);
  EXPECT_TRUE(wire.sent[0].upgrade_result().accepted());
}

TEST_F(ProtocolSessionTest, PlaybackIsNotCutOffByAnUpgrade) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a");
  openSession(*protocol, "sid-1");
  wire.sent.clear();

  pb::Envelope env;
  env.mutable_upgrade()->set_target_version("0.4.0");
  feed(*protocol, env);

  EXPECT_TRUE(upgrade->requested.empty());
  ASSERT_EQ(wire.sent.size(), 1u);
  EXPECT_FALSE(wire.sent[0].upgrade_result().accepted());
}

TEST_F(ProtocolSessionTest, AnInstallThatCannotUpgradeItselfSaysSo) {
  // A flatpak or from-source build: refusing is what stops a Core offering an
  // upgrade that would silently do nothing.
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  upgrade->available = false;
  protocol->onUp();
  welcome(*protocol, "server-a");

  ASSERT_FALSE(wire.sent.empty());
  EXPECT_FALSE(wire.sent[0].hello().upgrade_supported());

  wire.sent.clear();
  pb::Envelope env;
  env.mutable_upgrade();
  feed(*protocol, env);

  ASSERT_EQ(wire.sent.size(), 1u);
  EXPECT_FALSE(wire.sent[0].upgrade_result().accepted());
  EXPECT_TRUE(upgrade->requested.empty());
}

TEST_F(ProtocolSessionTest, HelloSaysThisInstallCanUpgradeItself) {
  FakeWire wire;
  auto protocol = makeProtocol(wire);

  protocol->onUp();

  ASSERT_FALSE(wire.sent.empty());
  EXPECT_TRUE(wire.sent[0].hello().upgrade_supported());
}

TEST_F(ProtocolSessionTest, ACoreWeCannotFollowGetsNothingButTheRescueMessages) {
  // Only hello/goodbye/upgrade are carried by every version; the rest mean
  // whatever the two ends last agreed, which no longer holds.
  FakeWire wire;
  auto protocol = makeProtocol(wire);
  welcome(*protocol, "server-a", kMaxRendererProtocolVersion + 1);

  pb::Envelope request;
  request.set_message_id(7);
  request.mutable_config_request();
  feed(*protocol, request);

  pb::Envelope update;
  update.set_message_id(8);
  pb::ConfigUpdate::Setting *setting =
      update.mutable_config_update()->add_settings();
  setting->set_path("output.buffer_ms");
  setting->set_value("250");
  feed(*protocol, update);

  EXPECT_TRUE(wire.sent.empty());
}
