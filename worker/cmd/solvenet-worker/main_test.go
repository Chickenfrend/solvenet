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

func TestOpenAIProfileFlags(t *testing.T) {
	t.Setenv("OPENAI_API_KEY", "fixture-key")
	t.Setenv("OPENAI_API_KEY_FILE", "")
	for _, profile := range []string{provider.ChatJSON, provider.ResponsesReasoning} {
		args := []string{"-provider", "openai", "-model", "unfamiliar-model", "-openai-profile", profile,
			"-openai-context", "16384", "-openai-context-bytes", "8192", "-openai-max-output", "2048"}
		if profile == provider.ResponsesReasoning {
			args = append(args, "-openai-reasoning-effort", "medium")
		}
		cfg, err := parseConfig(args, &bytes.Buffer{})
		if err != nil {
			t.Fatal(err)
		}
		exec, model, err := makeExecutor(cfg)
		if err != nil {
			t.Fatal(err)
		}
		o := exec.(*provider.OpenAI)
		if model != "openai/unfamiliar-model" || o.Config.ContextTokens != 16384 || o.Config.ContextBytes != 8192 || o.Config.MaxOutputTokens != 2048 || o.SupportsGenerationSettings() != (profile == provider.ChatJSON) {
			t.Fatalf("incorrect profile: %+v", o.Config)
		}
	}
	for _, flag := range []string{"-openai-context", "-openai-context-bytes", "-openai-max-output"} {
		for _, value := range []string{"0", "-1", "1048577"} {
			if _, err := parseConfig([]string{"-provider", "openai", flag, value}, &bytes.Buffer{}); err == nil {
				t.Fatalf("accepted %s %s", flag, value)
			}
		}
	}
}

func TestOpenAIOmittedOutputFlagUsesModelDefault(t *testing.T) {
	t.Setenv("OPENAI_API_KEY", "fixture-key")
	t.Setenv("OPENAI_API_KEY_FILE", "")
	cfg, err := parseConfig([]string{"-provider", "openai", "-model", "gpt-4o-mini"}, &bytes.Buffer{})
	if err != nil || cfg.openaiConfig.MaxOutputTokens != 0 {
		t.Fatalf("cfg=%+v err=%v", cfg.openaiConfig, err)
	}
	executor, _, err := makeExecutor(cfg)
	if err != nil || executor.(*provider.OpenAI).Config.MaxOutputTokens != 16384 {
		t.Fatalf("incorrect resolved output cap: err=%v", err)
	}
	cfg, err = parseConfig([]string{"-provider", "openai", "-model", "new-model", "-openai-profile", "chat-json", "-openai-max-output", "8192"}, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	executor, _, err = makeExecutor(cfg)
	if err != nil || executor.(*provider.OpenAI).Config.MaxOutputTokens != 8192 {
		t.Fatalf("explicit output cap lost: err=%v", err)
	}
}
