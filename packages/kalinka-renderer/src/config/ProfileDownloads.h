#pragma once
#include "kalinka/renderer/v1/renderer.pb.h"
#include <boost/asio/io_context.hpp>
#include <boost/asio/thread_pool.hpp>

#include <deque>
#include <functional>
#include <memory>

/// Serialises config writes across Core connections while a profile is fetched.
class ProfileDownloads : public std::enable_shared_from_this<ProfileDownloads> {
public:
  explicit ProfileDownloads(boost::asio::io_context &io) : io(io) {}
  using Done = std::function<void(const std::string &, const std::string &)>;
  void prepare(const kalinka::renderer::v1::ConfigUpdate &update, Done done);

private:
  struct Request {
    std::string id, url, error;
    Done done;
  };
  void next();
  boost::asio::io_context &io;
  std::deque<Request> queue;
  boost::asio::thread_pool worker{1};
};
