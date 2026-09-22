# Lean verification environment

This Lake project pins the Lean version used by the coordinator verifier. The
initial environment imports only Lean's core library. Dependencies such as
Mathlib can be added here when the accepted problem format requires them.

The verifier writes each candidate to a temporary file and invokes
`lake env lean` from this directory.
