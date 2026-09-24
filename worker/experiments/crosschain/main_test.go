package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"solvenet/worker/internal/provider"
)

type fakeVerifier struct {
	checks int
	ready  bool
}

func TestVerifierBridgePath(t *testing.T) {
	path := filepath.Join("../../..", verifierBridgePath)
	if filepath.Ext(path) != ".py" {
		t.Fatalf("bridge is not Python: %q", path)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("runner bridge path unavailable: %v", err)
	}
}

func (f *fakeVerifier) Ready(context.Context) (any, error) {
	if !f.ready {
		return nil, fmt.Errorf("not ready")
	}
	return "pinned Lean 4.19.0", nil
}
func (f *fakeVerifier) Check(_ context.Context, _ problem, c string) (verification, error) {
	f.checks++
	if c == "rfl" {
		return verification{Status: "verified", ElapsedMS: 1}, nil
	}
	return verification{Status: "rejected", Diagnostics: "Unsolved Goals", ElapsedMS: 2}, nil
}

func TestClassifiersAndTemplate(t *testing.T) {
	for _, tc := range []struct{ candidate, want string }{{"-- note\n · exact True.intro", "exact"}, {"introduction", "other"}, {"·", "other"}, {"/- note -/\nrfl", "other"}, {"rw [Nat.add_zero]", "rw"}, {"intros h", "intro"}, {"simpa", "simp"}, {"", "none"}, {"-exact foo", "other"}} {
		if got := approach(tc.candidate); got != tc.want {
			t.Errorf("%q: %q != %q", tc.candidate, got, tc.want)
		}
	}
	a := attempt{Candidate: "exact h", Verification: &verification{Status: "rejected", Diagnostics: "unknown identifier; UNKNOWN TACTIC; unsolved goals"}}
	if outcome(a) != "unknown_tactic" {
		t.Fatal(outcome(a))
	}
	a.FailureCategory = "provider_failure"
	a.Candidate = ""
	if outcome(a) != "request_failure" {
		t.Fatal(outcome(a))
	}
	a = attempt{Approach: "exact", Outcome: "unsolved_goals"}
	b := attempt{Approach: "rw", Outcome: "type_mismatch"}
	baseline, _ := message(a, b, false)
	collab, _ := message(a, b, true)
	if baseline != fmt.Sprintf(template, "withheld", "withheld", "withheld", "withheld") || collab != fmt.Sprintf(template, "exact", "unsolved_goals", "rw", "type_mismatch") {
		t.Fatal("template mismatch")
	}
	a.Approach = "secret theorem"
	if _, e := message(a, b, true); e == nil {
		t.Fatal("accepted free-text label")
	}
}

func TestFakeOllamaPairedBudgetsAndAbort(t *testing.T) {
	for _, tc := range []struct {
		name              string
		success           bool
		abort             bool
		wantCalls, checks int
	}{{"third", false, false, 4, 4}, {"prefix-success", true, false, 2, 2}, {"lost-readiness", false, true, 1, 1}} {
		t.Run(tc.name, func(t *testing.T) {
			calls := 0
			v := &fakeVerifier{ready: true}
			var payloads []map[string]any
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path == "/api/tags" {
					fmt.Fprintf(w, `{"models":[{"name":%q,"digest":%q}]}`, model, strings.TrimPrefix(expectedDigest, "sha256:"))
					return
				}
				calls++
				var body map[string]any
				if e := json.NewDecoder(r.Body).Decode(&body); e != nil {
					t.Error(e)
				}
				payloads = append(payloads, body)
				proof := "exact False.elim"
				if tc.success && calls == 1 {
					proof = "rfl"
				}
				if tc.abort && calls == 1 {
					v.ready = false
				}
				raw, _ := json.Marshal(map[string]string{"proof": proof})
				fmt.Fprintf(w, `{"done":true,"model":%q,"message":{"content":%q},"prompt_eval_count":10,"eval_count":4,"total_duration":100}`, model, string(raw))
			}))
			defer s.Close()
			o, _ := provider.NewOllama(s.URL, model, 4096)
			transport := &transport{base: o.Client.Transport}
			if transport.base == nil {
				transport.base = http.DefaultTransport
			}
			o.Client.Transport = transport
			root := t.TempDir()
			out := filepath.Join(root, "result.json")
			r := runner{root: root, ollama: o, transport: transport, verifier: v, readiness: "pinned Lean 4.19.0", sourceHashes: map[string]string{}, out: out}
			p := problem{ID: "sample", Statement: ": True", Imports: []string{"Init"}}
			e := r.execute(context.Background(), []problem{p}, []int64{121})
			if (e != nil) != tc.abort {
				t.Fatalf("execute: %v", e)
			}
			if calls != tc.wantCalls || v.checks != tc.checks {
				t.Fatalf("calls=%d checks=%d", calls, v.checks)
			}
			b, readErr := os.ReadFile(out)
			if readErr != nil {
				t.Fatal(readErr)
			}
			if strings.Contains(string(b), "reference_proof") {
				t.Fatal("reference proof leaked")
			}
			if tc.abort {
				if len(r.data.Blocks) != 0 || r.data.Partial == nil || r.data.Partial.Prefix[0].Candidate == "" {
					t.Fatalf("partial attempt lost: %+v", r.data.Partial)
				}
				return
			}
			b0 := r.data.Blocks[0]
			if b0.Prefix[0].PayloadHash == "" || b0.Prefix[0].ID == "" {
				t.Fatal("shared prefix not recorded")
			}
			if tc.success {
				if b0.Map != nil || b0.Baseline != nil || b0.Collaborative != nil {
					t.Fatal("third request after success")
				}
				return
			}
			if b0.Map.Sources != [2]string{b0.Prefix[0].ID, b0.Prefix[1].ID} || b0.Baseline.Seed != 123 || b0.Collaborative.Seed != 123 || b0.Baseline.PayloadHash == b0.Collaborative.PayloadHash {
				t.Fatal("paired controls incorrect")
			}
			for _, payload := range payloads {
				text, _ := json.Marshal(payload)
				if strings.Contains(string(text), "False.elim") || strings.Contains(string(text), "Unsolved Goals") || strings.Contains(string(text), "reference_proof") {
					t.Fatal("raw predecessor leaked")
				}
			}
			for _, payload := range payloads[2:] {
				options := payload["options"].(map[string]any)
				if options["seed"] != float64(123) || options["num_predict"] != float64(256) || options["temperature"] != 0.6 {
					t.Fatalf("settings: %v", options)
				}
			}
		})
	}
}

func TestFakeOllamaFailureMapAndAlternatingOrder(t *testing.T) {
	var thirdOrder []string
	calls := 0
	s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/api/tags" {
			fmt.Fprintf(w, `{"models":[{"name":%q,"digest":%q}]}`, model, strings.TrimPrefix(expectedDigest, "sha256:"))
			return
		}
		calls++
		var request struct {
			Messages []struct {
				Content string `json:"content"`
			} `json:"messages"`
		}
		if e := json.NewDecoder(r.Body).Decode(&request); e != nil {
			t.Error(e)
		}
		if len(request.Messages) == 3 {
			if strings.Contains(request.Messages[2].Content, "withheld") {
				thirdOrder = append(thirdOrder, "baseline")
			} else {
				thirdOrder = append(thirdOrder, "collaborative")
			}
		}
		if calls%4 == 1 {
			fmt.Fprint(w, `{"done":true,"message":{"content":"bad JSON"},"prompt_eval_count":10,"eval_count":4}`)
			return
		}
		if calls%4 == 2 {
			w.WriteHeader(503)
			fmt.Fprint(w, `service unavailable`)
			return
		}
		fmt.Fprint(w, `{"done":true,"message":{"content":"{\"proof\":\"exact False.elim\"}"},"prompt_eval_count":10,"eval_count":4}`)
	}))
	defer s.Close()
	o, _ := provider.NewOllama(s.URL, model, 4096)
	tr := &transport{base: http.DefaultTransport}
	o.Client.Transport = tr
	v := &fakeVerifier{ready: true}
	r := runner{root: t.TempDir(), ollama: o, transport: tr, verifier: v, readiness: "pinned Lean 4.19.0", sourceHashes: map[string]string{}, out: filepath.Join(t.TempDir(), "result.json")}
	if e := r.execute(context.Background(), []problem{{ID: "p", Statement: ": True", Imports: []string{"Init"}}}, []int64{121, 132}); e != nil {
		t.Fatal(e)
	}
	if calls != 8 || v.checks != 4 || !r.data.Complete {
		t.Fatalf("calls=%d checks=%d", calls, v.checks)
	}
	if got := r.data.Blocks[0].Map; got.Outcomes != [2]string{"format", "request_failure"} || got.Approaches != [2]string{"none", "none"} {
		t.Fatalf("map=%+v", got)
	}
	if strings.Join(thirdOrder, ",") != "baseline,collaborative,collaborative,baseline" {
		t.Fatalf("order=%v", thirdOrder)
	}
}
