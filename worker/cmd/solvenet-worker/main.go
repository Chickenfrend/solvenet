package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"solvenet/worker/internal/daemon"
	"solvenet/worker/internal/provider"
)

type config struct {
	coordinatorURL string
	id             string
	providerName   string
	model          string
	ollamaURL      string
	ollamaContext  int
	progressURL    string
	openaiURL      string
	openaiConfig   provider.OpenAIConfig
	proof          string
	delay          time.Duration
	once           bool
}

func parseConfig(args []string, output io.Writer) (config, error) {
	var cfg config
	flags := flag.NewFlagSet("solvenet-worker", flag.ContinueOnError)
	flags.SetOutput(output)
	flags.StringVar(&cfg.coordinatorURL, "coordinator", "http://127.0.0.1:8080", "coordinator URL")
	flags.StringVar(&cfg.id, "id", "local-scripted-worker", "stable worker identifier")
	flags.StringVar(&cfg.providerName, "provider", "scripted", "executor: scripted, ollama or openai")
	flags.StringVar(&cfg.model, "model", "", "provider model name (without provider prefix)")
	flags.StringVar(&cfg.openaiURL, "openai-url", "https://api.openai.com/v1", "worker-local OpenAI API base URL")
	flags.StringVar(&cfg.openaiConfig.Profile, "openai-profile", "", "API contract: chat-json or responses-reasoning (required for unfamiliar models)")
	flags.StringVar(&cfg.openaiConfig.ReasoningEffort, "openai-reasoning-effort", "", "Responses reasoning effort: low, medium or high; omitted uses provider default")
	flags.IntVar(&cfg.openaiConfig.ContextTokens, "openai-context", 32768, "model context capacity in tokens (conservative byte-token admission)")
	flags.IntVar(&cfg.openaiConfig.ContextBytes, "openai-context-bytes", 32768, "full provider input byte capacity including framing reserve")
	flags.IntVar(&cfg.openaiConfig.MaxOutputTokens, "openai-max-output", 0, "model output-token capacity subject to v1 limits (omitted uses conservative model default)")
	flags.StringVar(&cfg.ollamaURL, "ollama-url", "http://127.0.0.1:11434", "local Ollama server URL")
	flags.IntVar(&cfg.ollamaContext, "ollama-context", provider.DefaultOllamaContext, fmt.Sprintf("Ollama context size in tokens (1-%d)", provider.MaxOllamaContext))
	flags.StringVar(&cfg.progressURL, "progress-url", "", "private homelab site origin for Ollama progress (requires SOLVENET_PROGRESS_TOKEN)")
	flags.StringVar(&cfg.proof, "proof", "rfl", "scripted proof body")
	flags.DurationVar(&cfg.delay, "delay", 0, "scripted execution delay")
	flags.BoolVar(&cfg.once, "once", false, "claim at most one job, then exit")
	if err := flags.Parse(args); err != nil {
		return cfg, err
	}
	if flags.NArg() != 0 {
		return cfg, fmt.Errorf("unexpected positional arguments: %v", flags.Args())
	}
	if cfg.ollamaContext <= 0 || cfg.ollamaContext > provider.MaxOllamaContext {
		return cfg, fmt.Errorf("ollama-context must be between 1 and %d tokens", provider.MaxOllamaContext)
	}
	outputSpecified := false
	flags.Visit(func(f *flag.Flag) {
		if f.Name == "openai-max-output" {
			outputSpecified = true
		}
	})
	if cfg.providerName == "openai" && (cfg.openaiConfig.ContextTokens < 1 || cfg.openaiConfig.ContextTokens > 1024*1024 || cfg.openaiConfig.ContextBytes < 1 || cfg.openaiConfig.ContextBytes > 1024*1024 || cfg.openaiConfig.MaxOutputTokens < 0 || (outputSpecified && cfg.openaiConfig.MaxOutputTokens == 0) || cfg.openaiConfig.MaxOutputTokens > daemon.MaxOutputTokens) {
		return cfg, fmt.Errorf("OpenAI context capacities must be 1-1048576 and output capacity 1-32768")
	}
	return cfg, nil
}

func makeExecutor(cfg config) (daemon.Executor, string, error) {
	var executor daemon.Executor = provider.Scripted{Proof: cfg.proof, Delay: cfg.delay}
	requestedModel := "scripted"
	switch cfg.providerName {
	case "scripted":
		if cfg.model != "" {
			return nil, "", fmt.Errorf("-model requires -provider ollama or openai")
		}
	case "ollama":
		ollama, err := provider.NewOllama(cfg.ollamaURL, cfg.model, cfg.ollamaContext)
		if err != nil {
			return nil, "", err
		}
		executor, requestedModel = ollama, "ollama/"+cfg.model
	case "openai":
		key := os.Getenv("OPENAI_API_KEY")
		if path := os.Getenv("OPENAI_API_KEY_FILE"); path != "" {
			if key != "" {
				return nil, "", fmt.Errorf("set only one of OPENAI_API_KEY and OPENAI_API_KEY_FILE")
			}
			data, err := os.ReadFile(path)
			if err != nil {
				return nil, "", fmt.Errorf("OpenAI credential file unavailable")
			}
			key = strings.TrimSpace(string(data))
		}
		openai, err := provider.NewOpenAIWithConfig(cfg.openaiURL, cfg.model, key, cfg.openaiConfig)
		if err != nil {
			return nil, "", err
		}
		executor, requestedModel = openai, "openai/"+cfg.model
	default:
		return nil, "", fmt.Errorf("-provider must be scripted, ollama or openai")
	}
	return executor, requestedModel, nil
}

func main() {
	cfg, err := parseConfig(os.Args[1:], os.Stderr)
	if err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return
		}
		log.Fatal(err)
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	executor, requestedModel, err := makeExecutor(cfg)
	if err != nil {
		log.Fatal(err)
	}
	if cfg.id == "local-scripted-worker" && cfg.providerName != "scripted" {
		cfg.id = "local-" + cfg.providerName + "-worker"
	}
	w := daemon.Worker{URL: cfg.coordinatorURL, ID: cfg.id, Model: requestedModel, Client: &http.Client{Timeout: 10 * time.Second}, Executor: executor,
		SupportsGenerationSettings: cfg.providerName == "ollama" || cfg.providerName == "openai"}
	w.SupportsModelRespond = cfg.providerName == "ollama" || cfg.providerName == "openai"
	if openai, ok := executor.(*provider.OpenAI); ok {
		w.SupportsGenerationSettings = openai.SupportsGenerationSettings()
	}
	if cfg.progressURL != "" && cfg.providerName == "ollama" {
		token := os.Getenv("SOLVENET_PROGRESS_TOKEN")
		u, err := url.Parse(cfg.progressURL)
		if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.Fragment != "" || len(token) < 32 {
			log.Fatal("progress-url must be an HTTP(S) origin and SOLVENET_PROGRESS_TOKEN must be at least 32 characters")
		}
		progressClient := &http.Client{Timeout: 300 * time.Millisecond, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
		w.Progress = func(ctx context.Context, runID, jobID, assignmentID, raw string) {
			// The site stores only a preview. A failed progress post never affects a proof.
			if len(raw) > 8192 {
				raw = raw[:8192]
				raw = strings.ToValidUTF8(raw, "")
			}
			body, _ := json.Marshal(map[string]string{"run_id": runID, "job_id": jobID, "assignment_id": assignmentID, "output": raw})
			for len(body) > 9000 && len(raw) > 0 {
				raw = strings.ToValidUTF8(raw[:len(raw)*3/4], "")
				body, _ = json.Marshal(map[string]string{"run_id": runID, "job_id": jobID, "assignment_id": assignmentID, "output": raw})
			}
			req, err := http.NewRequestWithContext(ctx, "POST", strings.TrimRight(cfg.progressURL, "/")+"/internal/progress", bytes.NewReader(body))
			if err != nil {
				return
			}
			req.Header.Set("Authorization", "Bearer "+token)
			req.Header.Set("Content-Type", "application/json")
			resp, err := progressClient.Do(req)
			if err == nil {
				resp.Body.Close()
			}
		}
	}
	log.Printf("worker %s offering %s", cfg.id, requestedModel)
	for ctx.Err() == nil {
		worked, err := w.Once(ctx)
		if err != nil {
			log.Printf("worker: %v", err)
		}
		if worked && err == nil {
			log.Print("submitted result")
		}
		if openai, ok := executor.(*provider.OpenAI); ok && openai.AuthFailed() {
			log.Print("OpenAI credential rejected; stopping worker until credentials are corrected")
			return
		}
		if !worked && err == nil && cfg.once {
			log.Print("no compatible jobs available")
		}
		if cfg.once {
			if err != nil {
				os.Exit(1)
			}
			return
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(time.Second):
		}
	}
}
