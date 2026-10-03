package provider

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"solvenet/worker/internal/daemon"
)

func profileReply(profile, text, finish string, refusal bool, usage any) map[string]any {
	if profile == ChatJSON {
		message := map[string]any{"content": text}
		if refusal {
			message["refusal"] = "private refusal"
		}
		return map[string]any{"model": "arbitrary-snapshot", "choices": []any{map[string]any{"message": message, "finish_reason": finish}}, "usage": usage}
	}
	content := map[string]any{"type": "output_text", "text": text}
	if refusal {
		content = map[string]any{"type": "refusal", "refusal": "private refusal"}
	}
	return map[string]any{"model": "arbitrary-snapshot", "status": finish, "output": []any{
		map[string]any{"type": "reasoning", "summary": []any{}},
		map[string]any{"type": "message", "role": "assistant", "status": "completed", "content": []any{content}}}, "usage": usage}
}

func TestOpenAIProfilesContracts(t *testing.T) {
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		for _, kind := range []string{"model.respond", "model.generate"} {
			for _, outcome := range []string{"valid", "missing-usage", "null-tokens", "zero-tokens", "refusal", "incomplete", "malformed", "bad-nesting", "raw-bound", "401", "403", "429", "400", "404"} {
				t.Run(profile+"/"+kind+"/"+outcome, func(t *testing.T) {
					graph := `{"graph_schema":"solvenet.graph.v1","claims":[{"key":"a","statement":": True","imports":["Init"],"environment":"pinned"}],"findings":[{"key":"f","claim":"$a","text":"No useful decomposition yet."}]}`
					field, value := "proof", "trivial"
					if kind == "model.respond" {
						field, value = "text", graph
					}
					outer, _ := json.Marshal(map[string]string{field: value})
					finish := "stop"
					usage := map[string]any{"prompt_tokens": 21, "completion_tokens": 8}
					if profile == ResponsesReasoning {
						finish = "completed"
						usage = map[string]any{"input_tokens": 21, "output_tokens": 8}
					}
					var suppliedUsage any = usage
					if outcome == "missing-usage" {
						suppliedUsage = nil
					}
					if outcome == "null-tokens" {
						for k := range usage {
							usage[k] = nil
						}
					}
					if outcome == "zero-tokens" {
						for k := range usage {
							usage[k] = 0
						}
					}
					if outcome == "incomplete" {
						finish = "length"
						if profile == ResponsesReasoning {
							finish = "incomplete"
						}
					}
					if outcome == "malformed" {
						outer = []byte(`{"text":null}`)
					}
					if outcome == "bad-nesting" {
						outer, _ = json.Marshal(map[string]any{field: map[string]any{"graph_schema": "solvenet.graph.v1"}})
					}
					if outcome == "raw-bound" {
						outer = []byte(strings.Repeat("x", daemon.MaxRawResponseBytes+1))
					}
					s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						if r.Header.Get("Authorization") != "Bearer fixture-secret" {
							t.Error("incorrect credential")
						}
						var request map[string]json.RawMessage
						if json.NewDecoder(r.Body).Decode(&request) != nil {
							t.Error("invalid request")
						}
						if string(request["model"]) != `"unfamiliar/unchanged-model"` {
							t.Error("model redirected")
						}
						if profile == ChatJSON {
							if r.URL.Path != "/chat/completions" || string(request["max_tokens"]) != "128" || request["max_output_tokens"] != nil || string(request["response_format"]) != `{"type":"json_object"}` {
								t.Errorf("bad chat request: %s", request)
							}
						} else {
							if r.URL.Path != "/responses" || string(request["max_output_tokens"]) != "128" || request["max_tokens"] != nil || request["temperature"] != nil || request["seed"] != nil || string(request["store"]) != "false" || string(request["reasoning"]) != `{"effort":"low"}` {
								t.Errorf("bad Responses request: %s", request)
							}
							var text struct {
								Format struct {
									Type   string
									Strict bool
									Schema struct{ Required []string }
								}
							}
							json.Unmarshal(request["text"], &text)
							if text.Format.Type != "json_schema" || !text.Format.Strict || len(text.Format.Schema.Required) != 1 || text.Format.Schema.Required[0] != field {
								t.Error("bad output schema")
							}
							var input []daemon.Message
							json.Unmarshal(request["input"], &input)
							if len(input) != 3 || input[0].Role != "developer" || !strings.Contains(input[1].Content, ": True") || input[2].Content != "frozen graph packet" {
								t.Error("lost prompt")
							}
						}
						for code, name := range map[int]string{401: "401", 403: "403", 429: "429", 400: "400", 404: "404"} {
							if outcome == name {
								w.WriteHeader(code)
								w.Write([]byte("fixture-secret private account details"))
								return
							}
						}
						json.NewEncoder(w).Encode(profileReply(profile, string(outer), finish, outcome == "refusal", suppliedUsage))
					}))
					defer s.Close()
					config := OpenAIConfig{Profile: profile}
					if profile == ResponsesReasoning {
						config.ReasoningEffort = "low"
					}
					o, err := NewOpenAIWithConfig(s.URL, "unfamiliar/unchanged-model", "fixture-secret", config)
					if err != nil {
						t.Fatal(err)
					}
					result, err := o.Execute(context.Background(), daemon.Job{Kind: kind, Model: "openai/job-cannot-redirect", Statement: ": True", Imports: []string{"Init"}, MaxOutputTokens: 128, Messages: []daemon.Message{{Role: "user", Content: "frozen graph packet"}}})
					ok := outcome == "valid" || outcome == "missing-usage" || outcome == "null-tokens" || outcome == "zero-tokens"
					outputMatches := result.Text == value
					if ok && kind == "model.respond" {
						actual, actualErr := decodeRawJSON(result.Text)
						expected, expectedErr := decodeRawJSON(value)
						outputMatches = actualErr == nil && expectedErr == nil && reflect.DeepEqual(actual, expected)
					}
					if ok && (err != nil || !outputMatches) || !ok && err == nil {
						t.Fatalf("result=%+v err=%v", result, err)
					}
					encoded, _ := json.Marshal(result)
					if strings.Contains(string(encoded), "fixture-secret") || err != nil && (strings.Contains(err.Error(), "private") || strings.Contains(err.Error(), "fixture-secret")) {
						t.Fatal("provider details leaked")
					}
					if result.Generation.TotalDurationNS == nil || *result.Generation.TotalDurationNS <= 0 {
						t.Fatal("missing provider elapsed time")
					}
					if outcome == "missing-usage" || outcome == "null-tokens" {
						if result.Usage["input_tokens"] != nil || result.Usage["output_tokens"] != nil {
							t.Fatal("unknown usage became known")
						}
					}
					if outcome == "zero-tokens" && (result.Usage["input_tokens"] == nil || *result.Usage["input_tokens"] != 0) {
						t.Fatal("actual zero usage lost")
					}
					if outcome == "refusal" || outcome == "incomplete" || outcome == "malformed" || outcome == "bad-nesting" {
						if result.Usage["output_tokens"] == nil || *result.Usage["output_tokens"] != 8 {
							t.Fatal("failure lost usage")
						}
					}
					if outcome == "401" || outcome == "403" {
						if !o.AuthFailed() || daemon.FailureClassOf(err) != daemon.FailurePermanent {
							t.Fatal("auth category lost")
						}
					}
					if outcome == "429" && daemon.FailureClassOf(err) != daemon.FailureTransient {
						t.Fatal("rate category lost")
					}
					if len(result.Generation.RawResponse) > daemon.MaxRawResponseBytes {
						t.Fatal("raw output unbounded")
					}
				})
			}
		}
	}
}

func TestOpenAIProfileLocalValidation(t *testing.T) {
	for _, config := range []OpenAIConfig{{}, {Profile: "unknown"}, {Profile: ChatJSON, ReasoningEffort: "low"}, {Profile: ResponsesReasoning, ReasoningEffort: "none"}, {Profile: ChatJSON, ContextTokens: -1}, {Profile: ChatJSON, MaxOutputTokens: 32769}} {
		if _, err := NewOpenAIWithConfig("http://127.0.0.1:1", "new-model", "fixture-secret", config); err == nil {
			t.Fatalf("accepted invalid config %+v", config)
		}
	}
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		calls := 0
		s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(500) }))
		o, _ := NewOpenAIWithConfig(s.URL, "new-model", "fixture-secret", OpenAIConfig{Profile: profile, ContextTokens: 2048, ContextBytes: 2048, MaxOutputTokens: 128})
		jobs := []daemon.Job{{MaxOutputTokens: 129}, {MaxOutputTokens: 128, Messages: []daemon.Message{{Role: "user", Content: strings.Repeat("x", 2048)}}}, {MaxOutputTokens: 128, GenerationSettings: daemon.GenerationSettings{Temperature: floatPointer(3)}}}
		if profile == ResponsesReasoning {
			jobs = append(jobs, daemon.Job{MaxOutputTokens: 128, GenerationSettings: daemon.GenerationSettings{Seed: int64Pointer(1)}})
		}
		for _, job := range jobs {
			if _, err := o.Execute(context.Background(), job); err == nil {
				t.Fatal("invalid job accepted")
			}
		}
		if calls != 0 {
			t.Fatal("bad inputs reached provider")
		}
		s.Close()
	}
}

func TestOpenAIResponsesCancellationAndRedirect(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, "/forbidden", 307) }))
	defer s.Close()
	o, _ := NewOpenAIWithConfig(s.URL, "new-model", "fixture-secret", OpenAIConfig{Profile: ResponsesReasoning})
	if _, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 1}); err == nil || !strings.Contains(err.Error(), "307") {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := o.Execute(ctx, daemon.Job{MaxOutputTokens: 1}); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	ctx, cancel = context.WithTimeout(context.Background(), time.Nanosecond)
	defer cancel()
	if _, err := o.Execute(ctx, daemon.Job{MaxOutputTokens: 1}); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal(err)
	}
}

func TestOpenAIResponsesUntrustedMetadataAndItems(t *testing.T) {
	for _, mode := range []string{"key-echo", "metadata-bound", "incomplete-message", "tool", "commentary", "multiple-messages", "error", "missing-text", "null-text"} {
		t.Run(mode, func(t *testing.T) {
			reply := profileReply(ResponsesReasoning, `{"proof":"trivial"}`, "completed", false, map[string]int{"input_tokens": 21, "output_tokens": 8})
			output := reply["output"].([]any)
			message := output[1].(map[string]any)
			switch mode {
			case "key-echo":
				reply["model"] = "fixture-secret"
				reply["status"] = "fixture-secret"
				message["content"] = []any{map[string]string{"type": "output_text", "text": "fixture-secret"}}
			case "metadata-bound":
				reply["model"] = strings.Repeat("x", daemon.MaxModelBytes+1)
			case "incomplete-message":
				message["status"] = "incomplete"
			case "tool":
				message["type"] = "function_call"
			case "commentary":
				message["phase"] = "commentary"
			case "multiple-messages":
				reply["output"] = append(output, message)
			case "error":
				reply["error"] = map[string]string{"message": "private fixture-secret"}
			case "missing-text":
				message["content"] = []any{}
			case "null-text":
				message["content"] = []any{map[string]any{"type": "output_text", "text": nil}, map[string]string{"type": "output_text", "text": `{"proof":"trivial"}`}}
			}
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { json.NewEncoder(w).Encode(reply) }))
			defer s.Close()
			o, _ := NewOpenAIWithConfig(s.URL, "new-model", "fixture-secret", OpenAIConfig{Profile: ResponsesReasoning})
			result, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 128})
			encoded, _ := json.Marshal(result)
			if err == nil || strings.Contains(err.Error(), "fixture-secret") || strings.Contains(string(encoded), "fixture-secret") || *result.Usage["output_tokens"] != 8 {
				t.Fatalf("result=%s err=%v", encoded, err)
			}
		})
	}
}

func TestOpenAIUnsupportedModelSafeCode(t *testing.T) {
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.WriteHeader(404)
			w.Write([]byte(`{"error":{"code":"model_not_found","message":"fixture-secret private account details"}}`))
		}))
		o, _ := NewOpenAIWithConfig(s.URL, "new-model", "fixture-secret", OpenAIConfig{Profile: profile})
		result, err := o.Execute(context.Background(), daemon.Job{Model: "openai/job-cannot-redirect", MaxOutputTokens: 128})
		if err == nil || !strings.Contains(err.Error(), "unsupported or inaccessible model") || strings.Contains(err.Error(), "private") || strings.Contains(err.Error(), "fixture-secret") || result.Generation.RawResponse != "" || daemon.FailureClassOf(err) != daemon.FailurePermanent {
			t.Fatal(err)
		}
		s.Close()
	}
}

func TestOpenAIResponsesTimesOutInFlight(t *testing.T) {
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		select {
		case <-r.Context().Done():
		case <-time.After(time.Second):
		}
	}))
	defer s.Close()
	o, _ := NewOpenAIWithConfig(s.URL, "new-model", "fixture-secret", OpenAIConfig{Profile: ResponsesReasoning})
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	_, err := o.Execute(ctx, daemon.Job{MaxOutputTokens: 128})
	if !errors.Is(err, context.DeadlineExceeded) || daemon.FailureCategoryOf(err) != daemon.ProviderFailure {
		t.Fatal(err)
	}
}

func TestOpenAIRedactsDecodedEnvelope(t *testing.T) {
	const key = "fixture-secret"
	var fullyEscaped strings.Builder
	for _, r := range key {
		fmt.Fprintf(&fullyEscaped, `\u%04x`, r)
	}
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		for _, kind := range []string{"model.respond", "model.generate"} {
			for _, escaped := range []string{`\u0066ixture-secret`, fullyEscaped.String()} {
				t.Run(profile+"/"+kind+"/"+escaped, func(t *testing.T) {
					field, value := "proof", "trivial -- fixture-secret"
					if kind == "model.respond" {
						field, value = "text", `{"graph_schema":"solvenet.graph.v1","note":"fixture-secret"}`
					}
					outer, _ := json.Marshal(map[string]string{field: value})
					encoded := strings.ReplaceAll(string(outer), key, escaped)
					s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						finish := "stop"
						if profile == ResponsesReasoning {
							finish = "completed"
						}
						json.NewEncoder(w).Encode(profileReply(profile, encoded, finish, false, nil))
					}))
					defer s.Close()
					o, err := NewOpenAIWithConfig(s.URL, "new-model", key, OpenAIConfig{Profile: profile})
					if err != nil {
						t.Fatal(err)
					}
					result, err := o.Execute(context.Background(), daemon.Job{Kind: kind, MaxOutputTokens: 128})
					if err != nil || result.Text != strings.ReplaceAll(value, key, "[redacted]") || strings.Contains(result.Text, key) {
						t.Fatalf("result=%+v err=%v", result, err)
					}
					if strings.Contains(result.Generation.RawResponse, key) {
						t.Fatal("raw plaintext credential leaked")
					}
					assertNoRecoverableRawKey(t, result.Generation.RawResponse, key)
					decodedTextEnvelope, _ := json.Marshal(map[string]string{"text": result.Text})
					assertNoRecoverableRawKey(t, string(decodedTextEnvelope), key)
				})
			}
		}
	}
}

func TestOpenAIDefaultOutputCapRejectsBeforeHTTP(t *testing.T) {
	calls := 0
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { calls++; w.WriteHeader(500) }))
	defer s.Close()
	o, err := NewOpenAI(s.URL, "gpt-4o-mini", "fixture-secret")
	if err != nil {
		t.Fatal(err)
	}
	if o.Config.MaxOutputTokens != 16384 {
		t.Fatalf("default output cap = %d", o.Config.MaxOutputTokens)
	}
	_, err = o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 20000})
	if err == nil || !strings.Contains(err.Error(), "output-token limit") || calls != 0 {
		t.Fatalf("calls=%d err=%v", calls, err)
	}
	for _, tc := range []struct {
		model string
		want  int
	}{{"new-model", 16384}, {"gpt-5.4", 32768}, {"gpt-6.1-sol", 32768}} {
		o, err := NewOpenAIWithConfig(s.URL, tc.model, "fixture-secret", OpenAIConfig{Profile: ResponsesReasoning})
		if err != nil || o.Config.MaxOutputTokens != tc.want {
			t.Fatalf("model=%s config=%+v err=%v", tc.model, o.Config, err)
		}
	}
}

func TestOpenAIResponsesFailedBodyClassification(t *testing.T) {
	for _, tc := range []struct {
		name      string
		bodyError any
		class     daemon.FailureClass
	}{
		{"rate", map[string]string{"code": "rate_limit_exceeded", "message": "fixture-secret private account details"}, daemon.FailureTransient},
		{"server", map[string]string{"code": "server_error", "message": "fixture-secret private account details"}, daemon.FailureTransient},
		{"permanent", map[string]string{"code": "invalid_prompt", "message": "fixture-secret private account details"}, daemon.FailurePermanent},
		{"unknown", map[string]string{"code": "fixture-secret private account details"}, daemon.FailurePermanent},
		{"oversized", map[string]string{"code": "server_error", "message": strings.Repeat("fixture-secret", 1024)}, daemon.FailurePermanent},
		{"malformed", "fixture-secret private account details", daemon.FailurePermanent},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				json.NewEncoder(w).Encode(map[string]any{"model": "new-model", "status": "failed", "error": tc.bodyError, "output": []any{}, "usage": map[string]int{"input_tokens": 21, "output_tokens": 8}})
			}))
			defer s.Close()
			o, _ := NewOpenAIWithConfig(s.URL, "new-model", "fixture-secret", OpenAIConfig{Profile: ResponsesReasoning})
			result, err := o.Execute(context.Background(), daemon.Job{MaxOutputTokens: 128})
			if err == nil || daemon.FailureClassOf(err) != tc.class || daemon.FailureCategoryOf(err) != daemon.ProviderFailure {
				t.Fatalf("result=%+v err=%v", result, err)
			}
			if *result.Usage["input_tokens"] != 21 || *result.Usage["output_tokens"] != 8 || result.Generation.FinishReason != "failed" || result.Generation.TotalDurationNS == nil || *result.Generation.TotalDurationNS <= 0 {
				t.Fatalf("lost failure accounting: %+v", result)
			}
			encoded, _ := json.Marshal(result)
			if strings.Contains(err.Error(), "fixture-secret") || strings.Contains(err.Error(), "private") || strings.Contains(string(encoded), "fixture-secret") || strings.Contains(string(encoded), "private") {
				t.Fatal("provider body leaked")
			}
		})
	}
}

// Recursively inspect decoded retained JSON, including JSON carried inside text.
// A literal escaped spelling must not be recoverable by another JSON decode.
func assertNoRecoverableRawKey(t *testing.T, raw, key string) {
	t.Helper()
	var inspect func(any)
	inspect = func(value any) {
		switch v := value.(type) {
		case map[string]any:
			for name, child := range v {
				inspect(name)
				inspect(child)
			}
		case []any:
			for _, child := range v {
				inspect(child)
			}
		case string:
			if strings.Contains(v, key) {
				t.Fatalf("recoverable credential in retained raw: %q", v)
			}
			if nested, err := decodeRawJSON(v); err == nil {
				inspect(nested)
			}
		}
	}
	if raw == "" || raw == unavailableOpenAIRaw || raw == "[raw output unavailable: size limit exceeded]" {
		return
	}
	value, err := decodeRawJSON(raw)
	if err != nil {
		t.Fatalf("retained unchecked malformed raw: %q", raw)
	}
	inspect(value)
}

func TestOpenAIRawEnvelopeRedactionAcrossOutcomes(t *testing.T) {
	const key = "fixture-secret"
	const escaped = `\u0066ixture-secret`
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		for _, kind := range []string{"model.respond", "model.generate"} {
			for _, mode := range []string{"valid", "invalid-envelope", "malformed", "malformed-nested", "refusal", "incomplete"} {
				t.Run(profile+"/"+kind+"/"+mode, func(t *testing.T) {
					field, value := "proof", "trivial -- fixture-secret"
					if kind == "model.respond" {
						field, value = "text", `{"graph_schema":"solvenet.graph.v1","note":"\u0066ixture-secret"}`
					}
					outer, _ := json.Marshal(map[string]string{field: value})
					raw := strings.ReplaceAll(string(outer), key, escaped)
					if mode == "invalid-envelope" {
						raw = `{"unexpected":{"\u0066ixture-secret":"{\"secret\":\"\\u0066ixture-secret\"}"}}`
					}
					if mode == "malformed" {
						raw = `{"text":"\u0066ixture-secret`
					}
					if mode == "malformed-nested" {
						raw = `{"wrong":"{\"secret\":\"\\u0066ixture-secret"}`
					}
					finish := "stop"
					usage := map[string]int{"prompt_tokens": 21, "completion_tokens": 8}
					if profile == ResponsesReasoning {
						finish = "completed"
						usage = map[string]int{"input_tokens": 21, "output_tokens": 8}
					}
					if mode == "incomplete" {
						finish = "length"
						if profile == ResponsesReasoning {
							finish = "incomplete"
						}
					}
					s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
						json.NewEncoder(w).Encode(profileReply(profile, raw, finish, mode == "refusal", usage))
					}))
					defer s.Close()
					o, _ := NewOpenAIWithConfig(s.URL, "new-model", key, OpenAIConfig{Profile: profile})
					result, err := o.Execute(context.Background(), daemon.Job{Kind: kind, MaxOutputTokens: 128})
					if (mode == "valid") != (err == nil) {
						t.Fatalf("result=%+v err=%v", result, err)
					}
					assertNoRecoverableRawKey(t, result.Generation.RawResponse, key)
					if mode != "refusal" && mode != "malformed" && result.Generation.RawResponse == unavailableOpenAIRaw {
						t.Fatal("lost structurally checkable envelope diagnostics")
					}
					if *result.Usage["input_tokens"] != 21 || *result.Usage["output_tokens"] != 8 || result.Generation.TotalDurationNS == nil || *result.Generation.TotalDurationNS <= 0 {
						t.Fatalf("lost accounting: %+v", result)
					}
				})
			}
		}
	}
}

func TestOpenAIRawSafeFallbacksAndBounds(t *testing.T) {
	const key = "fixture-secret"
	for _, raw := range []string{`{"text":"\u0066ixture-secret"} trailing`, `{"text":"opaque \\u0066ixture-secret"}`, `{"text":"{\"key\":\"\\u0066ixture-secret"}`} {
		generation := safeOpenAIRaw(raw, key)
		assertNoRecoverableRawKey(t, generation.RawResponse, key)
	}
	nested := `{"note":"\u0066ixture-secret"}`
	for i := 0; i < 9; i++ {
		encoded, _ := json.Marshal(map[string]string{"text": nested})
		nested = string(encoded)
	}
	assertNoRecoverableRawKey(t, safeOpenAIRaw(nested, key).RawResponse, key)
	oversized := safeOpenAIRaw(strings.Repeat(`\u0066ixture-secret`, daemon.MaxRawResponseBytes), key)
	if !oversized.RawResponseTruncated || len(oversized.RawResponse) > daemon.MaxRawResponseBytes {
		t.Fatal("raw bound contract lost")
	}
	assertNoRecoverableRawKey(t, oversized.RawResponse, key)
}

func TestOpenAITaskTextNestedRedaction(t *testing.T) {
	const key = "fixture-secret"
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		for _, mode := range []string{"prose", "graph", "malformed-nested", "invalid-envelope", "incomplete"} {
			t.Run(profile+"/"+mode, func(t *testing.T) {
				text := "No useful decomposition. Compare a < b && b > c; keep ordinary prose."
				if mode == "graph" || mode == "incomplete" {
					text = `{"graph_schema":"solvenet.graph.v1","note":"\u0066ixture-secret","nested":"{\"note\":\"\\u0066ixture-secret\"}","claims":[],"details":{"\u0066ixture-secret":"safe"}}`
				}
				if mode == "malformed-nested" {
					text = `{"note":"\u0066ixture-secret`
				}
				raw, _ := encodeOpenAIRaw(map[string]string{"text": text})
				if mode == "invalid-envelope" {
					raw = `{"wrong":"{\"note\":\"\\u0066ixture-secret\"}"}`
				}
				finish := "stop"
				usage := map[string]int{"prompt_tokens": 21, "completion_tokens": 8}
				if profile == ResponsesReasoning {
					finish = "completed"
					usage = map[string]int{"input_tokens": 21, "output_tokens": 8}
				}
				if mode == "incomplete" {
					finish = "length"
					if profile == ResponsesReasoning {
						finish = "incomplete"
					}
				}
				s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					json.NewEncoder(w).Encode(profileReply(profile, raw, finish, false, usage))
				}))
				defer s.Close()
				o, _ := NewOpenAIWithConfig(s.URL, "new-model", key, OpenAIConfig{Profile: profile})
				result, err := o.Execute(context.Background(), daemon.Job{Kind: "model.respond", MaxOutputTokens: 128})
				failed := mode == "invalid-envelope" || mode == "incomplete" || mode == "malformed-nested"
				if failed != (err != nil) {
					t.Fatalf("result=%+v err=%v", result, err)
				}
				envelope, _ := json.Marshal(map[string]string{"text": result.Text})
				assertNoRecoverableRawKey(t, string(envelope), key)
				assertNoRecoverableRawKey(t, result.Generation.RawResponse, key)
				if mode == "prose" && result.Text != text {
					t.Fatal("ordinary task prose changed")
				}
				if mode == "graph" {
					graph, err := decodeRawJSON(result.Text)
					if err != nil {
						t.Fatal(err)
					}
					object := graph.(map[string]any)
					if len(object) != 5 || object["graph_schema"] != "solvenet.graph.v1" || object["note"] != "[redacted]" || len(object["claims"].([]any)) != 0 || object["details"].(map[string]any)["[redacted]"] != "safe" {
						t.Fatalf("graph shape lost: %+v", object)
					}
				}
				if *result.Usage["output_tokens"] != 8 || result.Generation.TotalDurationNS == nil {
					t.Fatal("accounting lost")
				}
			})
		}
	}
}

func TestOpenAIRawHTMLDiagnosticsDoNotRejectBoundedProof(t *testing.T) {
	for _, profile := range []string{ChatJSON, ResponsesReasoning} {
		for _, overLimit := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/overLimit=%v", profile, overLimit), func(t *testing.T) {
				proof := "trivial -- " + strings.Repeat("<>&", 22000)
				if overLimit {
					proof = "trivial -- " + strings.Repeat("x", daemon.MaxRawResponseBytes)
				}
				raw, _ := encodeOpenAIRaw(map[string]string{"proof": proof})
				if overLimit != (len(raw) > daemon.MaxRawResponseBytes) {
					t.Fatal("fixture size incorrect")
				}
				finish := "stop"
				usage := map[string]int{"prompt_tokens": 21, "completion_tokens": 8}
				if profile == ResponsesReasoning {
					finish = "completed"
					usage = map[string]int{"input_tokens": 21, "output_tokens": 8}
				}
				s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					json.NewEncoder(w).Encode(profileReply(profile, raw, finish, false, usage))
				}))
				defer s.Close()
				o, _ := NewOpenAIWithConfig(s.URL, "new-model", "fixture-secret", OpenAIConfig{Profile: profile})
				result, err := o.Execute(context.Background(), daemon.Job{Kind: "model.generate", MaxOutputTokens: 128})
				if overLimit != (err != nil) || result.Generation.RawResponseTruncated != overLimit {
					t.Fatalf("result=%+v err=%v", result.Generation, err)
				}
				if !overLimit {
					if result.Text != proof || len(result.Generation.RawResponse) != len(raw) || strings.Contains(result.Generation.RawResponse, `\u003c`) {
						t.Fatal("HTML diagnostic encoding changed the accepted proof")
					}
				}
				if *result.Usage["input_tokens"] != 21 || *result.Usage["output_tokens"] != 8 || result.Generation.TotalDurationNS == nil {
					t.Fatal("accounting lost")
				}
			})
		}
	}
}
