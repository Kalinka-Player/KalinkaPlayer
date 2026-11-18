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
 * the MSB, as DsdContainer hands raw DSD. The PCM rate is dsdPcmRate()'s;
 * DSD at a rate the device takes none of its family's is not handled. The
 * filter is the one asked for, else 30 kHz for DSD64 and 50 kHz above. The
 * output is scaled by the DSD gain.
 * @note As DspProcessor: output() may come from any thread.
 */
class DsdToPcmProcessor final : public DspProcessor {
public:
  /**
   * @param device What the device was probed to take; empty when unknown.
   * @param gain The DSD gain, linear.
   * @param multiple The base rate's multiple `dsd.pcm_rate` asks for; none
   * for automatic.
   * @param filter The filter `dsd.filter` asks for; none for automatic.
   */
  DsdToPcmProcessor(std::vector<StreamAudioFormat> device, double gain,
                    std::optional<unsigned> multiple = std::nullopt,
                    std::optional<DsdFilter> filter = std::nullopt);

  bool handles(const StreamAudioFormat &format) const override;
  Output output(const StreamAudioFormat &format) const override;
  size_t start(const StreamAudioFormat &format) override;
  size_t history() const override;
  size_t process(const uint8_t *in, uint8_t *out, size_t frames) override;

private:
  const std::vector<StreamAudioFormat> device;
  const double gain;
  const std::optional<unsigned> multiple;
  const std::optional<DsdFilter> filter;
  std::optional<DsdToPcm> converter;
  unsigned channels = 0;
  std::vector<std::vector<double>> planar;
  std::vector<double *> planes;
  /// The idle pattern for every channel, what silence is fed as.
  std::vector<uint8_t> idle;

  /// The conversion @p format gets; none when the device takes no rate of
  /// its family.
  std::optional<DsdToPcm::Spec> spec(const StreamAudioFormat &format) const;
};
