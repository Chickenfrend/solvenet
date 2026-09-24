package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"solvenet/worker/internal/daemon"
)

const maxOllamaResponse = 1024 * 1024
const ollamaDigestTimeout = 200 * time.Millisecond
const ollamaHealthTimeout = 800 * time.Millisecond
const ollamaHealthRetry = 10 * time.Second

const DefaultOllamaContext = 4096
const MaxOllamaContext = 1024 * 1024

const proofInstructions = `Return a JSON object with exactly one field, "proof".
The proof must contain only Lean 4 tactic commands that belong after "by".
Do not include the enclosing "by", a theorem/example/def declaration, Markdown fences, or explanation.
Do not use sorry or admit. The coordinator will check the proof against the original theorem.`

// Ollama uses a locally configured endpoint and model, never a URL from a job.
type Ollama struct {
	URL         string
	Model       string
	ContextSize int
	Client      *http.Client
	healthMu    sync.Mutex
	healthUntil time.Time
	health      daemon.Health
}

func NewOllama(baseURL, model string, contextSize int) (*Ollama, error) {
	u, err := url.Parse(baseURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, fmt.Errorf("ollama-url must be an HTTP(S) URL without credentials, query, or fragment")
	}
	if strings.TrimSpace(model) == "" || len("ollama/"+model) > daemon.MaxModelBytes {
		return nil, fmt.Errorf("model must be nonempty and at most %d bytes", daemon.MaxModelBytes-len("ollama/"))
	}
	if contextSize <= 0 || contextSize > MaxOllamaContext {
		return nil, fmt.Errorf("ollama-context must be between 1 and %d tokens", MaxOllamaContext)
	}
	return &Ollama{URL: strings.TrimRight(baseURL, "/"), Model: model, ContextSize: contextSize,
		Client: &http.Client{CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

// Health checks both service reachability and installation of this model.
// Only fixed reasons cross the worker/coordinator boundary.
func (o *Ollama) Health(ctx context.Context) daemon.Health {
	o.healthMu.Lock()
	defer o.healthMu.Unlock()
	if time.Now().Before(o.healthUntil) {
		return o.health
	}
	o.health = o.checkHealth(ctx)
	// An unavailable provider should not be probed on every claim.
	o.healthUntil = time.Now().Add(ollamaHealthRetry)
	return o.health
}

func (o *Ollama) invalidateHealth() {
	o.healthMu.Lock()
	o.healthUntil = time.Time{}
	o.healthMu.Unlock()
}

func (o *Ollama) checkHealth(ctx context.Context) daemon.Health {
	bad := func(reason string) daemon.Health { return daemon.Health{Status: "unavailable", Reason: reason} }
	checkCtx, cancel := context.WithTimeout(ctx, ollamaHealthTimeout)
	defer cancel()
	req, err := http.NewRequestWithContext(checkCtx, "GET", o.URL+"/api/tags", nil)
	if err != nil {
		return bad("Ollama service unreachable")
	}
	resp, err := o.Client.Do(req)
	if err != nil {
		return bad("Ollama service unreachable")
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return bad("Ollama service unavailable")
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxOllamaResponse+1))
	if err != nil || len(data) > maxOllamaResponse {
		return bad("Ollama model list unavailable")
	}
	var tags struct {
		Models []struct {
			Name string `json:"name"`
		} `json:"models"`
	}
	if json.Unmarshal(data, &tags) != nil || tags.Models == nil {
		return bad("Ollama model list unavailable")
	}
	for _, model := range tags.Models {
		if model.Name == o.Model || (strings.TrimSuffix(model.Name, ":latest") == o.Model && !strings.Contains(o.Model, ":")) {
			return daemon.Health{Status: "ready"}
		}
	}
	return bad("Ollama model not installed")
}

func rawGeneration(raw string) *daemon.Generation {
	truncated := len(raw) > daemon.MaxRawResponseBytes
	if truncated {
		raw = raw[:daemon.MaxRawResponseBytes]
		for !utf8.ValidString(raw) {
			raw = raw[:len(raw)-1]
		}
	}
	return &daemon.Generation{RawResponse: raw, RawResponseTruncated: truncated}
}

func (o *Ollama) Execute(ctx context.Context, job daemon.Job) (daemon.Execution, error) {
	var execution daemon.Execution
	providerFailure := func(err error) error { return daemon.Categorize(err, daemon.ProviderFailure) }
	formattingFailure := func(err error) error { return daemon.Categorize(err, daemon.FormattingFailure) }
	if job.MaxOutputTokens <= 0 || job.MaxOutputTokens > daemon.MaxOutputTokens {
		return execution, providerFailure(daemon.Permanent(fmt.Errorf("invalid job output-token limit")))
	}
	if job.MaxOutputTokens >= o.ContextSize {
		return execution, providerFailure(daemon.Permanent(fmt.Errorf("job.max_output_tokens (%d) must be less than Ollama context size (%d); reduce the job output budget or increase -ollama-context", job.MaxOutputTokens, o.ContextSize)))
	}
	// The provider owns its output contract. Put trusted structured problem
	// context before the coordinator's strategy or repair feedback.
	// For repair jobs this keeps the candidate and Lean feedback as the final,
	// most recent user message rather than obscuring it with a theorem reminder.
	messages := []daemon.Message{
		{Role: "system", Content: proofInstructions},
		{Role: "user", Content: "Lean imports: " + strings.Join(job.Imports, ", ") +
			"\nTheorem (text after its name):\n" + job.Statement},
	}
	messages = append(messages, job.Messages...)
	options := map[string]any{"num_predict": job.MaxOutputTokens, "num_ctx": o.ContextSize}
	if job.GenerationSettings.Temperature != nil {
		options["temperature"] = *job.GenerationSettings.Temperature
	}
	if job.GenerationSettings.Seed != nil {
		options["seed"] = *job.GenerationSettings.Seed
	}
	body := map[string]any{
		"model": o.Model, "messages": messages, "stream": false,
		"format": map[string]any{"type": "object", "properties": map[string]any{
			"proof": map[string]string{"type": "string"}}, "required": []string{"proof"}, "additionalProperties": false},
		"options": options,
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return execution, providerFailure(daemon.Permanent(err))
	}
	req, err := http.NewRequestWithContext(ctx, "POST", o.URL+"/api/chat", bytes.NewReader(payload))
	if err != nil {
		return execution, providerFailure(daemon.Permanent(err))
	}
	req.Header.Set("Content-Type", "application/json")
	digest := o.modelDigest(ctx)
	if err := ctx.Err(); err != nil {
		return execution, providerFailure(err)
	}
	execution.Generation = o.rawGeneration("", job)
	execution.Generation.ModelDigest = digest
	response, err := o.Client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return execution, providerFailure(ctx.Err())
		}
		o.invalidateHealth()
		return execution, providerFailure(daemon.Transient(fmt.Errorf("Ollama request failed (check local service)")))
	}
	defer response.Body.Close()
	data, readErr := io.ReadAll(io.LimitReader(response.Body, maxOllamaResponse+1))
	execution.Generation = o.rawGeneration(strings.ToValidUTF8(string(data), "�"), job)
	execution.Generation.ModelDigest = digest
	if readErr != nil {
		if ctx.Err() != nil {
			return execution, providerFailure(ctx.Err())
		}
		o.invalidateHealth()
		return execution, providerFailure(daemon.Transient(fmt.Errorf("reading Ollama response: %w", readErr)))
	}
	if len(data) > maxOllamaResponse {
		return execution, providerFailure(daemon.Permanent(fmt.Errorf("Ollama response exceeded 1 MiB")))
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		o.invalidateHealth()
		err := fmt.Errorf("Ollama HTTP %d (check service and installed model %q); response retained in generation.raw_response", response.StatusCode, o.Model)
		if response.StatusCode == http.StatusRequestTimeout || response.StatusCode == http.StatusTooManyRequests || response.StatusCode >= 500 {
			return execution, providerFailure(daemon.Transient(err))
		}
		return execution, providerFailure(daemon.Permanent(err))
	}
	var reply struct {
		Model   string `json:"model"`
		Message struct {
			Content string `json:"content"`
		} `json:"message"`
		Done           bool   `json:"done"`
		DoneReason     string `json:"done_reason"`
		Error          string `json:"error"`
		PromptCount    *int   `json:"prompt_eval_count"`
		EvalCount      *int   `json:"eval_count"`
		TotalDuration  *int64 `json:"total_duration"`
		LoadDuration   *int64 `json:"load_duration"`
		PromptDuration *int64 `json:"prompt_eval_duration"`
		EvalDuration   *int64 `json:"eval_duration"`
	}
	if err := json.Unmarshal(data, &reply); err != nil {
		return execution, providerFailure(daemon.Permanent(fmt.Errorf("invalid Ollama response JSON: %w", err)))
	}
	if reply.Error != "" {
		return execution, providerFailure(daemon.Transient(fmt.Errorf("Ollama reported an error; response retained in generation.raw_response")))
	}
	generation := o.rawGeneration(reply.Message.Content, job)
	generation.ModelDigest = digest
	generation.Model, generation.FinishReason = reply.Model, reply.DoneReason
	generation.TotalDurationNS, generation.LoadDurationNS = reply.TotalDuration, reply.LoadDuration
	generation.PromptEvalDurationNS, generation.EvalDurationNS = reply.PromptDuration, reply.EvalDuration
	execution.Generation = generation
	execution.Usage = map[string]*int{"input_tokens": reply.PromptCount, "output_tokens": reply.EvalCount}
	for key, count := range execution.Usage {
		if count != nil && *count < 0 {
			execution.Usage[key] = nil
		}
	}
	for _, duration := range []**int64{&generation.TotalDurationNS, &generation.LoadDurationNS, &generation.PromptEvalDurationNS, &generation.EvalDurationNS} {
		if *duration != nil && **duration < 0 {
			*duration = nil
		}
	}
	if len(reply.Model) > daemon.MaxModelBytes || len(reply.DoneReason) > daemon.MaxFinishReasonBytes {
		generation.Model, generation.FinishReason = "", ""
		return execution, providerFailure(daemon.Permanent(fmt.Errorf("Ollama model/finish metadata exceeded size limit")))
	}
	if !reply.Done {
		return execution, providerFailure(daemon.Permanent(fmt.Errorf("Ollama returned an incomplete non-streaming response")))
	}
	if generation.RawResponseTruncated {
		return execution, formattingFailure(daemon.Permanent(fmt.Errorf("Ollama generated text exceeded %d bytes", daemon.MaxRawResponseBytes)))
	}
	proof, err := extractProof(reply.Message.Content)
	if err != nil {
		return execution, formattingFailure(daemon.Permanent(fmt.Errorf("Ollama proof format: %w", err)))
	}
	execution.Text = proof
	return execution, nil
}

func (o *Ollama) rawGeneration(raw string, job daemon.Job) *daemon.Generation {
	generation := rawGeneration(raw)
	generation.ContextLength = o.ContextSize
	generation.MaxOutputTokens = job.MaxOutputTokens
	generation.Temperature = job.GenerationSettings.Temperature
	generation.Seed = job.GenerationSettings.Seed
	return generation
}

func (o *Ollama) modelDigest(ctx context.Context) string {
	// Best effort: /api/tags reports the installed model's SHA-256 digest.
	// No endpoint or lookup errors are sent to the coordinator.
	// Leave most of a short generation deadline for the actual chat request.
	timeout := ollamaDigestTimeout
	if deadline, ok := ctx.Deadline(); ok {
		remaining := time.Until(deadline)
		if remaining/4 < timeout {
			timeout = remaining / 4
		}
	}
	if timeout <= 0 || ctx.Err() != nil {
		return ""
	}
	lookupCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	req, err := http.NewRequestWithContext(lookupCtx, "GET", o.URL+"/api/tags", nil)
	if err != nil {
		return ""
	}
	resp, err := o.Client.Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return ""
	}
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxOllamaResponse+1))
	if err != nil || len(data) > maxOllamaResponse {
		return ""
	}
	var tags struct {
		Models []struct {
			Name   string `json:"name"`
			Digest string `json:"digest"`
		} `json:"models"`
	}
	if json.Unmarshal(data, &tags) != nil {
		return ""
	}
	for _, model := range tags.Models {
		if model.Name == o.Model && modelDigestPattern.MatchString(model.Digest) {
			return "sha256:" + model.Digest
		}
	}
	return ""
}

var modelDigestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

var declarationStart = regexp.MustCompile(`^(?:by|theorem|lemma|example|def|abbrev|axiom|opaque|namespace|import)(?:\s|$)`)

func extractProof(raw string) (string, error) {
	var object map[string]json.RawMessage
	if err := json.Unmarshal([]byte(raw), &object); err != nil {
		return "", fmt.Errorf("expected JSON object with a proof string")
	}
	if len(object) != 1 || object["proof"] == nil {
		return "", fmt.Errorf("expected exactly one proof field")
	}
	var proof string
	if err := json.Unmarshal(object["proof"], &proof); err != nil {
		return "", fmt.Errorf("proof must be a string")
	}
	proof = strings.TrimSpace(proof)
	if strings.HasPrefix(proof, "```") {
		lines := strings.Split(proof, "\n")
		if len(lines) < 3 || (strings.TrimSpace(lines[0]) != "```lean" && strings.TrimSpace(lines[0]) != "```") || strings.TrimSpace(lines[len(lines)-1]) != "```" {
			return "", fmt.Errorf("expected one surrounding Lean code fence")
		}
		proof = strings.TrimSpace(strings.Join(lines[1:len(lines)-1], "\n"))
	}
	// Some proof models include the enclosing `by` despite the prompt. Accept
	// only that one leading wrapper; Lean still checks the resulting tactic body.
	if declarationStart.MatchString(proof) && strings.HasPrefix(proof, "by") {
		proof = strings.TrimSpace(proof[len("by"):])
	}
	if proof == "" {
		return "", fmt.Errorf("proof is empty")
	}
	if strings.Contains(proof, "```") || declarationStart.MatchString(proof) {
		return "", fmt.Errorf("expected tactic body, not a declaration, enclosing by, or multiple fences")
	}
	return proof, nil
}
