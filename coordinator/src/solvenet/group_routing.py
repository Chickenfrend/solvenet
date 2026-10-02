"""Small local routing policy; worker presence is evidence, not a credential source."""

import json

ROLES = ('planner', 'investigator', 'critic', 'synthesizer')


def validate_routing(models, capabilities):
    from .store import validate_task_request
    if not isinstance(models, dict) or set(models) != set(ROLES):
        raise ValueError('Explicit planner, investigator, critic and synthesizer models required')
    if capabilities is None:
        capabilities = {}
    if not isinstance(capabilities, dict) or len(capabilities) > 16:
        raise ValueError('Invalid model capabilities')
    normalized = {}
    for role, choices in models.items():
        choices = [choices] if isinstance(choices, str) else choices
        if (not isinstance(choices, list) or not 1 <= len(choices) <= 8 or
                any(not isinstance(m, str) for m in choices) or len(set(choices)) != len(choices)):
            raise ValueError('Invalid role model choices')
        for model in choices:
            validate_task_request(model, 'plan', [{'role': 'user', 'content': 'plan'}], 512)
        normalized[role] = choices
    if set(capabilities) - set().union(*map(set, normalized.values())):
        raise ValueError('Capability for unconfigured model')
    for model, config in capabilities.items():
        if not isinstance(config, dict) or set(config) - {'tasks', 'context_bytes', 'context_tokens', 'cost'}:
            raise ValueError('Invalid model capability')
        tasks = config.get('tasks', ['plan', 'finding', 'critique', 'proof'])
        if (not isinstance(tasks, list) or not tasks or
                any(t not in ('plan', 'finding', 'critique', 'proof') for t in tasks)):
            raise ValueError('Invalid task fit')
        if type(config.get('context_bytes', 8192)) is not int or config.get('context_bytes', 8192) < 1:
            raise ValueError('Invalid context capacity')
        if 'context_tokens' in config and (type(config['context_tokens']) is not int or
                                          not 1 <= config['context_tokens'] <= 1024 * 1024):
            raise ValueError('Invalid token context capacity')
        if type(config.get('cost', 1)) is not int or not 1 <= config.get('cost', 1) <= 8:
            raise ValueError('Invalid model cost')
    return normalized


def choose(db, group_id, models, capabilities, role, task_type, context_bytes, remaining,
           *, avoid=None, now=0, lease_seconds=30, context_token_bound=None):
    """Return (model, cost, explanation) or a bounded inability to route."""
    candidates = []
    for order, model in enumerate(models[role]):
        spec = capabilities.get(model, {})
        cost = spec.get('cost', 1)
        presence = db.execute('SELECT health,seen_at,supports_model_respond FROM worker_presence WHERE model=?',
                              (model,)).fetchall()
        live = [p for p in presence if p['seen_at'] > now - max(lease_seconds, 5)]
        ready = [p for p in live if p['health'] in ('ready', 'unobserved')]
        if task_type != 'proof' and ready and not any(p['supports_model_respond'] for p in ready):
            availability = 'proof_only'
        elif ready:
            availability = 'available'
        elif live:
            availability = 'unavailable'
        elif presence:
            availability = 'offline'
        else:
            availability = 'unknown'
        outcomes = db.execute('''SELECT j.status, j.id, j.kind FROM group_jobs gj
            JOIN jobs j ON j.id=gj.job_id
            WHERE gj.group_id=? AND j.model=? AND
            (j.task_type=? OR (?='proof' AND j.kind='model.generate'))''',
            (group_id, model, task_type, task_type)).fetchall()
        completed = sum(r['status'] == 'done' for r in outcomes)
        failed = sum(r['status'] == 'failed' for r in outcomes)
        accepted = db.execute('''SELECT count(*) FROM group_messages gm JOIN group_jobs gj
            ON gj.job_id=gm.job_id JOIN jobs j ON j.id=gj.job_id
            WHERE gj.group_id=? AND j.model=? AND j.task_type=?
            AND gm.review_status='accepted' ''', (group_id, model, task_type)).fetchone()[0]
        verified = db.execute('''SELECT count(*) FROM group_jobs gj JOIN jobs j ON j.id=gj.job_id
            JOIN assignments a ON a.job_id=j.id JOIN attempts t ON t.assignment_id=a.id
            JOIN verifications v ON v.attempt_id=t.id
            WHERE gj.group_id=? AND j.model=? AND j.kind='model.generate' AND v.status='verified' ''',
            (group_id, model)).fetchone()[0] if task_type == 'proof' else 0
        reasons = []
        if task_type not in spec.get('tasks', ['plan', 'finding', 'critique', 'proof']):
            reasons.append('task_mismatch')
        if context_bytes > spec.get('context_bytes', 8192):
            reasons.append('context_exceeded')
        if (context_token_bound is not None and spec.get('context_tokens') is not None and
                context_token_bound > spec['context_tokens']):
            reasons.append('context_window_exceeded')
        if 2 * cost > remaining:
            reasons.append('budget_exceeded')
        if availability in ('offline', 'unavailable', 'proof_only') or (
                capabilities and availability == 'unknown'):
            reasons.append(availability)
        candidates.append({'model': model, 'availability': availability, 'task_fit': task_type in spec.get(
            'tasks', ['plan', 'finding', 'critique', 'proof']), 'context_bytes': spec.get('context_bytes', 8192),
                           'context_tokens': spec.get('context_tokens'),
                           'cost': cost, 'observed_completed_calls': completed,
                           'observed_accepted_findings': accepted, 'observed_verified_proofs': verified,
                           'observed_failed_calls': failed,
                           'reasons': reasons, 'order': order})
    viable = [c for c in candidates if not c['reasons']]
    # Prefer observed quality over cheap failed calls; cost breaks quality ties.
    viable.sort(key=lambda c: (c['model'] == avoid,
                                c['observed_failed_calls'] > (c['observed_accepted_findings'] +
                                                              c['observed_verified_proofs']),
                                -(c['observed_accepted_findings'] + c['observed_verified_proofs']),
                                c['cost'] if role == 'investigator' else 0, c['order']))
    selected = viable[0] if viable else None
    return (selected['model'] if selected else None, 2 * selected['cost'] if selected else None,
             json.dumps({'role': role, 'task_type': task_type, 'context_bytes': context_bytes,
                         'context_token_upper_bound': context_token_bound,
                        'remaining_work': remaining, 'avoid_after_failure': avoid,
                        'candidates': candidates, 'selected': selected['model'] if selected else None,
                        'evidence': 'single_model_fallback_not_hierarchy_evidence' if len(set(
                            sum((models[r] for r in ROLES), []))) == 1 else 'configured_fit_presence_and_outcomes'},
                       sort_keys=True))
