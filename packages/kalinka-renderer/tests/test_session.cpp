#include <gtest/gtest.h>

#include <boost/asio.hpp>

#include <algorithm>
#include <chrono>
#include <memory>

#include "fakes.h"
#include "session/Session.h"

namespace renderer_pb = kalinka::renderer::v1;
using namespace std::chrono_literals;

class SessionTest : public ::testing::Test {
protected:
  std::shared_ptr<Session> makeSession(std::chrono::seconds grace = 60s) {
    return Session::create(ioc, "sid-1", "owner-1", grace, player,
                           [this] { ++ended; });
  }

  boost::asio::io_context ioc;
  std::shared_ptr<FakePlayer> player = std::make_shared<FakePlayer>();
  int ended = 0;
};

TEST_F(SessionTest, AttachAnswersWithSnapshot) {
  auto session = makeSession();
  auto transport = std::make_shared<FakeTransport>();
  session->attach(transport);

  ASSERT_EQ(transport->sent.size(), 1u);
  EXPECT_TRUE(transport->sent[0].has_state_snapshot());
  EXPECT_EQ(transport->sent[0].session_id(), "sid-1");
}

TEST_F(SessionTest, CommandsBecomePlayerCalls) {
  auto session = makeSession();

  renderer_pb::Command cmd;
  cmd.mutable_set_source()->mutable_source()->set_uri("http://a/1.flac");
  session->onCommand(cmd);
  cmd.Clear();
  cmd.mutable_enqueue_source()->mutable_source()->set_uri("http://a/2.flac");
  session->onCommand(cmd);
  cmd.Clear();
  cmd.mutable_remove_source()->set_source_token("tok-2");
  session->onCommand(cmd);
  cmd.Clear();
  cmd.mutable_clear_queue();
  session->onCommand(cmd);
  cmd.Clear();
  cmd.mutable_pause();
  session->onCommand(cmd);
  cmd.Clear();
  cmd.mutable_resume();
  session->onCommand(cmd);
  cmd.Clear();
  cmd.mutable_set_volume()->set_percent(42);
  session->onCommand(cmd);
  cmd.Clear();
  cmd.mutable_seek()->set_position_ms(1234);
  session->onCommand(cmd);
  cmd.Clear();
  cmd.mutable_stop();
  session->onCommand(cmd);

  EXPECT_EQ(player->calls,
            (std::vector<std::string>{
                "set_source:http://a/1.flac", "enqueue_source:http://a/2.flac",
                "remove_source:tok-2", "clear_queue", "pause", "resume",
                "set_volume:42", "seek:1234", "stop"}));
}

TEST_F(SessionTest, RequestSnapshotPublishesInsteadOfTouchingThePlayer) {
  auto session = makeSession();
  auto transport = std::make_shared<FakeTransport>();
  session->attach(transport);

  renderer_pb::Command cmd;
  cmd.mutable_request_snapshot();
  session->onCommand(cmd);

  ASSERT_EQ(transport->sent.size(), 2u);  // attach snapshot + requested one
  EXPECT_TRUE(transport->sent[1].has_state_snapshot());
  EXPECT_TRUE(player->calls.empty());
}

TEST_F(SessionTest, StateGoesOutOnTheNewestRoute) {
  auto session = makeSession();
  auto older = std::make_shared<FakeTransport>();
  auto newer = std::make_shared<FakeTransport>();
  session->attach(older);
  session->attach(newer);

  player->emitPlaybackState(renderer_pb::PLAYBACK_STATE_ERROR);

  ASSERT_EQ(newer->sent.size(), 2u);  // its attach snapshot, then the state
  EXPECT_TRUE(newer->sent[1].has_playback_state_changed());
  EXPECT_EQ(older->sent.size(), 1u);  // only its own attach snapshot
}

TEST_F(SessionTest, StateFallsBackWhenTheNewestRouteCannotCarryIt) {
  auto session = makeSession();
  auto older = std::make_shared<FakeTransport>();
  auto newer = std::make_shared<FakeTransport>();
  session->attach(older);
  session->attach(newer);

  newer->carry = false;
  player->emitPlaybackState(renderer_pb::PLAYBACK_STATE_ERROR);

  ASSERT_EQ(older->sent.size(), 2u);
  EXPECT_TRUE(older->sent[1].has_playback_state_changed());
}

TEST_F(SessionTest, LastRouteGoneWaitsForTheGracePeriod) {
  auto session = makeSession(0s);
  auto transport = std::make_shared<FakeTransport>();
  session->attach(transport);

  session->onConnectionClosed(transport.get());
  EXPECT_EQ(ended, 0);  // grace armed, still running

  ioc.run();  // fires the (zero-length) grace timer
  EXPECT_EQ(ended, 1);
  EXPECT_EQ(player->calls, (std::vector<std::string>{"stop"}));
  EXPECT_FALSE(player->sink);  // state may not outlive the session
}

TEST_F(SessionTest, OwnerReturningInGraceKeepsTheSession) {
  auto session = makeSession(0s);
  auto transport = std::make_shared<FakeTransport>();
  session->attach(transport);

  session->onConnectionClosed(transport.get());
  auto returned = std::make_shared<FakeTransport>();
  session->attach(returned);

  ioc.run();
  EXPECT_EQ(ended, 0);
  ASSERT_EQ(returned->sent.size(), 1u);  // greeted with a fresh snapshot
  EXPECT_TRUE(returned->sent[0].has_state_snapshot());
}

TEST_F(SessionTest, CloseTellsEveryAttachedConnectionOnce) {
  auto session = makeSession();
  auto a = std::make_shared<FakeTransport>();
  auto b = std::make_shared<FakeTransport>();
  session->attach(a);
  session->attach(b);

  session->close("test");
  session->close("again");  // idempotent

  EXPECT_EQ(a->sessionClosed, 1);
  EXPECT_EQ(b->sessionClosed, 1);
  EXPECT_EQ(ended, 1);
}

TEST_F(SessionTest, NothingGoesOutAfterClose) {
  auto session = makeSession();
  auto transport = std::make_shared<FakeTransport>();
  session->attach(transport);
  session->close("test");

  session->publishSnapshot();
  session->attach(transport);  // ignored: nothing to attach to

  EXPECT_EQ(transport->sent.size(), 1u);  // only the original attach snapshot
}

TEST_F(SessionTest, ReattachingTheSameConnectionDoesNotDoubleIt) {
  auto session = makeSession(0s);
  auto transport = std::make_shared<FakeTransport>();
  session->attach(transport);
  session->attach(transport);  // a retried open

  // If the route were doubled, one closed connection would leave a stale
  // second entry and the session would think its owner is still reachable.
  session->onConnectionClosed(transport.get());
  ioc.run();
  EXPECT_EQ(ended, 1);
}

TEST_F(SessionTest, UnknownConnectionClosingChangesNothing) {
  auto session = makeSession();
  auto transport = std::make_shared<FakeTransport>();
  session->attach(transport);

  FakeTransport stranger;
  session->onConnectionClosed(&stranger);

  EXPECT_EQ(ended, 0);
  player->emitPlaybackState(renderer_pb::PLAYBACK_STATE_ERROR);
  EXPECT_EQ(transport->sent.size(), 2u);  // still routed
}

TEST_F(SessionTest, SessionVolumePolicyIsAppliedAtOpenAndUndoneAtClose) {
  auto session = Session::create(ioc, "sid-1", "owner-1", 60s, player,
                                 [this] { ++ended; },
                                 SessionVolumePolicy{true});
  ASSERT_EQ(player->sessionVolumes.size(), 1u);
  EXPECT_TRUE(player->sessionVolumes[0].forceFixedOutput);

  session->close("done");
  EXPECT_EQ(player->sessionVolumeEnds, 1);
}

TEST_F(SessionTest, DirectSessionAppliesTheRendererSafetyPolicy) {
  auto session = makeSession();
  ASSERT_EQ(player->sessionVolumes.size(), 1u);
  EXPECT_FALSE(player->sessionVolumes[0].forceFixedOutput);
  session->close("done");
}
