cmake_minimum_required(VERSION 3.16)
project(kalinka-renderer LANGUAGES CXX)

# Packaging passes -DKALINKA_VERSION on each configure; cached, it would
# outlive the commit it named, so a plain build asks git instead.
set(renderer_version "${KALINKA_VERSION}")
unset(KALINKA_VERSION CACHE)
if(NOT renderer_version)
  execute_process(
    COMMAND bash -c ". scripts/version.sh && renderer_version"
    WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
    OUTPUT_VARIABLE renderer_version
    OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE version_result
    ERROR_QUIET)
  if(version_result EQUAL 0 AND renderer_version)
    execute_process(
      COMMAND git rev-parse --path-format=absolute --git-path logs/HEAD
      WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
      OUTPUT_VARIABLE head_log
      OUTPUT_STRIP_TRAILING_WHITESPACE
      ERROR_QUIET)
    if(EXISTS "${head_log}")
      # Re-derive after every commit or checkout, not just the first configure.
      set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${head_log}")
    endif()
  else()
    # Unparseable, so a Core never takes it for a release to upgrade from.
    set(renderer_version "unknown")
  endif()
endif()
message(STATUS "kalinka-renderer version: ${renderer_version}")
# Only these sources, so a new version recompiles two files, not the targets.
set_source_files_properties(src/main.cpp src/net/ProtocolSession.cpp PROPERTIES
  COMPILE_DEFINITIONS KALINKA_RENDERER_VERSION="${renderer_version}")

set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Protobuf's package config includes its transitive dependencies, such as Abseil.
find_package(Protobuf CONFIG QUIET)
if(NOT Protobuf_FOUND)
  find_package(Protobuf MODULE REQUIRED)
endif()
# CONFIG: use Boost's own package config (the FindBoost module is removed in
# new CMake, policy CMP0167). Header-only use: asio + beast.
find_package(Boost CONFIG REQUIRED)
find_package(spdlog REQUIRED)
find_package(Threads REQUIRED)
find_package(ALSA REQUIRED)
find_package(OpenSSL REQUIRED COMPONENTS Crypto)
find_package(CURL 7.85 REQUIRED) # CURLOPT_PROTOCOLS_STR for profile downloads
find_package(PkgConfig REQUIRED)
pkg_check_modules(FLACPP REQUIRED IMPORTED_TARGET flac++)
pkg_check_modules(VORBISFILE REQUIRED IMPORTED_TARGET vorbisfile)
pkg_check_modules(XML2 REQUIRED IMPORTED_TARGET libxml-2.0)
find_path(SOXR_INCLUDE_DIR soxr.h REQUIRED)
find_library(SOXR_LIBRARY NAMES soxr REQUIRED)
# Debian 13's 1.2.0 is the floor the DSP kernels are written against.
find_package(hwy 1.2 CONFIG REQUIRED)
# faad2 ships a pkg-config file only from 2.11; Debian bookworm has 2.10.
find_path(FAAD2_INCLUDE_DIR neaacdec.h)
find_library(FAAD2_LIBRARY faad)
if(NOT FAAD2_INCLUDE_DIR OR NOT FAAD2_LIBRARY)
  message(FATAL_ERROR "faad2 not found: install libfaad-dev or faad2-devel")
endif()
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
  COMMAND protobuf::protoc
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
  src/native_player/AacDecoder.cpp
  src/native_player/AdtsStreamDecoder.cpp
  src/native_player/AlacDecoder.cpp
  src/native_player/AlsaVolumeControl.cpp
  src/native_player/AudioGraphHttpStream.cpp
  src/native_player/AudioGraphNode.cpp
  src/native_player/AudioPlayer.cpp
  src/native_player/PlaybackEvents.cpp
  src/native_player/AudioSampleFormat.cpp
  src/native_player/AudioStreamSwitcher.cpp
  src/native_player/ByteRing.cpp
  src/native_player/BufferedStreamDecoder.cpp
  src/native_player/Config.cpp
  src/native_player/ContainerStreamDecoder.cpp
  src/native_player/DsdBlockKernels.cpp
  src/native_player/DsdContainer.cpp
  src/native_player/DsdFormat.cpp
  src/native_player/DashStreamDecoder.cpp
  src/native_player/DashManifest.cpp
  src/native_player/FragmentedMp4.cpp
  src/native_player/FlacFrameDecoder.cpp
  src/native_player/FlacStreamDecoder.cpp
  src/native_player/Log.cpp
  src/native_player/LivePcmStreamDecoder.cpp
  src/native_player/LivePcmClock.cpp
  src/native_player/Mp4.cpp
  src/native_player/Mp4SampleTable.cpp
  src/native_player/Mp4StreamDecoder.cpp
  src/native_player/Mp3StreamDecoder.cpp
  src/native_player/PerfMon.cpp
  src/native_player/PlaybackPcm.cpp
  src/native_player/SampleDecoder.cpp
  src/native_player/SourceStreamDecoder.cpp
  src/native_player/StateMonitor.cpp
  src/native_player/StreamState.cpp
  src/native_player/TrackDecoder.cpp
  src/native_player/VorbisStreamDecoder.cpp
  src/native_player/WavContainer.cpp
  src/native_player/dsp/peq/RoomEq.cpp
  src/native_player/dsp/peq/ParallelPeq.cpp
  src/native_player/dsp/peq/PeqKernels.cpp
  src/native_player/dsp/DelayLine.cpp
  src/native_player/dsp/DspStage.cpp
  src/native_player/dsp/dsd/ByteHistory.cpp
  src/native_player/dsp/dsd/DsdKernels.cpp
  src/native_player/dsp/dsd/DsdToPcm.cpp
  src/native_player/dsp/dsd/DsdToPcmProcessor.cpp
  src/native_player/dsp/dsd/DsdFilterSetting.cpp
  src/native_player/dsp/dsd/DsdGainSetting.cpp
  src/native_player/dsp/dsd/DsdPcmRate.cpp
  src/native_player/dsp/dsd/Stage1.cpp
  src/native_player/dsp/fir/FirKernels.cpp
  src/native_player/dsp/fir/HalfBandDecimator.cpp
  src/native_player/dsp/fir/SymmetricFir.cpp
  src/native_player/dsp/output/GainRamp.cpp
  src/native_player/dsp/output/OutputFade.cpp
  src/native_player/dsp/output/OutputFormat.cpp
  src/native_player/dsp/output/OutputKernels.cpp
  src/native_player/dsp/output/OutputStage.cpp
  src/native_player/dsp/output/PackedFade.cpp
  src/native_player/dsp/output/PlanarPcm.cpp
  src/native_player/dsp/output/Quantiser.cpp
  src/native_player/dsp/output/TpdfDither.cpp
)
if(CMAKE_SYSTEM_PROCESSOR MATCHES "^(aarch64|arm64)$")
  enable_language(ASM)
  target_sources(kalinka_native_player PRIVATE
    src/native_player/dsp/peq/ParallelPeqAarch64.S)
endif()
# The source directory too: Highway's dispatch includes each kernel source
# again by its path from there, once per instruction set.
target_include_directories(kalinka_native_player PRIVATE
  ${FAAD2_INCLUDE_DIR}
  ${CMAKE_CURRENT_SOURCE_DIR}/src/native_player
)
target_include_directories(kalinka_native_player PUBLIC ${SOXR_INCLUDE_DIR})
target_link_libraries(kalinka_native_player PUBLIC
  ALSA::ALSA
  CURL::libcurl
  PkgConfig::CURLPP
  PkgConfig::FLACPP
  PkgConfig::VORBISFILE
  PkgConfig::XML2
  ${FAAD2_LIBRARY}
  ${SOXR_LIBRARY}
  Boost::headers
  spdlog::spdlog
  Threads::Threads
  hwy::hwy
)

# --- kalinka_renderer_core: everything that decides --------------------------
# The session, config, upgrade, capability and protocol planes — no live
# sockets, discovery or daemon plumbing, so unit tests link exactly what they
# exercise.
add_library(kalinka_renderer_core STATIC
  src/capabilities/CapabilityService.cpp
  src/config/DspProfile.cpp
  src/config/DspProfileCache.cpp
  src/config/ProfileDownloads.cpp
  src/config/DspSettings.cpp
  src/config/SettingsPersistence.cpp
  src/config/ConfigService.cpp
  src/net/ProtocolSession.cpp
  src/session/Session.cpp
  src/session/SessionManager.cpp
  src/upgrade/TriggerFileUpgradeService.cpp
)
target_include_directories(kalinka_renderer_core PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/src
)
target_link_libraries(kalinka_renderer_core PUBLIC
  kalinka_renderer_proto
  OpenSSL::Crypto
  CURL::libcurl
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
target_include_directories(kalinka-renderer SYSTEM PRIVATE
  ${CMAKE_CURRENT_SOURCE_DIR}/third_party/mdns
)
target_link_libraries(kalinka-renderer PRIVATE
  kalinka_renderer_core
  kalinka_native_player
)

include(GNUInstallDirs)
install(TARGETS kalinka-renderer RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})

# --- fuzzers (optional: Clang's libFuzzer, for parsers fed from the network) -
option(KALINKA_BUILD_FUZZERS "Build libFuzzer targets with ASan and UBSan" OFF)
if(KALINKA_BUILD_FUZZERS)
  if(NOT CMAKE_CXX_COMPILER_ID MATCHES "Clang")
    message(FATAL_ERROR "KALINKA_BUILD_FUZZERS needs Clang for libFuzzer")
  endif()
  add_executable(DspProfile_fuzz tests/fuzz/DspProfile_fuzz.cpp src/config/DspProfile.cpp)
  target_include_directories(DspProfile_fuzz PRIVATE src)
  target_link_libraries(DspProfile_fuzz PRIVATE kalinka_renderer_proto)
  foreach(name AlacFrames FragmentedMp4Init FragmentedMp4Segments Mp4Movie)
    add_executable(${name}_fuzz
      tests/fuzz/${name}_fuzz.cpp
      src/native_player/AlacDecoder.cpp
      src/native_player/AudioSampleFormat.cpp
      src/native_player/FragmentedMp4.cpp
      src/native_player/Mp4.cpp
      src/native_player/Mp4SampleTable.cpp
    )
    target_include_directories(${name}_fuzz PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/src/native_player
    )
    target_compile_definitions(${name}_fuzz PRIVATE
      KALINKA_DASH_INIT_SEGMENT="${CMAKE_CURRENT_SOURCE_DIR}/tests/data/dash/16/init-stream0.m4s"
    )
  endforeach()
  foreach(name AlacFrames FragmentedMp4Init FragmentedMp4Segments Mp4Movie DspProfile)
    target_compile_options(${name}_fuzz PRIVATE
      -fsanitize=fuzzer,address,undefined -fno-sanitize-recover=undefined
    )
    target_link_options(${name}_fuzz PRIVATE
      -fsanitize=fuzzer,address,undefined
    )
  endforeach()
endif()

# --- DSP test harness: signals, analysers and DSD fixtures ------------------
# Shared by the unit tests and the DSP benchmark; never shipped.
add_library(kalinka_dsp_harness STATIC EXCLUDE_FROM_ALL
  tests/dsp/Analysis.cpp
  tests/dsp/ReferenceFilters.cpp
  tests/dsp/SigmaDelta.cpp
  tests/dsp/Signals.cpp
)
target_include_directories(kalinka_dsp_harness PUBLIC
  ${CMAKE_CURRENT_SOURCE_DIR}/tests/dsp
)

# --- kalinka-dsp-bench (optional): per-kernel cost against real time ---------
option(KALINKA_BUILD_BENCHMARKS "Build kalinka-dsp-bench" OFF)
if(KALINKA_BUILD_BENCHMARKS)
  add_executable(kalinka-dsp-bench
    bench/DsdToPcm.cpp
    bench/JoinError.cpp
    bench/Kernels.cpp
    bench/Playback.cpp
    bench/SimdScalar.cpp
    bench/main.cpp
  )
  target_include_directories(kalinka-dsp-bench PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/bench
    ${CMAKE_CURRENT_SOURCE_DIR}/src/native_player
  )
  target_link_libraries(kalinka-dsp-bench PRIVATE
    kalinka_native_player
    kalinka_dsp_harness
  )
  string(TOUPPER "${CMAKE_BUILD_TYPE}" bench_config)
  string(STRIP "${CMAKE_CXX_FLAGS} ${CMAKE_CXX_FLAGS_${bench_config}}" bench_flags)
  target_compile_definitions(kalinka-dsp-bench PRIVATE
    KALINKA_BENCH_FLAGS="${bench_flags}")

  # std::experimental::simd sizes at compile time: one build per instruction set.
  function(kalinka_bench_experimental suffix label)
    add_library(kalinka_bench_simd_${suffix} OBJECT bench/SimdExperimental.cpp)
    target_include_directories(kalinka_bench_simd_${suffix} PRIVATE
      ${CMAKE_CURRENT_SOURCE_DIR}/bench)
    target_compile_definitions(kalinka_bench_simd_${suffix} PRIVATE
      KALINKA_BENCH_EXPERIMENTAL=experimental${suffix}Kernels
      "KALINKA_BENCH_EXPERIMENTAL_NAME=\"std::experimental::simd, ${label}\"")
    target_compile_options(kalinka_bench_simd_${suffix} PRIVATE ${ARGN})
    target_sources(kalinka-dsp-bench PRIVATE
      $<TARGET_OBJECTS:kalinka_bench_simd_${suffix}>)
  endfunction()
  if(CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|AMD64")
    target_sources(kalinka-dsp-bench PRIVATE bench/SimdX86.cpp)
    kalinka_bench_experimental(Sse2 "SSE2")
    kalinka_bench_experimental(Avx2 "AVX2+FMA" -mavx2 -mfma)
  elseif(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm64")
    target_sources(kalinka-dsp-bench PRIVATE bench/SimdNeon.cpp)
    kalinka_bench_experimental(Neon "NEON")
  endif()
  target_sources(kalinka-dsp-bench PRIVATE bench/SimdHighway.cpp)
endif()

# --- unit tests (optional: built only when GoogleTest is installed) ----------
find_package(GTest CONFIG)
if(GTest_FOUND)
  enable_testing()
  add_executable(kalinka-renderer-tests
    src/config/RendererName.cpp
    src/discovery/CoreGrouper.cpp
    src/discovery/DiscoveryCache.cpp
    src/discovery/SocketPlan.cpp
    src/net/WsTransport.cpp
    src/player/NativePlayer.cpp
    src/player/StateTranslator.cpp
    tests/test_audio_player.cpp
    tests/test_capability_service.cpp
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
    tests/native_player/AacDecoder_test.cpp
    tests/native_player/AdtsStreamDecoder_test.cpp
    tests/native_player/AlacDecoder_test.cpp
    tests/native_player/AlsaAudioEmitter_test.cpp
    tests/native_player/AlsaDeviceEnumeration_test.cpp
    tests/native_player/AlsaVolumeControl_test.cpp
    tests/native_player/AudioGraphNode_test.cpp
    tests/native_player/AudioGraphHttpStream_test.cpp
    tests/native_player/AudioInfo_test.cpp
    tests/native_player/AudioPlayer_test.cpp
    tests/native_player/AudioSampleFormat_test.cpp
    tests/native_player/AudioStreamSwitcher_test.cpp
    tests/native_player/BitPerfectOutput_test.cpp
    tests/native_player/ByteRing_test.cpp
    tests/native_player/BufferedStreamDecoder_test.cpp
    tests/native_player/ContainerStreamDecoder_test.cpp
    tests/native_player/DsdBlockKernels_test.cpp
    tests/native_player/DsdContainer_test.cpp
    tests/native_player/DsdPcmPlayback_test.cpp
    tests/native_player/DashStreamDecoder_test.cpp
    tests/native_player/DashFormat_test.cpp
    tests/native_player/FileInputNode_test.cpp
    tests/native_player/FlacStreamDecoder_test.cpp
    tests/native_player/LocalHttpServer.cpp
    tests/native_player/Log_test.cpp
    tests/native_player/LivePcmStreamDecoder_test.cpp
    tests/native_player/Mp3StreamDecoder_test.cpp
    tests/native_player/Mp4Format_test.cpp
    tests/native_player/Mp4StreamDecoder_test.cpp
    tests/native_player/PlaybackPcm_test.cpp
    tests/native_player/SineWaveNode_test.cpp
    tests/native_player/SourceStreamDecoder_test.cpp
    tests/native_player/StateMonitor_test.cpp
    tests/native_player/TestEnvironment.cpp
    tests/native_player/Utils_test.cpp
    tests/native_player/VorbisStreamDecoder_test.cpp
    tests/native_player/WavContainer_test.cpp
    tests/native_player/integration_test.cpp
    tests/native_player/dsp/RoomEq_test.cpp
    tests/native_player/dsp/DspStage_test.cpp
    tests/native_player/dsp/DspStageConversion_test.cpp
    tests/native_player/dsp/dsd/ByteHistory_test.cpp
    tests/native_player/dsp/dsd/DsdToPcm_test.cpp
    tests/native_player/dsp/dsd/DsdToPcmProcessor_test.cpp
    tests/native_player/dsp/dsd/DsdFilterSetting_test.cpp
    tests/native_player/dsp/dsd/DsdGainSetting_test.cpp
    tests/native_player/dsp/dsd/DsdPcmRate_test.cpp
    tests/native_player/dsp/dsd/Stage1_test.cpp
    tests/native_player/dsp/fir/FirFilters_test.cpp
    tests/native_player/dsp/output/GainRamp_test.cpp
    tests/native_player/dsp/output/OutputFormat_test.cpp
    tests/native_player/dsp/output/OutputKernels_test.cpp
    tests/native_player/dsp/output/OutputStage_test.cpp
    tests/native_player/dsp/output/PackedFade_test.cpp
    tests/native_player/dsp/output/PlanarPcm_test.cpp
    tests/native_player/dsp/output/Quantiser_test.cpp
    tests/native_player/PlaybackEvents_test.cpp
    tests/native_player/dsp/output/TpdfDither_test.cpp
    tests/dsp/Harness_test.cpp
  )
  target_link_libraries(kalinka-renderer-tests PRIVATE
    kalinka_renderer_core
    kalinka_native_player
    kalinka_dsp_harness
    GTest::gmock
    GTest::gtest_main
  )
  # A device playing in real time, which ALSA loads for tests that starve one.
  add_library(kalinka-paced-pcm MODULE tests/native_player/PacedPcmPlugin.cpp)
  target_link_libraries(kalinka-paced-pcm PRIVATE ALSA::ALSA)
  # ALSA's headers name a loadable plugin's version symbol only under PIC.
  target_compile_definitions(kalinka-paced-pcm PRIVATE PIC)
  add_dependencies(kalinka-renderer-tests kalinka-paced-pcm)
  target_compile_definitions(kalinka-renderer-tests PRIVATE
    KALINKA_TEST_DATA_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/data"
    KALINKA_TEST_FILES_DIR="${CMAKE_CURRENT_SOURCE_DIR}/tests/native_player/files"
    KALINKA_PACED_PCM="$<TARGET_FILE:kalinka-paced-pcm>"
  )
  target_include_directories(kalinka-renderer-tests PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/src/native_player
    ${CMAKE_CURRENT_SOURCE_DIR}/tests/native_player
  )
  include(GoogleTest)
  # TIMEOUT: a deadlock regression hangs instead of failing.
  gtest_discover_tests(kalinka-renderer-tests PROPERTIES TIMEOUT 60)
endif()
