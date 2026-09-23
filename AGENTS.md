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
reasoning process, not yet a first-class scheduled object. A run currently
requests one model and may start multiple search chains, but these do not imply
distinct agents or worker daemons. One worker may eventually run multiple
agents. The [terminology glossary](docs/ticket-18-terminology.md) distinguishes
jobs, leased assignments, and completed candidate attempts.

For now, the coordinator will be in python, and the worker daemon will be in go.

## First Milestone

Build the smallest end-to-end system that can:

1. Accept a Lean theorem.
2. Ask for multiple candidate proofs (eventually from collaborating logical agents).
3. Run the candidates through Lean.
4. Report which candidates verify.

Local execution is sufficient for the first version. Distributed execution can come later.

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

The immediate goal is to determine whether collaborative proof-search strategies can outperform independent initial search chains under comparable compute budgets.

## Working Style

When asked to implement something:

1. Inspect the existing code before proposing a new architecture.
2. Make the smallest coherent change that satisfies the task.
3. Preserve existing interfaces unless there is a clear reason to change them.
4. Run relevant tests and formatters when available.
5. Do not invent requirements that are not present in the repository or task.
