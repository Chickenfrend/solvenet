"""Ephemeral, bounded snapshots from the authenticated local Ollama worker."""

import threading
import time
from html import escape

MAX_ENTRIES = 32
MAX_OUTPUT = 8192
TTL = 60
STALE = 12


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = {}

    def put(self, run_id, job_id, assignment_id, output):
        now = time.monotonic()
        key = (run_id, job_id, assignment_id)
        with self.lock:
            self.entries = {k: v for k, v in self.entries.items() if now - v[0] < TTL}
            previous = self.entries.get(key)
            if previous and now - previous[0] < .2:
                return False
            if key not in self.entries and len(self.entries) >= MAX_ENTRIES:
                del self.entries[min(self.entries, key=lambda k: self.entries[k][0])]
            self.entries[key] = (now, previous[1] if previous else now, output)
        return True

    def snapshot(self, run_id):
        now = time.monotonic()
        with self.lock:
            self.entries = {k: v for k, v in self.entries.items() if now - v[0] < TTL}
            matches = [(k, v) for k, v in self.entries.items() if k[0] == run_id]
        return max(matches, key=lambda item: item[1][0]) if matches else None


def fragment(run_id, status, snapshot, active_assignment=None):
    active = status == 'running'
    poll = (f' hx-get="/runs/{escape(run_id)}/progress" hx-trigger="every 2s" hx-swap="outerHTML"'
            if active else '')
    if not active:
        body = '<p>Run finished. Refresh for the final proof, Lean verdict and usage.</p>'
    elif snapshot is None or (active_assignment not in (None, False) and
                             (snapshot[0][1], snapshot[0][2]) !=
                             (active_assignment['job_id'], active_assignment['id'])):
        body = '<p>Waiting for model output. The worker may still be loading the model.</p>'
    elif active_assignment is False:
        body = '<p>Waiting for the next assignment. Previous model output is no longer active.</p>'
    else:
        (_, job_id, assignment_id), (updated, started, output) = snapshot
        elapsed = max(0, int(time.monotonic() - started))
        if time.monotonic() - updated >= STALE:
            body = '<p>Progress expired or stalled. Refresh for the latest persisted run state.</p>'
        else:
            body = (f'<p>Generating · {elapsed}s elapsed · job {escape(job_id)} · assignment {escape(assignment_id)}</p>'
                    + (f'<p>Partial, unverified model output (preview, at most {MAX_OUTPUT} bytes):</p><pre>{escape(output)}</pre>'
                       if output else '<p>Waiting for model output.</p>'))
    return (f'<section id="run-progress" aria-live="polite"{poll}><h2>Live model output</h2>{body}'
            f'<p><a href="/runs/{escape(run_id)}">Refresh run details</a></p></section>')
