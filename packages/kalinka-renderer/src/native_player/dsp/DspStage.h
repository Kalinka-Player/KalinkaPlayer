#ifndef DSP_STAGE_H
#define DSP_STAGE_H

#include "AudioGraphNode.h"
#include "dsp/DspProcessor.h"

#include <deque>
#include <memory>
#include <vector>

/**
 * @brief The one place between the stream switcher and the sink where audio
 * is processed.
 *
 * Without a processor it forwards every read, wait, seek and state untouched,
 * and adds no delay. With one, it hides the processor's latency: output frame
 * n of a source is that source's input frame n, so positions need no
 * correction downstream. It reads that latency ahead of the sink, and at a
 * join between compatible input formats keeps the processor running. It
 * announces the incoming source after handing on the outgoing source's last
 * frame. At any other join it plays the outgoing tail out first. Formats the
 * processor does not handle pass untouched.
 *
 * @note read(), the waits, the seeks, acceptSourceChange(),
 * streamReadPosition() and liveSourceFrame() belong to the one thread that
 * pulls audio, which owns everything the stage holds. discard(),
 * discardAll(), describe() and handsOn() may come from any thread;
 * connectTo() and disconnect() only while nothing pulls. The stage has no
 * lock of its own: upstream states and discards reach the pulling thread
 * through the node's state lock alone.
 */
class DspStage : public AudioGraphOutputNode, public AudioGraphInputNode {
public:
  /// @param processor What audio runs through, or null for nothing at all.
  explicit DspStage(std::unique_ptr<DspProcessor> processor = nullptr);
  ~DspStage() override;

  void connectTo(std::shared_ptr<AudioGraphOutputNode> source) override;
  void disconnect(std::shared_ptr<AudioGraphOutputNode> source) override;

  size_t read(void *data, size_t size) override;
  size_t waitForData(std::stop_token stopToken = std::stop_token(),
                     size_t size = 1) override;
  size_t waitForDataFor(std::stop_token stopToken,
                        std::chrono::milliseconds timeout,
                        size_t size) override;

  /// Refused while what plays belongs to a source upstream has already left.
  /// Upstream seeks the processor's history() early, and the frames before
  /// the target build its history. It can supply the preroll before its first
  /// read. Where upstream lands further back than asked, play goes on from there.
  size_t seekTo(size_t absolutePosition) override;
  /// Drops everything held and seeks @p stream upstream, even one upstream has
  /// already left, early as seekTo() does.
  size_t seekStream(StreamId stream, size_t position) override;
  std::optional<long> streamReadPosition() const override;
  std::optional<uint64_t> liveSourceFrame() const override;
  void acceptSourceChange() override;

  /// What the processor does to a source decoded as @p format; nothing when
  /// it passes untouched. From any thread.
  SignalPath describe(const StreamAudioFormat &format) const;
  /// The format a source decoded as @p format is handed on in. From any
  /// thread.
  StreamAudioFormat handsOn(const StreamAudioFormat &format) const;

  /// Drops what it holds of stream @p id, which has been removed upstream.
  void discard(StreamId id);
  /// Drops everything it holds, as when every stream has been removed.
  void discardAll();

private:
  /// One source's stretch of a run, in run frames.
  struct Segment {
    std::optional<StreamId> id;
    StreamInfo info; ///< Upstream's, in the format the processor takes.
    /// Its first frame's place in the source, in output frames.
    std::optional<long> start;
    uint64_t begin = 0; ///< In run output frames, as is end.
    uint64_t end = 0;
    bool open = true;  ///< Upstream may still add frames to it.
    bool left = false; ///< Upstream has moved on to another source.
    bool dropped = false;
  };

  /// The next thing the pulling thread has to do to get audio out.
  enum class Step { Done, Read, Flush, Accept };
  /// What fill() returns when the audio upstream is not for the processor.
  static constexpr size_t kPassThrough = size_t(-1);

  /// How the state shown downstream follows upstream's.
  enum class Follow {
    Upstream,       ///< As it is.
    UntilInputEnds, ///< As it is, until it ends the input of what plays.
    Fixed           ///< Not at all.
  };

  /// What other threads hand the pulling thread, and what is shown.
  /// @note Touched only inside updateState(), under the node's state lock.
  struct Shared {
    StreamState upstream{AudioGraphNodeState::STOPPED};
    uint64_t upstreamChanges = 0;
    Follow follow = Follow::Upstream;
    StreamState fixed{AudioGraphNodeState::STOPPED};
    std::optional<StreamId> playing; ///< The stream the sink is reading.
    /// What a discard of the playing stream shows at once.
    std::optional<StreamState> afterPlaying;
    std::vector<StreamId> discards;
    bool discardEverything = false;
  };

  const std::unique_ptr<DspProcessor> processor;
  std::shared_ptr<AudioGraphOutputNode> upstream;
  int upstreamCallback = -1;
  Shared shared;

  StreamState upstreamSeen{AudioGraphNodeState::STOPPED};
  uint64_t upstreamChangesSeen = 0;
  std::vector<StreamId> discardsTaken;
  std::deque<Segment> segments;
  /// A source upstream was switched to that cannot continue the run: it is
  /// announced once everything held has been handed on.
  std::optional<std::optional<StreamId>> boundary;
  /// A source upstream was switched to before its format was known: the run
  /// waits for it, neither continued nor flushed.
  std::optional<std::optional<StreamId>> joinPending;
  /// Where a seek landed, in output frames, for the next run to start at
  /// after running the history before it through the processor.
  struct LeadIn {
    std::optional<StreamId> id;
    long target;
  };
  std::optional<LeadIn> leadIn;
  size_t inputChunkFrames = 0;
  /// The format of the run started last, which the processor's history() describes.
  std::optional<StreamAudioFormat> runFormat;

  size_t inFrameBytes = 0;
  size_t outFrameBytes = 0;
  /// Input frames to one output frame in the run.
  unsigned ratio = 1;
  size_t latency = 0;
  uint64_t fedIn = 0; ///< Input frames read in the run.
  uint64_t fedOut = 0; ///< Outputs so far, the priming ones included.
  uint64_t handed = 0; ///< Run frames handed on, or skipped by a discard.
  size_t flushLeft = 0;
  std::vector<uint8_t> fifo; ///< Run frames [handed, produced()).
  size_t fifoOffset = 0;
  std::vector<uint8_t> input;
  std::vector<uint8_t> output;

  size_t passRead(void *data, size_t size);
  /// Pulls until the head holds what @p bytes asks for, or no more can come.
  /// @return frames readable, or kPassThrough.
  size_t fill(std::stop_token token, size_t bytes, bool wait,
              std::optional<std::chrono::steady_clock::time_point> deadline);
  Step plan(size_t frames, size_t &count);
  /// Adopt a compatible source encoding while the outgoing samples drain.
  bool continueIn(const StreamAudioFormat &format);
  /// Carry the run into source @p id when its format is compatible.
  void join(std::optional<StreamId> id, std::optional<long> position,
            bool idle);
  bool perform(Step step, size_t count);
  bool startRun(std::optional<long> position);
  void endRun();
  void append(const uint8_t *frames, size_t count);
  void dropTo(uint64_t frame);
  void popHead();
  uint64_t produced() const;
  size_t fifoFrames() const;
  size_t readable() const;
  bool headExhausted() const;
  const Segment *nextAfterHead() const;
  void skipDropped();
  /// Takes what other threads left: upstream's state and pending discards.
  void sync();
  /// Takes upstream's state alone, leaving discards for the next sync().
  void takeUpstream();
  void drop(StreamId id);
  void dropEverything();
  /// Shows downstream what the stage now holds.
  void publish();
  /// Seeks in the format of the target stream, including a retired source.
  size_t seek(std::optional<StreamId> stream, size_t position);
  /// Lets go of everything held once upstream has seeked to @p reached, in
  /// input frames, for a seek to @p target asked @p early output frames
  /// short of it; keeps it all when upstream refused.
  /// @return Where what is heard next begins, in output frames.
  size_t restartAfter(size_t reached, unsigned ratio, size_t target,
                      size_t early, std::optional<StreamId> id);
  StreamState shown(const Shared &shared) const;
  /// @p state as downstream sees it: in the processor's output format, and
  /// with upstream's own position in output frames when @p upstreamPosition.
  StreamState presented(StreamState state, bool upstreamPosition) const;
  /// Input frames to one output frame of a stream in this format.
  unsigned ratioOf(const std::optional<StreamInfo> &info) const;
  void onUpstreamState(const StreamState &state);
};

#endif
