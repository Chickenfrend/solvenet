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

func validAssignment() daemon.Assignment {
	return daemon.Assignment{
		Version: 1, ID: "assignment", Token: "token", LeaseExpiresAt: 1790000030,
		HeartbeatSeconds: 3600,
		Job: daemon.Job{ID: "job", Kind: "model.generate", Model: "scripted",
			Statement: "(n : Nat) : n + 0 = n", Imports: []string{"Init"},
			Messages: []daemon.Message{}, MaxOutputTokens: 2048, TimeoutSeconds: 120},
	}
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
			a := validAssignment()
			a.ID, a.HeartbeatSeconds, a.Job.TimeoutSeconds = "a", 0.01, 1
			json.NewEncoder(w).Encode(a)
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
			a := validAssignment()
			a.ID, a.Job.TimeoutSeconds = "a", 321
			json.NewEncoder(w).Encode(a)
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

func TestWorkerRejectsMalformedAssignmentsBeforeExecution(t *testing.T) {
	parent := "parent"
	tests := []struct {
		name, field string
		mutate      func(*daemon.Assignment)
	}{
		{"assignment ID", "assignment_id", func(a *daemon.Assignment) { a.ID = "" }},
		{"assignment ID too large", "assignment_id", func(a *daemon.Assignment) { a.ID = strings.Repeat("a", 257) }},
		{"lease token", "lease_token", func(a *daemon.Assignment) { a.Token = "" }},
		{"lease token too large", "lease_token", func(a *daemon.Assignment) { a.Token = strings.Repeat("t", 257) }},
		{"lease expiration", "lease_expires_at", func(a *daemon.Assignment) { a.LeaseExpiresAt = 0 }},
		{"heartbeat zero", "heartbeat_seconds", func(a *daemon.Assignment) { a.HeartbeatSeconds = 0 }},
		{"heartbeat too large", "heartbeat_seconds", func(a *daemon.Assignment) { a.HeartbeatSeconds = 86401 }},
		{"job ID", "job.id", func(a *daemon.Assignment) { a.Job.ID = "" }},
		{"job ID too large", "job.id", func(a *daemon.Assignment) { a.Job.ID = strings.Repeat("j", 257) }},
		{"kind", "job.kind", func(a *daemon.Assignment) { a.Job.Kind = "shell.execute" }},
		{"empty model", "job.model", func(a *daemon.Assignment) { a.Job.Model = "" }},
		{"model too large", "job.model", func(a *daemon.Assignment) { a.Job.Model = strings.Repeat("m", 257) }},
		{"different model", "job.model", func(a *daemon.Assignment) { a.Job.Model = "other" }},
		{"statement", "job.statement", func(a *daemon.Assignment) { a.Job.Statement = " \n" }},
		{"statement too large", "job.statement", func(a *daemon.Assignment) { a.Job.Statement = strings.Repeat("x", 64*1024+1) }},
		{"missing imports", "job.imports", func(a *daemon.Assignment) { a.Job.Imports = nil }},
		{"too many imports", "job.imports", func(a *daemon.Assignment) { a.Job.Imports = make([]string, 33) }},
		{"empty import", "job.imports[0]", func(a *daemon.Assignment) { a.Job.Imports[0] = "" }},
		{"import too large", "job.imports[0]", func(a *daemon.Assignment) { a.Job.Imports[0] = strings.Repeat("i", 257) }},
		{"too many messages", "job.messages", func(a *daemon.Assignment) { a.Job.Messages = make([]daemon.Message, 33) }},
		{"null messages", "job.messages", func(a *daemon.Assignment) { a.Job.Messages = nil }},
		{"message role", "job.messages[0].role", func(a *daemon.Assignment) { a.Job.Messages = []daemon.Message{{Role: "tool", Content: "x"}} }},
		{"message content", "job.messages[0].content", func(a *daemon.Assignment) { a.Job.Messages = []daemon.Message{{Role: "user", Content: ""}} }},
		{"message content too large", "job.messages[0].content", func(a *daemon.Assignment) {
			a.Job.Messages = []daemon.Message{{Role: "user", Content: strings.Repeat("x", 256*1024+1)}}
		}},
		{"output limit zero", "job.max_output_tokens", func(a *daemon.Assignment) { a.Job.MaxOutputTokens = 0 }},
		{"output limit too large", "job.max_output_tokens", func(a *daemon.Assignment) { a.Job.MaxOutputTokens = 32769 }},
		{"timeout zero", "job.timeout_seconds", func(a *daemon.Assignment) { a.Job.TimeoutSeconds = 0 }},
		{"timeout too large", "job.timeout_seconds", func(a *daemon.Assignment) { a.Job.TimeoutSeconds = 86401 }},
		{"negative repair depth", "job.repair_depth", func(a *daemon.Assignment) { a.Job.RepairDepth = -1 }},
		{"parent on initial", "job.parent_attempt_id", func(a *daemon.Assignment) { a.Job.ParentAttemptID = &parent }},
		{"missing repair parent", "job.parent_attempt_id", func(a *daemon.Assignment) { a.Job.RepairDepth = 1 }},
		{"empty repair parent", "job.parent_attempt_id", func(a *daemon.Assignment) { a.Job.RepairDepth, a.Job.ParentAttemptID = 1, new(string) }},
		{"repair parent too large", "job.parent_attempt_id", func(a *daemon.Assignment) {
			value := strings.Repeat("p", 257)
			a.Job.RepairDepth, a.Job.ParentAttemptID = 1, &value
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			a := validAssignment()
			test.mutate(&a)
			called := false
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
				if err := json.NewEncoder(w).Encode(a); err != nil {
					t.Error(err)
				}
			}))
			defer server.Close()
			executor := executorFunc(func(context.Context, daemon.Job) (daemon.Execution, error) {
				called = true
				return daemon.Execution{}, nil
			})
			worker := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client(), Executor: executor}
			worked, err := worker.Once(context.Background())
			if !worked || err == nil || !strings.Contains(err.Error(), test.field) {
				t.Fatalf("worked=%v err=%v; want field %q", worked, err, test.field)
			}
			if called {
				t.Fatal("executor called for malformed assignment")
			}
		})
	}
}

func TestWorkerRejectsOmittedRequiredFieldsBeforeExecution(t *testing.T) {
	for _, test := range []struct {
		name, field string
		remove      func(map[string]any)
	}{
		{"protocol version", "protocol_version", func(a map[string]any) { delete(a, "protocol_version") }},
		{"messages", "job.messages", func(a map[string]any) { delete(a["job"].(map[string]any), "messages") }},
		{"parent attempt", "job.parent_attempt_id", func(a map[string]any) { delete(a["job"].(map[string]any), "parent_attempt_id") }},
		{"repair depth", "job.repair_depth", func(a map[string]any) { delete(a["job"].(map[string]any), "repair_depth") }},
	} {
		t.Run(test.name, func(t *testing.T) {
			encoded, err := json.Marshal(validAssignment())
			if err != nil {
				t.Fatal(err)
			}
			var payload map[string]any
			if err := json.Unmarshal(encoded, &payload); err != nil {
				t.Fatal(err)
			}
			test.remove(payload)
			called := false
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { json.NewEncoder(w).Encode(payload) }))
			defer server.Close()
			worker := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client(), Executor: executorFunc(func(context.Context, daemon.Job) (daemon.Execution, error) {
				called = true
				return daemon.Execution{}, nil
			})}
			worked, err := worker.Once(context.Background())
			if !worked || err == nil || !strings.Contains(err.Error(), test.field) {
				t.Fatalf("worked=%v err=%v; want field %q", worked, err, test.field)
			}
			if called {
				t.Fatal("executor called when a required field was omitted")
			}
		})
	}
}

func TestWorkerDistinguishesUnsupportedProtocolVersion(t *testing.T) {
	a := validAssignment()
	a.Version = 2
	called := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { json.NewEncoder(w).Encode(a) }))
	defer server.Close()
	worker := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client(), Executor: executorFunc(func(context.Context, daemon.Job) (daemon.Execution, error) {
		called = true
		return daemon.Execution{}, nil
	})}
	worked, err := worker.Once(context.Background())
	var versionErr *daemon.UnsupportedProtocolVersionError
	if !worked || !errors.As(err, &versionErr) || versionErr.Version != 2 {
		t.Fatalf("worked=%v err=%v", worked, err)
	}
	if called {
		t.Fatal("executor called for unsupported protocol")
	}
}

func TestWorkerPromptlyRejectsUnusableAssignmentWithoutHeartbeat(t *testing.T) {
	for _, test := range []struct {
		name, kind string
		payload    func() any
	}{
		{"malformed v1", daemon.RejectionMalformedAssignment, func() any {
			a := validAssignment()
			a.Job.Statement = ""
			return a
		}},
		{"unsupported protocol", daemon.RejectionUnsupportedProtocol, func() any {
			a := validAssignment()
			a.Version = 2
			return a
		}},
		{"malformed field type", daemon.RejectionMalformedAssignment, func() any {
			return map[string]any{
				"protocol_version": 1, "assignment_id": "assignment", "lease_token": "token",
				"lease_expires_at": 1790000030, "heartbeat_seconds": "soon", "job": map[string]any{},
			}
		}},
	} {
		t.Run(test.name, func(t *testing.T) {
			var rejection daemon.Result
			heartbeats, executions := 0, 0
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/v1/claim":
					json.NewEncoder(w).Encode(test.payload())
				case "/v1/assignments/assignment/result":
					json.NewDecoder(r.Body).Decode(&rejection)
					w.Write([]byte(`{"accepted":true}`))
				case "/v1/assignments/assignment/heartbeat":
					heartbeats++
				default:
					t.Errorf("unexpected route %s", r.URL.Path)
					w.WriteHeader(http.StatusNotFound)
				}
			}))
			defer server.Close()
			worker := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client(), Executor: executorFunc(func(context.Context, daemon.Job) (daemon.Execution, error) {
				executions++
				return daemon.Execution{}, nil
			})}
			worked, err := worker.Once(context.Background())
			if !worked || err == nil {
				t.Fatalf("worked=%v err=%v", worked, err)
			}
			if executions != 0 || heartbeats != 0 {
				t.Fatalf("executions=%d heartbeats=%d", executions, heartbeats)
			}
			if rejection.Status != "rejected" || rejection.Token != "token" || rejection.RejectionKind != test.kind || rejection.Error == "" {
				t.Fatalf("rejection=%+v", rejection)
			}
		})
	}
}

func TestWorkerCannotRejectWithoutUsableLeaseCredentials(t *testing.T) {
	for _, payload := range []string{
		`{"protocol_version":1,"assignment_id":"assignment","job":{}}`,
		`{"protocol_version":1,"assignment_id":"assignment"`,
	} {
		requests := 0
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			requests++
			if r.URL.Path != "/v1/claim" {
				t.Errorf("unexpected unauthenticated release request %s", r.URL.Path)
			}
			w.Write([]byte(payload))
		}))
		worker := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client(), Executor: executorFunc(func(context.Context, daemon.Job) (daemon.Execution, error) {
			t.Fatal("executor called")
			return daemon.Execution{}, nil
		})}
		worked, err := worker.Once(context.Background())
		server.Close()
		if !worked || err == nil || requests != 1 {
			t.Fatalf("worked=%v err=%v requests=%d", worked, err, requests)
		}
	}
}

func TestWorkerAcceptsCompleteRepairAssignment(t *testing.T) {
	parent := "parent"
	a := validAssignment()
	a.Job.ParentAttemptID, a.Job.RepairDepth = &parent, 1
	a.Job.Messages = []daemon.Message{{Role: "system", Content: "strategy"}, {Role: "user", Content: "repair feedback"}}
	called := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/v1/claim":
			json.NewEncoder(w).Encode(a)
		case "/v1/assignments/assignment/result":
			w.Write([]byte(`{"accepted":true}`))
		default:
			t.Errorf("unexpected route %s", r.URL.Path)
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	worker := daemon.Worker{URL: server.URL, ID: "test", Model: "scripted", Client: server.Client(), Executor: executorFunc(func(_ context.Context, job daemon.Job) (daemon.Execution, error) {
		called = true
		if job.RepairDepth != 1 || job.ParentAttemptID == nil || *job.ParentAttemptID != parent {
			t.Fatalf("unexpected repair job: %+v", job)
		}
		return daemon.Execution{Text: "rfl"}, nil
	})}
	worked, err := worker.Once(context.Background())
	if err != nil || !worked || !called {
		t.Fatalf("worked=%v called=%v err=%v", worked, called, err)
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
					a := validAssignment()
					a.ID, a.Job.TimeoutSeconds = "a", 1
					json.NewEncoder(w).Encode(a)
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
