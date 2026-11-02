#include <gtest/gtest.h>

#include <memory>
#include <optional>
#include <vector>

#include "native_player/AudioStreamSwitcher.h"

namespace {

/// A stream that emits whatever states a test tells it to, and no data.
class FakeStream : public AudioGraphOutputNode {
public:
  explicit FakeStream(std::optional<StreamId> streamId = std::nullopt)
      : AudioGraphNode(streamId) {}

  size_t read(void *, size_t) override { return 0; }
  size_t waitForData(std::stop_token, size_t) override { return 0; }
  size_t waitForDataFor(std::stop_token, std::chrono::milliseconds,
                        size_t) override {
    return 0;
  }

  void emit(AudioGraphNodeState state) { setState(StreamState(state)); }
  void emit(const StreamState &state) { setState(state); }
};

/// Records every state a node publishes.
class Recorder {
public:
  explicit Recorder(AudioGraphNode &node) {
    node.onStateChange([this](AudioGraphNode *, StreamState state) {
      states.push_back(state);
      return true;
    });
  }

  std::vector<StreamState> states;

  const StreamState &last() const { return states.back(); }
};

std::shared_ptr<FakeStream> makeStream(StreamId id) {
  return std::make_shared<FakeStream>(id);
}

}  // namespace

TEST(StreamIdentity, AStreamNodeStampsItsOwnStates) {
  auto stream = makeStream(7);
  stream->emit(AudioGraphNodeState::STREAMING);

  EXPECT_EQ(stream->getState().streamId, std::optional<StreamId>(7));
}

TEST(StreamIdentity, LiveReadinessAndProgressSurviveStateDeduplication) {
  FakeStream stream(7);
  Recorder recorder(stream);
  StreamState state(AudioGraphNodeState::PREPARING);
  stream.emit(state);
  const auto beforeReady = recorder.states.size();
  state.liveReady = true;
  stream.emit(state);
  ASSERT_EQ(recorder.states.size(), beforeReady + 1);
  EXPECT_TRUE(recorder.last().liveReady);
  stream.emit(state);
  EXPECT_EQ(recorder.states.size(), beforeReady + 1);

  state.state = AudioGraphNodeState::STREAMING;
  state.liveReady = false;
  state.liveSourceFrame = 44100;
  stream.emit(state);
  const auto beforeProgress = recorder.states.size();
  state.liveSourceFrame = 55125;
  stream.emit(state);
  ASSERT_EQ(recorder.states.size(), beforeProgress + 1);
  EXPECT_EQ(recorder.last().liveSourceFrame, 55125);
  stream.emit(state);
  EXPECT_EQ(recorder.states.size(), beforeProgress + 1);
}

TEST(StreamIdentity, ANodeBelongingToNoStreamLeavesStatesAnonymous) {
  FakeStream stream;
  stream.emit(AudioGraphNodeState::STREAMING);

  EXPECT_FALSE(stream.getState().streamId.has_value());
}

TEST(StreamIdentity, SameStateForAnotherStreamIsStillAChange) {
  AudioStreamSwitcher switcher;
  auto first = makeStream(1);
  auto second = makeStream(2);
  switcher.connectTo(first);
  switcher.acceptSourceChange();
  first->emit(AudioGraphNodeState::STREAMING);
  switcher.connectTo(second);
  Recorder recorder(switcher);

  // A handover between two playing streams: same enum either side of it.
  first->emit(AudioGraphNodeState::FINISHED);
  switcher.acceptSourceChange();
  second->emit(AudioGraphNodeState::STREAMING);

  EXPECT_EQ(recorder.last().state, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(recorder.last().streamId, std::optional<StreamId>(2));
  EXPECT_EQ(recorder.states.front().streamId, std::optional<StreamId>(1));
}

TEST(StreamIdentity, TheSwitcherAnnouncesTheIncomingStream) {
  AudioStreamSwitcher switcher;
  auto first = makeStream(11);

  switcher.connectTo(first);

  EXPECT_EQ(switcher.getState().state, AudioGraphNodeState::SOURCE_CHANGED);
  EXPECT_EQ(switcher.getState().streamId, std::optional<StreamId>(11));
}

TEST(StreamIdentity, TheSwitcherForwardsTheCurrentStreamsIdentity) {
  AudioStreamSwitcher switcher;
  auto first = makeStream(11);
  switcher.connectTo(first);
  switcher.acceptSourceChange();

  first->emit(AudioGraphNodeState::STREAMING);

  EXPECT_EQ(switcher.getState().state, AudioGraphNodeState::STREAMING);
  EXPECT_EQ(switcher.getState().streamId, std::optional<StreamId>(11));
}

TEST(StreamIdentity, AHandoverNamesTheStreamTakingOver) {
  AudioStreamSwitcher switcher;
  auto first = makeStream(11);
  auto second = makeStream(22);
  switcher.connectTo(first);
  switcher.acceptSourceChange();
  first->emit(AudioGraphNodeState::STREAMING);
  switcher.connectTo(second);  // queued behind the one playing

  first->emit(AudioGraphNodeState::FINISHED);

  EXPECT_EQ(switcher.getState().state, AudioGraphNodeState::SOURCE_CHANGED);
  EXPECT_EQ(switcher.getState().streamId, std::optional<StreamId>(22));

  // ...and after accepting it, the new stream speaks for itself.
  switcher.acceptSourceChange();
  second->emit(AudioGraphNodeState::STREAMING);
  EXPECT_EQ(switcher.getState().streamId, std::optional<StreamId>(22));
}

TEST(StreamIdentity, TheLastStreamFinishingNamesItself) {
  AudioStreamSwitcher switcher;
  auto only = makeStream(11);
  switcher.connectTo(only);
  switcher.acceptSourceChange();
  only->emit(AudioGraphNodeState::STREAMING);

  switcher.disconnect(only);

  EXPECT_EQ(switcher.getState().state, AudioGraphNodeState::FINISHED);
  EXPECT_EQ(switcher.getState().streamId, std::optional<StreamId>(11));
  // ...and with nothing active, the switcher speaks for no stream.
  EXPECT_FALSE(switcher.streamId().has_value());
}

TEST(StreamIdentity, TheSwitcherSpeaksForWhicheverStreamIsActive) {
  AudioStreamSwitcher switcher;
  auto first = makeStream(11);
  auto second = makeStream(22);

  EXPECT_FALSE(switcher.streamId().has_value());  // nothing connected yet

  switcher.connectTo(first);
  EXPECT_FALSE(switcher.streamId().has_value());  // announced, not yet taken
  switcher.acceptSourceChange();
  EXPECT_EQ(switcher.streamId(), std::optional<StreamId>(11));

  switcher.connectTo(second);
  first->emit(AudioGraphNodeState::FINISHED);
  EXPECT_FALSE(switcher.streamId().has_value());  // between two streams
  switcher.acceptSourceChange();
  EXPECT_EQ(switcher.streamId(), std::optional<StreamId>(22));
}

TEST(StreamIdentity, DroppingTheCurrentStreamHandsOverToTheNext) {
  AudioStreamSwitcher switcher;
  auto first = makeStream(11);
  auto second = makeStream(22);
  switcher.connectTo(first);
  switcher.acceptSourceChange();
  first->emit(AudioGraphNodeState::STREAMING);
  switcher.connectTo(second);

  switcher.disconnect(first);

  EXPECT_EQ(switcher.getState().state, AudioGraphNodeState::SOURCE_CHANGED);
  EXPECT_EQ(switcher.getState().streamId, std::optional<StreamId>(22));
}

TEST(StreamIdentity, QueueingBehindAPlayingStreamSaysNothing) {
  AudioStreamSwitcher switcher;
  auto first = makeStream(11);
  switcher.connectTo(first);
  switcher.acceptSourceChange();
  first->emit(AudioGraphNodeState::STREAMING);

  Recorder recorder(switcher);
  switcher.connectTo(makeStream(22));

  // Only the state the recorder was greeted with on subscribing.
  EXPECT_EQ(recorder.states.size(), 1u);
  EXPECT_EQ(recorder.last().streamId, std::optional<StreamId>(11));
}
