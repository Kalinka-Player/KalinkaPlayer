#pragma once

#include <boost/asio.hpp>

#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <string_view>
#include <thread>
#include <vector>

/**
 * @brief Serves one file over HTTP on 127.0.0.1, so the tests that stream over
 * HTTP need neither the network nor a third-party host.
 *
 * `/ranged` answers a Range request with 206 and a Content-Range, as a music
 * server does; `/whole` ignores Range and sends the whole file with
 * `Accept-Ranges: none`; `/held` does as `/whole` does, but sends only the
 * first HELD_BYTES until release() is called; any other path is a 404.
 *
 * The routes that stall answer as `/ranged` does, but send the headers and only
 * part of the body, then hold the connection open without a word until the
 * client hangs up or the server stops. `/stall` sends STALLED_BODY_BYTES of
 * every response and `/stall-once` of its first alone; `/stall-often` sends
 * STALL_OFTEN_BYTES of every response longer than that; `/silent-once` sends
 * nothing at all to its first request, not even the headers.
 *
 * Every connection is served on a thread of its own. The server must outlive
 * the streams reading from it: destroying it closes their connections and
 * joins every thread.
 */
class LocalHttpServer {
public:
  /// Too little for a stream to count a stalled request as progress.
  static constexpr size_t STALLED_BODY_BYTES = 700;
  /// Enough for a stream to count each stalled request as progress.
  static constexpr size_t STALL_OFTEN_BYTES = 32768;
  /// A little over the 32 KB buffer the stream tests use, so a stream held
  /// here waits for room in its buffer and holds the last bytes received.
  static constexpr size_t HELD_BYTES = 36864;

  explicit LocalHttpServer(const std::string &filePath);
  ~LocalHttpServer();

  LocalHttpServer(const LocalHttpServer &) = delete;
  LocalHttpServer &operator=(const LocalHttpServer &) = delete;

  /// The address of @p path on this server, for example url("/ranged").
  std::string url(const std::string &path) const;

  /// Lets every `/held` response, current and future, send the rest.
  void release();

private:
  void acceptConnections();
  void serve(boost::asio::ip::tcp::socket &socket);
  std::optional<size_t> sentBeforeStall(std::string_view target,
                                        size_t bodySize);
  bool goesSilent(std::string_view target);
  bool waitForRelease();

  std::string body_;
  boost::asio::io_context io_;
  boost::asio::ip::tcp::acceptor acceptor_;
  std::atomic<bool> stalledOnce_ = false;
  std::atomic<bool> silencedOnce_ = false;
  std::mutex mutex_;
  std::condition_variable releasedOrStopping_;
  bool released_ = false;
  bool stopping_ = false;
  std::vector<std::shared_ptr<boost::asio::ip::tcp::socket>> sockets_;
  std::vector<std::jthread> connections_;
  std::jthread acceptorThread_;
};
