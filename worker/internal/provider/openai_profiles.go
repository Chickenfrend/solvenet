package provider

import (
	"encoding/json"
	"fmt"
	"strings"

	"solvenet/worker/internal/daemon"
)

const ChatJSON = "chat-json"
const ResponsesReasoning = "responses-reasoning"

// OpenAIConfig is worker-local. Profiles describe API behavior, not model families.
// Reasoning is deliberately not added to the v1 job protocol.
type OpenAIConfig struct {
	Profile         string
	ReasoningEffort string
	ContextTokens   int
	ContextBytes    int
	MaxOutputTokens int
}

func resolveOpenAIConfig(model string, c OpenAIConfig) (OpenAIConfig, error) {
	if c.Profile == "" {
		switch model {
		case "gpt-4o-mini":
			c.Profile = ChatJSON
		case "gpt-5.4", "gpt-6.1-sol":
			c.Profile = ResponsesReasoning
		default:
			return c, fmt.Errorf("unfamiliar OpenAI model requires explicit -openai-profile (chat-json or responses-reasoning)")
		}
	}
	if c.Profile != ChatJSON && c.Profile != ResponsesReasoning {
		return c, fmt.Errorf("invalid OpenAI profile: use chat-json or responses-reasoning")
	}
	if c.ReasoningEffort != "" {
		if c.Profile != ResponsesReasoning {
			return c, fmt.Errorf("chat-json does not support reasoning effort")
		}
		if c.ReasoningEffort != "low" && c.ReasoningEffort != "medium" && c.ReasoningEffort != "high" {
			return c, fmt.Errorf("responses-reasoning supports only low, medium or high reasoning effort")
		}
	}
	if c.ContextTokens == 0 {
		c.ContextTokens = 32768
	}
	if c.ContextBytes == 0 {
		c.ContextBytes = 32768
	}
	if c.MaxOutputTokens == 0 {
		// A shared API contract does not imply equal model output capacity.
		c.MaxOutputTokens = 16384
		if model == "gpt-5.4" || model == "gpt-6.1-sol" {
			c.MaxOutputTokens = daemon.MaxOutputTokens
		}
	}
	if c.ContextTokens < 1 || c.ContextTokens > 1024*1024 || c.ContextBytes < 1 || c.ContextBytes > 1024*1024 || c.MaxOutputTokens < 1 || c.MaxOutputTokens > daemon.MaxOutputTokens {
		return c, fmt.Errorf("OpenAI context capacities must be 1-1048576 and output capacity 1-32768")
	}
	return c, nil
}

func (o *OpenAI) SupportsGenerationSettings() bool { return o.Config.Profile == ChatJSON }

func (o *OpenAI) extractResponse(data []byte, job daemon.Job, execution daemon.Execution) (daemon.Execution, error) {
	fail := func(message string) (daemon.Execution, error) {
		return execution, daemon.Categorize(daemon.Permanent(fmt.Errorf("OpenAI %s", message)), daemon.ProviderFailure)
	}
	var reply struct {
		Model  string          `json:"model"`
		Status string          `json:"status"`
		Error  json.RawMessage `json:"error"`
		Output []struct {
			Type    string `json:"type"`
			Role    string `json:"role"`
			Status  string `json:"status"`
			Phase   string `json:"phase"`
			Content []struct {
				Type string  `json:"type"`
				Text *string `json:"text"`
			} `json:"content"`
		} `json:"output"`
		Usage struct {
			Input  *int `json:"input_tokens"`
			Output *int `json:"output_tokens"`
		} `json:"usage"`
	}
	if json.Unmarshal(data, &reply) != nil {
		return fail("invalid response JSON")
	}
	execution.Usage = map[string]*int{"input_tokens": reply.Usage.Input, "output_tokens": reply.Usage.Output}
	for key, n := range execution.Usage {
		if n != nil && *n < 0 {
			execution.Usage[key] = nil
		}
	}
	redact := func(s string) string { return strings.ReplaceAll(s, o.Key, "[redacted]") }
	execution.Generation.Model, execution.Generation.FinishReason = redact(reply.Model), redact(reply.Status)
	if len(execution.Generation.Model) > daemon.MaxModelBytes || len(execution.Generation.FinishReason) > daemon.MaxFinishReasonBytes {
		execution.Generation.Model, execution.Generation.FinishReason = "", ""
		return fail("response metadata exceeded size limit")
	}
	if len(reply.Error) != 0 && string(reply.Error) != "null" {
		// A successful HTTP response can contain a failed Responses operation.
		// Only recognize bounded codes; provider messages never leave the worker.
		var bodyError struct {
			Code string `json:"code"`
		}
		if len(reply.Error) <= 8192 && json.Unmarshal(reply.Error, &bodyError) == nil {
			if bodyError.Code == "rate_limit_exceeded" || bodyError.Code == "server_error" {
				return execution, daemon.Categorize(daemon.Transient(fmt.Errorf("OpenAI response failed with a retryable provider error")), daemon.ProviderFailure)
			}
		}
		return fail("response failed with a provider error")
	}
	var text strings.Builder
	count := 0
	for _, item := range reply.Output {
		if item.Type == "reasoning" {
			continue
		} // No reasoning summaries or opaque state are forwarded.
		if item.Type != "message" || item.Role != "assistant" || item.Status != "completed" || (item.Phase != "" && item.Phase != "final_answer") {
			return fail("unexpected or incomplete output item")
		}
		count++
		for _, content := range item.Content {
			if content.Type == "refusal" {
				return fail("refused the request")
			}
			if content.Type != "output_text" {
				return fail("unexpected output content")
			}
			if content.Text == nil {
				return fail("returned malformed text content")
			}
			text.WriteString(*content.Text)
		}
	}
	raw := safeOpenAIRaw(text.String(), o.Key)
	raw.Model, raw.FinishReason = execution.Generation.Model, execution.Generation.FinishReason
	raw.MaxOutputTokens = job.MaxOutputTokens
	execution.Generation = raw
	if reply.Status != "completed" {
		return fail("response incomplete or failed")
	}
	if count != 1 || text.Len() == 0 {
		return fail("returned no single text message")
	}
	if raw.RawResponseTruncated {
		return execution, daemon.Categorize(daemon.Permanent(fmt.Errorf("OpenAI generated text exceeded size limit")), daemon.FormattingFailure)
	}
	output, err := extractOutput(redact(text.String()), job)
	if err != nil {
		return execution, daemon.Categorize(daemon.Permanent(fmt.Errorf("OpenAI output format: %w", err)), daemon.FormattingFailure)
	}
	execution.Text = redact(output)
	if job.Kind == "model.respond" {
		execution.Text = redactRawString(output, o.Key, 0)
	}
	return execution, nil
}
