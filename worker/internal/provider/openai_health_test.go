package provider

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"solvenet/worker/internal/daemon"
)

func TestOpenAIHealthNoAutomaticCalls(t *testing.T) {
	calls := 0
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		if body["max_tokens"] != float64(256) {
			t.Errorf("unexpected cap: %v", body)
		}
		_, _ = w.Write([]byte(`{"choices":[{"message":{"content":"{\"text\":\"ok\"}"},"finish_reason":"stop"}]}`))
	}))
	defer s.Close()
	o, _ := NewOpenAI(s.URL, "gpt-4o-mini", "mock-key")
	for i := 0; i < 3; i++ {
		if h := o.Check(context.Background(), false); h.Status != "unobserved" {
			t.Fatal(h)
		}
	}
	if calls != 0 {
		t.Fatal("startup made a request")
	}
	if h := o.Check(context.Background(), true); h.Status != "ready" {
		t.Fatal(h)
	}
	if calls != 1 {
		t.Fatal(calls)
	}
}

func TestOpenAIHealthFailuresSanitized(t *testing.T) {
	for _, tc := range []struct {
		code   int
		reason string
	}{
		{401, "OpenAI credential rejected"}, {403, "OpenAI credential rejected"},
		{400, "OpenAI profile or model access unsupported"}, {404, "OpenAI model unavailable"},
		{429, "OpenAI rate limited"}, {503, "OpenAI service unavailable"},
	} {
		t.Run(tc.reason+http.StatusText(tc.code), func(t *testing.T) {
			calls := 0
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				w.WriteHeader(tc.code)
				_, _ = w.Write([]byte(`{"error":{"message":"mock-secret\u002dkey"}}`))
			}))
			defer s.Close()
			o, _ := NewOpenAI(s.URL, "gpt-4o-mini", "mock-secret-key")
			h := o.Check(context.Background(), true)
			wantStatus, wantReason := "unavailable", tc.reason
			if tc.code == 400 {
				wantStatus, wantReason = "unobserved", "OpenAI compatibility check inconclusive"
			}
			if tc.code == 429 || tc.code >= 500 {
				wantStatus, wantReason = "unobserved", tc.reason+" (recovering via scheduled jobs)"
			}
			if h.Status != wantStatus || h.Reason != wantReason {
				t.Fatal(h)
			}
			b, _ := json.Marshal(h)
			if strings.Contains(string(b), "mock") {
				t.Fatal("credential leaked")
			}
			if o.AuthFailed() {
				o.Check(context.Background(), true)
				if calls != 1 {
					t.Fatal("repeated request after auth failure")
				}
			}
		})
	}
}

func TestResponsesHealthIncompleteIsInconclusive(t *testing.T) {
	for _, status := range []string{"incomplete", "completed"} {
		t.Run(status, func(t *testing.T) {
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				var body map[string]any
				_ = json.NewDecoder(r.Body).Decode(&body)
				if body["max_output_tokens"] != float64(1024) || r.URL.Path != "/responses" {
					t.Errorf("bad request %v", body)
				}
				_, _ = w.Write([]byte(`{"status":"` + status + `","output":[{"type":"message","role":"assistant","status":"completed","content":[{"type":"output_text","text":"{\"text\":\"ok\"}"}]}]}`))
			}))
			defer s.Close()
			o, _ := NewOpenAIWithConfig(s.URL, "custom-model", "mock-key", OpenAIConfig{Profile: ResponsesReasoning})
			h := o.Check(context.Background(), true)
			want := "ready"
			if status == "incomplete" {
				want = "unobserved"
			}
			if h.Status != want {
				t.Fatal(h)
			}
		})
	}
}

func TestOpenAIHealthNetworkAndCancellation(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	s.Close()
	o, _ := NewOpenAI(s.URL, "gpt-4o-mini", "mock-key")
	if h := o.Check(context.Background(), true); h.Reason != "OpenAI network unavailable (recovering via scheduled jobs)" {
		t.Fatal(h)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if h := o.Check(ctx, true); h.Reason != "OpenAI deadline exceeded" {
		t.Fatal(h)
	}
}

func TestResponsesOperationFailurePublicHealth(t *testing.T) {
	for _, tc := range []struct{ code, reason string }{
		{"rate_limit_exceeded", "OpenAI rate limited"},
		{"server_error", "OpenAI service unavailable"},
	} {
		t.Run(tc.code, func(t *testing.T) {
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				_, _ = w.Write([]byte(`{"status":"failed","error":{"code":"` + tc.code + `","message":"mock-key"}}`))
			}))
			defer s.Close()
			o, _ := NewOpenAIWithConfig(s.URL, "custom-model", "mock-key", OpenAIConfig{Profile: ResponsesReasoning})
			if h := o.Check(context.Background(), true); h.Status != "unobserved" || h.Reason != tc.reason+" (recovering via scheduled jobs)" {
				t.Fatal(h)
			}
		})
	}
}

func TestOpenAIHealthClaimBoundary(t *testing.T) {
	// An escaped upstream echo must not enter result metadata or public health.
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"model":"mock\\u002dkey","choices":[{"message":{"content":"{\"text\":\"ok\"}"},"finish_reason":"stop"}]}`))
	}))
	defer s.Close()
	o, _ := NewOpenAI(s.URL, "gpt-4o-mini", "mock-key")
	e, err := o.Execute(context.Background(), daemon.Job{Kind: "model.respond", MaxOutputTokens: 256})
	if err != nil {
		t.Fatal(err)
	}
	b, _ := json.Marshal(e)
	if strings.Contains(string(b), "mock") {
		t.Fatal("escaped credential in result metadata")
	}
}
