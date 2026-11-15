cmake_minimum_required(VERSION 3.25)
project(kalinka-fft-bench LANGUAGES C CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

option(FFT_BENCH_WITH_FFTW "Benchmark the system FFTW 3 (needs its headers)" ON)
option(FFT_BENCH_WITH_KFR "Fetch and benchmark KFR" ON)
option(FFT_BENCH_WITH_ZLDSP "Fetch and benchmark zldsp_fft, on Google Highway" ON)

include(FetchContent)

FetchContent_Declare(pffft
  URL https://github.com/marton78/pffft/archive/refs/tags/v1.3.0.tar.gz
  URL_HASH SHA256=055058d886e72b45e797179681b0d8b658688a37c1dc6fdbff2094074d9515ac
  SYSTEM)
FetchContent_Declare(pocketfft
  URL https://github.com/mreineck/pocketfft/archive/4d865cba24359b05b37da4e75aee24498c3b78e7.tar.gz
  URL_HASH SHA256=db236e44b1020dd0b415e5040ce10d8be604fefb7ce83ec0b32bc0f78975aa2f
  SYSTEM)
FetchContent_MakeAvailable(pffft pocketfft)

add_executable(kalinka-fft-bench
  src/main.cpp
  src/HeapProbe.cpp
  src/ReferenceFft.cpp
  src/PffftFft.cpp
  src/PocketFft.cpp)
target_compile_options(kalinka-fft-bench PRIVATE -Wall -Wextra)
target_include_directories(kalinka-fft-bench PRIVATE src)
target_include_directories(kalinka-fft-bench SYSTEM PRIVATE ${pocketfft_SOURCE_DIR})
target_link_libraries(kalinka-fft-bench PRIVATE PFFFT)

string(TOUPPER "${CMAKE_BUILD_TYPE}" build_type)
string(STRIP "${CMAKE_C_FLAGS} ${CMAKE_C_FLAGS_${build_type}}" c_flags)
string(STRIP "${CMAKE_CXX_FLAGS} ${CMAKE_CXX_FLAGS_${build_type}}" cxx_flags)
target_compile_definitions(kalinka-fft-bench PRIVATE
  FFT_BENCH_COMPILER="${CMAKE_CXX_COMPILER_ID} ${CMAKE_CXX_COMPILER_VERSION}"
  FFT_BENCH_FLAGS="C: ${c_flags}, C++: ${cxx_flags}"
  FFT_BENCH_PFFFT_REF="v1.3.0"
  FFT_BENCH_POCKETFFT_REF="cpp@4d865cb")

if(FFT_BENCH_WITH_FFTW)
  find_path(FFTW3_INCLUDE_DIR fftw3.h REQUIRED)
  find_library(FFTW3F_LIBRARY fftw3f REQUIRED)
  find_library(FFTW3_LIBRARY fftw3 REQUIRED)
  target_sources(kalinka-fft-bench PRIVATE src/FftwFft.cpp)
  target_include_directories(kalinka-fft-bench SYSTEM PRIVATE ${FFTW3_INCLUDE_DIR})
  target_link_libraries(kalinka-fft-bench PRIVATE ${FFTW3F_LIBRARY} ${FFTW3_LIBRARY})
  target_compile_definitions(kalinka-fft-bench PRIVATE FFT_BENCH_WITH_FFTW)
endif()

if(FFT_BENCH_WITH_KFR)
  FetchContent_Declare(kfr
    URL https://github.com/kfrlib/kfr/archive/refs/tags/7.2.0.tar.gz
    URL_HASH SHA256=4f1603d5b6e609c25d25cff58fe7154a371583fd6d379615b342e44223be0763)
  set(KFR_ENABLE_DSP OFF)
  set(KFR_ENABLE_IO OFF)
  set(KFR_ENABLE_AUDIO OFF)
  set(KFR_INSTALL_HEADERS OFF)
  set(KFR_INSTALL_LIBRARIES OFF)
  set(KFR_INSTALL_LICENSE OFF)
  FetchContent_MakeAvailable(kfr)
  target_sources(kalinka-fft-bench PRIVATE src/KfrFft.cpp)
  target_link_libraries(kalinka-fft-bench PRIVATE kfr kfr_dft)
  # Not SYSTEM in FetchContent_Declare: that would recompile KFR itself.
  get_target_property(kfr_include_dirs kfr INTERFACE_INCLUDE_DIRECTORIES)
  target_include_directories(kalinka-fft-bench SYSTEM PRIVATE ${kfr_include_dirs})
  target_compile_definitions(kalinka-fft-bench PRIVATE FFT_BENCH_WITH_KFR)
endif()

if(FFT_BENCH_WITH_ZLDSP)
  # Highway declares these as plain cache entries, which a normal variable
  # would not override.
  set(HWY_ENABLE_TESTS OFF CACHE BOOL "")
  set(HWY_ENABLE_EXAMPLES OFF CACHE BOOL "")
  set(HWY_ENABLE_CONTRIB OFF CACHE BOOL "")
  set(HWY_ENABLE_INSTALL OFF CACHE BOOL "")
  FetchContent_Declare(highway
    URL https://github.com/google/highway/archive/refs/tags/1.4.0.tar.gz
    URL_HASH SHA256=e72241ac9524bb653ae52ced768b508045d4438726a303f10181a38f764a453c
    SYSTEM)
  FetchContent_Declare(zldsp_fft
    URL https://github.com/ZL-Audio/zldsp_fft/archive/8e9b4c1571d12b36e9c34a30c7a7546c072a8b6b.tar.gz
    URL_HASH SHA256=b470fce9a4be2df9e1afd13cd5d0d51dd6d8dc0249299b015d06cee95fe96a7a)
  FetchContent_MakeAvailable(highway zldsp_fft)
  target_sources(kalinka-fft-bench PRIVATE src/ZldspFft.cpp)
  target_include_directories(kalinka-fft-bench SYSTEM PRIVATE ${zldsp_fft_SOURCE_DIR}/src)
  target_link_libraries(kalinka-fft-bench PRIVATE hwy)
  target_compile_definitions(kalinka-fft-bench PRIVATE
    FFT_BENCH_WITH_ZLDSP
    FFT_BENCH_ZLDSP_REF="8e9b4c1")
endif()
