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

#include <array>
#include <atomic>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <random>
#include <vector>

class FirFilterBank;

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
 * the source began or room correction last changed. Integer samples that
 * reach it as float with nothing to compute are restored exactly; the first
 * time in a source that a sample is not, it posts Unrestored.
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
                       uint64_t seed = std::random_device{}(),
                       std::vector<unsigned> firRates = {});
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
  std::optional<StreamId> seekTargetId(StreamId requested) override {
    return upstream ? upstream->seekTargetId(requested) : std::nullopt;
  }
  std::optional<long> streamReadPosition() const override;
  std::optional<uint64_t> liveSourceFrame() const override;
  void acceptSourceChange() override;

  /// Heard from the next read on, over the ramp.
  void setGain(double linear) override;
  /// What DSD converted to PCM is raised by besides the volume, linear; heard
  /// from the next read on, over the ramp.
  void setDsdGain(double linear);
  /// Switches the PCM correction and final preamp on the next read.
  void setRoomEq(peq::Settings settings,
                 std::shared_ptr<const FirFilterBank> fir = {});
  /// Discards filter history when the audible stream is explicitly removed.
  void discardRoomEq(std::optional<StreamId> id = std::nullopt);
  void fadeOut() override;
  void fadeIn() override;
  void settle(double level) override;
  bool silent() const override;
  /// Samples clamped to the output's range so far.
  uint64_t clippedSamples() const { return clipped.load(); }
  /// Where Clipping and Unrestored are posted; set before anything is read.
  /// @param unsettledFrames Frames of the last upstream read made during a
  /// filter change.
  /// @param carriesDecoded Whether upstream's float is the decoded integers
  /// as they came, so a sample that does not restore exactly is news. Asked
  /// on the thread that pulls audio; absent, it always is.
  void reportTo(std::shared_ptr<PlaybackEvents> events,
                std::function<size_t()> unsettledFrames = {},
                std::function<bool()> carriesDecoded = {}) {
    this->unsettledFrames = std::move(unsettledFrames);
    this->carriesDecoded = std::move(carriesDecoded);
    this->events = std::move(events);
  }
  /**
   * @brief Every step it takes a source through that was decoded as
   * @p decoded, reaches it as @p source and leaves as @p output, each format
   * change included, marked bit-perfect where no sample changes value.
   * @param firDry @p source is the decoded integers, carried in float by a
   * room correction stage with no filter for them.
   * @param softwareVolume Whether the volume is set here.
   * @param stream The stream described, whose exact restores it vouches for.
   * @note From any thread.
   */
  std::vector<SignalStep> describe(const StreamAudioFormat &decoded,
                                   const StreamAudioFormat &source,
                                   const StreamAudioFormat &output,
                                   bool firDry = false,
                                   bool softwareVolume = false,
                                   std::optional<StreamId> stream = {}) const;

private:
  /// What upstream sends, and what is presented for it.
  struct Presented {
    StreamAudioFormat source;
    OutputChoice output;
    unsigned channels;
    /// DSD converted to PCM upstream, which the DSD gain raises.
    bool fromDsd = false;
    bool fromInteger = false;
    StreamAudioFormat preferred;
  };

  const std::vector<StreamAudioFormat> device;
  const std::vector<unsigned> firRates;
  std::shared_ptr<AudioGraphOutputNode> upstream;
  int upstreamCallback = -1;
  std::atomic<double> target{1.0};
  std::atomic<double> dsdGain{1.0};
  std::atomic<bool> settlePending{true};
  std::atomic<uint64_t> clipped{0};
  /// Blocks of a stream dithered for want of an exact restore since room
  /// correction last changed.
  struct RestoreFailures {
    std::optional<StreamId> stream;
    uint64_t blocks = 0;
  };
  /// The stream being read, then the one before it, which may still be
  /// heard; under presentedMutex.
  std::array<RestoreFailures, 2> restoreFailures;
  std::function<bool()> carriesDecoded;
  std::shared_ptr<PlaybackEvents> events;
  std::function<size_t()> unsettledFrames;
  /// Under presentedMutex. A read using an older revision cannot relatch the
  /// warning after a setting change, even if its quantiser finishes later.
  uint64_t clippingRevision = 0;
  uint64_t windowClips = 0;
  void resetClipping();
  /// Under presentedMutex.
  void forgetRestoreFailures();
  /// The stream being read, as upstream last stated it; under presentedMutex.
  std::optional<StreamId> reading;
  // Plan writers serialize with one another while the pulling thread uses the
  // last published plan.
  std::mutex eqPreparationMutex;
  mutable std::mutex presentedMutex;
  std::optional<Presented> presented;

  peq::Settings eqSettings;
  std::shared_ptr<const FirFilterBank> automaticFir;
  peq::Plan eqPlan;
  uint64_t eqRevision = 0;
  uint64_t appliedRevision = 0;
  bool resetEq = true;
  bool discardAtBoundary = false;
  peq::RoomEq eq;
  Presented present(const StreamAudioFormat &source, bool fromDsd,
                    bool fromInteger, const StreamAudioFormat &preferred);
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
  /// What one produce() call counted outside filter transitions.
  struct Produced {
    uint64_t clips = 0;
    uint64_t unrestored = 0;
  };
  Produced produce(const Presented &now, const uint8_t *in, size_t frames,
                   uint8_t *out, size_t unsettled);
  size_t toSource(const Presented &now, size_t bytes) const;
  size_t toOutput(const Presented &now, size_t bytes) const;
};
