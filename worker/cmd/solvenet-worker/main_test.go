package main

import (
	"bytes"
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
