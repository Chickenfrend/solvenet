// Cross-chain failure-map experiment runner. Run from the repository root via
// cd worker && go run ./experiments/crosschain -root .. -out ../experiments/challenge-v1-cross-chain-failure-map-2026-09-24/local-run.json
package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"time"

	"solvenet/worker/internal/daemon"
	"solvenet/worker/internal/provider"
)

const fixtureHash = "cd199d3ccd299533a38b14ead2d768b7edf47565f7c6b2d14f63822030d85337"
const providerHash = "8893049b0c2dccb797178c828fc12e40dc1bde9e0c8ab8ddad401184e7fcdcb5"
const expectedDigest = "sha256:98d095df8e1d58c088000c8a97fe5d4bc28b97fad4ef77d7a794be30544299d1"
const model = "goedel-prover-v2-8b:q4_k_m"
const verifierBridgePath = "experiments/challenge-v1-cross-chain-failure-map-2026-09-24/verifier_bridge.py"
const template = "Two independent attempts on this theorem did not verify. Their coarse outcomes were:\nchain 1: %s / %s\nchain 2: %s / %s\nChoose a fresh approach. Return a complete proof of the original theorem."

var seeds = []int64{121, 132, 143, 154, 165}
var approaches = map[string]bool{"intro": true, "constructor": true, "cases": true, "induction": true, "rw": true, "simp": true, "apply": true, "exact": true, "rfl": true, "decide": true, "other": true, "none": true, "withheld": true}
var outcomes = map[string]bool{"format": true, "request_failure": true, "timeout": true, "verifier_error": true, "unknown_tactic": true, "unsolved_goals": true, "type_mismatch": true, "unknown_identifier": true, "other_rejection": true, "withheld": true}

type problem struct {
	ID        string   `json:"id"`
	Statement string   `json:"statement"`
	Imports   []string `json:"imports"`
}
type fixture struct {
	SetID       string    `json:"set_id"`
	Version     int       `json:"version"`
	Environment string    `json:"environment"`
	Problems    []problem `json:"problems"`
}
type verification struct {
	Status      string `json:"status"`
	Diagnostics string `json:"diagnostics"`
	ElapsedMS   int64  `json:"elapsed_ms"`
}
type attempt struct {
	ID              string             `json:"id"`
	Seed            int64              `json:"seed"`
	PayloadHash     string             `json:"payload_hash"`
	Payload         json.RawMessage    `json:"payload"`
	Execution       daemon.Execution   `json:"-"`
	Response        string             `json:"response"`
	Candidate       string             `json:"candidate"`
	Generation      *daemon.Generation `json:"generation,omitempty"`
	Usage           map[string]*int    `json:"usage,omitempty"`
	Error           string             `json:"error,omitempty"`
	FailureCategory string             `json:"failure_category,omitempty"`
	Verification    *verification      `json:"verification,omitempty"`
	Approach        string             `json:"approach"`
	Outcome         string             `json:"outcome"`
}
type block struct {
	ProblemID     string     `json:"problem_id"`
	BaseSeed      int64      `json:"base_seed"`
	Prefix        [2]attempt `json:"prefix"`
	Map           *finding   `json:"map,omitempty"`
	Baseline      *attempt   `json:"baseline,omitempty"`
	Collaborative *attempt   `json:"collaborative,omitempty"`
}
type finding struct {
	Sources    [2]string `json:"source_attempt_ids"`
	Approaches [2]string `json:"approaches"`
	Outcomes   [2]string `json:"outcomes"`
}
type runData struct {
	Config   any     `json:"config"`
	Blocks   []block `json:"blocks"`
	Partial  *block  `json:"partial,omitempty"`
	Aborted  string  `json:"aborted,omitempty"`
	Complete bool    `json:"complete"`
}

func hash(b []byte) string { return fmt.Sprintf("%x", sha256.Sum256(b)) }
func fileHash(path string) (string, error) {
	b, e := os.ReadFile(path)
	if e != nil {
		return "", e
	}
	return hash(b), nil
}
func approach(candidate string) string {
	if candidate == "" {
		return "none"
	}
	for _, line := range strings.Split(candidate, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "--") {
			continue
		}
		for _, bullet := range []string{"·", "-", "+"} {
			if strings.HasPrefix(line, bullet) && len(line) > len(bullet) && strings.ContainsAny(line[len(bullet):len(bullet)+1], " \t") {
				line = strings.TrimSpace(line[len(bullet):])
				break
			}
		}
		parts := strings.Fields(line)
		if len(parts) == 0 {
			return "other"
		}
		switch parts[0] {
		case "intros":
			return "intro"
		case "rewrite":
			return "rw"
		case "simpa":
			return "simp"
		}
		// Fields does not split punctuation: tokens must be followed by whitespace or EOL.
		if approaches[parts[0]] && parts[0] != "none" && parts[0] != "withheld" {
			return parts[0]
		}
		return "other"
	}
	return "other"
}
func outcome(a attempt) string {
	if a.FailureCategory == daemon.ProviderFailure {
		return "request_failure"
	}
	if a.Candidate == "" {
		return "format"
	}
	if a.Verification == nil {
		return "verifier_error"
	}
	switch a.Verification.Status {
	case "timeout":
		return "timeout"
	case "verifier_error":
		return "verifier_error"
	}
	d := strings.ToLower(a.Verification.Diagnostics)
	for _, item := range []struct{ needle, label string }{{"unknown tactic", "unknown_tactic"}, {"unsolved goals", "unsolved_goals"}, {"type mismatch", "type_mismatch"}, {"unknown identifier", "unknown_identifier"}} {
		if strings.Contains(d, item.needle) {
			return item.label
		}
	}
	return "other_rejection"
}
func message(a, b attempt, collaborative bool) (string, error) {
	x, y, u, v := "withheld", "withheld", "withheld", "withheld"
	if collaborative {
		x, y, u, v = a.Approach, a.Outcome, b.Approach, b.Outcome
	}
	if !approaches[x] || !approaches[u] || !outcomes[y] || !outcomes[v] {
		return "", errors.New("invalid map label")
	}
	if collaborative && (x == "withheld" || u == "withheld" || y == "withheld" || v == "withheld") {
		return "", errors.New("withheld collaboration label")
	}
	return fmt.Sprintf(template, x, y, u, v), nil
}

type verifier interface {
	Check(context.Context, problem, string) (verification, error)
	Ready(context.Context) (any, error)
}
type bridge struct{ path string }

func (b bridge) invoke(ctx context.Context, input any, out any) error {
	data, _ := json.Marshal(input)
	cmd := exec.CommandContext(ctx, "python3", b.path)
	cmd.Stdin = bytes.NewReader(data)
	result, e := cmd.CombinedOutput()
	if e != nil {
		return fmt.Errorf("verifier bridge: %w: %s", e, result)
	}
	return json.Unmarshal(result, out)
}
func (b bridge) Check(ctx context.Context, p problem, c string) (verification, error) {
	var v verification
	e := b.invoke(ctx, map[string]any{"action": "verify", "statement": p.Statement, "imports": p.Imports, "candidate": c}, &v)
	return v, e
}
func (b bridge) Ready(ctx context.Context) (any, error) {
	var v map[string]any
	e := b.invoke(ctx, map[string]any{"action": "readiness"}, &v)
	if e != nil {
		return nil, e
	}
	if v["ready"] != true {
		return nil, fmt.Errorf("verifier unavailable: %v", v["error"])
	}
	if !strings.Contains(fmt.Sprint(v["version"]), "4.19.0") {
		return nil, fmt.Errorf("wrong Lean version: %v", v["version"])
	}
	return v, nil
}

// transport records the exact bytes passed to /api/chat by the pinned provider.
type transport struct {
	base            http.RoundTripper
	payload         []byte
	calls           int
	expectedExtra   string
	expectedSeed    int64
	expectedProblem problem
}

func (t *transport) RoundTrip(r *http.Request) (*http.Response, error) {
	if r.URL.Path == "/api/chat" {
		body, e := io.ReadAll(r.Body)
		if e != nil {
			return nil, e
		}
		t.payload = body
		t.calls++
		r.Body = io.NopCloser(bytes.NewReader(body))
		if e = validatePayload(body, t.expectedProblem, t.expectedSeed, t.expectedExtra); e != nil {
			return nil, e
		}
	}
	return t.base.RoundTrip(r)
}

func validatePayload(raw []byte, p problem, seed int64, extra string) error {
	var body struct {
		Model    string           `json:"model"`
		Messages []daemon.Message `json:"messages"`
		Stream   bool             `json:"stream"`
		Format   map[string]any   `json:"format"`
		Options  map[string]any   `json:"options"`
	}
	if e := json.Unmarshal(raw, &body); e != nil {
		return e
	}
	wantCount := 2
	if extra != "" {
		wantCount = 3
	}
	if body.Model != model || !body.Stream || len(body.Messages) != wantCount || body.Messages[0].Role != "system" || body.Messages[1] != (daemon.Message{Role: "user", Content: "Lean imports: " + strings.Join(p.Imports, ", ") + "\nTheorem (text after its name):\n" + p.Statement}) || body.Options["num_ctx"] != float64(4096) || body.Options["num_predict"] != float64(256) || body.Options["temperature"] != 0.6 || body.Options["seed"] != float64(seed) {
		return errors.New("provider payload violates experiment controls")
	}
	if extra != "" && (body.Messages[2].Role != "user" || body.Messages[2].Content != extra) {
		return errors.New("third template differs")
	}
	if body.Format["type"] != "object" || body.Format["additionalProperties"] != false {
		return errors.New("proof schema changed")
	}
	properties, ok := body.Format["properties"].(map[string]any)
	if !ok || len(properties) != 1 || !reflect.DeepEqual(properties["proof"], map[string]any{"type": "string"}) || !reflect.DeepEqual(body.Format["required"], []any{"proof"}) {
		return errors.New("proof schema changed")
	}
	return nil
}

type runner struct {
	root         string
	ollama       *provider.Ollama
	transport    *transport
	verifier     verifier
	readiness    any
	sourceHashes map[string]string
	data         runData
	out          string
}

func (r *runner) guard(ctx context.Context) error {
	for path, want := range r.sourceHashes {
		got, e := fileHash(filepath.Join(r.root, path))
		if e != nil || got != want {
			return fmt.Errorf("source changed: %s", path)
		}
	}
	ready, e := r.verifier.Ready(ctx)
	if e != nil || !reflect.DeepEqual(ready, r.readiness) {
		return fmt.Errorf("verifier readiness/config changed: %v", e)
	}
	if r.ollama.Health(ctx).Status != "ready" {
		return errors.New("model unavailable")
	}
	// Health caches status; a fresh tags request is required for every guard.
	req, e := http.NewRequestWithContext(ctx, "GET", r.ollama.URL+"/api/tags", nil)
	if e != nil {
		return e
	}
	resp, e := r.ollama.Client.Do(req)
	if e != nil {
		return e
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return errors.New("model tags unavailable")
	}
	var tags struct {
		Models []struct{ Name, Digest string }
	}
	if e = json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&tags); e != nil {
		return e
	}
	for _, m := range tags.Models {
		if m.Name == model && "sha256:"+m.Digest == expectedDigest {
			return nil
		}
	}
	return errors.New("model digest changed or unavailable")
}
func (r *runner) save() error {
	b, e := json.MarshalIndent(r.data, "", "  ")
	if e != nil {
		return e
	}
	if e = os.WriteFile(r.out+".tmp", b, 0600); e != nil {
		return e
	}
	return os.Rename(r.out+".tmp", r.out)
}
func (r *runner) call(ctx context.Context, p problem, id string, seed int64, extra string) (attempt, error) {
	a := attempt{ID: id, Seed: seed}
	if e := r.guard(ctx); e != nil {
		return a, e
	}
	job := daemon.Job{Statement: p.Statement, Imports: p.Imports, MaxOutputTokens: 256, GenerationSettings: daemon.GenerationSettings{Temperature: ptr(0.6), Seed: &seed}}
	if extra != "" {
		job.Messages = []daemon.Message{{Role: "user", Content: extra}}
	}
	r.transport.payload = nil
	r.transport.expectedProblem = p
	r.transport.expectedSeed = seed
	r.transport.expectedExtra = extra
	before := r.transport.calls
	requestCtx, cancel := context.WithTimeout(ctx, 120*time.Second)
	execResult, err := r.ollama.Execute(requestCtx, job)
	cancel()
	if r.transport.calls != before+1 {
		return a, errors.New("expected exactly one provider call")
	}
	a.Payload = append([]byte(nil), r.transport.payload...)
	a.PayloadHash = hash(a.Payload)
	a.Generation = execResult.Generation
	a.Usage = execResult.Usage
	a.Candidate = execResult.Text
	if a.Generation != nil {
		a.Response = a.Generation.RawResponse
		if a.Generation.RawResponseTruncated {
			return a, errors.New("provider response exceeded retained raw-response limit")
		}
		if a.Generation.ModelDigest != expectedDigest {
			return a, errors.New("model digest changed during call")
		}
	}
	if err != nil {
		a.Error = err.Error()
		a.FailureCategory = daemon.FailureCategoryOf(err)
	}
	if err == nil && a.Candidate != "" {
		v, e := r.verifier.Check(ctx, p, a.Candidate)
		if e != nil {
			return a, e
		}
		a.Verification = &v
	}
	a.Approach = approach(a.Candidate)
	a.Outcome = outcome(a)
	if e := r.guard(ctx); e != nil {
		return a, e
	}
	return a, nil
}
func ptr(v float64) *float64  { return &v }
func verified(a attempt) bool { return a.Verification != nil && a.Verification.Status == "verified" }
func (r *runner) execute(ctx context.Context, problems []problem, baseSeeds []int64) error {
	for i, seed := range baseSeeds {
		for _, p := range problems {
			b := block{ProblemID: p.ID, BaseSeed: seed}
			r.data.Partial = &b
			if e := r.save(); e != nil {
				return e
			}
			for j := 0; j < 2; j++ {
				id := fmt.Sprintf("%s-%d-prefix-%d", p.ID, seed, j+1)
				a, e := r.call(ctx, p, id, seed+int64(j), "")
				b.Prefix[j] = a
				r.data.Partial = &b
				if saveErr := r.save(); saveErr != nil {
					return saveErr
				}
				if e != nil {
					return e
				}
			}
			if !verified(b.Prefix[0]) && !verified(b.Prefix[1]) {
				b.Map = &finding{Sources: [2]string{b.Prefix[0].ID, b.Prefix[1].ID}, Approaches: [2]string{b.Prefix[0].Approach, b.Prefix[1].Approach}, Outcomes: [2]string{b.Prefix[0].Outcome, b.Prefix[1].Outcome}}
				order := []string{"baseline", "collaborative"}
				if i%2 == 1 {
					order[0], order[1] = order[1], order[0]
				}
				for _, arm := range order {
					msg, e := message(b.Prefix[0], b.Prefix[1], arm == "collaborative")
					if e != nil {
						return e
					}
					a, err := r.call(ctx, p, fmt.Sprintf("%s-%d-%s", p.ID, seed, arm), seed+2, msg)
					if arm == "baseline" {
						b.Baseline = &a
					} else {
						b.Collaborative = &a
					}
					r.data.Partial = &b
					if e := r.save(); e != nil {
						return e
					}
					if err != nil {
						return err
					}
				}
				if b.Baseline.PayloadHash == b.Collaborative.PayloadHash || b.Baseline.Seed != b.Collaborative.Seed || b.Baseline.Seed != seed+2 {
					return errors.New("third-arm payload or seed controls failed")
				}
			}
			r.data.Blocks = append(r.data.Blocks, b)
			r.data.Partial = nil
			if e := r.save(); e != nil {
				return e
			}
		}
	}
	r.data.Complete = true
	return r.save()
}
func main() {
	root := flag.String("root", "..", "repository root")
	out := flag.String("out", "", "private output JSON (must not exist)")
	ollamaURL := flag.String("ollama-url", "http://127.0.0.1:11434", "local Ollama URL")
	flag.Parse()
	if *out == "" {
		fmt.Fprintln(os.Stderr, "-out is required")
		os.Exit(2)
	}
	if _, e := os.Stat(*out); e == nil {
		fmt.Fprintln(os.Stderr, "output exists; refusing to overwrite")
		os.Exit(2)
	}
	rootAbs, e := filepath.Abs(*root)
	if e != nil {
		panic(e)
	}
	endpoint, e := url.Parse(*ollamaURL)
	if e != nil || (endpoint.Hostname() != "localhost" && endpoint.Hostname() != "127.0.0.1" && endpoint.Hostname() != "::1") {
		panic("Ollama must be a local loopback endpoint")
	}
	r := runner{root: rootAbs, out: *out}
	paths := []string{"problems/challenge-v1.json", "worker/internal/provider/ollama.go", "worker/experiments/crosschain/main.go", "worker/experiments/crosschain/main_test.go", verifierBridgePath, "experiments/challenge-v1-cross-chain-failure-map-2026-09-24/report.py", "experiments/challenge-v1-cross-chain-failure-map-2026-09-24/test_report.py"}
	r.sourceHashes = map[string]string{}
	for _, path := range paths {
		h, e := fileHash(filepath.Join(rootAbs, path))
		if e != nil {
			panic(e)
		}
		r.sourceHashes[path] = h
	}
	if r.sourceHashes[paths[0]] != fixtureHash || r.sourceHashes[paths[1]] != providerHash {
		panic("fixture or pinned provider hash mismatch")
	}
	fb, e := os.ReadFile(filepath.Join(rootAbs, paths[0]))
	if e != nil {
		panic(e)
	}
	var f fixture
	if e = json.Unmarshal(fb, &f); e != nil {
		panic(e)
	}
	if f.SetID != "challenge" || f.Version != 1 || f.Environment != "leanprover/lean4:v4.19.0" || len(f.Problems) != 12 {
		panic("fixture identity mismatch")
	}
	r.verifier = bridge{filepath.Join(rootAbs, verifierBridgePath)}
	ctx := context.Background()
	r.readiness, e = r.verifier.Ready(ctx)
	if e != nil {
		panic(e)
	}
	r.ollama, e = provider.NewOllama(*ollamaURL, model, 4096)
	if e != nil {
		panic(e)
	}
	r.transport = &transport{base: r.ollama.Client.Transport}
	if r.transport.base == nil {
		r.transport.base = http.DefaultTransport
	}
	r.ollama.Client.Transport = r.transport
	r.data.Config = map[string]any{"source_sha256": r.sourceHashes, "model": "ollama/" + model, "model_digest": expectedDigest, "temperature": 0.6, "num_ctx": 4096, "num_predict": 256, "request_timeout_seconds": 120, "verifier": r.readiness, "seeds": seeds, "fixture_sha256": fixtureHash}
	if e = r.guard(ctx); e != nil {
		panic(e)
	}
	if e = r.save(); e != nil {
		panic(e)
	}
	if e = r.execute(ctx, f.Problems, seeds); e != nil {
		r.data.Aborted = e.Error()
		if saveErr := r.save(); saveErr != nil {
			panic(saveErr)
		}
		fmt.Fprintln(os.Stderr, "partial run:", e)
		os.Exit(1)
	}
}
