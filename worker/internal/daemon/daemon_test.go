package daemon_test

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"solvenet/worker/internal/daemon"
	"solvenet/worker/internal/provider"
)

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
