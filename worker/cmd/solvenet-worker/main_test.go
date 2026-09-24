package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"solvenet/worker/internal/provider"
)

func TestOllamaContextFlag(t *testing.T) {
	for _, test := range []struct {
		name string
		args []string
		want int
	}{
		{"default", []string{"-provider", "ollama", "-model", "test"}, provider.DefaultOllamaContext},
		{"configured", []string{"-provider", "ollama", "-model", "test", "-ollama-context", "16384"}, 16384},
	} {
		t.Run(test.name, func(t *testing.T) {
			cfg, err := parseConfig(test.args, &bytes.Buffer{})
			if err != nil {
				t.Fatal(err)
			}
			executor, _, err := makeExecutor(cfg)
			if err != nil {
				t.Fatal(err)
			}
			ollama := executor.(*provider.Ollama)
			if ollama.ContextSize != test.want {
				t.Fatalf("context=%d want=%d", ollama.ContextSize, test.want)
			}
		})
	}
}

func TestOllamaContextFlagValidation(t *testing.T) {
	for _, value := range []string{"0", "-1", "1048577"} {
		_, err := parseConfig([]string{"-provider", "ollama", "-model", "test", "-ollama-context", value}, &bytes.Buffer{})
		if err == nil || !strings.Contains(err.Error(), "ollama-context") {
			t.Fatalf("value=%s err=%v", value, err)
		}
	}
}

func TestOpenAIWorkerCredentialAndModel(t *testing.T) {
	cfg, err := parseConfig([]string{"-provider", "openai", "-model", "gpt-4o-mini"}, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	t.Setenv("OPENAI_API_KEY", "")
	t.Setenv("OPENAI_API_KEY_FILE", "")
	if _, _, err := makeExecutor(cfg); err == nil || !strings.Contains(err.Error(), "credential unavailable") {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "api-key")
	if err := os.WriteFile(path, []byte("secret-test-key\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("OPENAI_API_KEY_FILE", path)
	exec, model, err := makeExecutor(cfg)
	if err != nil || model != "openai/gpt-4o-mini" || exec.(*provider.OpenAI).Key != "secret-test-key" {
		t.Fatalf("model=%s err=%v", model, err)
	}
	t.Setenv("OPENAI_API_KEY", "other-key")
	if _, _, err := makeExecutor(cfg); err == nil || strings.Contains(err.Error(), "other-key") {
		t.Fatalf("err=%v", err)
	}
	t.Setenv("OPENAI_API_KEY_FILE", filepath.Join(t.TempDir(), "missing"))
	t.Setenv("OPENAI_API_KEY", "")
	if _, _, err := makeExecutor(cfg); err == nil || strings.Contains(err.Error(), "missing") {
		t.Fatalf("err=%v", err)
	}
}
