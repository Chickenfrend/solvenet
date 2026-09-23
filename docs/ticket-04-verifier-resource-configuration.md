# Ticket 4: verifier resource configuration

## Problem

Lean and Docker deadlines and Docker resource limits were scattered literals.
The container entry point also used its own 10-second Lean default, so changing
the outer verifier could not reliably change the inner deadline.

## Design

`LeanVerifierConfig` owns the Lean wall-clock and diagnostic limits.
`ContainerVerifierConfig` owns the outer deadline and its required overhead;
construction rejects an outer deadline shorter than the Lean deadline plus one
second. `DockerResourceLimits` names CPU, memory, swap, process, file-size, and
temporary-storage values. This is a small set of explicit data classes, not a
general configuration framework.

The host writes the Lean deadline and diagnostic limit into `request.json`. The
container entry point validates them through `LeanVerifierConfig` and passes the
same object to `LeanVerifier`. Result transport is sized from that diagnostic
limit. Defaults remain: 10 seconds for Lean, 30 seconds for Docker, 64 KiB of
diagnostics, 1 CPU, 1 GiB memory and swap, 64 processes, a 1 MiB file limit, and
a 128 MiB `/tmp`.

## CLI and API impact

The coordinator adds `--lean-timeout` and `--container-timeout`. The latter is
used only by Docker. Invalid, non-finite, non-positive, or incoherent deadlines
fail at startup. Code embedding the verifier can supply the three explicit
configuration data classes; existing default constructor calls still work.

## Files changed

- `coordinator/src/solvenet/verifier.py`: Lean configuration and validation.
- `coordinator/src/solvenet/sandbox.py`: container/resource configuration,
  deadline validation, and request propagation.
- `coordinator/src/solvenet/server.py`: useful timeout CLI options.
- `coordinator/tests/test_verifier.py`, `coordinator/tests/test_sandbox.py`:
  focused validation and Docker invocation tests.
- `README.md`, `coordinator/README.md`: effective limits and CLI documentation.

## Tests and results

Run with:

```sh
PYTHONPATH=coordinator/src python3 -m unittest discover -s coordinator/tests -v
```

Result: 53 tests passed and 14 optional Lean/Docker tests were skipped because
their external tools or opt-in environment variable were unavailable.

## Remaining concerns

Docker size strings are passed to Docker for final interpretation. The one-second
overhead allowance prevents an impossible outer/inner ordering but does not
guarantee enough startup time on an overloaded host; operators can choose a
larger outer deadline. The local verifier remains a process limiter, not a
security sandbox.
