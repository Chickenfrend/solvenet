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
		if request.Model != "qwen2.5-coder:7b" || request.Stream || request.Options.Predict != 256 || request.Options.Context != 8192 {
			t.Errorf("wrong model/options: %+v", request)
		}
		if request.Format.Type != "object" || len(request.Format.Required) != 1 || request.Format.Required[0] != "proof" || request.Format.Additional {
			t.Error("missing schema")
		}
		if len(request.Messages) != 4 || request.Messages[0].Role != "system" || request.Messages[0].Content != proofInstructions ||
			request.Messages[1].Role != "user" || !strings.Contains(request.Messages[1].Content, "Mathlib") || !strings.Contains(request.Messages[1].Content, "n + 0 = n") ||
			request.Messages[2].Content != "Try simplification before rewriting." || request.Messages[3].Role != "user" || request.Messages[3].Content != "Previous attempt: refl" {
			t.Errorf("lost context: %+v", request.Messages)
		}
		prompt := ""
		for _, message := range request.Messages {
			prompt += message.Content
		}
		if strings.Count(prompt, "(n : Nat) : n + 0 = n") != 1 {
			t.Errorf("theorem was not included exactly once: %+v", request.Messages)
		}
		json.NewEncoder(w).Encode(map[string]any{
			"model": "qwen2.5-coder:7b-reported", "message": map[string]string{"content": "{\"proof\":\"```lean\\nrfl\\n```\"}"},
			"done": true, "done_reason": "stop", "prompt_eval_count": 52, "eval_count": 12,
			"total_duration": 1000000, "load_duration": 100, "prompt_eval_duration": 200, "eval_duration": 300,
		})
	}))
	defer server.Close()
	o, err := NewOllama(server.URL, "qwen2.5-coder:7b", 8192)
	if err != nil {
		t.Fatal(err)
	}
	result, err := o.Execute(context.Background(), daemon.Job{
		Statement: "(n : Nat) : n + 0 = n", Imports: []string{"Mathlib"}, MaxOutputTokens: 256,
		Messages: []daemon.Message{{Role: "user", Content: "Try simplification before rewriting."}, {Role: "user", Content: "Previous attempt: refl"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Text != "rfl" || *result.Usage["input_tokens"] != 52 || *result.Usage["output_tokens"] != 12 || result.Generation.Model != "qwen2.5-coder:7b-reported" || result.Generation.FinishReason != "stop" || result.Generation.ContextLength != 8192 || result.Generation.MaxOutputTokens != 256 || *result.Generation.EvalDurationNS != 300 || !strings.Contains(result.Generation.RawResponse, "```lean") {
		t.Fatalf("lost output/metadata: %+v", result)
	}
}

func TestOllamaDoesNotDeduplicateCoordinatorMessagesByContent(t *testing.T) {
	statement := ": True"
	var messages []daemon.Message
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var request struct {
			Messages []daemon.Message `json:"messages"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		messages = request.Messages
		json.NewEncoder(w).Encode(map[string]any{
			"done": true, "message": map[string]string{"content": `{"proof":"trivial"}`},
		})
	}))
	defer server.Close()
	o, _ := NewOllama(server.URL, "test", DefaultOllamaContext)
	_, err := o.Execute(context.Background(), daemon.Job{
		Statement: statement, Imports: []string{"Init"}, MaxOutputTokens: 10,
		Messages: []daemon.Message{{Role: "user", Content: statement}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if len(messages) != 3 || messages[2].Content != statement {
		t.Fatalf("coordinator message was removed by content: %+v", messages)
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
		class      daemon.FailureClass
	}{
		{"not-found", 404, `{"error":"model not found"}`, "HTTP 404", daemon.FailurePermanent},
		{"unavailable", 503, `service unavailable`, "HTTP 503", daemon.FailureTransient},
		{"invalid-envelope", 200, `not JSON`, "invalid Ollama response JSON", daemon.FailurePermanent},
		{"error-envelope", 200, `{"error":"runner stopped"}`, "reported an error", daemon.FailureTransient},
		{"invalid-proof", 200, `{"done":true,"message":{"content":"refl"},"eval_count":7}`, "proof format", daemon.FailurePermanent},
		{"incomplete", 200, `{"done":false,"message":{"content":"partial"}}`, "incomplete", daemon.FailurePermanent},
		{"truncated-json", 200, `{"done":true,"done_reason":"length","message":{"content":"{\"proof\":"}}`, "proof format", daemon.FailurePermanent},
		{"large", 200, strings.Repeat("x", maxOllamaResponse+1), "exceeded 1 MiB", daemon.FailurePermanent},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(test.status); w.Write([]byte(test.body)) }))
			defer server.Close()
			o, _ := NewOllama(server.URL, "test", DefaultOllamaContext)
			result, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 10})
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("err=%v", err)
			}
			if class := daemon.FailureClassOf(err); class != test.class {
				t.Fatalf("class=%q, want %q", class, test.class)
			}
			if result.Generation == nil || result.Generation.RawResponse == "" || len(result.Generation.RawResponse) > maxRawResponse {
				t.Fatal("missing or unbounded raw response")
			}
			if result.Generation.ContextLength != DefaultOllamaContext || result.Generation.MaxOutputTokens != 10 {
				t.Fatalf("request metadata disagrees with options: %+v", result.Generation)
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
	o, _ := NewOllama(server.URL, "test", DefaultOllamaContext)
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
	o, _ := NewOllama(server.URL, "test", DefaultOllamaContext)
	result, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 10})
	if err != nil || result.Text != "rfl" || result.Usage["input_tokens"] != nil || result.Generation.FinishReason != "length" {
		t.Fatalf("result=%+v err=%v", result, err)
	}
}

func TestOllamaContextValidation(t *testing.T) {
	for _, contextSize := range []int{0, -1, MaxOllamaContext + 1} {
		if _, err := NewOllama("http://localhost:11434", "test", contextSize); err == nil || !strings.Contains(err.Error(), "ollama-context") {
			t.Fatalf("context=%d err=%v", contextSize, err)
		}
	}
	if _, err := NewOllama("http://localhost:11434", "test", MaxOllamaContext); err != nil {
		t.Fatalf("maximum context rejected: %v", err)
	}
}

func TestOllamaRejectsIncompatibleOutputBudget(t *testing.T) {
	o, err := NewOllama("http://localhost:11434", "test", 256)
	if err != nil {
		t.Fatal(err)
	}
	for _, outputTokens := range []int{256, 257} {
		_, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: outputTokens})
		if err == nil || !strings.Contains(err.Error(), "job.max_output_tokens") || !strings.Contains(err.Error(), "-ollama-context") {
			t.Fatalf("output=%d err=%v", outputTokens, err)
		}
		if daemon.FailureClassOf(err) != daemon.FailurePermanent {
			t.Fatalf("output=%d was not classified permanent", outputTokens)
		}
	}
}
