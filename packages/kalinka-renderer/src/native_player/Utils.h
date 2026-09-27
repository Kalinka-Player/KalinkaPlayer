#ifndef UTILS_H
#define UTILS_H

#include <array>
#include <functional>
#include <memory>
#include <stop_token>
#include <tuple>
#include <type_traits>

template <std::size_t N> class CombinedStopToken {
  static_assert(N > 0, "Number of tokens must be greater than 0");

public:
  template <typename... Tokens>
  CombinedStopToken(Tokens... tokens) : stopTokens_{tokens...} {
    static_assert(sizeof...(Tokens) == N,
                  "Number of tokens must match template parameter N");
    static_assert((std::is_same_v<Tokens, std::stop_token> && ...),
                  "All Tokens must be std::stop_token");
    registerCallbacks(std::index_sequence_for<Tokens...>{});
  }

  std::stop_token get_token() { return combinedSource_.get_token(); }

private:
  using Callback = std::stop_callback<std::function<void()>>;
  std::array<std::stop_token, N> stopTokens_;
  std::stop_source combinedSource_;
  std::array<std::unique_ptr<Callback>, N> callbacks_;

  template <std::size_t... Is>
  void registerCallbacks(std::index_sequence<Is...>) {
    (registerCallback<Is>(), ...);
  }

  template <std::size_t I> void registerCallback() {
    auto &token = stopTokens_[I];
    callbacks_[I] = std::make_unique<Callback>(
        token, [this]() { combinedSource_.request_stop(); });
  }
};

template <typename... Tokens>
CombinedStopToken<sizeof...(Tokens)> combineStopTokens(Tokens... tokens) {
  return CombinedStopToken<sizeof...(Tokens)>(tokens...);
}

template <typename SignalType> class Signal {
private:
  std::stop_source signalStopSource;
  std::condition_variable_any cv;
  mutable std::mutex mutex;

  std::optional<SignalType> value;
  SignalType ackValue;
  // Set once the answering thread has gone: nothing is waited for any more.
  bool closed = false;

public:
  Signal() = default;
  ~Signal() { cv.notify_all(); }

  void sendValue(const SignalType &value) {
    {
      std::unique_lock lock(mutex);
      if (closed) {
        return;
      }
      this->value = value;
      ackValue = SignalType();
      signalStopSource.request_stop();
    }
    cv.notify_all();
  }

  std::optional<SignalType> getValue() const {
    std::unique_lock lock(mutex);
    return value;
  }

  SignalType getResponse(std::stop_token stopToken) {
    std::unique_lock lock(mutex);
    cv.wait(lock, stopToken, [this] { return closed || !value.has_value(); });
    if (stopToken.stop_requested()) {
      return SignalType();
    }
    return ackValue;
  }

  void respond(const SignalType &ackValue) {
    std::unique_lock lock(mutex);
    value.reset();
    this->ackValue = ackValue;
    signalStopSource = std::stop_source();
    lock.unlock();
    cv.notify_all();
  }

  std::stop_token getStopToken() const {
    std::lock_guard lock(mutex);
    return signalStopSource.get_token();
  }

  std::optional<SignalType> waitValue(std::stop_token stopToken) {
    std::unique_lock lock(mutex);
    cv.wait(lock, stopToken, [this] { return closed || value.has_value(); });
    return value;
  }

  /**
   * @brief The answering thread has ended: a request pending now, or made
   * later, gets `finalAck` at once, and a waitValue() returns without a value.
   * @note Call it on the answering thread's way out. Without it a request made
   * after the thread ended waits forever, since nothing is left to answer it.
   */
  void close(const SignalType &finalAck) {
    {
      std::unique_lock lock(mutex);
      closed = true;
      value.reset();
      ackValue = finalAck;
    }
    cv.notify_all();
  }

  /// Undoes close() for an answering thread that is starting again.
  void reopen() {
    std::unique_lock lock(mutex);
    closed = false;
    value.reset();
    signalStopSource = std::stop_source();
  }
};

#endif