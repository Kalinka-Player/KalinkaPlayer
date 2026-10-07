// kalinka-renderer — standalone native renderer for Kalinka Core.
//
// Browses for _kalinkaplayer._tcp Cores (or connects to fixed endpoints given
// with --server), registers with each via the Hello/Welcome handshake, and
// plays for whichever one opens a playback session. SIGINT/SIGTERM shut it
// down gracefully (playback stopped, Goodbye, WebSocket close, joined
// threads).

#include <spdlog/sinks/basic_file_sink.h>
#include <spdlog/sinks/stdout_color_sinks.h>
#include <spdlog/spdlog.h>
#include <unistd.h>

#include <boost/asio.hpp>
#include <cstdlib>
#include <filesystem>
#include <memory>
#include <string>
#include <vector>

#include "Daemon.h"
#include "Identity.h"
#include "RendererServices.h"
#include "upgrade/TriggerFileUpgradeService.h"
#include "config/ConfigService.h"
#include "config/RendererName.h"
#include "discovery/MdnsDiscovery.h"
#include "native_player/Log.h"
#include "net/ConnectionManager.h"
#include "player/NativePlayer.h"
#include "session/SessionManager.h"

namespace asio = boost::asio;

namespace {

struct Options {
  bool daemon = false;
  std::string friendlyName;
  std::string logFile;
  std::vector<CoreEndpoint> staticServers;  // skip discovery when non-empty
  int sessionGraceSeconds = 60;
};

void usage(const char *argv0) {
  std::printf(
      "Usage: %s [options]\n"
      "  --name <name>         Friendly name announced to Cores; overrides\n"
      "                        the configured one (default: the setting, or\n"
      "                        \"Kalinka Renderer on <hostname>\")\n"
      "  --server <host:port>  Connect to a fixed Core instead of mDNS\n"
      "                        discovery; may be repeated\n"
      "  --daemon              Detach and run in the background\n"
      "  --log-file <path>     Log to a file (default when daemonized:\n"
      "                        $KALINKA_PREFIX/var/log/kalinka/renderer.log)\n"
      "  --session-grace <s>   Keep a session this long after its Core\n"
      "                        disconnects before releasing it (default 60)\n"
      "  --help                Show this help\n",
      argv0);
}

bool parseArgs(int argc, char **argv, Options &opts) {
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    auto value = [&]() -> const char * {
      return i + 1 < argc ? argv[++i] : nullptr;
    };
    if (arg == "--daemon") {
      opts.daemon = true;
    } else if (arg == "--name") {
      const char *v = value();
      if (!v) return false;
      opts.friendlyName = v;
    } else if (arg == "--session-grace") {
      const char *v = value();
      if (!v) return false;
      opts.sessionGraceSeconds = std::atoi(v);
      if (opts.sessionGraceSeconds < 0) {
        std::fprintf(stderr, "--session-grace must not be negative\n");
        return false;
      }
    } else if (arg == "--log-file") {
      const char *v = value();
      if (!v) return false;
      opts.logFile = v;
    } else if (arg == "--server") {
      const char *v = value();
      if (!v) return false;
      const std::string hostPort = v;
      const auto colon = hostPort.rfind(':');
      if (colon == std::string::npos || colon == 0 ||
          colon + 1 == hostPort.size()) {
        std::fprintf(stderr, "--server expects host:port, got '%s'\n", v);
        return false;
      }
      const int port = std::atoi(hostPort.c_str() + colon + 1);
      if (port <= 0 || port > 65535) {
        std::fprintf(stderr, "Invalid port in '%s'\n", v);
        return false;
      }
      const std::string host = hostPort.substr(0, colon);
      opts.staticServers.push_back(
          CoreEndpoint{hostPort, host, static_cast<uint16_t>(port), hostPort});
    } else if (arg == "--help" || arg == "-h") {
      usage(argv[0]);
      std::exit(0);
    } else {
      std::fprintf(stderr, "Unknown option '%s'\n", arg.c_str());
      return false;
    }
  }
  return true;
}

void setupLogging(const Options &opts) {
  std::string logFile = opts.logFile;
  if (logFile.empty() && opts.daemon) {
    const char *prefix = std::getenv("KALINKA_PREFIX");
    logFile = (std::filesystem::path(prefix && *prefix ? prefix : "/") /
               "var/log/kalinka/renderer.log")
                  .string();
  }
  if (!logFile.empty()) {
    try {
      std::filesystem::create_directories(
          std::filesystem::path(logFile).parent_path());
      spdlog::set_default_logger(
          spdlog::basic_logger_mt("renderer", logFile));
    } catch (const spdlog::spdlog_ex &e) {
      // Daemonized stderr goes to /dev/null anyway; nothing better to do.
      std::fprintf(stderr, "Could not open log file %s: %s\n", logFile.c_str(),
                   e.what());
    }
  } else {
    spdlog::set_default_logger(spdlog::stdout_color_mt("renderer"));
  }
  applyLogPattern(*spdlog::default_logger(),
                  useJournalFormat(!logFile.empty(), STDOUT_FILENO));
  spdlog::set_level(spdlog::level::info);
  spdlog::flush_on(spdlog::level::info);
}

}  // namespace

int main(int argc, char **argv) {
  Options opts;
  if (!parseArgs(argc, argv, opts)) {
    usage(argv[0]);
    return 2;
  }

  // Detach before any threads or sockets exist.
  if (opts.daemon && !daemonize()) {
    return 1;
  }
  setupLogging(opts);

  const Identity identity = Identity::load();
  auto name = std::make_shared<RendererName>(opts.friendlyName);
  spdlog::info("kalinka-renderer {} starting: '{}' (renderer_id={}, pid={})",
               KALINKA_RENDERER_VERSION, name->value(), identity.rendererId,
               getpid());

  asio::io_context ioc;
  auto player = std::make_shared<NativePlayer>(ioc);
  auto capabilities = std::make_shared<CapabilityService>(player);
  RendererServices services{
      std::make_shared<SessionManager>(
          ioc, std::chrono::seconds(opts.sessionGraceSeconds), player),
      std::make_shared<ConfigService>(
          std::vector<std::shared_ptr<ConfigContributor>>{
              name, player, player->bufferSettings(),
              player->networkSettings()},
          [capabilities] { capabilities->refresh(); }),
      std::make_shared<TriggerFileUpgradeService>(),
      capabilities,
  };
  ConnectionManager manager(ioc, identity, name->value(), services);

  // Discovery callbacks run on the discovery thread; ConnectionManager posts
  // them onto the io_context.
  std::unique_ptr<MdnsDiscovery> discovery;
  if (opts.staticServers.empty()) {
    discovery = std::make_unique<MdnsDiscovery>(
        [&manager](CoreEndpoint ep) { manager.add(std::move(ep)); },
        [&manager](CoreEndpoint ep) { manager.replace(std::move(ep)); },
        [&manager](std::string key) { manager.remove(std::move(key)); });
    if (!discovery->start()) {
      spdlog::error("Discovery unavailable and no --server given; exiting");
      return 1;
    }
  } else {
    for (const auto &ep : opts.staticServers) {
      manager.add(ep);
    }
  }

  asio::signal_set signals(ioc, SIGINT, SIGTERM);
  signals.async_wait(
      [&](const boost::system::error_code &ec, int signal) {
        if (ec) return;
        spdlog::info("Received signal {}, shutting down", signal);
        if (discovery) {
          discovery->stop();  // joins the discovery thread
        }
        // Playback first: it must not outlive the process into the owner
        // grace, and closing while connections are up tells them cleanly.
        services.sessions->shutdown();
        manager.stop();  // Goodbye + close on every connection
      });

  // Runs until the signal handler has fired and every connection has closed
  // (each stop() carries a 2s hard deadline, so this cannot hang).
  ioc.run();

  spdlog::info("kalinka-renderer terminated");
  return 0;
}
