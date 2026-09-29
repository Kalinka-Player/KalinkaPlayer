#include "LocalHttpServer.h"

#include <boost/beast/core.hpp>
#include <boost/beast/http.hpp>

#include <sys/socket.h>

#include <algorithm>
#include <charconv>
#include <chrono>
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

enum class Delivery {
  Whole,
  /// Not even the headers, then the connection is held open.
  Silent,
  /// The headers and `sentFirst` body bytes, then the connection is held open.
  Stall,
  /// The headers and `sentFirst` body bytes, the rest once release() is called.
  Held,
  /// The headers, then the body a byte every TRICKLE_GAP.
  Trickle,
};

constexpr std::chrono::milliseconds TRICKLE_GAP{250};

struct Reply {
  Response response;
  Delivery delivery = Delivery::Whole;
  size_t sentFirst = 0;
};

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

Response respond(const Request &request, http::status status,
                 std::string body = {}) {
  Response response;
  response.version(request.version());
  response.keep_alive(request.keep_alive());
  response.result(status);
  response.body() = std::move(body);
  response.prepare_payload();
  return response;
}

Response ranged(const Request &request, const std::string &body) {
  const auto range = requestedRange(request[http::field::range], body.size());
  Response response;
  if (!range) {
    response = respond(request, http::status::ok, body);
  } else if (range->first >= body.size()) {
    response = respond(request, http::status::range_not_satisfiable);
    response.set(http::field::content_range,
                 "bytes */" + std::to_string(body.size()));
  } else {
    response =
        respond(request, http::status::partial_content,
                body.substr(range->first, range->second - range->first + 1));
    response.set(http::field::content_range,
                 "bytes " + std::to_string(range->first) + "-" +
                     std::to_string(range->second) + "/" +
                     std::to_string(body.size()));
  }
  response.set(http::field::accept_ranges, "bytes");
  return response;
}

Reply stalled(Response response, size_t sentFirst) {
  return {std::move(response), Delivery::Stall, sentFirst};
}

/// Every route in one place; @p first is whether @p request is the first to
/// its path.
Reply answer(const Request &request, const std::string &body, bool first) {
  const std::string_view target = request.target();
  if (target == "/live" || target == "/live-held") {
    Response response = respond(request, http::status::ok, body);
    response.erase(http::field::content_length);
    response.set(http::field::accept_ranges, "none");
    response.set("X-Kalinka-Live", "1");
    response.keep_alive(false);
    if (target == "/live-held") {
      return {std::move(response), Delivery::Held, 1000};
    }
    response.chunked(true);
    return {std::move(response)};
  }
  if (target == "/ranged") {
    return {ranged(request, body)};
  }
  if (target == "/whole" || target == "/held") {
    Response response = respond(request, http::status::ok, body);
    response.set(http::field::accept_ranges, "none");
    if (target == "/held") {
      return {std::move(response), Delivery::Held, LocalHttpServer::HELD_BYTES};
    }
    return {std::move(response)};
  }
  if (target == "/stall") {
    return stalled(ranged(request, body), LocalHttpServer::STALLED_BODY_BYTES);
  }
  if (target == "/stall-once") {
    Response response = ranged(request, body);
    return first ? stalled(std::move(response),
                           LocalHttpServer::STALLED_BODY_BYTES)
                 : Reply{std::move(response)};
  }
  if (target == "/stall-often") {
    Response response = ranged(request, body);
    return response.body().size() > LocalHttpServer::STALL_OFTEN_BYTES
               ? stalled(std::move(response), LocalHttpServer::STALL_OFTEN_BYTES)
               : Reply{std::move(response)};
  }
  if (target == "/silent-once") {
    return {ranged(request, body), first ? Delivery::Silent : Delivery::Whole};
  }
  if (target == "/forgets-ranges") {
    return first ? stalled(ranged(request, body),
                           LocalHttpServer::STALL_OFTEN_BYTES)
                 : Reply{respond(request, http::status::ok, body)};
  }
  if (target == "/fail-once") {
    return {first ? respond(request, http::status::service_unavailable)
                  : ranged(request, body)};
  }
  if (target == "/moved") {
    Response response = respond(request, http::status::found);
    response.set(http::field::location, "/ranged");
    return {std::move(response)};
  }
  if (target == "/slow-missing") {
    return {respond(request, http::status::not_found,
                    std::string(LocalHttpServer::TRICKLE_TIME / TRICKLE_GAP,
                                '.')),
            Delivery::Trickle};
  }
  return {respond(request, http::status::not_found)};
}

void writeHead(tcp::socket &socket, Response &response, size_t bodyBytes,
               beast::error_code &error) {
  http::response_serializer<http::string_body> serializer(response);
  http::write_header(socket, serializer, error);
  if (!error) {
    const std::string &body = response.body();
    asio::write(socket,
                asio::buffer(body.data(), std::min(bodyBytes, body.size())),
                error);
  }
}

// Silent but open until the client hangs up or shutdown() wakes us.
void holdOpen(tcp::socket &socket) {
  beast::error_code ignored;
  socket.wait(tcp::socket::wait_read, ignored);
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
  releasedOrStopping_.notify_all();
  // A blocking accept only returns for a connection, so make one.
  boost::system::error_code ignored;
  tcp::socket waker(io_);
  waker.connect(acceptor_.local_endpoint(), ignored);
  acceptorThread_.join();

  // Unlocked: the acceptor is joined, and a held response needs mutex_ to end.
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

void LocalHttpServer::serve(tcp::socket &socket) {
  beast::flat_buffer buffer;
  for (;;) {
    Request request;
    beast::error_code error;
    http::read(socket, buffer, request, error);
    if (error) {
      return;
    }
    Reply reply = answer(request, body_, firstRequestTo(request.target()));
    Response &response = reply.response;
    switch (reply.delivery) {
    case Delivery::Whole:
      http::write(socket, response, error);
      break;
    case Delivery::Silent:
      holdOpen(socket);
      return;
    case Delivery::Stall:
      writeHead(socket, response, reply.sentFirst, error);
      holdOpen(socket);
      return;
    case Delivery::Held: {
      writeHead(socket, response, reply.sentFirst, error);
      if (error || !waitForRelease()) {
        return;
      }
      const std::string_view body = response.body();
      const std::string_view rest =
          body.substr(std::min(reply.sentFirst, body.size()));
      asio::write(socket, asio::buffer(rest.data(), rest.size()), error);
      break;
    }
    case Delivery::Trickle:
      writeHead(socket, response, 0, error);
      for (size_t sent = 0; !error && sent < response.body().size(); ++sent) {
        std::this_thread::sleep_for(TRICKLE_GAP);
        asio::write(socket, asio::buffer(response.body().data() + sent, 1),
                    error);
      }
      break;
    }
    if (error || !response.keep_alive()) {
      socket.shutdown(tcp::socket::shutdown_both, error);
      socket.close(error);
      return;
    }
  }
}

void LocalHttpServer::release() {
  {
    std::lock_guard lock(mutex_);
    released_ = true;
  }
  releasedOrStopping_.notify_all();
}

bool LocalHttpServer::waitForRelease() {
  std::unique_lock lock(mutex_);
  releasedOrStopping_.wait(lock, [this] { return released_ || stopping_; });
  return !stopping_;
}

size_t LocalHttpServer::requestsTo(std::string_view path) const {
  std::lock_guard lock(mutex_);
  const auto counted = requests_.find(path);
  return counted == requests_.end() ? 0 : counted->second;
}

bool LocalHttpServer::firstRequestTo(std::string_view target) {
  std::lock_guard lock(mutex_);
  return ++requests_[std::string(target)] == 1;
}
