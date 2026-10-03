# SolveNet

SolveNet is an experimental distributed multi-agent problem-solving system.

The initial goal is to use multiple LLM agents to collaboratively solve Lean-formalized mathematics problems. Longer term, SolveNet may allow people to contribute local or API-backed LLM compute over a distributed network.

This project is currently at an early prototype stage. Prefer simple, working implementations over infrastructure designed for hypothetical future requirements.

## Initial Architecture

The project will likely have two major components:

* **Coordinator** — manages problems, jobs, candidate results, and global search state.
* **Worker daemon** — runs model-backed jobs on local or remote machines and reports results to the coordinator.

Keep these concepts separate, but do not over-engineer their interface before it is needed.

A worker daemon is a machine/process-level component; an agent is a logical
reasoning process. The local group loop now persists collaborating agents with
bounded state, tasks and communication, while independent/repair v1 runs can
still request initial jobs for several models. Chains are not agents or worker
daemons. A worker can serve jobs for multiple agents. The
[terminology glossary](docs/ticket-18-terminology.md)
distinguishes jobs, leased assignments, and completed candidate attempts.

For now, the coordinator will be in python, and the worker daemon will be in go.

## First Milestone (delivered locally)

Build the smallest end-to-end system that can:

1. Accept a Lean theorem.
2. Ask for multiple candidate proofs (eventually from collaborating logical agents).
3. Run the candidates through Lean.
4. Report which candidates verify.

Local execution is sufficient for the first version. Distributed execution can come later.

The local collaborating agent group and G1–G6 claim-graph path are implemented;
independent proof candidates, repair chains and the default fixed group mode
remain supported. The G6 scripted-model demonstration checks actual information
handoff and composed lemma use with real Lean. A capped local qwen2.5-coder:7b
observation completed but produced no structured graph proposals and a rejected
target proof; real-model collaboration effectiveness and efficiency remain
unmeasured. See [the G6 record](docs/graph-demonstration.md). Public untrusted
contributors still require the separate trusted-checker gate: malicious tactic
IO can forge candidate-writable in-process completion/use receipts.

## Engineering Guidelines

* Keep implementations small and easy to replace.
* Avoid premature abstractions.
* Do not build infrastructure solely for anticipated future scale.
* Prefer explicit data structures and interfaces.
* Treat all LLM output as untrusted.
* Lean verification is the source of truth for formal proofs.
* Keep model-provider-specific code isolated.
* Never expose user API keys to remote services unnecessarily.
* Add tests for behavior that is sufficiently stable to test.
* When making architectural changes, favor designs that allow local and distributed workers to eventually share the same job protocol.

## Scope

Do not implement speculative features unless they are required by the current task.

In particular, do not prematurely build:

* peer-to-peer networking
* reputation or credit systems
* payment systems
* complex distributed consensus
* generalized support for arbitrary problem domains
* sophisticated scheduling algorithms
* large plugin systems

The local group loop divides work on a multi-step Lean problem, routes bounded
tasks by configured capability and availability, exchanges findings, critiques
results and synthesizes a Lean-checked proof. Continue measuring compute and
verification cost and test actual model behavior before drawing efficiency
conclusions. See [collaboration direction](docs/collaboration-direction.md).

## Working Style

When asked to implement something:

1. Inspect the existing code before proposing a new architecture.
2. Make the smallest coherent change that satisfies the task.
3. Preserve existing interfaces unless there is a clear reason to change them.
4. Run relevant tests and formatters when available.
5. Do not invent requirements that are not present in the repository or task.
