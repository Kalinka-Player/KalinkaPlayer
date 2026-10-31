package provision

import (
	"context"
	"log/slog"
	"time"

	"kalinka/supervisor/internal/wifi"
)

// radioRecovery runs outside the heartbeat loop. Each radio gets at most three
// attempts per offline setup window; a successful step is never repeated.
type radioRecovery struct {
	steps         []func(context.Context) error
	retry, settle time.Duration
	done          chan struct{}
	cancel        context.CancelFunc
	stopping      bool
}

func newRadioRecovery(steps ...func(context.Context) error) *radioRecovery {
	return &radioRecovery{steps: steps, retry: 10 * time.Second, settle: 10 * time.Second}
}

// reconcile returns whether hotspot startup may proceed. A returning network
// cancels recovery, including its delay before borrowing Wi-Fi for the hotspot.
func (r *radioRecovery) reconcile(ctx context.Context, allowed bool) bool {
	if r.done != nil {
		if !allowed {
			r.cancel()
			r.stopping = true
		}
		select {
		case <-r.done:
			if !r.stopping {
				return true
			}
			r.cancel()
			r.done, r.cancel, r.stopping = nil, nil, false
		default:
			return false
		}
	}
	if !allowed {
		return true
	}
	worker, cancel := context.WithCancel(ctx)
	r.cancel, r.done = cancel, make(chan struct{})
	go func() {
		defer close(r.done)
		r.run(worker)
	}()
	return false
}

func (r *radioRecovery) run(ctx context.Context) {
	finished := make([]bool, len(r.steps))
	for attempt := 0; attempt < 3; attempt++ {
		pending := false
		for i, step := range r.steps {
			if ctx.Err() != nil {
				return
			}
			if finished[i] {
				continue
			}
			call, cancel := context.WithTimeout(ctx, 10*time.Second)
			err := step(call)
			cancel()
			if err == nil {
				finished[i] = true
			} else {
				pending = true
				slog.Warn("Radio recovery failed", "attempt", attempt+1, "error", err)
			}
		}
		if !pending {
			break
		}
		if attempt < 2 && wifi.Pause(ctx, r.retry) != nil {
			return
		}
	}
	// Allow a saved client network to reconnect before starting the hotspot.
	_ = wifi.Pause(ctx, r.settle)
}

func (r *radioRecovery) close() {
	if r.cancel != nil {
		r.cancel()
		<-r.done
	}
}
