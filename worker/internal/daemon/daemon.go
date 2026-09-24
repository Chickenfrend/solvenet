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
	"net/url"
	"strings"
	"time"
	"unicode/utf8"
)

type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type Job struct {
	ID                     string             `json:"id"`
	RunID                  string             `json:"run_id,omitempty"`
	Kind                   string             `json:"kind"`
	TaskType               string             `json:"task_type,omitempty"`
	Model                  string             `json:"model"`
	Statement              string             `json:"statement"`
	Imports                []string           `json:"imports"`
	Messages               []Message          `json:"messages"`
	MaxOutputTokens        int                `json:"max_output_tokens"`
	TimeoutSeconds         int                `json:"timeout_seconds"`
	ParentAttemptID        *string            `json:"parent_attempt_id"`
	RepairDepth            int                `json:"repair_depth"`
	GenerationSettings     GenerationSettings `json:"generation_settings,omitempty"`
	messagesPresent        bool
	parentAttemptIDPresent bool
	repairDepthPresent     bool
}

// GenerationSettings is the bounded v1 set of optional provider controls.
type GenerationSettings struct {
	Temperature *float64 `json:"temperature,omitempty"`
	Seed        *int64   `json:"seed,omitempty"`
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
	if raw, ok := fields["generation_settings"]; ok {
		var settings map[string]json.RawMessage
		if err := json.Unmarshal(raw, &settings); err != nil || settings == nil {
			return fmt.Errorf("job.generation_settings must be an object")
		}
		for key := range settings {
			if key != "temperature" && key != "seed" {
				return fmt.Errorf("unsupported job.generation_settings field %q", key)
			}
		}
		if bytes.Equal(bytes.TrimSpace(settings["temperature"]), []byte("null")) || bytes.Equal(bytes.TrimSpace(settings["seed"]), []byte("null")) {
			return fmt.Errorf("job.generation_settings values must not be null")
		}
	}
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
	Type string `json:"type,omitempty"`
}

// Generation describes the provider response, including failed extraction.
// Model is provider-reported; the requested model remains on the job.
type Generation struct {
	RawResponse          string   `json:"raw_response"`
	RawResponseTruncated bool     `json:"raw_response_truncated,omitempty"`
	Model                string   `json:"model,omitempty"`
	ModelDigest          string   `json:"model_digest,omitempty"`
	Temperature          *float64 `json:"temperature,omitempty"`
	Seed                 *int64   `json:"seed,omitempty"`
	FinishReason         string   `json:"finish_reason,omitempty"`
	TotalDurationNS      *int64   `json:"total_duration_ns,omitempty"`
	LoadDurationNS       *int64   `json:"load_duration_ns,omitempty"`
	PromptEvalDurationNS *int64   `json:"prompt_eval_duration_ns,omitempty"`
	EvalDurationNS       *int64   `json:"eval_duration_ns,omitempty"`
	ContextLength        int      `json:"context_length,omitempty"`
	MaxOutputTokens      int      `json:"max_output_tokens,omitempty"`
}

type Execution struct {
	Text       string
	Generation *Generation
	Usage      map[string]*int
}

type Result struct {
	Token           string          `json:"lease_token"`
	Status          string          `json:"status"`
	Output          *Output         `json:"output,omitempty"`
	Error           string          `json:"error,omitempty"`
	FailureClass    string          `json:"failure_class,omitempty"`
	FailureCategory string          `json:"failure_category,omitempty"`
	RejectionKind   string          `json:"rejection_kind,omitempty"`
	Usage           map[string]*int `json:"usage,omitempty"`
	Generation      *Generation     `json:"generation,omitempty"`
}

const (
	RejectionMalformedAssignment = "malformed_assignment"
	RejectionUnsupportedProtocol = "unsupported_protocol"
)

type FailureClass string

const (
	FailureTransient FailureClass = "transient"
	FailurePermanent FailureClass = "permanent"
)

type executionError struct {
	class FailureClass
	err   error
}

const (
	ProviderFailure   = "provider_failure"
	FormattingFailure = "formatting_failure"
	OtherFailure      = "other_failure"
)

type categorizedError struct {
	category string
	err      error
}

func (e *categorizedError) Error() string { return e.err.Error() }
func (e *categorizedError) Unwrap() error { return e.err }

func Categorize(err error, category string) error {
	return &categorizedError{category: category, err: err}
}

func FailureCategoryOf(err error) string {
	var categorized *categorizedError
	if errors.As(err, &categorized) {
		return categorized.category
	}
	return OtherFailure
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

func boundedError(err error) string {
	message := strings.ToValidUTF8(err.Error(), "�")
	if len(message) > MaxErrorBytes {
		message = message[:MaxErrorBytes]
		for !utf8.ValidString(message) {
			message = message[:len(message)-1]
		}
	}
	return message
}

type Executor interface {
	Execute(context.Context, Job) (Execution, error)
}

// Health is deliberately coarse: never send provider URLs or credentials.
type Health struct {
	Status string `json:"status"`
	Reason string `json:"reason,omitempty"`
}

type HealthChecker interface {
	Health(context.Context) Health
}

type Worker struct {
	URL                        string
	ID                         string
	Model                      string
	Client                     *http.Client
	Executor                   Executor
	SupportsGenerationSettings bool
	SupportsModelRespond       bool
	Progress                   func(context.Context, string, string, string, string)
}

type ProgressExecutor interface {
	ExecuteProgress(context.Context, Job, func(string)) (Execution, error)
}

const protocolVersion = 1

const (
	MaxTaskMessageBytes = 8192
	MaxTaskMessages     = 8
	MaxTaskResultBytes  = 8192
)

func validTaskType(value string) bool {
	switch value {
	case "plan", "question", "finding", "critique", "task_proposal":
		return true
	}
	return false
}

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
	if err := validateText(a.ID, "assignment_id", MaxIdentifierBytes); err != nil {
		return err
	}
	if err := validateText(a.Token, "lease_token", MaxIdentifierBytes); err != nil {
		return err
	}
	if math.IsNaN(a.LeaseExpiresAt) || math.IsInf(a.LeaseExpiresAt, 0) || a.LeaseExpiresAt <= 0 {
		return fmt.Errorf("lease_expires_at must be a positive finite Unix timestamp")
	}
	if math.IsNaN(a.HeartbeatSeconds) || math.IsInf(a.HeartbeatSeconds, 0) || a.HeartbeatSeconds <= 0 || a.HeartbeatSeconds > MaxHeartbeatSeconds {
		return fmt.Errorf("heartbeat_seconds must be greater than 0 and at most %d", MaxHeartbeatSeconds)
	}
	if err := validateText(a.Job.ID, "job.id", MaxIdentifierBytes); err != nil {
		return err
	}
	if a.Job.RunID != "" && validateText(a.Job.RunID, "job.run_id", MaxIdentifierBytes) != nil {
		return fmt.Errorf("invalid job.run_id")
	}
	if a.Job.Kind != "model.generate" && a.Job.Kind != "model.respond" {
		return fmt.Errorf("unsupported job.kind")
	}
	if a.Job.Kind == "model.respond" {
		if !validTaskType(a.Job.TaskType) {
			return fmt.Errorf("invalid job.task_type")
		}
	} else if a.Job.TaskType != "" {
		return fmt.Errorf("proof job must not have task_type")
	}
	if err := validateText(a.Job.Model, "job.model", MaxModelBytes); err != nil {
		return err
	}
	if a.Job.Model != workerModel {
		return fmt.Errorf("job.model %q does not match worker model %q", a.Job.Model, workerModel)
	}
	if err := validateText(a.Job.Statement, "job.statement", MaxStatementBytes); err != nil {
		return err
	}
	if len(a.Job.Imports) < 1 || len(a.Job.Imports) > MaxImports {
		return fmt.Errorf("job.imports must contain between 1 and %d modules", MaxImports)
	}
	for i, module := range a.Job.Imports {
		if err := validateText(module, fmt.Sprintf("job.imports[%d]", i), MaxImportBytes); err != nil {
			return err
		}
	}
	if !a.Job.messagesPresent || a.Job.Messages == nil {
		return fmt.Errorf("job.messages must be an array")
	}
	if len(a.Job.Messages) > MaxMessages {
		return fmt.Errorf("job.messages must contain at most %d messages", MaxMessages)
	}
	for i, message := range a.Job.Messages {
		if a.Job.Kind == "model.respond" && message.Role != "user" {
			return fmt.Errorf("job.messages[%d].role must be user for model.respond", i)
		}
		if message.Role != "system" && message.Role != "user" && message.Role != "assistant" {
			return fmt.Errorf("job.messages[%d].role must be system, user, or assistant", i)
		}
		if err := validateText(message.Content, fmt.Sprintf("job.messages[%d].content", i), MaxMessageContentBytes); err != nil {
			return err
		}
		if a.Job.Kind == "model.respond" && len(message.Content) > MaxTaskMessageBytes {
			return fmt.Errorf("job.messages[%d].content exceeds task limit", i)
		}
	}
	if a.Job.Kind == "model.respond" && (len(a.Job.Messages) == 0 || len(a.Job.Messages) > MaxTaskMessages || a.Job.ParentAttemptID != nil || a.Job.RepairDepth != 0) {
		return fmt.Errorf("invalid model.respond messages or repair fields")
	}
	if a.Job.MaxOutputTokens <= 0 || a.Job.MaxOutputTokens > MaxOutputTokens {
		return fmt.Errorf("job.max_output_tokens must be between 1 and %d", MaxOutputTokens)
	}
	if t := a.Job.GenerationSettings.Temperature; t != nil && (math.IsNaN(*t) || math.IsInf(*t, 0) || *t < 0 || *t > 2) {
		return fmt.Errorf("job.generation_settings.temperature must be between 0 and 2")
	}
	if s := a.Job.GenerationSettings.Seed; s != nil && *s < 0 {
		return fmt.Errorf("job.generation_settings.seed must be nonnegative")
	}
	if a.Job.TimeoutSeconds <= 0 || a.Job.TimeoutSeconds > MaxGenerationTimeoutSeconds {
		return fmt.Errorf("job.timeout_seconds must be between 1 and %d", MaxGenerationTimeoutSeconds)
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
		if err := validateText(*a.Job.ParentAttemptID, "job.parent_attempt_id", MaxIdentifierBytes); err != nil {
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
	const maxResponse = 2 * 1024 * 1024 // local response cap; a repair claim includes feedback
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
		if raw, ok := target.(*json.RawMessage); ok {
			*raw = append((*raw)[:0], data...)
		} else {
			err = json.Unmarshal(data, target)
		}
	}
	return resp.StatusCode, err
}

func recoverAssignmentCredentials(data []byte) (string, string, bool) {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return "", "", false
	}
	var id, token string
	if err := json.Unmarshal(fields["assignment_id"], &id); err != nil {
		return "", "", false
	}
	if err := json.Unmarshal(fields["lease_token"], &token); err != nil {
		return "", "", false
	}
	if validateText(id, "assignment_id", MaxIdentifierBytes) != nil ||
		validateText(token, "lease_token", MaxIdentifierBytes) != nil {
		return "", "", false
	}
	return id, token, true
}

func (w *Worker) submitResult(ctx context.Context, assignmentID string, result Result) error {
	var err error
	for attempt := 0; attempt < 3; attempt++ {
		var code int
		code, err = w.post(ctx, "/v1/assignments/"+url.PathEscape(assignmentID)+"/result", result, nil)
		if err == nil {
			return nil
		}
		if code >= 400 && code < 500 {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Duration(attempt+1) * 200 * time.Millisecond):
		}
	}
	return err
}

func (w *Worker) rejectAssignment(ctx context.Context, data []byte, cause error) error {
	id, token, ok := recoverAssignmentCredentials(data)
	if !ok {
		return cause
	}
	kind := RejectionMalformedAssignment
	var unsupported *UnsupportedProtocolVersionError
	if errors.As(cause, &unsupported) {
		kind = RejectionUnsupportedProtocol
	}
	message := boundedError(cause)
	result := Result{Token: token, Status: "rejected", Error: message, RejectionKind: kind}
	if err := w.submitResult(ctx, id, result); err != nil {
		return fmt.Errorf("%w (assignment rejection failed: %v)", cause, err)
	}
	return cause
}

// Once claims at most one job. It returns false when no compatible work exists.
func (w *Worker) Once(ctx context.Context) (bool, error) {
	var raw json.RawMessage
	claim := map[string]any{"worker_id": w.ID, "models": []string{w.Model}}
	if w.SupportsGenerationSettings {
		claim["capabilities"] = []string{"generation_settings"}
	}
	if w.SupportsModelRespond {
		capabilities, _ := claim["capabilities"].([]string)
		claim["capabilities"] = append(capabilities, "model_respond")
	}
	if checker, ok := w.Executor.(HealthChecker); ok {
		claim["provider_health"] = checker.Health(ctx)
	}
	code, err := w.post(ctx, "/v1/claim", claim, &raw)
	if err != nil {
		return false, err
	}
	if code == http.StatusNoContent {
		return false, nil
	}
	var a Assignment
	if err := json.Unmarshal(raw, &a); err != nil {
		return true, w.rejectAssignment(ctx, raw, fmt.Errorf("malformed assignment: %w", err))
	}
	if err := a.validate(w.Model); err != nil {
		return true, w.rejectAssignment(ctx, raw, err)
	}
	if a.Job.Kind == "model.respond" && !w.SupportsModelRespond {
		return true, w.rejectAssignment(ctx, raw, fmt.Errorf("worker does not support model.respond"))
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
	var execution Execution
	var executeErr error
	if executor, ok := w.Executor.(ProgressExecutor); ok && w.Progress != nil && a.Job.RunID != "" && a.Job.Kind == "model.generate" {
		publish := func(raw string) { w.Progress(jobCtx, a.Job.RunID, a.Job.ID, a.ID, raw) }
		publish("")
		execution, executeErr = executor.ExecuteProgress(jobCtx, a.Job, publish)
	} else {
		execution, executeErr = w.Executor.Execute(jobCtx, a.Job)
	}
	result := Result{Token: a.Token, Status: "completed", Output: &Output{Text: execution.Text},
		Generation: execution.Generation, Usage: execution.Usage}
	maximum := MaxCandidateBytes
	if a.Job.Kind == "model.respond" {
		maximum = MaxTaskResultBytes
		result.Output.Type = a.Job.TaskType
	}
	if executeErr == nil && (len(strings.TrimSpace(execution.Text)) == 0 || len(execution.Text) > maximum) {
		executeErr = Categorize(Permanent(fmt.Errorf("executor output must be 1–%d bytes", maximum)), FormattingFailure)
	}
	if executeErr != nil {
		message := boundedError(executeErr)
		result.Status, result.Error, result.Output = "failed", message, nil
		result.FailureClass = string(FailureClassOf(executeErr))
		result.FailureCategory = FailureCategoryOf(executeErr)
	}
	// The same payload is retried so a lost acknowledgement is harmless.
	return true, w.submitResult(ctx, a.ID, result)
}
