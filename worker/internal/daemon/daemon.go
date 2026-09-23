// Package daemon implements the v1 worker protocol with one execution slot.
package daemon

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"
	"time"
)

type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type Job struct {
	ID                     string    `json:"id"`
	Kind                   string    `json:"kind"`
	Model                  string    `json:"model"`
	Statement              string    `json:"statement"`
	Imports                []string  `json:"imports"`
	Messages               []Message `json:"messages"`
	MaxOutputTokens        int       `json:"max_output_tokens"`
	TimeoutSeconds         int       `json:"timeout_seconds"`
	ParentAttemptID        *string   `json:"parent_attempt_id"`
	RepairDepth            int       `json:"repair_depth"`
	messagesPresent        bool
	parentAttemptIDPresent bool
	repairDepthPresent     bool
}

func (j *Job) UnmarshalJSON(data []byte) error {
	type jobWire Job
	var decoded jobWire
	if err := json.Unmarshal(data, &decoded); err != nil {
		return err
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	*j = Job(decoded)
	_, j.messagesPresent = fields["messages"]
	_, j.parentAttemptIDPresent = fields["parent_attempt_id"]
	_, j.repairDepthPresent = fields["repair_depth"]
	return nil
}

type Assignment struct {
	Version          int     `json:"protocol_version"`
	ID               string  `json:"assignment_id"`
	Token            string  `json:"lease_token"`
	LeaseExpiresAt   float64 `json:"lease_expires_at"`
	HeartbeatSeconds float64 `json:"heartbeat_seconds"`
	Job              Job     `json:"job"`
	versionPresent   bool
}

func (a *Assignment) UnmarshalJSON(data []byte) error {
	type assignmentWire Assignment
	*a = Assignment{}
	var wire struct {
		*assignmentWire
		Version *int `json:"protocol_version"`
	}
	wire.assignmentWire = (*assignmentWire)(a)
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	if wire.Version != nil {
		a.Version = *wire.Version
		a.versionPresent = true
	}
	return nil
}

type Output struct {
	Text string `json:"text"`
}

// Generation describes the provider response, including failed extraction.
// Model is provider-reported; the requested model remains on the job.
type Generation struct {
	RawResponse          string `json:"raw_response"`
	RawResponseTruncated bool   `json:"raw_response_truncated,omitempty"`
	Model                string `json:"model,omitempty"`
	FinishReason         string `json:"finish_reason,omitempty"`
	TotalDurationNS      *int64 `json:"total_duration_ns,omitempty"`
	LoadDurationNS       *int64 `json:"load_duration_ns,omitempty"`
	PromptEvalDurationNS *int64 `json:"prompt_eval_duration_ns,omitempty"`
	EvalDurationNS       *int64 `json:"eval_duration_ns,omitempty"`
	ContextLength        int    `json:"context_length,omitempty"`
	MaxOutputTokens      int    `json:"max_output_tokens,omitempty"`
}

type Execution struct {
	Text       string
	Generation *Generation
	Usage      map[string]*int
}

type Result struct {
	Token        string          `json:"lease_token"`
	Status       string          `json:"status"`
	Output       *Output         `json:"output,omitempty"`
	Error        string          `json:"error,omitempty"`
	FailureClass string          `json:"failure_class,omitempty"`
	Usage        map[string]*int `json:"usage,omitempty"`
	Generation   *Generation     `json:"generation,omitempty"`
}

type FailureClass string

const (
	FailureTransient FailureClass = "transient"
	FailurePermanent FailureClass = "permanent"
)

type executionError struct {
	class FailureClass
	err   error
}

func (e *executionError) Error() string { return e.err.Error() }
func (e *executionError) Unwrap() error { return e.err }

func Transient(err error) error { return &executionError{class: FailureTransient, err: err} }
func Permanent(err error) error { return &executionError{class: FailurePermanent, err: err} }

// FailureClassOf preserves the v1 retry behavior for executors that return an
// ordinary, unclassified error.
func FailureClassOf(err error) FailureClass {
	var classified *executionError
	if errors.As(err, &classified) {
		return classified.class
	}
	return FailureTransient
}

type Executor interface {
	Execute(context.Context, Job) (Execution, error)
}

type Worker struct {
	URL      string
	ID       string
	Model    string
	Client   *http.Client
	Executor Executor
}

const (
	protocolVersion             = 1
	maxIdentifierBytes          = 256
	maxStatementBytes           = 64 * 1024
	maxImports                  = 32
	maxImportBytes              = 256
	maxMessages                 = 32
	maxMessageContentBytes      = 256 * 1024
	maxModelBytes               = 256
	maxOutputTokens             = 32768
	maxGenerationTimeoutSeconds = 24 * 60 * 60
	maxHeartbeatSeconds         = 24 * 60 * 60
)

// UnsupportedProtocolVersionError identifies a well-formed assignment for a
// protocol this worker cannot execute. It is separate from field validation so
// callers can handle compatibility failures distinctly.
type UnsupportedProtocolVersionError struct {
	Version int
}

func (e *UnsupportedProtocolVersionError) Error() string {
	return fmt.Sprintf("unsupported protocol_version %d (worker supports %d)", e.Version, protocolVersion)
}

func validateText(value, field string, maximum int) error {
	if strings.TrimSpace(value) == "" || len(value) > maximum {
		return fmt.Errorf("%s must be a nonempty string of at most %d bytes", field, maximum)
	}
	return nil
}

func (a Assignment) validate(workerModel string) error {
	if !a.versionPresent {
		return fmt.Errorf("protocol_version is required")
	}
	if a.Version != protocolVersion {
		return &UnsupportedProtocolVersionError{Version: a.Version}
	}
	if err := validateText(a.ID, "assignment_id", maxIdentifierBytes); err != nil {
		return err
	}
	if err := validateText(a.Token, "lease_token", maxIdentifierBytes); err != nil {
		return err
	}
	if math.IsNaN(a.LeaseExpiresAt) || math.IsInf(a.LeaseExpiresAt, 0) || a.LeaseExpiresAt <= 0 {
		return fmt.Errorf("lease_expires_at must be a positive finite Unix timestamp")
	}
	if math.IsNaN(a.HeartbeatSeconds) || math.IsInf(a.HeartbeatSeconds, 0) || a.HeartbeatSeconds <= 0 || a.HeartbeatSeconds > maxHeartbeatSeconds {
		return fmt.Errorf("heartbeat_seconds must be greater than 0 and at most %d", maxHeartbeatSeconds)
	}
	if err := validateText(a.Job.ID, "job.id", maxIdentifierBytes); err != nil {
		return err
	}
	if a.Job.Kind != "model.generate" {
		return fmt.Errorf("job.kind must be model.generate")
	}
	if err := validateText(a.Job.Model, "job.model", maxModelBytes); err != nil {
		return err
	}
	if a.Job.Model != workerModel {
		return fmt.Errorf("job.model %q does not match worker model %q", a.Job.Model, workerModel)
	}
	if err := validateText(a.Job.Statement, "job.statement", maxStatementBytes); err != nil {
		return err
	}
	if len(a.Job.Imports) < 1 || len(a.Job.Imports) > maxImports {
		return fmt.Errorf("job.imports must contain between 1 and %d modules", maxImports)
	}
	for i, module := range a.Job.Imports {
		if err := validateText(module, fmt.Sprintf("job.imports[%d]", i), maxImportBytes); err != nil {
			return err
		}
	}
	if !a.Job.messagesPresent || a.Job.Messages == nil {
		return fmt.Errorf("job.messages must be an array")
	}
	if len(a.Job.Messages) > maxMessages {
		return fmt.Errorf("job.messages must contain at most %d messages", maxMessages)
	}
	for i, message := range a.Job.Messages {
		if message.Role != "system" && message.Role != "user" && message.Role != "assistant" {
			return fmt.Errorf("job.messages[%d].role must be system, user, or assistant", i)
		}
		if err := validateText(message.Content, fmt.Sprintf("job.messages[%d].content", i), maxMessageContentBytes); err != nil {
			return err
		}
	}
	if a.Job.MaxOutputTokens <= 0 || a.Job.MaxOutputTokens > maxOutputTokens {
		return fmt.Errorf("job.max_output_tokens must be between 1 and %d", maxOutputTokens)
	}
	if a.Job.TimeoutSeconds <= 0 || a.Job.TimeoutSeconds > maxGenerationTimeoutSeconds {
		return fmt.Errorf("job.timeout_seconds must be between 1 and %d", maxGenerationTimeoutSeconds)
	}
	if !a.Job.repairDepthPresent {
		return fmt.Errorf("job.repair_depth is required")
	}
	if a.Job.RepairDepth < 0 {
		return fmt.Errorf("job.repair_depth must be nonnegative")
	}
	if !a.Job.parentAttemptIDPresent {
		return fmt.Errorf("job.parent_attempt_id is required (null for an initial job)")
	}
	if a.Job.RepairDepth == 0 && a.Job.ParentAttemptID != nil {
		return fmt.Errorf("job.parent_attempt_id must be null when job.repair_depth is 0")
	}
	if a.Job.RepairDepth > 0 && a.Job.ParentAttemptID == nil {
		return fmt.Errorf("job.parent_attempt_id is required when job.repair_depth is positive")
	}
	if a.Job.ParentAttemptID != nil {
		if err := validateText(*a.Job.ParentAttemptID, "job.parent_attempt_id", maxIdentifierBytes); err != nil {
			return err
		}
	}
	return nil
}

func (w *Worker) post(ctx context.Context, path string, body any, target any) (int, error) {
	payload, err := json.Marshal(body)
	if err != nil {
		return 0, err
	}
	req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(w.URL, "/")+path, bytes.NewReader(payload))
	if err != nil {
		return 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := w.Client.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	// A repair claim includes the previous candidate and Lean diagnostics.
	const maxResponse = 2 * 1024 * 1024
	data, err := io.ReadAll(io.LimitReader(resp.Body, maxResponse+1))
	if err != nil {
		return resp.StatusCode, err
	}
	if len(data) > maxResponse {
		return resp.StatusCode, fmt.Errorf("response too large")
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return resp.StatusCode, fmt.Errorf("HTTP %d: %s", resp.StatusCode, data)
	}
	if target != nil && resp.StatusCode != http.StatusNoContent {
		err = json.Unmarshal(data, target)
	}
	return resp.StatusCode, err
}

// Once claims at most one job. It returns false when no compatible work exists.
func (w *Worker) Once(ctx context.Context) (bool, error) {
	var a Assignment
	code, err := w.post(ctx, "/v1/claim", map[string]any{"worker_id": w.ID, "models": []string{w.Model}}, &a)
	if err != nil {
		return false, err
	}
	if code == http.StatusNoContent {
		return false, nil
	}
	if err := a.validate(w.Model); err != nil {
		return true, err
	}
	jobCtx, cancel := context.WithTimeout(ctx, time.Duration(a.Job.TimeoutSeconds)*time.Second)
	defer cancel()
	// Keep renewing the lease through result submission, not only generation.
	heartCtx, stopHeart := context.WithCancel(ctx)
	heartDone := make(chan struct{})
	defer func() { stopHeart(); <-heartDone }()
	go func() {
		defer close(heartDone)
		interval := time.Duration(a.HeartbeatSeconds * float64(time.Second))
		if interval < time.Millisecond {
			interval = time.Millisecond
		}
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-heartCtx.Done():
				return
			case <-ticker.C:
				status, _ := w.post(heartCtx, "/v1/assignments/"+a.ID+"/heartbeat", map[string]string{"lease_token": a.Token}, nil)
				if status == http.StatusConflict {
					cancel()
					return
				}
			}
		}
	}()
	execution, executeErr := w.Executor.Execute(jobCtx, a.Job)
	result := Result{Token: a.Token, Status: "completed", Output: &Output{Text: execution.Text},
		Generation: execution.Generation, Usage: execution.Usage}
	if executeErr == nil && (len(strings.TrimSpace(execution.Text)) == 0 || len(execution.Text) > 128*1024) {
		executeErr = Permanent(fmt.Errorf("executor output must be 1–131072 bytes"))
	}
	if executeErr != nil {
		message := executeErr.Error()
		if len(message) > 4000 {
			message = message[:4000]
		}
		result.Status, result.Error, result.Output = "failed", message, nil
		result.FailureClass = string(FailureClassOf(executeErr))
	}
	// The same payload is retried so a lost acknowledgement is harmless.
	for attempt := 0; attempt < 3; attempt++ {
		code, err = w.post(ctx, "/v1/assignments/"+a.ID+"/result", result, nil)
		if err == nil {
			return true, nil
		}
		if code >= 400 && code < 500 {
			return true, err
		}
		select {
		case <-ctx.Done():
			return true, ctx.Err()
		case <-time.After(time.Duration(attempt+1) * 200 * time.Millisecond):
		}
	}
	return true, err
}
