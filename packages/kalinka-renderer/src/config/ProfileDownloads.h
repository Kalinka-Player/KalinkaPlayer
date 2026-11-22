#pragma once
#include "kalinka/renderer/v1/renderer.pb.h"
#include <boost/asio/io_context.hpp>
#include <boost/asio/thread_pool.hpp>

#include <functional>
#include <memory>

/// Admits one profile fetch across Core connections. Called on the I/O thread.
class ProfileDownloads : public std::enable_shared_from_this<ProfileDownloads> {
public:
  explicit ProfileDownloads(boost::asio::io_context &io) : io(io) {}
  using Done = std::function<void(const std::string &, const std::string &)>;
  /// False while another write is fetching a profile. Refuse this whole update.
  [[nodiscard]] bool prepare(const kalinka::renderer::v1::ConfigUpdate &update,
                             Done done);

private:
  boost::asio::io_context &io;
  bool downloading = false;
  boost::asio::thread_pool worker{1};
};
