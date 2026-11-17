#pragma once

#include "dsp/DspProcessor.h"
#include "dsp/dsd/DsdToPcm.h"

#include <optional>
#include <vector>

/**
 * @brief Converts raw DSD to float64 PCM inside DspStage, so the filters'
 * state carries across a gapless DSD album.
 *
 * It takes DSD_U8, one byte per channel per frame with the earliest bit in
 * the MSB, as DsdContainer hands raw DSD. The PCM rate is 4 times the
 * source family's base when the device takes it, else the highest of 8 and 2
 * times it does take; with none of them known, 4 times. The filter is 30 kHz
 * for DSD64 and 50 kHz above. The output is scaled by the DSD gain.
 * @note As DspProcessor: output() may come from any thread.
 */
class DsdToPcmProcessor final : public DspProcessor {
public:
  /**
   * @param device What the device was probed to take; empty when unknown.
   * @param gain The DSD gain, linear.
   */
  DsdToPcmProcessor(std::vector<StreamAudioFormat> device, double gain);

  bool handles(const StreamAudioFormat &format) const override;
  Output output(const StreamAudioFormat &format) const override;
  size_t start(const StreamAudioFormat &format) override;
  size_t history() const override;
  size_t process(const uint8_t *in, uint8_t *out, size_t frames) override;

private:
  const std::vector<StreamAudioFormat> device;
  const double gain;
  std::optional<DsdToPcm> converter;
  unsigned channels = 0;
  std::vector<std::vector<double>> planar;
  std::vector<double *> planes;
  /// The idle pattern for every channel, what silence is fed as.
  std::vector<uint8_t> idle;

  /// The conversion @p format gets.
  DsdToPcm::Spec spec(const StreamAudioFormat &format) const;
};
