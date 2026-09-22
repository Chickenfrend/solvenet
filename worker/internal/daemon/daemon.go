// Package daemon implements the v1 worker protocol with one execution slot.
package daemon

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type Message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

type Job struct {
	ID              string    `json:"id"`
	Kind            string    `json:"kind"`
	Model           string    `json:"model"`
	Statement       string    `json:"statement"`
	Imports         []string  `json:"imports"`
	Messages        []Message `json:"messages"`
	MaxOutputTokens int       `json:"max_output_tokens"`
	TimeoutSeconds  int       `json:"timeout_seconds"`
}

type Assignment struct {
	Version          int     `json:"protocol_version"`
	ID               string  `json:"assignment_id"`
	Token            string  `json:"lease_token"`
	HeartbeatSeconds float64 `json:"heartbeat_seconds"`
	Job              Job     `json:"job"`
}

type Output struct {
	Text string `json:"text"`
}

type Result struct {
	Token  string          `json:"lease_token"`
	Status string          `json:"status"`
	Output *Output         `json:"output,omitempty"`
	Error  string          `json:"error,omitempty"`
	Usage  map[string]*int `json:"usage,omitempty"`
}

type Executor interface {
	Execute(context.Context, Job) (string, error)
}

type Worker struct {
	URL      string
	ID       string
	Model    string
	Client   *http.Client
	Executor Executor
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
	data, err := io.ReadAll(io.LimitReader(resp.Body, 262145))
	if err != nil {
		return resp.StatusCode, err
	}
	if len(data) > 262144 {
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
	if a.Version != 1 || a.ID == "" || a.Token == "" || a.HeartbeatSeconds <= 0 || a.Job.Kind != "model.generate" || a.Job.Model != w.Model {
		return true, fmt.Errorf("unsupported or malformed assignment")
	}
	seconds := a.Job.TimeoutSeconds
	if seconds <= 0 || seconds > 120 {
		seconds = 120
	}
	jobCtx, cancel := context.WithTimeout(ctx, time.Duration(seconds)*time.Second)
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
	proof, executeErr := w.Executor.Execute(jobCtx, a.Job)
	result := Result{Token: a.Token, Status: "completed", Output: &Output{Text: proof}}
	if executeErr == nil && (len(proof) == 0 || len(proof) > 128*1024) {
		executeErr = fmt.Errorf("executor output must be 1–131072 bytes")
	}
	if executeErr != nil {
		message := executeErr.Error()
		if len(message) > 4000 {
			message = message[:4000]
		}
		result = Result{Token: a.Token, Status: "failed", Error: message}
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
