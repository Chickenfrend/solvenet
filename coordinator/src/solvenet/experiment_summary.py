"""Snapshot summaries of experiment assignments and Lean results; no proof text."""

import json


def measurement(unit):
    return {'known_total': None, 'known_count': 0, 'unknown_count': 0, 'unit': unit}


def add(metric, value):
    if value is None:
        metric['unknown_count'] += 1
    else:
        metric['known_count'] += 1
        metric['known_total'] = (metric['known_total'] or 0) + value


def outcomes():
    return dict(completed=0, provider_failure=0, formatting_failure=0,
                other_failure=0, expired=0, rejected=0, active=0)


def summary(db, experiment):
    """Read one SQLite snapshot. Failed results have no attempt row but may have usage."""
    result = {key: experiment[key] for key in ('id', 'created_at', 'config')}
    result['problem_count'] = len(experiment['runs'])
    result['problems_solved'] = 0
    result['solve_rate'] = 0.0
    result['run_statuses'] = {}
    result['requests'] = outcomes()
    result['usage'] = {key: measurement('tokens') for key in ('input_tokens', 'output_tokens')}
    result['provider_generation_time'] = measurement('ns')
    result['lean_verification_time'] = measurement('ms')
    result['time_to_first_verified_proof'] = measurement('seconds')
    result['successes'] = {'initial': 0, 'repair': 0}
    result['per_depth'] = {}
    result['runs'] = []
    rows = db.execute("""SELECT r.id AS run_id, r.fixture_problem_id, r.status AS run_status,
        r.created_at, j.repair_depth, j.id AS job_id, j.parent_attempt_id,
        a.id AS assignment_id, a.status AS assignment_status, a.result,
        t.id AS attempt_id, t.usage AS attempt_usage, t.generation AS attempt_generation,
        v.status AS verification_status, v.elapsed_ms, v.verified_at
        FROM runs r JOIN jobs j ON j.run_id=r.id
        LEFT JOIN assignments a ON a.job_id=j.id
        LEFT JOIN attempts t ON t.assignment_id=a.id
        LEFT JOIN verifications v ON v.attempt_id=t.id
        WHERE r.experiment_id=? ORDER BY r.rowid, j.rowid, a.rowid""",
        (experiment['id'],)).fetchall()
    by_run = {}
    for run in experiment['runs']:
        entry = {'problem_id': run['problem_id'], 'run_id': run['run_id'],
                 'status': run['status'], 'first_verified_attempt_id': None,
                 'time_to_first_verified_proof_seconds': None, 'attempts': [],
                 'assignments': []}
        result['runs'].append(entry)
        by_run[run['run_id']] = entry
        result['run_statuses'][run['status']] = result['run_statuses'].get(run['status'], 0) + 1
        if run['status'] == 'solved':
            result['problems_solved'] += 1
    result['solve_rate'] = result['problems_solved'] / result['problem_count'] if result['problem_count'] else 0.0
    verified = {run['run_id']: [] for run in experiment['runs']}
    seen_jobs = set()
    for row in rows:
        depth = row['repair_depth']
        key = str(depth)
        if key not in result['per_depth']:
            result['per_depth'][key] = {'jobs': 0, 'requests': outcomes(),
                                        'verifications': {}, 'verified_attempts': 0}
        bucket = result['per_depth'][key]
        # A job without assignments occurs exactly once in the left join.
        if row['job_id'] not in seen_jobs:
            bucket['jobs'] += 1
            seen_jobs.add(row['job_id'])
        if row['assignment_id'] is None:
            continue
        payload = json.loads(row['result']) if row['result'] else {}
        status = row['assignment_status']
        if status == 'completed' and payload.get('status') == 'failed':
            error = payload.get('error', '')
            if payload.get('failure_category'):
                kind = payload['failure_category']
            elif error.startswith('Ollama proof format:'):
                kind = 'formatting_failure'
            elif error.startswith(('Ollama ', 'reading Ollama ', 'invalid Ollama ')):
                kind = 'provider_failure'
            else:
                kind = 'other_failure'
        else:
            kind = status
        result['requests'][kind] += 1
        bucket['requests'][kind] += 1
        by_run[row['run_id']]['assignments'].append(
            {'assignment_id': row['assignment_id'], 'job_id': row['job_id'],
             'depth': depth, 'outcome': kind, 'attempt_id': row['attempt_id']})
        # Completed generations are the only assignments with a candidate attempt.
        # Failed results may still have partial usage/timing; active/expired/rejected
        # assignments have unknown usage, not an inferred zero.
        usage = json.loads(row['attempt_usage']) if row['attempt_id'] else payload.get('usage') or {}
        generation = json.loads(row['attempt_generation']) if row['attempt_id'] else payload.get('generation') or {}
        for field in result['usage']:
            add(result['usage'][field], usage.get(field))
        add(result['provider_generation_time'], generation.get('total_duration_ns'))
        if row['attempt_id']:
            verification = row['verification_status']
            by_run[row['run_id']]['attempts'].append(
                {'attempt_id': row['attempt_id'], 'assignment_id': row['assignment_id'],
                 'depth': depth, 'parent_attempt_id': row['parent_attempt_id'],
                 'verification_status': verification})
            add(result['lean_verification_time'], row['elapsed_ms'])
            label = verification or 'pending'
            bucket['verifications'][label] = bucket['verifications'].get(label, 0) + 1
            if verification == 'verified':
                bucket['verified_attempts'] += 1
                verified[row['run_id']].append((depth, row['attempt_id'], row['created_at'], row['verified_at']))
    for run_id, proofs in verified.items():
        if not proofs:
            continue
        entry = by_run[run_id]
        result['successes']['initial' if any(p[0] == 0 for p in proofs) else 'repair'] += 1
        # Older migrated rows lack one or both timestamps. Never use migration time.
        timed = [p for p in proofs if p[2] is not None and p[3] is not None]
        if timed and len(timed) == len(proofs):
            first = min(timed, key=lambda p: p[3])
            entry['first_verified_attempt_id'] = first[1]
            entry['time_to_first_verified_proof_seconds'] = max(0, first[3] - first[2])
            add(result['time_to_first_verified_proof'], entry['time_to_first_verified_proof_seconds'])
        else:
            entry['first_verified_attempt_id'] = proofs[0][1]
            add(result['time_to_first_verified_proof'], None)
    result['limitations'] = (
        'Requests count leases (including retries), not model generations or equal token budgets. '
        'Token and provider duration totals cover known reported values only; unknown_count includes '
        'unreported, rejected, expired and active leases. Provider duration is provider-reported '
        'total_duration_ns, not end-to-end wall time. Failure categories come from workers when '
        'available; historical results fall back to Ollama error prefixes. '
        'Time to first proof is measured from run creation to persisted Lean verification, '
        'including queue and retry time; historical rows without timestamps are unavailable. '
        'Running experiments have provisional solve rates.')
    return result


def markdown(report):
    """Readable, side-by-side friendly export; all IDs point to run inspection."""
    def metric(value):
        total = value['known_total'] if value['known_total'] is not None else 'unavailable'
        return f"{total} {value['unit']} ({value['known_count']} known, {value['unknown_count']} unknown)"

    lines = [f"# Experiment {report['id']}", '', '```json',
             json.dumps(report['config'], indent=2, ensure_ascii=False), '```', '',
             f"Created (Unix seconds): {report['created_at']}",
             f"Solved: {report['problems_solved']}/{report['problem_count']} ({report['solve_rate']:.1%})",
             f"Run statuses: {json.dumps(report['run_statuses'], sort_keys=True)}",
             f"Requests by outcome: {json.dumps(report['requests'], sort_keys=True)}",
             f"Input usage: {metric(report['usage']['input_tokens'])}",
             f"Output usage: {metric(report['usage']['output_tokens'])}",
             f"Provider generation time: {metric(report['provider_generation_time'])}",
             f"Lean verification time: {metric(report['lean_verification_time'])}",
             f"Time to first verified proof: {metric(report['time_to_first_verified_proof'])}",
             f"Initial/repair successes: {report['successes']['initial']}/{report['successes']['repair']}",
             '', '## Depth outcomes', '', '| Depth | Jobs | Requests | Verifications | Verified attempts |',
             '| --- | ---: | --- | --- | ---: |']
    for depth, value in sorted(report['per_depth'].items(), key=lambda pair: int(pair[0])):
        lines.append(f"| {depth} | {value['jobs']} | {json.dumps(value['requests'], sort_keys=True)} | "
                     f"{json.dumps(value['verifications'], sort_keys=True)} | {value['verified_attempts']} |")
    lines.extend(['', '## Runs', '', '| Problem | Run | Status | First verified attempt | Seconds to proof |',
                  '| --- | --- | --- | --- | ---: |'])
    for run in report['runs']:
        duration = run['time_to_first_verified_proof_seconds']
        lines.append(f"| {run['problem_id']} | [{run['run_id']}](/v1/runs/{run['run_id']}) | "
                     f"{run['status']} | {run['first_verified_attempt_id'] or '—'} | "
                     f"{duration if duration is not None else 'unavailable'} |")
    lines.extend(['', 'Attempt and assignment IDs, ancestry, candidates and diagnostics: '
                   f'`GET /v1/experiments/{report["id"]}/summary` and `GET /v1/runs/{{run_id}}`.',
                   '', report['limitations'], ''])
    return '\n'.join(lines)
