package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"solvenet/worker/internal/daemon"
)

const maxOpenAIResponse = 1024 * 1024

// OpenAI uses a worker-local credential and model; neither is taken from the job.
type OpenAI struct {
	URL, Model, Key string
	Client          *http.Client
	Config          OpenAIConfig
	authFailed      atomic.Bool
	healthMu        sync.Mutex
	health          daemon.Health
}

func (o *OpenAI) AuthFailed() bool { return o.authFailed.Load() }

func NewOpenAI(baseURL, model, key string) (*OpenAI, error) {
	return NewOpenAIWithConfig(baseURL, model, key, OpenAIConfig{})
}

func NewOpenAIWithConfig(baseURL, model, key string, config OpenAIConfig) (*OpenAI, error) {
	u, err := url.Parse(baseURL)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Scheme != "https" && !(u.Scheme == "http" && (u.Hostname() == "localhost" || u.Hostname() == "127.0.0.1" || u.Hostname() == "::1"))) {
		return nil, fmt.Errorf("openai-url must be HTTPS (HTTP is allowed only for loopback testing) without credentials, query or fragment")
	}
	if strings.TrimSpace(model) != model || model == "" || len("openai/"+model) > daemon.MaxModelBytes || strings.ContainsAny(model, "\r\n\t ") {
		return nil, fmt.Errorf("invalid OpenAI model ID")
	}
	config, err = resolveOpenAIConfig(model, config)
	if err != nil {
		return nil, err
	}
	if strings.TrimSpace(key) == "" {
		return nil, fmt.Errorf("OpenAI credential unavailable: set OPENAI_API_KEY or OPENAI_API_KEY_FILE on the worker")
	}
	if strings.TrimSpace(key) != key || strings.ContainsAny(key, "\r\n") {
		return nil, fmt.Errorf("invalid OpenAI credential format")
	}
	return &OpenAI{URL: strings.TrimRight(baseURL, "/"), Model: model, Key: key, Config: config,
		Client: &http.Client{CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func (o *OpenAI) Execute(ctx context.Context, job daemon.Job) (result daemon.Execution, resultErr error) {
	defer func() { o.observe(resultErr) }()
	if o.AuthFailed() {
		return daemon.Execution{}, daemon.Categorize(withOpenAIHealth(daemon.Permanent(fmt.Errorf("OpenAI HTTP 401 (credential rejected; restart required)")), "OpenAI credential rejected"), daemon.ProviderFailure)
	}
	generation := rawGeneration("")
	generation.MaxOutputTokens = job.MaxOutputTokens
	generation.Temperature, generation.Seed = job.GenerationSettings.Temperature, job.GenerationSettings.Seed
	execution := daemon.Execution{Generation: generation}
	started := time.Now()
	defer func() {
		elapsed := time.Since(started).Nanoseconds()
		if result.Generation != nil {
			result.Generation.TotalDurationNS = &elapsed
			result.Generation.ContextLength = o.Config.ContextTokens
		}
	}()
	fail := func(err error) (daemon.Execution, error) {
		return execution, daemon.Categorize(err, daemon.ProviderFailure)
	}
	if job.MaxOutputTokens <= 0 || job.MaxOutputTokens > o.Config.MaxOutputTokens {
		return fail(daemon.Permanent(fmt.Errorf("invalid job output-token limit")))
	}
	if job.Kind != "" && job.Kind != "model.respond" && job.Kind != "model.generate" {
		return fail(daemon.Permanent(fmt.Errorf("unsupported OpenAI job kind")))
	}
	if o.Config.Profile == ResponsesReasoning && (job.GenerationSettings.Temperature != nil || job.GenerationSettings.Seed != nil) {
		return fail(daemon.Permanent(fmt.Errorf("responses-reasoning profile does not support temperature or seed")))
	}
	if t := job.GenerationSettings.Temperature; t != nil && (math.IsNaN(*t) || math.IsInf(*t, 0) || *t < 0 || *t > 2) {
		return fail(daemon.Permanent(fmt.Errorf("invalid temperature")))
	}
	if s := job.GenerationSettings.Seed; s != nil && *s < 0 {
		return fail(daemon.Permanent(fmt.Errorf("invalid seed")))
	}
	messages := []daemon.Message{{Role: "system", Content: instructions(job)},
		{Role: "user", Content: "Lean imports: " + strings.Join(job.Imports, ", ") + "\nTheorem (text after its name):\n" + job.Statement}}
	messages = append(messages, job.Messages...)
	body := map[string]any{"model": o.Model, "messages": messages, "stream": false,
		"max_tokens": job.MaxOutputTokens, "response_format": map[string]string{"type": "json_object"}}
	if job.GenerationSettings.Temperature != nil {
		body["temperature"] = *job.GenerationSettings.Temperature
	}
	if job.GenerationSettings.Seed != nil {
		body["seed"] = *job.GenerationSettings.Seed
	}
	endpoint := "/chat/completions"
	if o.Config.Profile == ResponsesReasoning {
		endpoint = "/responses"
		for i := range messages {
			if messages[i].Role == "system" {
				messages[i].Role = "developer"
			}
		}
		field := "proof"
		if job.Kind == "model.respond" {
			field = "text"
		}
		body = map[string]any{"model": o.Model, "input": messages, "stream": false, "store": false,
			"max_output_tokens": job.MaxOutputTokens, "text": map[string]any{"format": map[string]any{
				"type": "json_schema", "name": "solvenet_output", "strict": true,
				"schema": map[string]any{"type": "object", "properties": map[string]any{field: map[string]string{"type": "string"}}, "required": []string{field}, "additionalProperties": false}}}}
		if o.Config.ReasoningEffort != "" {
			body["reasoning"] = map[string]string{"effort": o.Config.ReasoningEffort}
		}
	}
	payload, err := json.Marshal(body)
	if err != nil {
		return fail(daemon.Permanent(fmt.Errorf("invalid OpenAI request")))
	}
	// Full provider request bytes upper-bound input tokens; reserve framing too.
	if len(payload)+512 > o.Config.ContextBytes || len(payload)+512+job.MaxOutputTokens > o.Config.ContextTokens {
		return fail(daemon.Permanent(fmt.Errorf("OpenAI full prompt exceeds configured context capacity")))
	}
	generation.ContextLength = o.Config.ContextTokens
	req, err := http.NewRequestWithContext(ctx, "POST", o.URL+endpoint, bytes.NewReader(payload))
	if err != nil {
		return fail(daemon.Permanent(fmt.Errorf("invalid OpenAI endpoint")))
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+o.Key)
	response, err := o.Client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			return fail(ctx.Err())
		}
		return fail(withOpenAIHealth(daemon.Transient(fmt.Errorf("OpenAI request failed (check network and service)")), "OpenAI network unavailable"))
	}
	defer response.Body.Close()
	// Never forward provider error bodies: they may contain account or credential details.
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
			o.authFailed.Store(true)
		}
		if response.StatusCode == 400 || response.StatusCode == 404 {
			// Inspect only a bounded, known code; never forward provider messages.
			data, _ := io.ReadAll(io.LimitReader(response.Body, 8193))
			var failure struct {
				Error struct {
					Code string `json:"code"`
				} `json:"error"`
			}
			if len(data) <= 8192 && json.Unmarshal(data, &failure) == nil && failure.Error.Code == "model_not_found" {
				return fail(withOpenAIHealth(daemon.Permanent(fmt.Errorf("OpenAI unsupported or inaccessible model (HTTP %d)", response.StatusCode)), "OpenAI model unavailable"))
			}
		}
		err := fmt.Errorf("OpenAI HTTP %d (check worker credential, model and provider availability)", response.StatusCode)
		if response.StatusCode == 408 || response.StatusCode == 429 || response.StatusCode >= 500 {
			return fail(withOpenAIHealth(daemon.Transient(err), openAIHTTPHealth(response.StatusCode).Reason))
		}
		return fail(withOpenAIHealth(daemon.Permanent(err), openAIHTTPHealth(response.StatusCode).Reason))
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxOpenAIResponse+1))
	if err != nil {
		if ctx.Err() != nil {
			return fail(ctx.Err())
		}
		return fail(withOpenAIHealth(daemon.Transient(fmt.Errorf("reading OpenAI response failed")), "OpenAI network unavailable"))
	}
	if err := ctx.Err(); err != nil {
		return fail(err)
	}
	if len(data) > maxOpenAIResponse {
		return fail(daemon.Permanent(fmt.Errorf("OpenAI response exceeded 1 MiB")))
	}
	if o.Config.Profile == ResponsesReasoning {
		return o.extractResponse(data, job, execution)
	}
	var reply struct {
		Model   string `json:"model"`
		Choices []struct {
			Message struct {
				Content *string `json:"content"`
				Refusal string  `json:"refusal"`
			} `json:"message"`
			FinishReason string `json:"finish_reason"`
		} `json:"choices"`
		Usage struct {
			Prompt     *int `json:"prompt_tokens"`
			Completion *int `json:"completion_tokens"`
		} `json:"usage"`
	}
	if err := json.Unmarshal(data, &reply); err != nil {
		return fail(daemon.Permanent(fmt.Errorf("invalid OpenAI response JSON")))
	}
	execution.Usage = map[string]*int{"input_tokens": reply.Usage.Prompt, "output_tokens": reply.Usage.Completion}
	for key, count := range execution.Usage {
		if count != nil && *count < 0 {
			execution.Usage[key] = nil
		}
	}
	if len(reply.Choices) != 1 {
		return fail(daemon.Permanent(fmt.Errorf("OpenAI returned no single text choice")))
	}
	choice := reply.Choices[0]
	// A faulty or compromised upstream must not echo the local credential into
	// coordinator-visible generation metadata or raw model output.
	redact := func(value string) string { return strings.ReplaceAll(value, o.Key, "[redacted]") }
	generation.Model, generation.FinishReason = redactRawString(reply.Model, o.Key, 0), redactRawString(choice.FinishReason, o.Key, 0)
	if len(generation.Model) > daemon.MaxModelBytes || len(generation.FinishReason) > daemon.MaxFinishReasonBytes {
		generation.Model, generation.FinishReason = "", ""
		return fail(daemon.Permanent(fmt.Errorf("OpenAI response metadata exceeded size limit")))
	}
	execution.Generation.Model, execution.Generation.FinishReason = generation.Model, generation.FinishReason
	if choice.Message.Refusal != "" || choice.FinishReason == "content_filter" {
		return execution, daemon.Categorize(daemon.Permanent(fmt.Errorf("OpenAI refused the request")), daemon.ProviderFailure)
	}
	if choice.Message.Content == nil {
		return fail(daemon.Permanent(fmt.Errorf("OpenAI returned no text choice")))
	}
	text := redact(*choice.Message.Content)
	execution.Generation = safeOpenAIRaw(*choice.Message.Content, o.Key)
	execution.Generation.Model, execution.Generation.FinishReason = generation.Model, generation.FinishReason
	execution.Generation.MaxOutputTokens = job.MaxOutputTokens
	execution.Generation.Temperature, execution.Generation.Seed = job.GenerationSettings.Temperature, job.GenerationSettings.Seed
	if choice.FinishReason != "stop" {
		reason := "unsupported finish reason"
		if choice.FinishReason == "length" {
			reason = "output limit (length)"
		}
		return execution, daemon.Categorize(daemon.Permanent(fmt.Errorf("OpenAI generation ended with %s", reason)), daemon.ProviderFailure)
	}
	if execution.Generation.RawResponseTruncated {
		return execution, daemon.Categorize(daemon.Permanent(fmt.Errorf("OpenAI generated text exceeded %d bytes", daemon.MaxRawResponseBytes)), daemon.FormattingFailure)
	}
	proof, err := extractOutput(text, job)
	if err != nil {
		label := "proof"
		if job.Kind == "model.respond" {
			label = "task"
		}
		return execution, daemon.Categorize(daemon.Permanent(fmt.Errorf("OpenAI %s format: %w", label, err)), daemon.FormattingFailure)
	}
	execution.Text = redact(proof)
	if job.Kind == "model.respond" {
		execution.Text = redactRawString(proof, o.Key, 0)
	}
	return execution, nil
}
