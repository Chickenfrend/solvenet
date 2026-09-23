package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/signal"
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
	flags.StringVar(&cfg.providerName, "provider", "scripted", "executor: scripted or ollama")
	flags.StringVar(&cfg.model, "model", "", "installed Ollama model, e.g. qwen2.5-coder:7b")
	flags.StringVar(&cfg.ollamaURL, "ollama-url", "http://127.0.0.1:11434", "local Ollama server URL")
	flags.IntVar(&cfg.ollamaContext, "ollama-context", provider.DefaultOllamaContext, fmt.Sprintf("Ollama context size in tokens (1-%d)", provider.MaxOllamaContext))
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
	return cfg, nil
}

func makeExecutor(cfg config) (daemon.Executor, string, error) {
	var executor daemon.Executor = provider.Scripted{Proof: cfg.proof, Delay: cfg.delay}
	requestedModel := "scripted"
	switch cfg.providerName {
	case "scripted":
		if cfg.model != "" {
			return nil, "", fmt.Errorf("-model requires -provider ollama")
		}
	case "ollama":
		ollama, err := provider.NewOllama(cfg.ollamaURL, cfg.model, cfg.ollamaContext)
		if err != nil {
			return nil, "", err
		}
		executor, requestedModel = ollama, "ollama/"+cfg.model
	default:
		return nil, "", fmt.Errorf("-provider must be scripted or ollama")
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
	if cfg.providerName == "ollama" && cfg.id == "local-scripted-worker" {
		cfg.id = "local-ollama-worker"
	}
	w := daemon.Worker{URL: cfg.coordinatorURL, ID: cfg.id, Model: requestedModel, Client: &http.Client{Timeout: 10 * time.Second}, Executor: executor,
		SupportsGenerationSettings: cfg.providerName == "ollama"}
	log.Printf("worker %s offering %s", cfg.id, requestedModel)
	for ctx.Err() == nil {
		worked, err := w.Once(ctx)
		if err != nil {
			log.Printf("worker: %v", err)
		}
		if worked && err == nil {
			log.Print("submitted result")
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
