#include "AudioGraphNode.h"

#include <gtest/gtest.h>

namespace {
class Node : public AudioGraphNode {
public:
  using AudioGraphNode::updateState;
};
} // namespace

TEST(AudioGraphNodeUpdate, SetsWhatItDerivesFromTheCurrentState) {
  Node node;
  std::vector<AudioGraphNodeState> heard;
  node.onStateChange([&heard](AudioGraphNode *, StreamState state) {
    heard.push_back(state.state);
    return true;
  });
  node.updateState([](const StreamState &current) -> std::optional<StreamState> {
    EXPECT_EQ(current.state, AudioGraphNodeState::STOPPED);
    return StreamState(AudioGraphNodeState::PREPARING);
  });
  EXPECT_EQ(node.getState().state, AudioGraphNodeState::PREPARING);
  EXPECT_EQ(heard, (std::vector{AudioGraphNodeState::STOPPED,
                                AudioGraphNodeState::PREPARING}));
}

TEST(AudioGraphNodeUpdate, NothingDerivedLeavesTheStateAndTellsNobody) {
  Node node;
  int heard = 0;
  node.onStateChange([&heard](AudioGraphNode *, StreamState) {
    ++heard;
    return true;
  });
  node.updateState([](const StreamState &) -> std::optional<StreamState> {
    return std::nullopt;
  });
  EXPECT_EQ(node.getState().state, AudioGraphNodeState::STOPPED);
  EXPECT_EQ(heard, 1) << "only the greeting on subscribing";
}

TEST(AudioGraphNodeUpdate, DecisionsFromManyThreadsNeverInterleave) {
  Node node;
  node.updateState([](const StreamState &) -> std::optional<StreamState> {
    return StreamState(AudioGraphNodeState::STREAMING, 0);
  });
  std::vector<std::thread> threads;
  for (int t = 0; t < 4; ++t) {
    threads.emplace_back([&node] {
      for (int i = 0; i < 1000; ++i) {
        node.updateState([](const StreamState &current)
                             -> std::optional<StreamState> {
          StreamState next = current;
          ++next.position;
          next.state = next.state == AudioGraphNodeState::STREAMING
                           ? AudioGraphNodeState::PAUSED
                           : AudioGraphNodeState::STREAMING;
          return next;
        });
      }
    });
  }
  for (auto &thread : threads) {
    thread.join();
  }
  EXPECT_EQ(node.getState().position, 4000) << "no increment was lost";
}
