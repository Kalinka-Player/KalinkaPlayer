#pragma once

#include "AudioGraphNode.h"
#include "PlaybackEvents.h"
#include "SignalPath.h"
#include "dsp/output/GainControl.h"
#include "dsp/output/GainRamp.h"
#include "dsp/output/OutputFade.h"
#include "dsp/output/OutputFormat.h"
#include "dsp/output/PlanarPcm.h"
#include "dsp/output/Quantiser.h"
#include "dsp/peq/RoomEq.h"

#include <atomic>
#include <memory>
#include <mutex>
#include <optional>
#include <random>
#include <vector>

/**
 * @brief The end of the DSP, between the stage and the sink: software volume,
 * the sink's fades, TPDF dither, and the integer format the device takes.
 *
 * It presents each PCM source in the format chooseOutput() picks for the
 * device, and passes everything else on as upstream has it: states,
 * positions, seeks and source changes. At unity gain integer samples are left
 * alone, put into a wider container only where the device needs one; in the
 * source's own format they are upstream's very bytes. Float going to an
 * integer format is quantised, at unity too. Any other gain ramps
 * over 25 ms and goes through the Quantiser. It settles at once after a seek
 * or once nothing plays, with no audio to click against. DSD the stage
 * upstream converted to PCM is raised by the DSD gain as well, which ramps as
 * the volume does. A fade multiplies the volume and goes through the
 * Quantiser the same way. DSD and DoP pass untouched, and refuse any gain but
 * unity. Whenever it clamps samples it posts Clipping, with the count since
 * the source began.
 *
 * @note read(), the waits, the seeks and the fades belong to the one thread
 * that pulls audio. setGain(), setDsdGain() and describe() may come from any
 * thread.
 */
class OutputStage : public AudioGraphOutputNode,
                    public AudioGraphInputNode,
                    public GainControl,
                    public OutputFade {
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
  /// What DSD converted to PCM is raised by besides the volume, linear; heard
  /// from the next read on, over the ramp.
  void setDsdGain(double linear);
  /// Switches the PCM correction and final preamp on the next read.
  void setRoomEq(peq::Settings settings);
  /// Discards filter history when the audible stream is explicitly removed.
  void discardRoomEq(std::optional<StreamId> id = std::nullopt);
  void fadeOut() override;
  void fadeIn() override;
  void settle(double level) override;
  bool silent() const override;
  /// Samples clamped to the output's range so far.
  uint64_t clippedSamples() const { return clipped.load(); }
  /// Where Clipping is posted; set before anything is read.
  void reportTo(std::shared_ptr<PlaybackEvents> events) {
    this->events = std::move(events);
  }
  /**
   * @brief The steps it takes a source through that was decoded as
   * @p decoded, reaches it as @p source and leaves as @p output: the DSD gain
   * on DSD converted to PCM, and the dither of float onto integer samples.
   * The volume is not one. From any thread.
   */
  std::vector<SignalStep> describe(const StreamAudioFormat &decoded,
                                   const StreamAudioFormat &source,
                                   const StreamAudioFormat &output) const;

private:
  /// What upstream sends, and what is presented for it.
  struct Presented {
    StreamAudioFormat source;
    OutputChoice output;
    unsigned channels;
    /// DSD converted to PCM upstream, which the DSD gain raises.
    bool fromDsd = false;
  };

  const std::vector<StreamAudioFormat> device;
  std::shared_ptr<AudioGraphOutputNode> upstream;
  int upstreamCallback = -1;
  std::atomic<double> target{1.0};
  std::atomic<double> dsdGain{1.0};
  std::atomic<bool> settlePending{true};
  std::atomic<uint64_t> clipped{0};
  std::shared_ptr<PlaybackEvents> events;
  /// The clamped count when the source being read began, and when it was
  /// last posted; both the pulling thread's.
  uint64_t clippedBefore = 0;
  uint64_t clippedPosted = 0;
  /// The stream being read, as upstream last stated it; under presentedMutex.
  std::optional<StreamId> reading;
  // Plan writers serialize with one another while the pulling thread uses the
  // last published plan.
  std::mutex eqPreparationMutex;
  mutable std::mutex presentedMutex;
  std::optional<Presented> presented;

  peq::Settings eqSettings;
  peq::Plan eqPlan;
  uint64_t eqRevision = 0;
  uint64_t appliedRevision = 0;
  bool resetEq = true;
  bool discardAtBoundary = false;
  peq::RoomEq eq;
  Presented present(const StreamAudioFormat &source, bool fromDsd);
  GainRamp ramp;
  GainRamp fade;
  /// What the fade heads for, and a level it takes at once first.
  double fadeTarget = 1.0;
  std::optional<double> fadeSettle;
  Quantiser quantiser;
  std::vector<uint8_t> input;
  PlanarScratch planar;

  void onUpstreamState(const StreamState &state);
  std::optional<Presented> current() const;
  /// Moves both ramps toward their targets for @p now, settling either first
  /// when asked.
  void follow(const Presented &now);
  void produce(const Presented &now, const uint8_t *in, size_t frames,
               uint8_t *out);
  size_t toSource(const Presented &now, size_t bytes) const;
  size_t toOutput(const Presented &now, size_t bytes) const;
};
