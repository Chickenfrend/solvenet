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

func TestOpenAIDuplicateGeneratedEnvelopeRejected(t *testing.T) {
	const key = "synthetic-envelope-key"
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		for _, kind := range []string{"model.generate", "model.respond"} {
			field := "proof"
			if kind == "model.respond" {
				field = "text"
			}
			for _, first := range []string{"rfl", key, `\u0073ynthetic-envelope-key`} {
				for _, escapedField := range []bool{false, true} {
					t.Run(profile+"/"+kind+"/"+first+map[bool]string{false: "/literal-field", true: "/escaped-field"}[escapedField], func(t *testing.T) {
						secondField := field
						if escapedField {
							secondField = map[string]string{"proof": `\u0070roof`, "text": `\u0074ext`}[field]
						}
						raw := `{"` + field + `":"` + first + `","` + secondField + `":"rfl"}`
						calls := 0
						s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
							calls++
							content := raw
							if calls > 1 {
								content = `{"` + field + `":"rfl"}`
							}
							finish := "stop"
							usage := map[string]int{"prompt_tokens": 21, "completion_tokens": 8}
							if profile == ResponsesReasoning {
								finish = "completed"
								usage = map[string]int{"input_tokens": 21, "output_tokens": 8}
							}
							json.NewEncoder(w).Encode(profileReply(profile, content, finish, false, usage))
						}))
						defer s.Close()
						o, err := NewOpenAIWithConfig(s.URL, "fixture", key, OpenAIConfig{Profile: profile})
						if err != nil {
							t.Fatal(err)
						}
						job := daemon.Job{Kind: kind, MaxOutputTokens: 128}
						e, err := o.Execute(context.Background(), job)
						if err == nil || daemon.FailureClassOf(err) != daemon.FailurePermanent || daemon.FailureCategoryOf(err) != daemon.FormattingFailure || e.Text != "" {
							t.Fatalf("duplicate accepted or wrong failure: %+v %v", e, err)
						}
						if e.Generation.RawResponse != unavailableOpenAIRaw || e.Generation.RawResponseTruncated {
							t.Fatalf("unsafe diagnostic: %+v", e.Generation)
						}
						if *e.Usage["input_tokens"] != 21 || *e.Usage["output_tokens"] != 8 || e.Generation.TotalDurationNS == nil || *e.Generation.TotalDurationNS <= 0 {
							t.Fatalf("lost failure accounting: %+v", e)
						}
						encoded, _ := json.Marshal(e)
						if strings.Contains(string(encoded), key) || strings.Contains(err.Error(), key) {
							t.Fatal("credential leaked")
						}
						if h := o.Health(context.Background()); h.Status != "unobserved" {
							t.Fatalf("duplicate established compatibility: %+v", h)
						}
						good, err := o.Execute(context.Background(), job)
						if err != nil || good.Text != "rfl" || calls != 2 || o.Health(context.Background()).Status != "ready" {
							t.Fatalf("valid envelope changed: %+v %v calls=%d", good, err, calls)
						}
					})
				}
			}
		}
	}
}
