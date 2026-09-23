// Package provider keeps model execution independent of coordinator transport.
package provider

import (
	"context"
	"time"

	"solvenet/worker/internal/daemon"
)

// Scripted returns a fixed proof without credentials or external model calls.
type Scripted struct {
	Proof string
	Delay time.Duration
}

func (s Scripted) Execute(ctx context.Context, _ daemon.Job) (daemon.Execution, error) {
	timer := time.NewTimer(s.Delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return daemon.Execution{}, ctx.Err()
	case <-timer.C:
		return daemon.Execution{Text: s.Proof}, nil
	}
}
