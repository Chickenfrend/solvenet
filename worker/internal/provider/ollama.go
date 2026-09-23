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
	"unicode/utf8"

	"solvenet/worker/internal/daemon"
)

const maxOllamaResponse = 1024 * 1024
const maxRawResponse = 128 * 1024

const proofInstructions = `Return a JSON object with exactly one field, "proof".
The proof must contain only Lean 4 tactic commands that belong after "by".
Do not include the enclosing "by", a theorem/example/def declaration, Markdown fences, or explanation.
Do not use sorry or admit. The coordinator will check the proof against the original theorem.`

// Ollama uses a locally configured endpoint and model, never a URL from a job.
type Ollama struct {
	URL    string
	Model  string
	Client *http.Client
}

func NewOllama(baseURL, model string) (*Ollama, error) {
	u, err := url.Parse(baseURL)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" {
		return nil, fmt.Errorf("ollama-url must be an HTTP(S) URL without credentials, query, or fragment")
	}
	if strings.TrimSpace(model) == "" || len("ollama/"+model) > 256 {
		return nil, fmt.Errorf("model must be nonempty and at most 249 bytes")
	}
	return &Ollama{URL: strings.TrimRight(baseURL, "/"), Model: model,
		Client: &http.Client{CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func rawGeneration(raw string) *daemon.Generation {
	truncated := len(raw) > maxRawResponse
	if truncated {
		raw = raw[:maxRawResponse]
		for !utf8.ValidString(raw) {
			raw = raw[:len(raw)-1]
		}
	}
	return &daemon.Generation{RawResponse: raw, RawResponseTruncated: truncated}
}

func (o *Ollama) Execute(ctx context.Context, job daemon.Job) (daemon.Execution, error) {
	var execution daemon.Execution
	if job.MaxOutputTokens <= 0 || job.MaxOutputTokens > 32768 {
		return execution, fmt.Errorf("invalid job output-token limit")
	}
	// Put trusted problem context before the coordinator's conversational context.
	// For repair jobs this keeps the candidate and Lean feedback as the final,
	// most recent user message rather than obscuring it with a theorem reminder.
	messages := []daemon.Message{
		{Role: "system", Content: proofInstructions},
		{Role: "user", Content: "Lean imports: " + strings.Join(job.Imports, ", ") +
			"\nTheorem (text after its name):\n" + job.Statement},
	}
	for _, message := range job.Messages {
		if message.Role == "system" {
			// The v1 coordinator's system instruction requests plain text, so do
			// not send that contradictory formatting instruction to Ollama.
			if message.Content == "Return only a Lean tactic proof body." {
				continue
			}
		}
		// The environment message above already includes the original statement.
		if message.Role == "user" && strings.TrimSpace(message.Content) == strings.TrimSpace(job.Statement) {
			continue
		}
		messages = append(messages, message)
	}
	body := map[string]any{
		"model": o.Model, "messages": messages, "stream": false,
		"format": map[string]any{"type": "object", "properties": map[string]any{
			"proof": map[string]string{"type": "string"}}, "required": []string{"proof"}, "additionalProperties": false},
		"options": map[string]int{"num_predict": job.MaxOutputTokens, "num_ctx": 4096},
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return execution, err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", o.URL+"/api/chat", bytes.NewReader(payload))
	if err != nil {
		return execution, err
	}
	req.Header.Set("Content-Type", "application/json")
	response, err := o.Client.Do(req)
	if err != nil {
		return execution, fmt.Errorf("Ollama request: %w", err)
	}
	defer response.Body.Close()
	data, readErr := io.ReadAll(io.LimitReader(response.Body, maxOllamaResponse+1))
	execution.Generation = rawGeneration(strings.ToValidUTF8(string(data), "�"))
	if readErr != nil {
		return execution, fmt.Errorf("reading Ollama response: %w", readErr)
	}
	if len(data) > maxOllamaResponse {
		return execution, fmt.Errorf("Ollama response exceeded 1 MiB")
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		return execution, fmt.Errorf("Ollama HTTP %d (check service and installed model %q); response retained in generation.raw_response", response.StatusCode, o.Model)
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
		return execution, fmt.Errorf("invalid Ollama response JSON: %w", err)
	}
	if reply.Error != "" {
		return execution, fmt.Errorf("Ollama reported an error; response retained in generation.raw_response")
	}
	generation := rawGeneration(reply.Message.Content)
	generation.Model, generation.FinishReason = reply.Model, reply.DoneReason
	generation.ContextLength, generation.MaxOutputTokens = 4096, job.MaxOutputTokens
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
	if len(reply.Model) > 256 || len(reply.DoneReason) > 256 {
		generation.Model, generation.FinishReason = "", ""
		return execution, fmt.Errorf("Ollama model/finish metadata exceeded size limit")
	}
	if !reply.Done {
		return execution, fmt.Errorf("Ollama returned an incomplete non-streaming response")
	}
	if generation.RawResponseTruncated {
		return execution, fmt.Errorf("Ollama generated text exceeded 128 KiB")
	}
	proof, err := extractProof(reply.Message.Content)
	if err != nil {
		return execution, fmt.Errorf("Ollama proof format: %w", err)
	}
	execution.Text = proof
	return execution, nil
}

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
	if proof == "" {
		return "", fmt.Errorf("proof is empty")
	}
	if strings.Contains(proof, "```") || declarationStart.MatchString(proof) {
		return "", fmt.Errorf("expected tactic body, not a declaration, enclosing by, or multiple fences")
	}
	return proof, nil
}
