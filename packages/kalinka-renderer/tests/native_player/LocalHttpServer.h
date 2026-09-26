#pragma once

#include <boost/asio.hpp>

#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

/**
 * @brief Serves one file over HTTP on 127.0.0.1, so the tests that stream over
 * HTTP need neither the network nor a third-party host.
 *
 * `/ranged` answers a Range request with 206 and a Content-Range, as a music
 * server does; `/whole` ignores Range and sends the whole file with
 * `Accept-Ranges: none`; any other path is a 404. Every connection is served
 * on a thread of its own. The server must outlive the streams reading from
 * it: destroying it closes their connections and joins every thread.
 */
class LocalHttpServer {
public:
  explicit LocalHttpServer(const std::string &filePath);
  ~LocalHttpServer();

  LocalHttpServer(const LocalHttpServer &) = delete;
  LocalHttpServer &operator=(const LocalHttpServer &) = delete;

  /// The address of @p path on this server, for example url("/ranged").
  std::string url(const std::string &path) const;

private:
  void acceptConnections();
  void serve(boost::asio::ip::tcp::socket &socket) const;

  std::string body_;
  boost::asio::io_context io_;
  boost::asio::ip::tcp::acceptor acceptor_;
  std::mutex mutex_;
  bool stopping_ = false;
  std::vector<std::shared_ptr<boost::asio::ip::tcp::socket>> sockets_;
  std::vector<std::jthread> connections_;
  std::jthread acceptorThread_;
};
