package provider

import (
	"context"
	"errors"
	"time"

	"solvenet/worker/internal/daemon"
)

// Carries only a validated public health state through the existing error
// categorization wrappers. It never includes provider response bodies.
type openAIHealthError struct {
	err    error
	health daemon.Health
}

func (e *openAIHealthError) Error() string { return e.err.Error() }
func (e *openAIHealthError) Unwrap() error { return e.err }

func withOpenAIHealth(err error, reason string) error {
	return &openAIHealthError{err: err, health: daemon.Health{Status: "unavailable", Reason: reason}}
}

// Fixed public reasons are the entire status boundary; no provider text, URLs,
// account metadata or credentials are retained. Health never performs IO.
func (o *OpenAI) Health(context.Context) daemon.Health {
	o.healthMu.Lock()
	defer o.healthMu.Unlock()
	if o.health.Status == "" {
		return daemon.Health{Status: "unobserved"}
	}
	return o.health
}

func (o *OpenAI) setHealth(h daemon.Health) {
	o.healthMu.Lock()
	o.health = h
	o.healthMu.Unlock()
}

func openAIHTTPHealth(code int) daemon.Health {
	reason := "OpenAI profile or model access unsupported"
	switch {
	case code == 401 || code == 403:
		reason = "OpenAI credential rejected"
	case code == 404:
		reason = "OpenAI model unavailable"
	case code == 429:
		reason = "OpenAI rate limited"
	case code == 408 || code >= 500:
		reason = "OpenAI service unavailable"
	}
	return daemon.Health{Status: "unavailable", Reason: reason}
}

func (o *OpenAI) observe(err error) {
	if err == nil {
		o.setHealth(daemon.Health{Status: "ready"})
		return
	}
	var typed *openAIHealthError
	if errors.As(err, &typed) {
		o.setHealth(typed.health)
		return
	}
	// All messages here originate in the adapter, never in a provider body.
	reason := "OpenAI compatibility check inconclusive"
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		reason = "OpenAI deadline exceeded"
	}
	o.setHealth(daemon.Health{Status: "unavailable", Reason: reason})
}

// Check is explicitly operator-invoked. The paid variant tests the configured
// structured generation contract once, with no retries, under a 60s deadline.
// Responses includes reasoning in this cap: incomplete is inconclusive, not ready.
func (o *OpenAI) Check(ctx context.Context, paid bool) daemon.Health {
	if !paid {
		return o.Health(ctx)
	}
	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	cap := 256
	if o.Config.Profile == ResponsesReasoning {
		cap = 1024
	}
	if cap > o.Config.MaxOutputTokens {
		cap = o.Config.MaxOutputTokens
	}
	_, _ = o.Execute(ctx, daemon.Job{Kind: "model.respond", TaskType: "question",
		Statement: ": True", Imports: []string{"Init"}, MaxOutputTokens: cap,
		Messages: []daemon.Message{{Role: "user", Content: "Return the text ok in the required JSON envelope."}}})
	return o.Health(ctx)
}
