cmake_minimum_required(VERSION 3.16)
project(kalinka-renderer VERSION 0.3.0 LANGUAGES CXX)

# What the binary announces to a Core and logs at startup. The packaging
# scripts pass the version the package carries, so a build off a release says
# which commit it came from; a plain build is the release being worked towards.
set(KALINKA_VERSION "${PROJECT_VERSION}" CACHE STRING
    "Version the renderer reports")

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Protobuf REQUIRED)
# CONFIG: use Boost's own package config (the FindBoost module is removed in
# new CMake, policy CMP0167). Header-only use: asio + beast.
find_package(Boost CONFIG REQUIRED)
find_package(spdlog REQUIRED)
find_package(Threads REQUIRED)
find_package(ALSA REQUIRED)
find_package(CURL REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(FLACPP REQUIRED IMPORTED_TARGET flac++)
pkg_check_modules(CURLPP REQUIRED IMPORTED_TARGET curlpp)
# Debian's curlpp.pc emits a relative "-Iinclude" (empty ${prefix}), which
# CMake rejects on the imported target. The headers are in the default search
# path there, so keeping only absolute entries loses nothing.
get_target_property(_curlpp_incs PkgConfig::CURLPP INTERFACE_INCLUDE_DIRECTORIES)
if(_curlpp_incs)
  list(FILTER _curlpp_incs INCLUDE REGEX "^/")
  set_target_properties(PkgConfig::CURLPP PROPERTIES
    INTERFACE_INCLUDE_DIRECTORIES "${_curlpp_incs}")
endif()

# mDNS/DNS-SD: vendored public-domain single header (third_party/mdns).

# --- kalinka_renderer_proto: generated protobuf-lite C++ ---------------------
set(PROTO_FILE ${CMAKE_CURRENT_SOURCE_DIR}/proto/kalinka/renderer/v1/renderer.proto)
set(PROTO_GEN_DIR ${CMAKE_CURRENT_BINARY_DIR}/proto_gen)
file(MAKE_DIRECTORY ${PROTO_GEN_DIR})

add_custom_command(
  OUTPUT ${PROTO_GEN_DIR}/kalinka/renderer/v1/renderer.pb.cc
         ${PROTO_GEN_DIR}/kalinka/renderer/v1/renderer.pb.h
  COMMAND ${Protobuf_PROTOC_EXECUTABLE}
          -I ${CMAKE_CURRENT_SOURCE_DIR}/proto
          --cpp_out=${PROTO_GEN_DIR}
          ${PROTO_FILE}
  DEPENDS ${PROTO_FILE}
  COMMENT "Generating C++ protobuf sources from renderer.proto"
)

add_library(kalinka_renderer_proto STATIC
  ${PROTO_GEN_DIR}/kalinka/renderer/v1/renderer.pb.cc
)
target_include_directories(kalinka_renderer_proto SYSTEM PUBLIC ${PROTO_GEN_DIR})
target_link_libraries(kalinka_renderer_proto PUBLIC protobuf::libprotobuf-lite)

# --- kalinka_native_player: the audio graph ----------------------------------
# The only copy: the server's src/native_player (which this started from) was
# removed when playback moved to renderers. Deltas from the original Python-
# extension build are marked "Renderer delta".
add_library(kalinka_native_player STATIC
  src/native_player/AlsaAudioEmitter.cpp
  src/native_player/AlsaDeviceEnumeration.cpp
  src/native_player/AlsaVolumeControl.cpp
  src/native_player/AudioGraphHttpStream.cpp
  src/native_player/AudioGraphNode.cpp
  src/native_player/AudioPlayer.cpp
  src/native_player/AudioSampleFormat.cpp
  src/native_player/AudioStreamSwitcher.cpp
  src/native_player/Buffer.cpp
  src/native_player/Config.cpp
  src/native_player/FlacStreamDecoder.cpp
  src/native_player/Log.cpp
  src/native_player/Mp3StreamDecoder.cpp
  src/native_player/PerfMon.cpp
  src/native_player/StateMonitor.cpp
  src/native_player/StreamState.cpp
)
target_link_libraries(kalinka_native_player PUBLIC
  ALSA::ALSA
  CURL::libcurl
  PkgConfig::CURLPP
  PkgConfig::FLACPP
  Boost::headers
  spdlog::spdlog
  Threads::Threads
)

# --- kalinka_renderer_core: everything that decides --------------------------
# The session, config, upgrade and protocol planes — no live sockets,
# discovery or daemon plumbing, so unit tests link exactly what they exercise.
add_library(kalinka_renderer_core STATIC
  src/config/ConfigService.cpp
  src/net/ProtocolSession.cpp
  src/session/Session.cpp
  src/session/SessionManager.cpp
  src/upgrade/TriggerFileUpgradeService.cpp
)
target_compile_definitions(kalinka_renderer_core PRIVATE
  KALINKA_RENDERER_VERSION="${KALINKA_VERSION}"
)
target_include_directories(kalinka_renderer_core PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/src
)
target_link_libraries(kalinka_renderer_core PUBLIC
  kalinka_renderer_proto
  Boost::headers
  spdlog::spdlog
  Threads::Threads
)

# --- kalinka-renderer executable ---------------------------------------------
add_executable(kalinka-renderer
  src/main.cpp
  src/Daemon.cpp
  src/Identity.cpp
  src/config/RendererName.cpp
  src/config/SettingsPersistence.cpp
  src/discovery/CoreGrouper.cpp
  src/discovery/DiscoveryCache.cpp
  src/discovery/MdnsDiscovery.cpp
  src/discovery/SocketPlan.cpp
  src/net/ConnectionManager.cpp
  src/net/CoreConnection.cpp
  src/net/WsTransport.cpp
  src/player/NativePlayer.cpp
  src/player/StateTranslator.cpp
  src/player/StubPlayer.cpp
)
target_compile_definitions(kalinka-renderer PRIVATE
  KALINKA_RENDERER_VERSION="${KALINKA_VERSION}"
)
target_include_directories(kalinka-renderer SYSTEM PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/mdns
)
target_link_libraries(kalinka-renderer PRIVATE
  kalinka_renderer_core
  kalinka_native_player
)

include(GNUInstallDirs)
install(TARGETS kalinka-renderer RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})

# --- unit tests (optional: built only when GoogleTest is installed) ----------
find_package(GTest CONFIG)
if(GTest_FOUND)
  enable_testing()
  add_executable(kalinka-renderer-tests
    src/config/RendererName.cpp
    src/config/SettingsPersistence.cpp
    src/discovery/CoreGrouper.cpp
    src/discovery/DiscoveryCache.cpp
    src/discovery/SocketPlan.cpp
    src/net/WsTransport.cpp
    src/player/NativePlayer.cpp
    src/player/StateTranslator.cpp
    tests/test_alsa_device_naming.cpp
    tests/test_audio_player.cpp
    tests/test_config_service.cpp
    tests/test_core_grouper.cpp
    tests/test_decoder_start_offset.cpp
    tests/test_discovery_cache.cpp
    tests/test_native_player_settings.cpp
    tests/test_protocol_session.cpp
    tests/test_upgrade_service.cpp
    tests/test_renderer_name.cpp
    tests/test_session.cpp
    tests/test_session_manager.cpp
    tests/test_socket_plan.cpp
    tests/test_state_translator.cpp
    tests/test_stream_identity.cpp
    tests/test_switcher_concurrency.cpp
    tests/test_volume_monitor_lifetime.cpp
    tests/test_ws_transport.cpp
    tests/native_player/AlsaAudioEmitter_test.cpp
    tests/native_player/AudioGraphHttpStream_test.cpp
    tests/native_player/AudioInfo_test.cpp
    tests/native_player/AudioPlayer_test.cpp
    tests/native_player/AudioSampleFormat_test.cpp
    tests/native_player/AudioStreamSwitcher_test.cpp
    tests/native_player/Buffer_test.cpp
    tests/native_player/FileInputNode_test.cpp
    tests/native_player/FlacStreamDecoder_test.cpp
    tests/native_player/LocalHttpServer.cpp
    tests/native_player/Log_test.cpp
    tests/native_player/Mp3StreamDecoder_test.cpp
    tests/native_player/SineWaveNode_test.cpp
    tests/native_player/StateMonitor_test.cpp
    tests/native_player/Utils_test.cpp
    tests/native_player/integration_test.cpp
  )
  target_link_libraries(kalinka-renderer-tests PRIVATE
    kalinka_renderer_core
    kalinka_native_player
    GTest::gmock
    GTest::gtest_main
  )
  target_compile_definitions(kalinka-renderer-tests PRIVATE
    KALINKA_TEST_DATA_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/data"
    KALINKA_TEST_FILES_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/native_player/files"
  )
  target_include_directories(kalinka-renderer-tests PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src/native_player
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/native_player
  )
  include(GoogleTest)
  # TIMEOUT: a deadlock regression hangs instead of failing.
  gtest_discover_tests(kalinka-renderer-tests PROPERTIES TIMEOUT 60)
endif()
