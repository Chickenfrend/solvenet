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
	proof := flag.String("proof", "rfl", "scripted proof body")
	delay := flag.Duration("delay", 0, "scripted execution delay")
	once := flag.Bool("once", false, "claim at most one job, then exit")
	flag.Parse()
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	w := daemon.Worker{URL: *url, ID: *id, Model: "scripted", Client: &http.Client{Timeout: 10 * time.Second}, Executor: provider.Scripted{Proof: *proof, Delay: *delay}}
	for ctx.Err() == nil {
		worked, err := w.Once(ctx)
		if err != nil {
			log.Printf("worker: %v", err)
		}
		if worked && err == nil {
			log.Print("submitted result")
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
