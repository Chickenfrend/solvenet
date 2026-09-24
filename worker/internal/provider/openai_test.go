package provider

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"solvenet/worker/internal/daemon"
)

func TestOpenAIRequestAndUsage(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/v1/chat/completions" || r.Method != "POST" || r.Header.Get("Authorization") != "Bearer local-secret" {
			t.Errorf("request path, method or authorization incorrect")
		}
		var body struct {
			Model          string           `json:"model"`
			Messages       []daemon.Message `json:"messages"`
			MaxTokens      int              `json:"max_tokens"`
			ResponseFormat struct {
				Type string `json:"type"`
			} `json:"response_format"`
			Temperature *float64 `json:"temperature"`
			Seed        *int64   `json:"seed"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			t.Error(err)
		}
		if body.Model != "gpt-4o-mini" || body.MaxTokens != 128 || body.ResponseFormat.Type != "json_object" || body.Temperature == nil || *body.Temperature != 0 || body.Seed == nil || *body.Seed != 42 || len(body.Messages) != 4 || body.Messages[0].Content != proofInstructions || !strings.Contains(body.Messages[1].Content, ": True") || body.Messages[3].Content != "Previous Lean feedback" {
			t.Errorf("missing proof context/options: %+v", body)
		}
		json.NewEncoder(w).Encode(map[string]any{"model": "gpt-4o-mini-2024-07-18", "choices": []any{map[string]any{"finish_reason": "stop", "message": map[string]string{"content": `{"proof":"trivial"}`}}}, "usage": map[string]int{"prompt_tokens": 51, "completion_tokens": 7}})
	}))
	defer server.Close()
	o, err := NewOpenAI(server.URL+"/v1", "gpt-4o-mini", "local-secret")
	if err != nil {
		t.Fatal(err)
	}
	result, err := o.Execute(context.Background(), daemon.Job{Statement: ": True", Imports: []string{"Init"}, MaxOutputTokens: 128, Messages: []daemon.Message{{Role: "system", Content: "Try tactics"}, {Role: "user", Content: "Previous Lean feedback"}}, GenerationSettings: daemon.GenerationSettings{Temperature: floatPointer(0), Seed: int64Pointer(42)}})
	if err != nil || result.Text != "trivial" || result.Generation.RawResponse != `{"proof":"trivial"}` || result.Generation.Model != "gpt-4o-mini-2024-07-18" || result.Generation.FinishReason != "stop" || *result.Usage["input_tokens"] != 51 || *result.Usage["output_tokens"] != 7 || result.Generation.MaxOutputTokens != 128 {
		t.Fatalf("result=%+v err=%v", result, err)
	}
}

func TestOpenAIFailures(t *testing.T) {
	for _, tc := range []struct {
		name     string
		status   int
		body     string
		class    daemon.FailureClass
		category string
		want     string
	}{
		{"revoked", 401, `{"error":"local-secret invalid"}`, daemon.FailurePermanent, daemon.ProviderFailure, "HTTP 401"},
		{"rate-limited", 429, `{"error":"limit"}`, daemon.FailureTransient, daemon.ProviderFailure, "HTTP 429"},
		{"service", 503, `unavailable`, daemon.FailureTransient, daemon.ProviderFailure, "HTTP 503"},
		{"invalid-json", 200, `broken`, daemon.FailurePermanent, daemon.ProviderFailure, "invalid OpenAI response"},
		{"missing-choice", 200, `{}`, daemon.FailurePermanent, daemon.ProviderFailure, "no single text choice"},
		{"length", 200, `{"choices":[{"finish_reason":"length","message":{"content":"{\"proof\":\"rfl\"}"}}],"usage":{"completion_tokens":3}}`, daemon.FailurePermanent, daemon.ProviderFailure, "length"},
		{"invalid-proof", 200, `{"choices":[{"finish_reason":"stop","message":{"content":"not JSON"}}],"usage":{"completion_tokens":3}}`, daemon.FailurePermanent, daemon.FormattingFailure, "proof format"},
		{"oversized", 200, strings.Repeat("x", maxOpenAIResponse+1), daemon.FailurePermanent, daemon.ProviderFailure, "exceeded"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(tc.status); w.Write([]byte(tc.body)) }))
			defer s.Close()
			o, _ := NewOpenAI(s.URL, "gpt-4o-mini", "local-secret")
			result, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 10})
			if err == nil || !strings.Contains(err.Error(), tc.want) || strings.Contains(err.Error(), "local-secret") || daemon.FailureClassOf(err) != tc.class || daemon.FailureCategoryOf(err) != tc.category {
				t.Fatalf("result=%+v err=%v", result, err)
			}
			if tc.status != 200 && result.Generation.RawResponse != "" {
				t.Fatal("provider error body forwarded")
			}
			if tc.status == 401 && !o.AuthFailed() {
				t.Fatal("rejected credential must stop future worker claims")
			}
			if tc.name == "invalid-proof" && (*result.Usage["output_tokens"] != 3 || result.Generation.RawResponse != "not JSON") {
				t.Fatal("lost usage or raw output")
			}
		})
	}
}

func TestOpenAIRefusalKeepsUsageAndFinishMetadata(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"model":"gpt-4o-mini","choices":[{"finish_reason":"stop","message":{"content":null,"refusal":"denied"}}],"usage":{"prompt_tokens":42,"completion_tokens":3}}`))
	}))
	defer s.Close()
	o, err := NewOpenAI(s.URL, "gpt-4o-mini", "local-secret")
	if err != nil {
		t.Fatal(err)
	}
	result, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 10})
	if err == nil || !strings.Contains(err.Error(), "refused") || *result.Usage["input_tokens"] != 42 || *result.Usage["output_tokens"] != 3 || result.Generation.FinishReason != "stop" {
		t.Fatalf("refusal result=%+v err=%v", result, err)
	}
}

func TestOpenAICancellationAndRedirect(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/redirect" {
			t.Fatal("redirect followed")
		}
		if r.URL.Path == "/chat/completions" {
			http.Redirect(w, r, "/redirect", http.StatusTemporaryRedirect)
		}
	}))
	defer s.Close()
	o, _ := NewOpenAI(s.URL, "gpt-4o-mini", "local-secret")
	_, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 1})
	if err == nil || !strings.Contains(err.Error(), "HTTP 307") {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	_, err = o.Execute(ctx, daemon.Job{MaxOutputTokens: 1})
	if err == nil || !strings.Contains(err.Error(), "canceled") {
		t.Fatal(err)
	}
	ctx, cancel = context.WithTimeout(context.Background(), time.Nanosecond)
	defer cancel()
	_, err = o.Execute(ctx, daemon.Job{MaxOutputTokens: 1})
	if err == nil {
		t.Fatal("deadline ignored")
	}
}

func TestOpenAIRedactsEchoedKey(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"model":"local-secret","choices":[{"finish_reason":"local-secret","message":{"content":"local-secret"}}]}`))
	}))
	defer s.Close()
	o, _ := NewOpenAI(s.URL, "gpt-4o-mini", "local-secret")
	result, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 10})
	if err == nil || strings.Contains(err.Error(), o.Key) || strings.Contains(result.Generation.RawResponse, o.Key) || strings.Contains(result.Generation.Model, o.Key) || strings.Contains(result.Generation.FinishReason, o.Key) {
		t.Fatalf("credential leaked: result=%+v err=%v", result, err)
	}
}

func TestOpenAITimesOutInFlight(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		select {
		case <-r.Context().Done():
		case <-time.After(time.Second):
			t.Error("server did not observe request cancellation")
		}
	}))
	defer server.Close()
	o, _ := NewOpenAI(server.URL, "gpt-4o-mini", "local-secret")
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, err := o.Execute(ctx, daemon.Job{MaxOutputTokens: 10})
	if !errors.Is(err, context.DeadlineExceeded) || daemon.FailureCategoryOf(err) != daemon.ProviderFailure {
		t.Fatalf("timeout not propagated: %v", err)
	}
}
