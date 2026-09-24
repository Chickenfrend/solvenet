"""Human-readable coordinator run inspection, with a replaceable active summary."""

import json
from collections import Counter
from html import escape

from . import problems


def text(value):
    return escape(str(value), quote=True)


def label(value):
    return text(value if value is not None else 'Unknown')


def usage(value):
    value = value if isinstance(value, dict) else {}
    fields = ['Input tokens', 'Output tokens']
    keys = ['input_tokens', 'output_tokens']
    fields.extend(text(key) for key in value if key not in keys)
    keys.extend(key for key in value if key not in keys)
    return '<dl class="usage">' + ''.join(
        f'<div><dt>{field}</dt><dd>{label(value.get(key)) if not isinstance(value.get(key), (dict, list)) else text(json.dumps(value[key], ensure_ascii=False))}</dd></div>'
        for field, key in zip(fields, keys)) + '</dl>'


def summary(run):
    active = run['status'] == 'running'
    jobs = run['jobs'] if type(run['jobs']) is int else len(run['jobs'])
    leases = run['assignments'] if type(run['assignments']) is int else len(run['assignments'])
    attempts = run['attempts'] if type(run['attempts']) is int else len(run['attempts'])
    poll = (' hx-get="/runs/' + text(run['id']) + '/status" hx-trigger="every 5s" '
            'hx-swap="outerHTML"') if active else ''
    return (f'<section id="run-status" aria-live="polite"{poll}>'
            f'<h2>Run status: {text(run["status"])}</h2>'
            f'<p>{jobs} jobs · {leases} leased assignments · '
            f'{attempts} completed candidate attempts</p>'
            + ('<p>Work is active. Status updates automatically when available; '
               'refresh the page to see new proofs and diagnostics.</p>' if active else
               '<p>Run finished. Refresh for the latest details.</p>') + '</section>')


def technical(title, record):
    """Preserve every coordinator field, including raw generation output."""
    return (f'<details class="run-technical"><summary>{text(title)}</summary>'
            f'<pre>{text(json.dumps(record, ensure_ascii=False, indent=2, default=str))}</pre></details>')


def outcome(run):
    attempts = run['attempts']
    verified = [a for a in attempts if a.get('verification_status') == 'verified']
    rejected = [a for a in attempts if a.get('verification_status') == 'rejected']
    verifier_errors = [a for a in attempts if a.get('verification_status') == 'verifier_error']
    if verified:
        winner = verified[0]
        repair = bool(winner.get('parent_attempt_id'))
        return ('<h2>Outcome: ' + ('Repaired proof verified' if repair else 'Initial proof verified')
                + '</h2><p>Lean verified this candidate' + (' after a repair.' if repair else '.')
                + f' <a href="#attempt-{text(winner["id"])}">View verified proof</a></p>')
    if verifier_errors:
        return (f'<h2>Outcome: Lean verification error</h2><p>{len(verifier_errors)} generated '
                'candidate(s) could not be checked. See the diagnostic in the activity below.</p>')
    if rejected:
        return (f'<h2>Outcome: {len(rejected)} candidate(s) rejected by Lean</h2>'
                '<p>Proofs were generated, but Lean did not verify them. See each verdict and its full diagnostics below.</p>')
    if run['status'] == 'running':
        return ('<h2>Outcome: Verification pending</h2><p>A generated candidate is awaiting Lean.</p>'
                if attempts else '<h2>Outcome: Awaiting candidates</h2><p>Waiting for model output.</p>')
    leases = run['assignments']
    failures = [lease for lease in leases if lease.get('status') == 'failed']
    if failures:
        reasons = Counter(str(lease['error']).strip() for lease in failures if lease.get('error'))
        reason, count = reasons.most_common(1)[0] if reasons else ('Unknown provider failure', 0)
        # Only describe the provider as unreachable when the reported error supports it.
        unreachable = any(word in reason.lower() for word in
                          ('unreachable', 'connection refused', 'connect:', 'connection error',
                           'dial tcp', 'offline', 'service unavailable'))
        cause = 'provider unreachable' if unreachable else 'provider/worker failure'
        sample = text(reason[:300] + ('…' if len(reason) > 300 else ''))
        return (f'<h2>Outcome: zero candidates; {cause}</h2>'
                f'<p>{len(failures)} failed leased assignment(s); no proof reached Lean. '
                f'{"Most common reported failure" if count > 1 else "Reported failure"}: {sample}</p>')
    if leases and all(lease.get('status') == 'rejected' for lease in leases):
        return '<h2>Outcome: zero candidates; assignments rejected</h2><p>No proof reached Lean.</p>'
    return '<h2>Outcome: zero candidates</h2><p>No proof reached Lean. See activity for details.</p>'


def detail(run, problem_title=None, progress_html='', model_names=None):
    e = text
    jobs = run['jobs']
    leases = run['assignments']
    attempts = run['attempts']
    model_ids = list(dict.fromkeys(job['model'] for job in jobs if job.get('model')))
    model_names = model_names or {}
    models = ', '.join(f'{e(model_names.get(m, m))} ({e(m)})' if model_names.get(m) and model_names[m] != m
                       else e(m) for m in model_ids) or 'Unknown'
    title = problem_title or (run.get('problem') or {}).get('title') or 'Run details'
    fixture = ''
    if run.get('fixture_set_id') and run.get('fixture_version') is not None and run.get('fixture_problem_id'):
        fixture = (f'<p>Fixture: {e(run["fixture_set_id"])} v{e(run["fixture_version"])}'
                   f' · {e(run["fixture_problem_id"])}</p>')
        if (problems.ID.fullmatch(str(run['fixture_set_id']))
                and problems.ID.fullmatch(str(run['fixture_problem_id']))
                and type(run['fixture_version']) is int and run['fixture_version'] >= 0):
            path = problems.url(run['fixture_set_id'], run['fixture_version'], run['fixture_problem_id'])
            fixture += f'<p><a href="{e(path)}">Back to problem</a></p>'
    events = []
    for index, job in enumerate(jobs, 1):
        if 'id' not in job:
            continue
        jid = job['id']
        parent = job.get('parent_attempt_id')
        ancestry = (f' · Repair of <a href="#attempt-{e(parent)}">candidate {e(parent)}</a>'
                    if parent else ' · Initial search')
        events.append(f'<li class="run-card" id="job-{e(jid)}"><h3>Job {index}: queued → {label(job.get("status"))}</h3>'
                      + f'<p>Model: {label(job.get("model"))}{ancestry}</p>' + technical('Job details', job) + '</li>')
        for lease in (l for l in leases if l.get('job_id') == jid):
            state = lease.get('status')
            events.append(f'<li class="run-card"><h3>Leased work → {label(state)}</h3>'
                          + (f'<p>Failure: {e(lease["error"])}</p>' if lease.get('error') else '')
                          + (f'<p>Failure class: {e(lease["failure_class"])}</p>' if lease.get('failure_class') else '')
                          + (f'<p>Rejection: {e(lease["rejection_kind"])}</p>' if lease.get('rejection_kind') else '')
                          + technical('Lease details, raw output and usage', lease) + '</li>')
            for attempt in (a for a in attempts if a.get('assignment_id') == lease.get('id')):
                aid = attempt.get('id')
                verdict = attempt.get('verification_status') or 'Pending'
                repair = attempt.get('parent_attempt_id')
                events.append(f'<li class="run-card" id="attempt-{e(aid)}">'
                              f'<h3>Generated candidate → Lean verification: {e(verdict)}</h3>'
                              + (f'<p>Repair of <a href="#attempt-{e(repair)}">candidate {e(repair)}</a></p>'
                                 if repair else '<p>Initial candidate</p>')
                              + ('<p>Lean rejected this candidate; expand for the full diagnostic.</p>'
                                 if verdict == 'rejected' else '')
                              + '<details class="run-technical"><summary>Full candidate proof, Lean diagnostics and usage</summary>'
                              + f'<h4>Full candidate proof</h4><pre>{e(attempt.get("candidate") or "")}</pre>'
                              + f'<h4>Lean diagnostics</h4><pre>{e(attempt.get("diagnostics") or "No diagnostics reported")}</pre>'
                              + '<h4>Reported usage</h4>' + usage(attempt.get('usage'))
                              + technical('Additional candidate metadata and raw output',
                                          {k: v for k, v in attempt.items()
                                           if k not in ('candidate', 'diagnostics', 'usage')}) + '</details></li>')
    return problems.page(title, _page(run, models, progress_html, fixture, events), htmx=True)


def _page(run, models, progress_html, fixture, events):
    return (f'<p class="lede">Selected model: {models}</p>' + summary(run) + progress_html
            + '<section class="run-outcome">' + outcome(run) + '</section>' + fixture
            + f'<p><a href="/runs/{text(run["id"])}">Refresh run details</a></p>'
            + '<h2>Activity</h2><ol class="run-activity">'
            + (''.join(events) or '<li>No jobs yet; no candidate attempts yet.</li>') + '</ol>'
            + technical('Run configuration and identifiers', {k: v for k, v in run.items()
                                                         if k not in ('jobs', 'assignments', 'attempts')}))
