package main

import (
	"context"
	"log/slog"
	"strings"
	"sync"
	"time"

	"kalinka/supervisor/internal/protocol"
	"kalinka/supervisor/internal/wifi"
)

// component is one restartable part of the supervisor: it runs while its gate
// is open, stops when the gate closes, and is retried after a failure without
// taking the rest of the process down.
type component struct {
	blocked       func() string
	run           func(context.Context) error
	poll, backoff time.Duration
	state         *componentState
}

// componentState is a component's lifecycle as clients and logs see it. Safe for concurrent use.
type componentState struct {
	name          string
	mu            sync.Mutex
	phase, reason string
}

// Phase is "off" for a component that is not supervised at all, else "waiting" or "running".
func (s *componentState) Phase() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.phase == "" {
		return "off"
	}
	return s.phase
}

func (s *componentState) Describe() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch {
	case s.phase == "":
		return "off"
	case s.reason != "":
		return s.phase + " (" + s.reason + ")"
	}
	return s.phase
}

func (s *componentState) set(phase, reason string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.phase == phase && s.reason == reason {
		return
	}
	s.phase, s.reason = phase, reason
	if phase == "running" {
		slog.Info(s.name + " starting")
	} else {
		slog.Info(s.name+" waiting", "reason", reason)
	}
}

// failureCode keeps backend detail out of the logs: only fixed protocol codes are reported.
func failureCode(err error) string {
	if p, ok := err.(protocol.Error); ok {
		return strings.ToLower(string(p))
	}
	return "unavailable"
}

// supervise returns once ctx ends and the component has stopped.
func supervise(ctx context.Context, c component) {
	for {
		if reason := c.blocked(); reason != "" {
			c.state.set("waiting", reason)
			if wifi.Pause(ctx, c.poll) != nil {
				return
			}
			continue
		}
		c.state.set("running", "")
		gateClosed, err := c.runWhileOpen(ctx)
		if ctx.Err() != nil {
			return
		}
		if gateClosed {
			continue
		}
		code := "stopped"
		if err != nil {
			code = failureCode(err)
			slog.Error(c.state.name+" failed", "reason", code)
		}
		c.state.set("waiting", "retrying after "+code)
		if wifi.Pause(ctx, c.backoff) != nil {
			return
		}
	}
}

func (c component) runWhileOpen(ctx context.Context) (gateClosed bool, err error) {
	runCtx, cancel := context.WithCancel(ctx)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- c.run(runCtx) }()
	ticker := time.NewTicker(c.poll)
	defer ticker.Stop()
	for {
		select {
		case err := <-done:
			return false, err
		case <-ticker.C:
			if reason := c.blocked(); reason != "" {
				cancel()
				<-done
				c.state.set("waiting", reason)
				return true, nil
			}
		}
	}
}
