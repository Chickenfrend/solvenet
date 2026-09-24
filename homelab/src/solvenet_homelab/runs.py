"""Human-readable coordinator run inspection, with a replaceable active summary."""

import json
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


def detail(run):
    e = text
    jobs = run['jobs']
    leases = run['assignments']
    attempts = run['attempts']
    fixture = ''
    if run.get('fixture_set_id') and run.get('fixture_version') is not None and run.get('fixture_problem_id'):
        fixture = (f'<p>Fixture: {e(run["fixture_set_id"])} v{e(run["fixture_version"])}'
                   f' · {e(run["fixture_problem_id"])}</p>')
        if (problems.ID.fullmatch(str(run['fixture_set_id']))
                and problems.ID.fullmatch(str(run['fixture_problem_id']))
                and type(run['fixture_version']) is int and run['fixture_version'] >= 0):
            path = problems.url(run['fixture_set_id'], run['fixture_version'], run['fixture_problem_id'])
            fixture += f'<p><a href="{e(path)}">View problem</a></p>'
    job_html = ''.join(
        f'<article id="job-{e(job["id"])}"><h3>Job {e(job["id"])} — {label(job.get("status"))}</h3>'
        f'<p>Model: {label(job.get("model"))} · Repair depth: {label(job.get("repair_depth"))}</p>'
        + (f'<p>Repair of candidate attempt <a href="#attempt-{e(job["parent_attempt_id"])}">'
           f'{e(job["parent_attempt_id"])}</a></p>' if job.get('parent_attempt_id') else
           '<p>Initial job</p>') + '</article>' for job in jobs if 'id' in job)
    lease_html = ''.join(
        f'<article><h3>Lease {e(lease["id"])} — {label(lease.get("status"))}</h3>'
        f'<p>Job <a href="#job-{e(lease.get("job_id"))}">{e(lease.get("job_id"))}</a>'
        f' · Worker: {label(lease.get("worker_id"))}</p>'
        + (f'<p>Failure: {e(lease["error"])}</p>' if lease.get('error') else '')
        + (f'<p>Failure class: {e(lease["failure_class"])}</p>' if lease.get('failure_class') else '')
        + (f'<p>Rejection: {e(lease["rejection_kind"])}</p>' if lease.get('rejection_kind') else '')
        + '<h4>Reported usage</h4>' + usage(lease.get('usage')) + '</article>'
        for lease in leases if 'id' in lease)
    attempt_html = ''.join(
        f'<article id="attempt-{e(attempt["id"])}"><h3>Candidate attempt {e(attempt["id"])}</h3>'
        f'<p>Job <a href="#job-{e(attempt.get("job_id"))}">{e(attempt.get("job_id"))}</a>'
        f' · Lease: {e(attempt.get("assignment_id"))} · Model: {label(attempt.get("model"))}</p>'
        + (f'<p>Repair of <a href="#attempt-{e(attempt["parent_attempt_id"])}">'
           f'candidate attempt {e(attempt["parent_attempt_id"])}</a></p>'
           if attempt.get('parent_attempt_id') else '<p>Initial candidate</p>')
        + f'<p>Lean verification: {label(attempt.get("verification_status") or "Pending")}</p>'
        + '<h4>Full candidate proof</h4>' + f'<pre>{e(attempt.get("candidate", ""))}</pre>'
        + '<h4>Lean diagnostics</h4>' + f'<pre>{e(attempt.get("diagnostics") or "No diagnostics reported")}</pre>'
        + '<h4>Reported usage</h4>' + usage(attempt.get('usage')) + '</article>'
        for attempt in attempts if 'id' in attempt)
    return problems.page('Run ' + str(run['id']),
                         f'<p><a href="/runs/{e(run["id"])}">Refresh run details</a></p>'
                         + fixture + summary(run) + '<h2>Jobs (generation requests)</h2>'
                         + (job_html or '<p>No jobs yet.</p>') + '<h2>Leased assignments (dispatches)</h2>'
                         + (lease_html or '<p>No leases yet.</p>')
                         + '<h2>Completed candidate attempts</h2>'
                         + (attempt_html or '<p>No candidate attempts yet.</p>'), htmx=True)
