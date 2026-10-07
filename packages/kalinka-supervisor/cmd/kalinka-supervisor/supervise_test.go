package main

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"kalinka/supervisor/internal/protocol"
)

type fakeComponent struct {
	mu      sync.Mutex
	gate    string
	starts  atomic.Int32
	results chan error
}

func (f *fakeComponent) blocked() string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.gate
}
func (f *fakeComponent) close(reason string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.gate = reason
}
func (f *fakeComponent) run(ctx context.Context) error {
	f.starts.Add(1)
	select {
	case err := <-f.results:
		return err
	case <-ctx.Done():
		return nil
	}
}

func eventually(t *testing.T, what string, check func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !check() {
		if time.Now().After(deadline) {
			t.Fatal("timed out waiting for " + what)
		}
		time.Sleep(time.Millisecond)
	}
}

func TestSuperviseFollowsGateAndRetries(t *testing.T) {
	f := &fakeComponent{gate: "no Bluetooth adapter hci0", results: make(chan error)}
	state := &componentState{name: "Test component"}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		supervise(ctx, component{blocked: f.blocked, run: f.run, poll: time.Millisecond, backoff: 20 * time.Millisecond, state: state})
		close(done)
	}()
	eventually(t, "the closed gate", func() bool { return state.Describe() == "waiting (no Bluetooth adapter hci0)" })
	if f.starts.Load() != 0 {
		t.Fatal("ran behind a closed gate")
	}
	f.close("")
	eventually(t, "the first start", func() bool { return f.starts.Load() == 1 && state.Phase() == "running" })

	f.results <- protocol.Unavailable
	eventually(t, "the backoff", func() bool { return state.Describe() == "waiting (retrying after unavailable)" })
	eventually(t, "the retry", func() bool { return f.starts.Load() == 2 && state.Phase() == "running" })

	f.close("no Wi-Fi interface")
	eventually(t, "the stop on a closed gate", func() bool { return state.Describe() == "waiting (no Wi-Fi interface)" })
	f.close("")
	eventually(t, "the restart", func() bool { return f.starts.Load() == 3 })

	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("supervise outlived its context")
	}
}

func TestStateOfUnsupervisedComponent(t *testing.T) {
	s := &componentState{name: "Nearby setup"}
	if s.Phase() != "off" || s.Describe() != "off" {
		t.Fatalf("unsupervised = %q, %q", s.Phase(), s.Describe())
	}
}
