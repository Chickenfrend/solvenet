# Protocol

The local worker API is specified in [v1.md](v1.md), including its wire-limit
table and the distinction between protocol bounds and deployment policy.

Limits are maintained manually in
[`coordinator/src/solvenet/protocol_limits.py`](../coordinator/src/solvenet/protocol_limits.py)
and [`worker/internal/daemon/limits.go`](../worker/internal/daemon/limits.go).
Boundary tests in both languages check the shared values.
