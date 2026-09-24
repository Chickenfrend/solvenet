package provider

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"solvenet/worker/internal/daemon"
)

func TestTaskOutputFormatting(t *testing.T) {
	job := daemon.Job{Kind: "model.respond", TaskType: "critique", Imports: []string{"Init"}, Statement: ": True",
		Messages: []daemon.Message{{Role: "user", Content: "Review this idea"}}, MaxOutputTokens: 100}
	for _, adapter := range []string{"ollama", "openai"} {
		for _, content := range []string{`{"text":"Need a lemma"}`, `{"proof":"rfl"}`, `{"text":""}`} {
			t.Run(adapter+"/"+content, func(t *testing.T) {
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					if r.URL.Path == "/api/tags" {
						w.Write([]byte(`{"models":[]}`))
						return
					}
					var request struct {
						Messages []daemon.Message `json:"messages"`
						Format   struct {
							Required []string `json:"required"`
						} `json:"format"`
					}
					if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
						t.Error(err)
					}
					if len(request.Messages) != 3 || request.Messages[2].Content != "Review this idea" ||
						!strings.Contains(request.Messages[0].Content, "critique") || !strings.Contains(request.Messages[1].Content, ": True") {
						t.Errorf("lost task context: %+v", request.Messages)
					}
					if adapter == "ollama" {
						if len(request.Format.Required) != 1 || request.Format.Required[0] != "text" {
							t.Errorf("schema: %+v", request.Format)
						}
						json.NewEncoder(w).Encode(map[string]any{"model": "tiny", "message": map[string]string{"content": content}, "done": true, "done_reason": "stop"})
					} else {
						if r.Header.Get("Authorization") != "Bearer secret" {
							t.Error("missing worker-local key")
						}
						json.NewEncoder(w).Encode(map[string]any{"model": "gpt-4o-mini", "choices": []any{map[string]any{"message": map[string]string{"content": content}, "finish_reason": "stop"}}})
					}
				}))
				defer server.Close()
				var executor daemon.Executor
				if adapter == "ollama" {
					o, err := NewOllama(server.URL, "tiny", DefaultOllamaContext)
					if err != nil {
						t.Fatal(err)
					}
					executor = o
				} else {
					o, err := NewOpenAI(server.URL, "gpt-4o-mini", "secret")
					if err != nil {
						t.Fatal(err)
					}
					executor = o
				}
				result, err := executor.Execute(context.Background(), job)
				if content == `{"text":"Need a lemma"}` {
					if err != nil || result.Text != "Need a lemma" {
						t.Fatalf("result=%+v err=%v", result, err)
					}
				} else if err == nil || daemon.FailureCategoryOf(err) != daemon.FormattingFailure || result.Generation == nil || result.Generation.RawResponse != content {
					t.Fatalf("result=%+v err=%v", result, err)
				}
			})
		}
	}
}
