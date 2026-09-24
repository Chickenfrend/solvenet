"""Redacted, descriptive summary of a completed private local-run JSON."""
import json
import sys
from collections import defaultdict


def verified(a):
    return bool(a and (a.get('verification') or {}).get('status') == 'verified')


def empty():
    return dict(requests=0, input_tokens=0, output_tokens=0, unknown_input=0,
                unknown_output=0, provider_duration_ns=0, unknown_duration=0,
                lean_checks=0, lean_ms=0, extracted=0, format=0, timeout=0,
                verifier_error=0, request_failure=0, third_success=0)


def add(cost, a, third=False):
    cost['requests'] += 1
    usage = a.get('usage') or {}
    for key, name in [('input_tokens', 'input'), ('output_tokens', 'output')]:
        if usage.get(key) is None:
            cost['unknown_' + name] += 1
        else:
            cost[key] += usage[key]
    duration = (a.get('generation') or {}).get('total_duration_ns')
    if duration is None:
        cost['unknown_duration'] += 1
    else:
        cost['provider_duration_ns'] += duration
    if a.get('candidate'):
        cost['extracted'] += 1
    v = a.get('verification')
    if v:
        cost['lean_checks'] += 1
        cost['lean_ms'] += v['elapsed_ms']
        if v['status'] in ('timeout', 'verifier_error'):
            cost[v['status']] += 1
    if a.get('outcome') in ('format', 'request_failure'):
        cost[a['outcome']] += 1
    if third and verified(a):
        cost['third_success'] += 1


def relative(a, b, key):
    return None if max(a[key], b[key]) == 0 else round(100 * abs(a[key] - b[key]) / max(a[key], b[key]), 2)


def summarize(data):
    if not data['complete'] or data.get('aborted') or len(data['blocks']) != 60:
        return {'status': 'partial', 'completed_blocks': len(data['blocks']),
                'partial_block': (data.get('partial') or {}).get('problem_id'),
                'abort_reason': data.get('aborted'), 'comparison': None}
    arms = {a: empty() for a in ('baseline', 'collaborative')}
    incremental = {a: empty() for a in arms}
    prefix = empty()
    seeds = defaultdict(lambda: {'baseline': set(), 'collaborative': set(), 'baseline_only': 0, 'collaborative_only': 0})
    fixtures = defaultdict(lambda: {'baseline': 0, 'collaborative': 0, 'baseline_only': 0, 'collaborative_only': 0})
    pairs = {'baseline_only': 0, 'collaborative_only': 0, 'both': 0, 'neither': 0}
    third_pairs = dict.fromkeys(pairs, 0)
    cumulative = []
    solved = {'baseline': 0, 'collaborative': 0}
    for b in data['blocks']:
        for a in b['prefix']:
            add(prefix, a)
            for arm in arms:
                add(arms[arm], a)
        prefix_solved = any(verified(a) for a in b['prefix'])
        if prefix_solved and (b.get('baseline') or b.get('collaborative') or b.get('map')):
            raise ValueError('third attempt/map after verified prefix')
        if not prefix_solved and (not b.get('baseline') or not b.get('collaborative')):
            raise ValueError('missing third attempts')
        results = {}
        for arm in arms:
            third = b.get(arm)
            if third:
                add(arms[arm], third, True)
                add(incremental[arm], third, True)
            results[arm] = prefix_solved or verified(third)
            if results[arm]:
                solved[arm] += 1
                seeds[b['base_seed']][arm].add(b['problem_id'])
                fixtures[b['problem_id']][arm] += 1
        key = ('both' if all(results.values()) else 'neither' if not any(results.values()) else
               'baseline_only' if results['baseline'] else 'collaborative_only')
        pairs[key] += 1
        if not prefix_solved:
            third_pairs[key] += 1
        if key.endswith('_only'):
            seeds[b['base_seed']][key] += 1
            fixtures[b['problem_id']][key] += 1
        cumulative.append({'seed': b['base_seed'], 'problem_id': b['problem_id'],
                           'solved': solved.copy(),
                           'cost': {arm: {k: arms[arm][k] for k in ('requests', 'input_tokens', 'output_tokens', 'lean_ms')}
                                    for arm in arms}})
    keys = ('input_tokens', 'output_tokens', 'lean_ms')
    differences = {scope: {k: relative(cost['baseline'], cost['collaborative'], k) for k in keys}
                   for scope, cost in [('all', arms), ('incremental_third', incremental)]}
    comparable = (all(v is not None and v <= 15 for d in differences.values() for v in d.values())
                  and all(not arms[a]['unknown_input'] and not arms[a]['unknown_output']
                          and not arms[a]['request_failure'] for a in arms)
                  and abs(arms['baseline']['lean_checks'] - arms['collaborative']['lean_checks']) <= 1)
    seed_counts = {str(seed): {'baseline': len(s['baseline']), 'collaborative': len(s['collaborative']),
                              'baseline_only': s['baseline_only'], 'collaborative_only': s['collaborative_only']}
                   for seed, s in seeds.items()}
    supported = (comparable and pairs['collaborative_only'] - pairs['baseline_only'] >= 4
                 and sum(s['collaborative_only'] > s['baseline_only'] for s in seed_counts.values()) >= 3
                 and sum(f['collaborative_only'] > 0 for f in fixtures.values()) >= 2)
    config = data['config']
    return {'status': 'complete', 'configuration': config, 'paired_outcomes': pairs,
            'third_required_outcomes': third_pairs, 'distinct_solved_per_seed': seed_counts,
            'per_fixture_paired_outcomes': dict(fixtures), 'charged_cost': arms,
            'physical_shared_prefix_cost': prefix, 'incremental_third_cost': incremental,
            'relative_cost_difference_percent': differences, 'cost_comparable': comparable,
            'next_local_protocol_experiment_supported': supported, 'cumulative': cumulative}


if __name__ == '__main__':
    with open(sys.argv[1], encoding='utf-8') as source:
        print(json.dumps(summarize(json.load(source)), indent=2, sort_keys=True))
