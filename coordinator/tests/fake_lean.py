"""Small Lean stand-in used to test process handling, not proof correctness."""

import pathlib
import os
import signal
import re
import json
import sys
import time


source = pathlib.Path(sys.argv[-1]).read_text(encoding="utf-8")
if pathlib.Path(sys.argv[-1]).name == "Preflight.lean":
    raise SystemExit(0)
if "FAKE_FLOOD" in source:
    while True:
        os.write(1, b"x" * 8192)
if "FAKE_CRASH" in source:
    os.kill(os.getpid(), signal.SIGKILL)
if "FAKE_TIMEOUT" in source:
    time.sleep(5)
if "FAKE_INVALID" in source:
    print("Candidate.lean: error: invalid proof")
    raise SystemExit(1)
print("'SolveNetVerification.candidate' does not depend on any axioms")
receipt = re.search(r'IO.FS.writeFile ("[^"\n]+")', source)
if receipt:
    pathlib.Path(json.loads(receipt.group(1))).write_text("accepted")
