"""Print a compact operator trace from the coordinator's existing group/run APIs."""

import argparse
import json
from urllib.request import urlopen


def fetch(url):
    with urlopen(url, timeout=5) as response:
        return json.load(response)


def measured(value):
    return 'unknown' if value is None else str(value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('group_id')
    parser.add_argument('--coordinator', default='http://127.0.0.1:8080')
    args = parser.parse_args()
    base = args.coordinator.rstrip('/')
    snapshot = fetch(f'{base}/v1/groups/{args.group_id}')
    group, loop = snapshot['group'], snapshot['loop']
    agents = {row['id']: row['request_key'] for row in group['agents']}
    tasks = {row['id']: row['request_key'] for row in group['tasks']}
    routes = {row['request_key']: row['explanation'] for row in group['routing']}
    calls = {}
    for row in group['calls']:
        calls.setdefault(row['request_key'], []).append(row)
    print(f'group {args.group_id}  phase={loop["phase"]} reason={loop["reason"]} '
          f'remaining_work={group["remaining_work"]}/{group["max_work"]}')
    for task in group['tasks']:
        key = task['request_key']
        route = routes.get(key, {})
        entries = calls.get(key, [])
        call = entries[-1] if entries else {}
        print(f'  {key:16} {agents[task["creator_id"]]} -> {agents[task["owner_id"]]} '
              f'parent={tasks.get(task["parent_id"], "-")} task={task["status"]} '
              f'model={route.get("selected")} job={call.get("job_status", "-")}')
        for entry in entries:
            result = entry.get('result') or {}
            usage = result.get('usage') or {}
            generation = result.get('generation') or {}
            print(f'    call {entry.get("assignment_id") or "not leased"}: '
                  f'worker={entry.get("worker_id") or "-"} '
                  f'lease={entry.get("status") or "-"} result={result.get("status", "-")} '
                  f'in={measured(usage.get("input_tokens"))} '
                  f'out={measured(usage.get("output_tokens"))} '
                  f'provider_ns={measured(generation.get("total_duration_ns"))} '
                  f'failure={str(result.get("failure_category") or result.get("error") or "-")[:120]}')
        if route.get('avoid_after_failure'):
            print(f'    avoid_after_failure={route["avoid_after_failure"]}')
        for candidate in route.get('candidates', []):
            if candidate['reasons']:
                print(f'    excluded {candidate["model"]}: {", ".join(candidate["reasons"])}')
    for message in group['messages']:
        excerpt = message['text'].replace('\n', ' ')[:120]
        print(f'  message {message["request_key"]}: {message["kind"]} '
              f'{message["review_status"]} {message["verification_status"]}: {excerpt}')
    for artifact in group['artifacts']:
        print(f'  artifact {artifact["request_key"]}: {artifact["status"]} '
              f'current={artifact["current_status"]} {artifact["statement"][:120]}')
    if group['run']:
        run_id = group['run']['run_id']
        run = fetch(f'{base}/v1/runs/{run_id}')
        print(f'  run {run_id}: {run["status"]}')
        for attempt in run['attempts']:
            print(f'    target attempt {attempt["id"]}: {attempt["verification_status"]} '
                  f'Lean elapsed_ms={attempt["elapsed_ms"]}')
    print('cost:', json.dumps(group['cost'], sort_keys=True))


if __name__ == '__main__':
    main()
