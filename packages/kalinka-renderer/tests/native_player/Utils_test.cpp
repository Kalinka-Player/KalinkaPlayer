#include <gtest/gtest.h>

#include "Utils.h"

#include <chrono>
#include <future>
#include <thread>

class CombinedStopTokenTest : public ::testing::Test {
protected:
  std::stop_source source1;
  std::stop_source source2;
  CombinedStopToken<2> combinedToken;
  CombinedStopTokenTest()
      : combinedToken(source1.get_token(), source2.get_token()) {}
};

TEST_F(CombinedStopTokenTest, request_stop_on_first_token) {
  auto token = combinedToken.get_token();
  EXPECT_TRUE(token.stop_possible());
  source1.request_stop();
  EXPECT_TRUE(source1.stop_requested());
  EXPECT_TRUE(token.stop_requested());
}

TEST_F(CombinedStopTokenTest, request_stop_on_second_token) {
  auto token = combinedToken.get_token();
  EXPECT_TRUE(token.stop_possible());
  source2.request_stop();
  EXPECT_TRUE(source2.stop_requested());
  EXPECT_TRUE(token.stop_requested());
}

TEST_F(CombinedStopTokenTest, request_stop_on_both_tokens) {
  auto token = combinedToken.get_token();
  EXPECT_TRUE(token.stop_possible());
  source1.request_stop();
  source2.request_stop();
  EXPECT_TRUE(source1.stop_requested());
  EXPECT_TRUE(source2.stop_requested());
  EXPECT_TRUE(token.stop_requested());
}

TEST_F(CombinedStopTokenTest, three_stop_tokens_stop1) {
  std::stop_source source3;
  auto combinedToken3 = combineStopTokens(
      source1.get_token(), source2.get_token(), source3.get_token());

  auto token = combinedToken3.get_token();
  EXPECT_TRUE(token.stop_possible());
  source1.request_stop();
  EXPECT_TRUE(source1.stop_requested());
  EXPECT_TRUE(token.stop_requested());
}

TEST_F(CombinedStopTokenTest, three_stop_tokens_stop2) {
  std::stop_source source3;
  auto combinedToken3 = combineStopTokens(
      source1.get_token(), source2.get_token(), source3.get_token());

  auto token = combinedToken3.get_token();
  EXPECT_TRUE(token.stop_possible());
  source2.request_stop();
  EXPECT_TRUE(source2.stop_requested());
  EXPECT_TRUE(token.stop_requested());
}

TEST_F(CombinedStopTokenTest, three_stop_tokens_stop3) {
  std::stop_source source3;
  auto combinedToken3 = combineStopTokens(
      source1.get_token(), source2.get_token(), source3.get_token());

  auto token = combinedToken3.get_token();
  EXPECT_TRUE(token.stop_possible());
  source2.request_stop();
  EXPECT_TRUE(source2.stop_requested());
  EXPECT_TRUE(token.stop_requested());
}

// Signal: a request/answer handshake between a caller and a worker thread.

TEST(SignalTest, a_request_pending_when_the_worker_ends_is_answered) {
  Signal<size_t> signal;
  signal.sendValue(42);
  auto answer = std::async(std::launch::async,
                           [&] { return signal.getResponse(std::stop_token()); });

  signal.close(static_cast<size_t>(-1));

  ASSERT_EQ(answer.wait_for(std::chrono::seconds(2)), std::future_status::ready);
  EXPECT_EQ(answer.get(), static_cast<size_t>(-1));
}

TEST(SignalTest, a_request_after_the_worker_ended_is_answered_at_once) {
  Signal<size_t> signal;
  signal.close(static_cast<size_t>(-1));

  signal.sendValue(42);

  EXPECT_FALSE(signal.getValue().has_value());
  EXPECT_EQ(signal.getResponse(std::stop_token()), static_cast<size_t>(-1));
}

TEST(SignalTest, waiting_for_a_request_ends_when_closed) {
  Signal<bool> signal;
  auto waited = std::async(std::launch::async,
                           [&] { return signal.waitValue(std::stop_token()); });

  signal.close(false);

  ASSERT_EQ(waited.wait_for(std::chrono::seconds(2)), std::future_status::ready);
  EXPECT_FALSE(waited.get().has_value());
}

TEST(SignalTest, reopening_serves_a_new_worker) {
  Signal<size_t> signal;
  signal.close(static_cast<size_t>(-1));

  signal.reopen();
  signal.sendValue(7);
  auto worker = std::thread([&] {
    auto value = signal.waitValue(std::stop_token());
    signal.respond(*value + 1);
  });

  EXPECT_EQ(signal.getResponse(std::stop_token()), 8u);
  worker.join();
}
