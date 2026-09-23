package provider

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"solvenet/worker/internal/daemon"
)

func TestOllamaRequestAndMetadata(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/api/chat" {
			t.Errorf("unexpected request: %s %s", r.Method, r.URL.Path)
		}
		var request struct {
			Model    string           `json:"model"`
			Messages []daemon.Message `json:"messages"`
			Stream   bool             `json:"stream"`
			Format   struct {
				Type       string   `json:"type"`
				Required   []string `json:"required"`
				Additional bool     `json:"additionalProperties"`
			} `json:"format"`
			Options struct {
				Predict int `json:"num_predict"`
				Context int `json:"num_ctx"`
			} `json:"options"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request.Model != "qwen2.5-coder:7b" || request.Stream || request.Options.Predict != 256 || request.Options.Context != 4096 {
			t.Errorf("wrong model/options: %+v", request)
		}
		if request.Format.Type != "object" || len(request.Format.Required) != 1 || request.Format.Required[0] != "proof" || request.Format.Additional {
			t.Error("missing schema")
		}
		if len(request.Messages) != 3 || !strings.Contains(request.Messages[1].Content, "Mathlib") || !strings.Contains(request.Messages[1].Content, "n + 0 = n") || request.Messages[2].Content != "Previous attempt: refl" {
			t.Errorf("lost context: %+v", request.Messages)
		}
		json.NewEncoder(w).Encode(map[string]any{
			"model": "qwen2.5-coder:7b-reported", "message": map[string]string{"content": "{\"proof\":\"```lean\\nrfl\\n```\"}"},
			"done": true, "done_reason": "stop", "prompt_eval_count": 52, "eval_count": 12,
			"total_duration": 1000000, "load_duration": 100, "prompt_eval_duration": 200, "eval_duration": 300,
		})
	}))
	defer server.Close()
	o, err := NewOllama(server.URL, "qwen2.5-coder:7b")
	if err != nil {
		t.Fatal(err)
	}
	result, err := o.Execute(context.Background(), daemon.Job{
		Statement: "(n : Nat) : n + 0 = n", Imports: []string{"Mathlib"}, MaxOutputTokens: 256,
		Messages: []daemon.Message{{Role: "system", Content: "Return only a Lean tactic proof body."}, {Role: "user", Content: "Previous attempt: refl"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Text != "rfl" || *result.Usage["input_tokens"] != 52 || *result.Usage["output_tokens"] != 12 || result.Generation.Model != "qwen2.5-coder:7b-reported" || result.Generation.FinishReason != "stop" || *result.Generation.EvalDurationNS != 300 || !strings.Contains(result.Generation.RawResponse, "```lean") {
		t.Fatalf("lost output/metadata: %+v", result)
	}
}

func TestProofExtraction(t *testing.T) {
	for _, test := range []struct {
		raw, proof string
		valid      bool
	}{
		{`{"proof":" rfl "}`, "rfl", true},
		{"{\"proof\":\"```lean\\nrfl\\n```\"}", "rfl", true},
		{`{"proof":"refl"}`, "refl", true}, // Logic must not be silently corrected.
		{`{"proof":"intro h\nexact h"}`, "intro h\nexact h", true},
		{`{"proof":"example : True := by trivial"}`, "", false},
		{`{"proof":"by rfl"}`, "", false},
		{`{"proof":""}`, "", false},
		{`{"proof":null}`, "", false},
		{`{"proof":42}`, "", false},
		{`{"proof":"rfl","other":"x"}`, "", false},
		{`{"other":"rfl"}`, "", false},
		{`{"proof":"rfl`, "", false},
		{`rfl`, "", false},
		{`{"proof":"rfl"} {"proof":"trivial"}`, "", false},
		{"{\"proof\":\"```lean\\nrfl\\n```\\n```lean\\nrfl\\n```\"}", "", false},
	} {
		t.Run(test.raw, func(t *testing.T) {
			proof, err := extractProof(test.raw)
			if (err == nil) != test.valid || proof != test.proof {
				t.Fatalf("proof=%q err=%v", proof, err)
			}
		})
	}
}

func TestOllamaFailuresRetainOutput(t *testing.T) {
	for _, test := range []struct {
		name       string
		status     int
		body, want string
	}{
		{"not-found", 404, `{"error":"model not found"}`, "HTTP 404"},
		{"invalid-envelope", 200, `not JSON`, "invalid Ollama response JSON"},
		{"error-envelope", 200, `{"error":"runner stopped"}`, "reported an error"},
		{"invalid-proof", 200, `{"done":true,"message":{"content":"refl"},"eval_count":7}`, "proof format"},
		{"incomplete", 200, `{"done":false,"message":{"content":"partial"}}`, "incomplete"},
		{"truncated-json", 200, `{"done":true,"done_reason":"length","message":{"content":"{\"proof\":"}}`, "proof format"},
		{"large", 200, strings.Repeat("x", maxOllamaResponse+1), "exceeded 1 MiB"},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(test.status); w.Write([]byte(test.body)) }))
			defer server.Close()
			o, _ := NewOllama(server.URL, "test")
			result, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 10})
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("err=%v", err)
			}
			if result.Generation == nil || result.Generation.RawResponse == "" || len(result.Generation.RawResponse) > maxRawResponse {
				t.Fatal("missing or unbounded raw response")
			}
			if test.name == "invalid-proof" && *result.Usage["output_tokens"] != 7 {
				t.Fatal("lost usage on failure")
			}
			if test.name == "large" && !result.Generation.RawResponseTruncated {
				t.Fatal("missing truncation marker")
			}
		})
	}
}

func TestOllamaCancellation(t *testing.T) {
	started := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		close(started)
		// Flush headers so Execute is reading the response when cancelled.
		w.(http.Flusher).Flush()
		<-r.Context().Done()
	}))
	defer server.Close()
	o, _ := NewOllama(server.URL, "test")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { _, err := o.Execute(ctx, daemon.Job{MaxOutputTokens: 10}); done <- err }()
	<-started
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("err=%v", err)
		}
	case <-time.After(time.Second):
		t.Fatal("request ignored cancellation")
	}
}

func TestOllamaMissingUsageAndLengthFinish(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"done":true,"done_reason":"length","message":{"content":"{\"proof\":\"rfl\"}"}}`))
	}))
	defer server.Close()
	o, _ := NewOllama(server.URL, "test")
	result, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 10})
	if err != nil || result.Text != "rfl" || result.Usage["input_tokens"] != nil || result.Generation.FinishReason != "length" {
		t.Fatalf("result=%+v err=%v", result, err)
	}
}
