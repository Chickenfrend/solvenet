package provider

import (
	"context"
	"strings"
	"testing"

	"solvenet/worker/internal/daemon"
)

func TestScriptedRejectsUnsupportedSettings(t *testing.T) {
	seed := int64(42)
	_, err := (Scripted{Proof: "trivial"}).Execute(context.Background(), daemon.Job{
		GenerationSettings: daemon.GenerationSettings{Seed: &seed},
	})
	if err == nil || daemon.FailureClassOf(err) != daemon.FailurePermanent || !strings.Contains(err.Error(), "generation_settings") {
		t.Fatalf("expected explicit permanent unsupported-settings error, got %v", err)
	}
	result, err := (Scripted{Proof: "trivial"}).Execute(context.Background(), daemon.Job{})
	if err != nil || result.Text != "trivial" {
		t.Fatalf("default scripted execution changed: %+v %v", result, err)
	}
}
