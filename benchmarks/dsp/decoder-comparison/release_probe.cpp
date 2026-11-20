// The pinned release keeps the measurement helpers private to its test TU.
// Include it here so this reporting tool runs the identical tone, FFT and
// phase-interleaving code, including the long-double arithmetic on each CPU.
#include "native_player/dsp/dsd/DsdToPcm_test.cpp"

#include <array>
#include <fstream>
#include <iomanip>
#include <iostream>

int main() {
  constexpr unsigned base = 44100, dsd = 64, pcm = 4;
  std::array<std::vector<double>, 3> traces;
  std::ofstream chains("chains.csv"), impulses("impulse.csv"),
      tones("platform-tone.csv");
  for (auto *stream : {&chains, &impulses, &tones})
    stream->exceptions(std::ios::badbit | std::ios::failbit);

  chains << "preset,latency_pcm_frames,passband_hz,stage1_taps,halfband_taps,"
            "output_taps\n";
  for (const auto &chain : dsd_filters::kChains) {
    if (chain.base != base || chain.dsdMultiple != dsd ||
        chain.pcmMultiple != pcm)
      continue;
    auto filter = DsdFilter(chain.filter);
    auto settings = spec(base, dsd, pcm, filter);
    DsdToPcm converter(settings);
    traces[chain.filter] = impulseResponse(settings);
    const auto half = dsd_filters::kHalfBand[chain.halfBands[0]].size;
    chains << std::array{"30 kHz", "50 kHz", "Wide"}[chain.filter] << ','
           << converter.latency() << ',' << converter.passbandHz() << ','
           << dsd_filters::kStage1[chain.stage1].size << ',' << 4 * half - 1
           << ','
           << (chain.band < 0 ? 0 : 2 * dsd_filters::kBand[chain.band].size - 1)
           << '\n';
  }
  impulses << "dsd_sample,30_khz,50_khz,wide\n" << std::setprecision(17);
  size_t length =
      std::max({traces[0].size(), traces[1].size(), traces[2].size()});
  for (size_t i = 0; i < length; ++i) {
    impulses << i;
    for (const auto &trace : traces)
      impulses << ',' << (i < trace.size() ? trace[i] : 0.);
    impulses << '\n';
  }

  tones << "dsd_multiple,pcm_hz,filter_khz,input_tone_dbfs,output_tone_dbfs,"
           "input_thdn_db,output_thdn_db,thdn_change_db\n";
  tones << std::fixed << std::setprecision(7);
  for (unsigned multiple : {64u, 128u, 256u}) {
    unsigned rate = multiple == 256 ? 8 : 4;
    auto result = tone(multiple, rate,
                       multiple == 64 ? DsdFilter::Cut30k : DsdFilter::Cut50k);
    tones << multiple << ',' << base * rate << ',' << (multiple == 64 ? 30 : 50)
          << ',' << result.fixture.fundamentalDbfs << ','
          << result.converted.fundamentalDbfs << ',' << result.fixture.thdnDb
          << ',' << result.converted.thdnDb << ','
          << result.converted.thdnDb - result.fixture.thdnDb << '\n';
  }
}
