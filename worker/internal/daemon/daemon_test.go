package daemon_test

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"solvenet/worker/internal/daemon"
	"solvenet/worker/internal/provider"
)

type executorFunc func(context.Context, daemon.Job) (daemon.Execution, error)

func (f executorFunc) Execute(ctx context.Context, job daemon.Job) (daemon.Execution, error) {
	return f(ctx, job)
}

func TestHeartbeatAndIdempotentRetry(t *testing.T) {
	var mu sync.Mutex
	heartbeats, submissions := 0, 0
	var first daemon.Result
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		defer mu.Unlock()
		switch r.URL.Path {
		case "/v1/claim":
			json.NewEncoder(w).Encode(daemon.Assignment{Version: 1, ID: "a", Token: "token", HeartbeatSeconds: 0.01, Job: daemon.Job{Kind: "model.generate", Model: "scripted", TimeoutSeconds: 1}})
		case "/v1/assignments/a/heartbeat":
			heartbeats++
			w.Write([]byte(`{}`))
		case "/v1/assignments/a/result":
			var result daemon.Result
			if err := json.NewDecoder(r.Body).Decode(&result); err != nil {
				t.Error(err)
			}
			submissions++
			if submissions == 1 {
				first = result
				w.WriteHeader(503)
				return
			}
			if result.Token != first.Token || result.Status != first.Status || result.Output.Text != first.Output.Text {
				t.Error("retry changed payload")
			}
			w.Write([]byte(`{"accepted":true}`))
		default:
			t.Errorf("unexpected route %s", r.URL.Path)
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	w := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client(), Executor: provider.Scripted{Proof: "rfl", Delay: 40 * time.Millisecond}}
	worked, err := w.Once(context.Background())
	if err != nil || !worked {
		t.Fatalf("worked=%v err=%v", worked, err)
	}
	mu.Lock()
	defer mu.Unlock()
	if heartbeats == 0 || submissions != 2 {
		t.Fatalf("heartbeats=%d submissions=%d", heartbeats, submissions)
	}
}

func TestNoWork(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) }))
	defer server.Close()
	w := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client()}
	worked, err := w.Once(context.Background())
	if worked || err != nil {
		t.Fatalf("worked=%v err=%v", worked, err)
	}
}

func TestWorkerUsesCoordinatorTimeoutAboveOldCap(t *testing.T) {
	var remaining time.Duration
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/claim":
			json.NewEncoder(w).Encode(daemon.Assignment{Version: 1, ID: "a", Token: "token", HeartbeatSeconds: 3600, Job: daemon.Job{Kind: "model.generate", Model: "scripted", TimeoutSeconds: 321}})
		case "/v1/assignments/a/result":
			w.Write([]byte(`{"accepted":true}`))
		default:
			w.WriteHeader(404)
		}
	}))
	defer server.Close()
	executor := executorFunc(func(ctx context.Context, _ daemon.Job) (daemon.Execution, error) {
		deadline, ok := ctx.Deadline()
		if !ok {
			t.Fatal("executor context has no deadline")
		}
		remaining = time.Until(deadline)
		return daemon.Execution{Text: "rfl"}, nil
	})
	w := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client(), Executor: executor}
	worked, err := w.Once(context.Background())
	if err != nil || !worked {
		t.Fatalf("worked=%v err=%v", worked, err)
	}
	if remaining < 320*time.Second || remaining > 321*time.Second {
		t.Fatalf("effective timeout %v, want approximately 321s", remaining)
	}
}

func TestWorkerRejectsInvalidTimeout(t *testing.T) {
	called := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(daemon.Assignment{Version: 1, ID: "a", Token: "token", HeartbeatSeconds: 1, Job: daemon.Job{Kind: "model.generate", Model: "scripted", TimeoutSeconds: 0}})
	}))
	defer server.Close()
	executor := executorFunc(func(context.Context, daemon.Job) (daemon.Execution, error) {
		called = true
		return daemon.Execution{}, nil
	})
	w := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client(), Executor: executor}
	worked, err := w.Once(context.Background())
	if !worked || err == nil || !strings.Contains(err.Error(), "job.timeout_seconds") {
		t.Fatalf("worked=%v err=%v", worked, err)
	}
	if called {
		t.Fatal("executor called for invalid timeout")
	}
}

func TestWorkerSubmitsFailureClassification(t *testing.T) {
	for _, test := range []struct {
		name string
		err  error
		want string
	}{
		{"unclassified-default", errors.New("temporary provider error"), "transient"},
		{"transient", daemon.Transient(errors.New("service unavailable")), "transient"},
		{"permanent", daemon.Permanent(errors.New("invalid configuration")), "permanent"},
	} {
		t.Run(test.name, func(t *testing.T) {
			var result daemon.Result
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/v1/claim":
					json.NewEncoder(w).Encode(daemon.Assignment{Version: 1, ID: "a", Token: "token", HeartbeatSeconds: 3600, Job: daemon.Job{Kind: "model.generate", Model: "scripted", TimeoutSeconds: 1}})
				case "/v1/assignments/a/result":
					json.NewDecoder(r.Body).Decode(&result)
					w.Write([]byte(`{"accepted":true}`))
				default:
					w.WriteHeader(404)
				}
			}))
			defer server.Close()
			executor := executorFunc(func(context.Context, daemon.Job) (daemon.Execution, error) {
				return daemon.Execution{}, test.err
			})
			worker := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client(), Executor: executor}
			if worked, err := worker.Once(context.Background()); err != nil || !worked {
				t.Fatalf("worked=%v err=%v", worked, err)
			}
			if result.Status != "failed" || result.FailureClass != test.want {
				t.Fatalf("result=%+v", result)
			}
		})
	}
}
