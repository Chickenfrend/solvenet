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
	"unicode/utf8"

	"solvenet/worker/internal/daemon"
)

func TestOllamaRequestAndMetadata(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/tags" {
			json.NewEncoder(w).Encode(map[string]any{"models": []any{map[string]string{"name": "qwen2.5-coder:7b", "digest": strings.Repeat("a", 64)}}})
			return
		}
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
				Predict     int      `json:"num_predict"`
				Context     int      `json:"num_ctx"`
				Temperature *float64 `json:"temperature"`
				Seed        *int64   `json:"seed"`
			} `json:"options"`
		}
		if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
			t.Fatal(err)
		}
		if request.Model != "qwen2.5-coder:7b" || request.Stream || request.Options.Predict != 256 || request.Options.Context != 8192 {
			t.Errorf("wrong model/options: %+v", request)
		}
		if request.Options.Temperature == nil || *request.Options.Temperature != 0 || request.Options.Seed == nil || *request.Options.Seed != 42 {
			t.Errorf("missing controlled options: %+v", request.Options)
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
		GenerationSettings: daemon.GenerationSettings{Temperature: floatPointer(0), Seed: int64Pointer(42)},
		Messages:           []daemon.Message{{Role: "user", Content: "Try simplification before rewriting."}, {Role: "user", Content: "Previous attempt: refl"}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if result.Text != "rfl" || *result.Usage["input_tokens"] != 52 || *result.Usage["output_tokens"] != 12 || result.Generation.Model != "qwen2.5-coder:7b-reported" || result.Generation.FinishReason != "stop" || result.Generation.ContextLength != 8192 || result.Generation.MaxOutputTokens != 256 || *result.Generation.EvalDurationNS != 300 || !strings.Contains(result.Generation.RawResponse, "```lean") {
		t.Fatalf("lost output/metadata: %+v", result)
	}
	if result.Generation.ModelDigest != "sha256:"+strings.Repeat("a", 64) || *result.Generation.Temperature != 0 || *result.Generation.Seed != 42 {
		t.Fatalf("lost settings/digest: %+v", result.Generation)
	}
}

func floatPointer(v float64) *float64 { return &v }
func int64Pointer(v int64) *int64     { return &v }

func TestOllamaDoesNotDeduplicateCoordinatorMessagesByContent(t *testing.T) {
	statement := ": True"
	var messages []daemon.Message
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/tags" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
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
		{`{"proof":"by intro h; exact h"}`, "intro h; exact h", true},
		{`{"proof":"by\n  intro h\n  exact h"}`, "intro h\n  exact h", true},
		{"{\"proof\":\"```lean\\nby rfl\\n```\"}", "rfl", true},
		{`{"proof":"example : True := by trivial"}`, "", false},
		{`{"proof":"by"}`, "", false},
		{`{"proof":"by by rfl"}`, "", false},
		{`{"proof":"byte"}`, "byte", true},
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

func TestOllamaEnclosingByPreservesRawResponse(t *testing.T) {
	raw := `{"proof":"by intro h; exact h"}`
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{
			"done": true, "message": map[string]string{"content": raw},
		})
	}))
	defer server.Close()
	o, err := NewOllama(server.URL, "test", DefaultOllamaContext)
	if err != nil {
		t.Fatal(err)
	}
	result, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 256})
	if err != nil || result.Text != "intro h; exact h" || result.Generation.RawResponse != raw {
		t.Fatalf("result=%+v err=%v", result, err)
	}
}

func TestOllamaFailuresRetainOutput(t *testing.T) {
	for _, test := range []struct {
		name       string
		status     int
		body, want string
		class      daemon.FailureClass
		category   string
	}{
		{"not-found", 404, `{"error":"model not found"}`, "HTTP 404", daemon.FailurePermanent, daemon.ProviderFailure},
		{"unavailable", 503, `service unavailable`, "HTTP 503", daemon.FailureTransient, daemon.ProviderFailure},
		{"invalid-envelope", 200, `not JSON`, "invalid Ollama response JSON", daemon.FailurePermanent, daemon.ProviderFailure},
		{"error-envelope", 200, `{"error":"runner stopped"}`, "reported an error", daemon.FailureTransient, daemon.ProviderFailure},
		{"invalid-proof", 200, `{"done":true,"message":{"content":"refl"},"eval_count":7}`, "proof format", daemon.FailurePermanent, daemon.FormattingFailure},
		{"incomplete", 200, `{"done":false,"message":{"content":"partial"}}`, "incomplete", daemon.FailurePermanent, daemon.ProviderFailure},
		{"truncated-json", 200, `{"done":true,"done_reason":"length","message":{"content":"{\"proof\":"}}`, "proof format", daemon.FailurePermanent, daemon.FormattingFailure},
		{"large", 200, strings.Repeat("x", maxOllamaResponse+1), "exceeded 1 MiB", daemon.FailurePermanent, daemon.ProviderFailure},
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
			if category := daemon.FailureCategoryOf(err); category != test.category {
				t.Fatalf("category=%q, want %q", category, test.category)
			}
			if result.Generation == nil || result.Generation.RawResponse == "" || len(result.Generation.RawResponse) > daemon.MaxRawResponseBytes {
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

func TestOllamaTransportAndSettingsCategories(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	o, _ := NewOllama(server.URL, "test", 20)
	server.Close()
	for _, test := range []struct {
		name  string
		job   daemon.Job
		class daemon.FailureClass
	}{
		{"transport", daemon.Job{MaxOutputTokens: 10}, daemon.FailureTransient},
		{"context-size", daemon.Job{MaxOutputTokens: 20}, daemon.FailurePermanent},
	} {
		t.Run(test.name, func(t *testing.T) {
			_, err := o.Execute(context.Background(), test.job)
			if err == nil || daemon.FailureCategoryOf(err) != daemon.ProviderFailure || daemon.FailureClassOf(err) != test.class {
				t.Fatalf("err=%v category=%q class=%q", err, daemon.FailureCategoryOf(err), daemon.FailureClassOf(err))
			}
		})
	}
}

func TestOllamaCancellation(t *testing.T) {
	started := make(chan struct{})
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/tags" {
			json.NewEncoder(w).Encode(map[string]any{"models": []any{map[string]string{"name": "test", "digest": strings.Repeat("a", 64)}}})
			return
		}
		if r.URL.Path != "/api/chat" {
			t.Errorf("unexpected path: %s", r.URL.Path)
			return
		}
		// Flush headers so Execute is reading the response when cancelled.
		w.(http.Flusher).Flush()
		close(started)
		<-r.Context().Done()
	}))
	defer server.Close()
	o, _ := NewOllama(server.URL, "test", DefaultOllamaContext)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	type outcome struct {
		result daemon.Execution
		err    error
	}
	done := make(chan outcome, 1)
	go func() {
		result, err := o.Execute(ctx, daemon.Job{MaxOutputTokens: 10, GenerationSettings: daemon.GenerationSettings{Temperature: floatPointer(0), Seed: int64Pointer(42)}})
		done <- outcome{result, err}
	}()
	<-started
	cancel()
	select {
	case got := <-done:
		if !errors.Is(got.err, context.Canceled) {
			t.Fatalf("err=%v", got.err)
		}
		if got.result.Generation == nil || got.result.Generation.ModelDigest != "sha256:"+strings.Repeat("a", 64) || got.result.Generation.ContextLength != DefaultOllamaContext || got.result.Generation.MaxOutputTokens != 10 || got.result.Generation.Temperature == nil || *got.result.Generation.Temperature != 0 || got.result.Generation.Seed == nil || *got.result.Generation.Seed != 42 {
			t.Fatalf("lost generation metadata on chat cancellation: %+v", got.result.Generation)
		}
	case <-time.After(time.Second):
		t.Fatal("request ignored cancellation")
	}
}

func TestOllamaStalledTagsDoesNotBlockChat(t *testing.T) {
	for _, test := range []struct {
		name     string
		budget   time.Duration
		status   int
		body     string
		wantText string
	}{
		{"normal-deadline", time.Second, 200, `{"done":true,"message":{"content":"{\"proof\":\"rfl\"}"}}`, "rfl"},
		{"short-deadline", 400 * time.Millisecond, 200, `{"done":true,"message":{"content":"{\"proof\":\"rfl\"}"}}`, "rfl"},
		{"chat-failure", time.Second, 503, `service unavailable`, ""},
	} {
		t.Run(test.name, func(t *testing.T) {
			tagsCancelled := make(chan struct{})
			chatCalled := make(chan struct{}, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/api/tags":
					<-r.Context().Done()
					close(tagsCancelled)
				case "/api/chat":
					chatCalled <- struct{}{}
					w.WriteHeader(test.status)
					w.Write([]byte(test.body))
				default:
					t.Errorf("unexpected path: %s", r.URL.Path)
				}
			}))
			defer server.Close()
			o, _ := NewOllama(server.URL, "test", DefaultOllamaContext)
			ctx, cancel := context.WithTimeout(context.Background(), test.budget)
			defer cancel()
			result, err := o.Execute(ctx, daemon.Job{MaxOutputTokens: 10, GenerationSettings: daemon.GenerationSettings{Temperature: floatPointer(0), Seed: int64Pointer(42)}})
			select {
			case <-tagsCancelled:
			default:
				t.Fatal("stalled tags lookup was not cancelled")
			}
			select {
			case <-chatCalled:
			default:
				t.Fatal("chat was not called")
			}
			if ctx.Err() != nil {
				t.Fatalf("digest lookup consumed generation deadline: %v", ctx.Err())
			}
			if test.status == 200 {
				if err != nil || result.Text != test.wantText {
					t.Fatalf("result=%+v err=%v", result, err)
				}
			} else if err == nil || daemon.FailureClassOf(err) != daemon.FailureTransient {
				t.Fatalf("chat failure err=%v", err)
			}
			if result.Generation == nil || result.Generation.ModelDigest != "" || result.Generation.ContextLength != DefaultOllamaContext || result.Generation.MaxOutputTokens != 10 || result.Generation.Temperature == nil || *result.Generation.Temperature != 0 || result.Generation.Seed == nil || *result.Generation.Seed != 42 {
				t.Fatalf("lost generation settings: %+v", result.Generation)
			}
			if test.status != 200 && result.Generation.RawResponse != test.body {
				t.Fatalf("lost chat failure response: %+v", result.Generation)
			}
		})
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

func TestRawGenerationUTF8ByteBoundary(t *testing.T) {
	for _, extra := range []int{0, 1} {
		raw := strings.Repeat("é", daemon.MaxRawResponseBytes/2) + strings.Repeat("x", extra)
		generation := rawGeneration(raw)
		if len(generation.RawResponse) != daemon.MaxRawResponseBytes || generation.RawResponseTruncated != (extra == 1) {
			t.Fatalf("extra=%d bytes=%d truncated=%v", extra, len(generation.RawResponse), generation.RawResponseTruncated)
		}
	}
	// Clipping inside a multibyte rune keeps a valid, bounded UTF-8 prefix.
	raw := strings.Repeat("é", daemon.MaxRawResponseBytes/2-1) + "€x"
	generation := rawGeneration(raw)
	if !generation.RawResponseTruncated || !utf8.ValidString(generation.RawResponse) || len(generation.RawResponse) > daemon.MaxRawResponseBytes {
		t.Fatalf("invalid clipped prefix: %+v", generation)
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
