package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"solvenet/worker/internal/daemon"
	"solvenet/worker/internal/provider"
)

func main() {
	url := flag.String("coordinator", "http://127.0.0.1:8080", "coordinator URL")
	id := flag.String("id", "local-scripted-worker", "stable worker identifier")
	providerName := flag.String("provider", "scripted", "executor: scripted or ollama")
	model := flag.String("model", "", "installed Ollama model, e.g. qwen2.5-coder:7b")
	ollamaURL := flag.String("ollama-url", "http://127.0.0.1:11434", "local Ollama server URL")
	proof := flag.String("proof", "rfl", "scripted proof body")
	delay := flag.Duration("delay", 0, "scripted execution delay")
	once := flag.Bool("once", false, "claim at most one job, then exit")
	flag.Parse()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	var executor daemon.Executor = provider.Scripted{Proof: *proof, Delay: *delay}
	requestedModel := "scripted"
	switch *providerName {
	case "scripted":
		if *model != "" {
			log.Fatal("-model requires -provider ollama")
		}
	case "ollama":
		ollama, err := provider.NewOllama(*ollamaURL, *model)
		if err != nil {
			log.Fatal(err)
		}
		executor, requestedModel = ollama, "ollama/"+*model
		if *id == "local-scripted-worker" {
			*id = "local-ollama-worker"
		}
	default:
		log.Fatal("-provider must be scripted or ollama")
	}
	w := daemon.Worker{URL: *url, ID: *id, Model: requestedModel, Client: &http.Client{Timeout: 10 * time.Second}, Executor: executor}
	log.Printf("worker %s offering %s", *id, requestedModel)
	for ctx.Err() == nil {
		worked, err := w.Once(ctx)
		if err != nil {
			log.Printf("worker: %v", err)
		}
		if worked && err == nil {
			log.Print("submitted result")
		}
		if !worked && err == nil && *once {
			log.Print("no compatible jobs available")
		}
		if *once {
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
