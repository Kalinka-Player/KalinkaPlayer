#include "LocalHttpServer.h"

#include <boost/beast/core.hpp>
#include <boost/beast/http.hpp>

#include <sys/socket.h>

#include <algorithm>
#include <charconv>
#include <fstream>
#include <iterator>
#include <optional>
#include <stdexcept>
#include <string_view>
#include <utility>

namespace asio = boost::asio;
namespace beast = boost::beast;
namespace http = beast::http;
using tcp = asio::ip::tcp;

namespace {

using Request = http::request<http::empty_body>;
using Response = http::response<http::string_body>;

std::string readFile(const std::string &path) {
  std::ifstream file(path, std::ios::binary);
  if (!file) {
    throw std::runtime_error("cannot read " + path);
  }
  return {std::istreambuf_iterator<char>(file),
          std::istreambuf_iterator<char>()};
}

std::optional<size_t> parseNumber(std::string_view text) {
  size_t value = 0;
  auto [end, error] = std::from_chars(text.data(), text.data() + text.size(), value);
  if (error != std::errc() || end != text.data() + text.size()) {
    return std::nullopt;
  }
  return value;
}

/// The inclusive byte span a `bytes=first-[last]` header asks for, with the
/// end clamped to the file; nullopt when there is no such header.
std::optional<std::pair<size_t, size_t>> requestedRange(std::string_view header,
                                                        size_t size) {
  constexpr std::string_view prefix = "bytes=";
  if (!header.starts_with(prefix)) {
    return std::nullopt;
  }
  header.remove_prefix(prefix.size());
  const size_t dash = header.find('-');
  if (dash == std::string_view::npos) {
    return std::nullopt;
  }
  const auto first = parseNumber(header.substr(0, dash));
  if (!first) {
    return std::nullopt;
  }
  size_t last = size == 0 ? 0 : size - 1;
  if (dash + 1 < header.size()) {
    const auto requestedLast = parseNumber(header.substr(dash + 1));
    if (!requestedLast) {
      return std::nullopt;
    }
    last = std::min(last, *requestedLast);
  }
  return std::make_pair(*first, last);
}

Response respond(const Request &request, const std::string &body) {
  Response response;
  response.version(request.version());
  response.keep_alive(request.keep_alive());
  const std::string_view target = request.target();

  if (target == "/whole") {
    response.result(http::status::ok);
    response.set(http::field::accept_ranges, "none");
    response.body() = body;
  } else if (target == "/ranged") {
    response.set(http::field::accept_ranges, "bytes");
    const auto range = requestedRange(request[http::field::range], body.size());
    if (!range) {
      response.result(http::status::ok);
      response.body() = body;
    } else if (range->first >= body.size()) {
      response.result(http::status::range_not_satisfiable);
      response.set(http::field::content_range,
                   "bytes */" + std::to_string(body.size()));
    } else {
      response.result(http::status::partial_content);
      response.set(http::field::content_range,
                   "bytes " + std::to_string(range->first) + "-" +
                       std::to_string(range->second) + "/" +
                       std::to_string(body.size()));
      response.body() =
          body.substr(range->first, range->second - range->first + 1);
    }
  } else {
    response.result(http::status::not_found);
  }
  response.prepare_payload();
  return response;
}

} // namespace

LocalHttpServer::LocalHttpServer(const std::string &filePath)
    : body_(readFile(filePath)),
      acceptor_(io_, tcp::endpoint(asio::ip::make_address("127.0.0.1"), 0)) {
  acceptorThread_ = std::jthread([this] { acceptConnections(); });
}

LocalHttpServer::~LocalHttpServer() {
  {
    std::lock_guard lock(mutex_);
    stopping_ = true;
  }
  // A blocking accept only returns for a connection, so make one.
  boost::system::error_code ignored;
  tcp::socket waker(io_);
  waker.connect(acceptor_.local_endpoint(), ignored);
  acceptorThread_.join();

  std::lock_guard lock(mutex_);
  // Shutting the descriptor down wakes a read blocked on it; closing the
  // asio socket from this thread would race the one serving it.
  for (const auto &socket : sockets_) {
    ::shutdown(socket->native_handle(), SHUT_RDWR);
  }
  connections_.clear();
}

std::string LocalHttpServer::url(const std::string &path) const {
  return "http://127.0.0.1:" + std::to_string(acceptor_.local_endpoint().port()) +
         path;
}

void LocalHttpServer::acceptConnections() {
  for (;;) {
    auto socket = std::make_shared<tcp::socket>(io_);
    boost::system::error_code error;
    acceptor_.accept(*socket, error);
    std::lock_guard lock(mutex_);
    if (stopping_ || error) {
      return;
    }
    sockets_.push_back(socket);
    connections_.emplace_back([this, socket] { serve(*socket); });
  }
}

void LocalHttpServer::serve(tcp::socket &socket) const {
  beast::flat_buffer buffer;
  for (;;) {
    Request request;
    beast::error_code error;
    http::read(socket, buffer, request, error);
    if (error) {
      return;
    }
    Response response = respond(request, body_);
    http::write(socket, response, error);
    if (error || !response.keep_alive()) {
      return;
    }
  }
}
