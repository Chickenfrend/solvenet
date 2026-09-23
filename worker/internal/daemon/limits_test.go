package daemon

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"unicode/utf8"
)

func validLimitAssignment(t *testing.T) Assignment {
	t.Helper()
	data := []byte(`{"protocol_version":1,"assignment_id":"a","lease_token":"t","lease_expires_at":1,"heartbeat_seconds":1,"job":{"id":"j","kind":"model.generate","model":"m","statement":"s","imports":["Init"],"messages":[],"max_output_tokens":1,"timeout_seconds":1,"parent_attempt_id":null,"repair_depth":0}}`)
	var a Assignment
	if err := json.Unmarshal(data, &a); err != nil {
		t.Fatal(err)
	}
	return a
}

type limitExecutor struct{ text string }

func (e limitExecutor) Execute(_ context.Context, _ Job) (Execution, error) {
	return Execution{Text: e.text}, nil
}

func TestCandidateSubmissionByteBoundary(t *testing.T) {
	for _, extra := range []int{0, 1} {
		t.Run(string(rune('0'+extra)), func(t *testing.T) {
			var result Result
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch r.URL.Path {
				case "/v1/claim":
					w.Write([]byte(`{"protocol_version":1,"assignment_id":"a","lease_token":"t","lease_expires_at":1,"heartbeat_seconds":86400,"job":{"id":"j","kind":"model.generate","model":"m","statement":"s","imports":["Init"],"messages":[],"max_output_tokens":1,"timeout_seconds":1,"parent_attempt_id":null,"repair_depth":0}}`))
				case "/v1/assignments/a/result":
					if err := json.NewDecoder(r.Body).Decode(&result); err != nil {
						t.Error(err)
					}
					w.Write([]byte(`{"accepted":true}`))
				default:
					t.Errorf("unexpected path %s", r.URL.Path)
				}
			}))
			defer server.Close()
			worker := Worker{URL: server.URL, ID: "w", Model: "m", Client: server.Client(),
				Executor: limitExecutor{text: strings.Repeat("é", MaxCandidateBytes/2) + strings.Repeat("x", extra)}}
			worked, err := worker.Once(context.Background())
			if err != nil || !worked {
				t.Fatalf("worked=%v err=%v", worked, err)
			}
			if extra == 0 && (result.Status != "completed" || len(result.Output.Text) != MaxCandidateBytes) {
				t.Fatalf("at limit: %+v", result)
			}
			if extra == 1 && (result.Status != "failed" || result.Output != nil || result.FailureClass != string(FailurePermanent)) {
				t.Fatalf("over limit: %+v", result)
			}
		})
	}
}

func TestErrorUTF8ByteBoundary(t *testing.T) {
	for _, extra := range []int{0, 1} {
		message := boundedError(errors.New(strings.Repeat("é", MaxErrorBytes/2) + strings.Repeat("x", extra)))
		if len(message) != MaxErrorBytes || !utf8.ValidString(message) {
			t.Fatalf("extra=%d bytes=%d valid=%v", extra, len(message), utf8.ValidString(message))
		}
	}
	message := boundedError(errors.New(strings.Repeat("é", MaxErrorBytes/2-1) + "€x"))
	if len(message) > MaxErrorBytes || !utf8.ValidString(message) {
		t.Fatalf("clipped through rune: %q", message)
	}
}

func TestAssignmentWireLimitBoundaries(t *testing.T) {
	checks := []struct {
		field string
		max   int
		set   func(*Assignment, int)
	}{
		{"assignment_id", MaxIdentifierBytes, func(a *Assignment, n int) { a.ID = strings.Repeat("é", n/2) + strings.Repeat("x", n%2) }},
		{"lease_token", MaxIdentifierBytes, func(a *Assignment, n int) { a.Token = strings.Repeat("é", n/2) + strings.Repeat("x", n%2) }},
		{"job.statement", MaxStatementBytes, func(a *Assignment, n int) { a.Job.Statement = strings.Repeat("é", n/2) + strings.Repeat("x", n%2) }},
		{"job.model", MaxModelBytes, func(a *Assignment, n int) { a.Job.Model = strings.Repeat("é", n/2) + strings.Repeat("x", n%2) }},
		{"job.imports[0]", MaxImportBytes, func(a *Assignment, n int) { a.Job.Imports[0] = strings.Repeat("é", n/2) + strings.Repeat("x", n%2) }},
		{"job.messages[0].content", MaxMessageContentBytes, func(a *Assignment, n int) {
			a.Job.Messages = []Message{{Role: "user", Content: strings.Repeat("é", n/2) + strings.Repeat("x", n%2)}}
		}},
		{"job.max_output_tokens", MaxOutputTokens, func(a *Assignment, n int) { a.Job.MaxOutputTokens = n }},
		{"job.timeout_seconds", MaxGenerationTimeoutSeconds, func(a *Assignment, n int) { a.Job.TimeoutSeconds = n }},
		{"heartbeat_seconds", MaxHeartbeatSeconds, func(a *Assignment, n int) { a.HeartbeatSeconds = float64(n) }},
	}
	for _, check := range checks {
		t.Run(check.field, func(t *testing.T) {
			for _, extra := range []int{0, 1} {
				a := validLimitAssignment(t)
				check.set(&a, check.max+extra)
				model := a.Job.Model
				err := a.validate(model)
				if (err == nil) != (extra == 0) {
					t.Fatalf("extra=%d err=%v", extra, err)
				}
				if err != nil && !strings.Contains(err.Error(), check.field) {
					t.Fatalf("wrong field: %v", err)
				}
			}
		})
	}
	for _, check := range []struct {
		field string
		max   int
		set   func(*Assignment, int)
	}{
		{"job.imports", MaxImports, func(a *Assignment, n int) {
			a.Job.Imports = make([]string, n)
			for i := range a.Job.Imports {
				a.Job.Imports[i] = "Init"
			}
		}},
		{"job.messages", MaxMessages, func(a *Assignment, n int) {
			a.Job.Messages = make([]Message, n)
			for i := range a.Job.Messages {
				a.Job.Messages[i] = Message{Role: "user", Content: "x"}
			}
		}},
	} {
		for _, extra := range []int{0, 1} {
			a := validLimitAssignment(t)
			check.set(&a, check.max+extra)
			if err := a.validate(a.Job.Model); (err == nil) != (extra == 0) {
				t.Fatalf("%s extra=%d err=%v", check.field, extra, err)
			}
		}
	}
}
