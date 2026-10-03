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

func TestOpenAIJobFailureEligibility(t *testing.T) {
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		for _, failure := range []string{"429", "malformed", "admission", "refusal", "context"} {
			t.Run(profile+"/"+failure, func(t *testing.T) {
				calls := 0
				s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls++
					finish := "stop"
					if profile == ResponsesReasoning {
						finish = "completed"
					}
					if calls == 1 && failure == "429" {
						w.WriteHeader(429)
						return
					}
					raw := `{"proof":"rfl"}`
					if calls == 1 && failure == "malformed" {
						raw = `{"proof":`
					}
					json.NewEncoder(w).Encode(profileReply(profile, raw, finish, calls == 1 && failure == "refusal", nil))
				}))
				defer s.Close()
				o, _ := NewOpenAIWithConfig(s.URL, "fixture", "synthetic-key", OpenAIConfig{Profile: profile})
				job := daemon.Job{Kind: "model.generate", MaxOutputTokens: 128}
				bad := job
				if failure == "admission" {
					bad.MaxOutputTokens = o.Config.MaxOutputTokens + 1
				}
				if failure == "context" {
					bad.Statement = strings.Repeat("x", o.Config.ContextBytes)
				}
				if _, err := o.Execute(context.Background(), bad); err == nil {
					t.Fatal("expected first failure")
				}
				if h := o.Health(context.Background()); h.Status != "unobserved" {
					t.Fatal(h)
				}
				for i := 0; i < 3; i++ {
					o.Health(context.Background())
				}
				if failure == "admission" || failure == "context" {
					if calls != 0 {
						t.Fatal("local failure sent provider request")
					}
				} else if calls != 1 {
					t.Fatal(calls)
				}
				if e, err := o.Execute(context.Background(), job); err != nil || e.Text != "rfl" {
					t.Fatalf("%+v %v", e, err)
				}
				if o.Health(context.Background()).Status != "ready" {
					t.Fatal("not recovered")
				}
				// A later local failure retains established compatibility.
				bad.MaxOutputTokens = o.Config.MaxOutputTokens + 1
				o.Execute(context.Background(), bad)
				if o.Health(context.Background()).Status != "ready" {
					t.Fatal("job-local failure revoked readiness")
				}
			})
		}
	}
}

func TestOpenAIProviderGlobalFailureIsSticky(t *testing.T) {
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		for _, code := range []int{401, 403, 404, 400} {
			t.Run(profile+"/"+http.StatusText(code), func(t *testing.T) {
				calls := 0
				s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					calls++
					w.WriteHeader(code)
					json.NewEncoder(w).Encode(map[string]any{"error": map[string]string{"code": "unsupported_parameter", "message": "synthetic-key"}})
				}))
				defer s.Close()
				o, _ := NewOpenAIWithConfig(s.URL, "fixture", "synthetic-key", OpenAIConfig{Profile: profile})
				for i := 0; i < 3; i++ {
					_, err := o.Execute(context.Background(), daemon.Job{Kind: "model.generate", MaxOutputTokens: 128})
					if err == nil || daemon.FailureClassOf(err) != daemon.FailurePermanent {
						t.Fatal(err)
					}
					if h := o.Health(context.Background()); h.Status != "unavailable" {
						t.Fatal(h)
					}
				}
				if calls != 1 {
					t.Fatalf("sticky failure made %d requests", calls)
				}
			})
		}
	}
}

func TestOpenAIExactFormalContent(t *testing.T) {
	const statement = `: "\n" = "\n"`
	const proof = `exact (show "\n" = "\n" from rfl)`
	const key = "fixture-secret"
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		for _, kind := range []string{"model.respond", "model.generate"} {
			for _, secret := range []bool{false, true} {
				t.Run(profile+"/"+kind+"/"+strings.TrimSpace(map[bool]string{false: "clean", true: "secret"}[secret]), func(t *testing.T) {
					graph := map[string]any{"graph_schema": "solvenet.graph.v1", "claims": []any{map[string]string{"key": "literal", "statement": statement}}, "artifacts": []any{map[string]string{"claim": "$literal", "proof": proof}}}
					if secret {
						graph["note"] = `{"\u0066ixture-secret":"\u0066ixture-secret","nested":"{\"note\":\"\\u0066ixture-secret\"}"}`
					}
					content := proof
					field := "proof"
					if kind == "model.respond" {
						field = "text"
						content, _ = encodeOpenAIRaw(graph)
					} else if secret {
						content += ` -- \u0066ixture-secret`
					}
					raw, _ := encodeOpenAIRaw(map[string]string{field: content})
					s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						finish := "stop"
						if profile == ResponsesReasoning {
							finish = "completed"
						}
						json.NewEncoder(w).Encode(profileReply(profile, raw, finish, false, nil))
					}))
					defer s.Close()
					o, _ := NewOpenAIWithConfig(s.URL, "fixture", key, OpenAIConfig{Profile: profile})
					e, err := o.Execute(context.Background(), daemon.Job{Kind: kind, Statement: statement, MaxOutputTokens: 128})
					if err != nil {
						t.Fatal(err)
					}
					if !secret && e.Text != content {
						t.Fatalf("changed formal bytes: %q != %q", e.Text, content)
					}
					if kind == "model.respond" {
						var got map[string]any
						if json.Unmarshal([]byte(e.Text), &got) != nil {
							t.Fatal(e.Text)
						}
						if got["claims"].([]any)[0].(map[string]any)["statement"] != statement || got["artifacts"].([]any)[0].(map[string]any)["proof"] != proof {
							t.Fatal("formal strings changed")
						}
					} else if e.Text != proof && e.Text != proof+" -- [redacted]" {
						t.Fatal(e.Text)
					}
					envelope, _ := encodeOpenAIRaw(map[string]string{"text": e.Text})
					assertNoRecoverableRawKey(t, envelope, key)
					assertNoRecoverableRawKey(t, e.Generation.RawResponse, key)
				})
			}
		}
	}
}

func TestOpenAIContentRejectsHiddenDuplicateSecrets(t *testing.T) {
	for _, text := range []string{
		`{"note":"fixture-secret","note":"safe"}`,
		`{"note":"safe","nested":"{\"note\":\"fixture-secret\",\"note\":\"safe\"}"}`,
		`{"note":"\u0066ixture-secret`,
	} {
		if output, err := redactOpenAIContent(text, "fixture-secret"); err == nil || output != "" {
			t.Fatalf("unsafe content accepted: %q %v", output, err)
		}
		assertNoRecoverableRawKey(t, safeOpenAIRaw(text, "fixture-secret").RawResponse, "fixture-secret")
	}
}
