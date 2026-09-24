package provider

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync/atomic"

	"solvenet/worker/internal/daemon"
)

const maxOpenAIResponse = 1024 * 1024

// OpenAI uses a worker-local credential and model; neither is taken from the job.
type OpenAI struct {
	URL, Model, Key string
	Client          *http.Client
	authFailed      atomic.Bool
}

func (o *OpenAI) AuthFailed() bool { return o.authFailed.Load() }

func NewOpenAI(baseURL, model, key string) (*OpenAI, error) {
	u, err := url.Parse(baseURL)
	if err != nil || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Scheme != "https" && !(u.Scheme == "http" && (u.Hostname() == "localhost" || u.Hostname() == "127.0.0.1" || u.Hostname() == "::1"))) {
		return nil, fmt.Errorf("openai-url must be HTTPS (HTTP is allowed only for loopback testing) without credentials, query or fragment")
	}
	if model != "gpt-4o-mini" {
		return nil, fmt.Errorf("OpenAI adapter currently supports only gpt-4o-mini")
	}
	if strings.TrimSpace(key) == "" {
		return nil, fmt.Errorf("OpenAI credential unavailable: set OPENAI_API_KEY or OPENAI_API_KEY_FILE on the worker")
	}
	if strings.TrimSpace(key) != key || strings.ContainsAny(key, "\r\n") {
		return nil, fmt.Errorf("invalid OpenAI credential format")
	}
	return &OpenAI{URL: strings.TrimRight(baseURL, "/"), Model: model, Key: key,
		Client: &http.Client{CheckRedirect: func(_ *http.Request, _ []*http.Request) error { return http.ErrUseLastResponse }}}, nil
}

func (o *OpenAI) Execute(ctx context.Context, job daemon.Job) (daemon.Execution, error) {
	generation := rawGeneration("")
	generation.MaxOutputTokens = job.MaxOutputTokens
	generation.Temperature, generation.Seed = job.GenerationSettings.Temperature, job.GenerationSettings.Seed
	execution := daemon.Execution{Generation: generation}
	fail := func(err error) (daemon.Execution, error) {
		return execution, daemon.Categorize(err, daemon.ProviderFailure)
	}
	if job.MaxOutputTokens <= 0 || job.MaxOutputTokens > daemon.MaxOutputTokens {
		return fail(daemon.Permanent(fmt.Errorf("invalid job output-token limit")))
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
	payload, err := json.Marshal(body)
	if err != nil {
		return fail(daemon.Permanent(fmt.Errorf("invalid OpenAI request")))
	}
	req, err := http.NewRequestWithContext(ctx, "POST", o.URL+"/chat/completions", bytes.NewReader(payload))
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
		return fail(daemon.Transient(fmt.Errorf("OpenAI request failed (check network and service)")))
	}
	defer response.Body.Close()
	// Never forward provider error bodies: they may contain account or credential details.
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		if response.StatusCode == http.StatusUnauthorized || response.StatusCode == http.StatusForbidden {
			o.authFailed.Store(true)
		}
		err := fmt.Errorf("OpenAI HTTP %d (check worker credential, model and provider availability)", response.StatusCode)
		if response.StatusCode == 408 || response.StatusCode == 429 || response.StatusCode >= 500 {
			return fail(daemon.Transient(err))
		}
		return fail(daemon.Permanent(err))
	}
	data, err := io.ReadAll(io.LimitReader(response.Body, maxOpenAIResponse+1))
	if err != nil {
		if ctx.Err() != nil {
			return fail(ctx.Err())
		}
		return fail(daemon.Transient(fmt.Errorf("reading OpenAI response failed")))
	}
	if err := ctx.Err(); err != nil {
		return fail(err)
	}
	if len(data) > maxOpenAIResponse {
		return fail(daemon.Permanent(fmt.Errorf("OpenAI response exceeded 1 MiB")))
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
	if len(reply.Choices) != 1 {
		return fail(daemon.Permanent(fmt.Errorf("OpenAI returned no single text choice")))
	}
	choice := reply.Choices[0]
	// A faulty or compromised upstream must not echo the local credential into
	// coordinator-visible generation metadata or raw model output.
	redact := func(value string) string { return strings.ReplaceAll(value, o.Key, "[redacted]") }
	generation.Model, generation.FinishReason = redact(reply.Model), redact(choice.FinishReason)
	if len(generation.Model) > daemon.MaxModelBytes || len(generation.FinishReason) > daemon.MaxFinishReasonBytes {
		generation.Model, generation.FinishReason = "", ""
		return fail(daemon.Permanent(fmt.Errorf("OpenAI response metadata exceeded size limit")))
	}
	execution.Usage = map[string]*int{"input_tokens": reply.Usage.Prompt, "output_tokens": reply.Usage.Completion}
	for key, count := range execution.Usage {
		if count != nil && *count < 0 {
			execution.Usage[key] = nil
		}
	}
	execution.Generation.Model, execution.Generation.FinishReason = generation.Model, generation.FinishReason
	if choice.Message.Refusal != "" || choice.FinishReason == "content_filter" {
		return execution, daemon.Categorize(daemon.Permanent(fmt.Errorf("OpenAI refused the request")), daemon.ProviderFailure)
	}
	if choice.Message.Content == nil {
		return fail(daemon.Permanent(fmt.Errorf("OpenAI returned no text choice")))
	}
	text := redact(*choice.Message.Content)
	execution.Generation = rawGeneration(text)
	execution.Generation.Model, execution.Generation.FinishReason = generation.Model, generation.FinishReason
	execution.Generation.MaxOutputTokens = job.MaxOutputTokens
	execution.Generation.Temperature, execution.Generation.Seed = job.GenerationSettings.Temperature, job.GenerationSettings.Seed
	if choice.FinishReason != "stop" {
		return execution, daemon.Categorize(daemon.Permanent(fmt.Errorf("OpenAI generation ended with finish reason %q", generation.FinishReason)), daemon.ProviderFailure)
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
	execution.Text = proof
	return execution, nil
}
