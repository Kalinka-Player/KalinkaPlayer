#pragma once

#include "AudioGraphNode.h"
#include "dsp/output/GainControl.h"
#include "dsp/output/GainRamp.h"
#include "dsp/output/OutputFormat.h"
#include "dsp/output/Quantiser.h"

#include <atomic>
#include <memory>
#include <mutex>
#include <optional>
#include <random>
#include <vector>

/**
 * @brief The end of the DSP, between the stage and the sink: software volume,
 * TPDF dither, and the integer format the device takes.
 *
 * It presents each PCM source in the format chooseOutput() picks for the
 * device, and passes everything else on as upstream has it: states,
 * positions, seeks and source changes. At unity gain the samples are left
 * alone, put into a wider container only where the device needs one; in the
 * source's own format they are upstream's very bytes. Any other gain ramps
 * over 25 ms and goes through the Quantiser. It settles at once after a seek
 * or once nothing plays, with no audio to click against. DSD and DoP pass
 * untouched, and refuse any gain but unity.
 *
 * @note read(), the waits and the seeks belong to the one thread that pulls
 * audio. setGain() may come from any thread.
 */
class OutputStage : public AudioGraphOutputNode,
                    public AudioGraphInputNode,
                    public GainControl {
public:
  /// @param device What the device was probed to take; empty when unknown.
  explicit OutputStage(std::vector<StreamAudioFormat> device,
                       uint64_t seed = std::random_device{}());
  ~OutputStage() override;

  void connectTo(std::shared_ptr<AudioGraphOutputNode> source) override;
  void disconnect(std::shared_ptr<AudioGraphOutputNode> source) override;

  size_t read(void *data, size_t size) override;
  size_t waitForData(std::stop_token stopToken = std::stop_token(),
                     size_t size = 1) override;
  size_t waitForDataFor(std::stop_token stopToken,
                        std::chrono::milliseconds timeout,
                        size_t size) override;
  size_t seekTo(size_t absolutePosition) override;
  size_t seekStream(StreamId stream, size_t position) override;
  std::optional<long> streamReadPosition() const override;
  std::optional<uint64_t> liveSourceFrame() const override;
  void acceptSourceChange() override;

  /// Heard from the next read on, over the ramp.
  void setGain(double linear) override;
  /// Samples clamped to the output's range so far.
  uint64_t clippedSamples() const { return clipped.load(); }

private:
  /// What upstream sends, and what is presented for it.
  struct Presented {
    StreamAudioFormat source;
    OutputChoice output;
  };

  const std::vector<StreamAudioFormat> device;
  std::shared_ptr<AudioGraphOutputNode> upstream;
  int upstreamCallback = -1;
  std::atomic<double> target{1.0};
  std::atomic<bool> settlePending{true};
  std::atomic<uint64_t> clipped{0};
  mutable std::mutex presentedMutex;
  std::optional<Presented> presented;

  GainRamp ramp;
  Quantiser quantiser;
  std::vector<uint8_t> input;
  std::vector<std::vector<double>> planar;
  std::vector<double *> planes;

  void onUpstreamState(const StreamState &state);
  std::optional<Presented> current() const;
  /// Moves the ramp toward the target, or settles it first when asked to.
  void follow(unsigned rate);
  void produce(const Presented &now, const uint8_t *in, size_t frames,
               uint8_t *out);
  size_t toSource(const Presented &now, size_t bytes) const;
  size_t toOutput(const Presented &now, size_t bytes) const;
};
