"""Problem pages and strictly bounded fixture-run form."""

import re
from html import escape
from urllib.parse import quote


ID = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
RUN_ID = re.compile(r'[0-9a-f]{32}\Z')


def url(set_id, version, problem_id):
    return f'/problems/{quote(set_id, safe="")}/{version}/{quote(problem_id, safe="")}'


def page(title, content, htmx=False):
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} · SolveNet</title><style>
* {{ box-sizing: border-box }} body {{ max-width: 55rem; margin: auto; padding: 1.25rem;
background: #171717; color: #fafafa; font: 1rem/1.5 system-ui,sans-serif; overflow-wrap: anywhere }}
a {{ color: #ffbd72; min-height: 2.75rem; display: inline-flex; align-items: center }}
 h1,h2 {{ color: #ffac52 }} article,form,#run-status {{ border: 1px solid #ed912c; border-radius: .75rem;
 padding: 1rem; margin: 1rem 0; min-width: 0 }} pre {{ white-space: pre-wrap; overflow-wrap: anywhere;
 overflow-x: auto; max-width: 100%; min-width: 0 }} .usage {{ display: grid; gap: .25rem }}
 .usage div {{ display: flex; flex-wrap: wrap; gap: .5rem; min-width: 0 }} .usage dt {{ font-weight: 700 }}
 .usage dd {{ margin: 0; min-width: 0; overflow-wrap: anywhere }}
label {{ display: block; margin: .8rem 0 }} input,select,button {{ display: block; font: inherit;
min-height: 2.75rem; max-width: 100%; width: 100%; padding: .35rem; }} button {{ background: #a94e00;
color: white; border: 2px solid #ffb25e; cursor: pointer }} .error {{ border: 2px solid #ffb25e; padding: 1rem }}
 </style>{'<script src="/static/htmx.min.js" defer></script>' if htmx else ''}</head><body><nav><a href="/">Models</a> · <a href="/problems">Problems</a></nav>
<main><h1>{escape(title)}</h1>{content}</main></body></html>'''


def list_page(sets, rows, runs, error=None):
    if error:
        return page('Problems', f'<p role="alert">{escape(error)}. Check the coordinator and refresh.</p>')
    items = []
    for fixture, problem in rows:
        path = url(fixture['set_id'], fixture['version'], problem['id'])
        matches = [r for r in runs if r.get('fixture_set_id') == fixture['set_id']
                   and r.get('fixture_version') == fixture['version']
                   and r.get('fixture_problem_id') == problem['id']]
        state = escape(str(matches[0]['status'])) if matches else 'No recent runs'
        items.append(f'<article><h2><a href="{escape(path, quote=True)}">{escape(problem["title"])}</a></h2>'
                     f'<p>{escape(fixture["set_id"])} v{fixture["version"]} · {state}</p></article>')
    return page('Problems', ''.join(items) or '<p>No problems available.</p>')


def detail(fixture, problem, models, runs, token, error=None, values=None):
    e = lambda v: escape(str(v), quote=True)
    values = values or {}
    choices = ''.join(f'<option value="{e(m[0])}"{" selected" if values.get("model") == m[0] else ""}>'
                      f'{e(m[1])} ({e(m[0])})</option>' for m in models)
    recent = ''.join(f'<li><a href="/runs/{e(r["run_id"])}">Run {e(r["run_id"])}</a> — {e(r["status"])}</li>'
                     for r in runs if r.get('fixture_set_id') == fixture['set_id']
                     and r.get('fixture_version') == fixture['version']
                     and r.get('fixture_problem_id') == problem['id'])
    inputs = ''.join(f'<label>{label}<input name="{key}" type="number" min="{low}" max="{high}" '
                     f'value="{e(values.get(key, default))}" required></label>'
                     for key, label, low, high, default in (
                         ('attempts', 'Initial chains', 1, 100, 3),
                         ('max_output_tokens', 'Max output tokens per job', 1, 32768, 2048),
                         ('generation_timeout_seconds', 'Generation timeout (seconds)', 1, 86400, 120)))
    strategy = values.get('strategy', 'independent')
    form = (f'<form method="post"><h2>Start a run</h2>'
            + (f'<p class="error" role="alert">{e(error)}</p>' if error else '')
            + f'<input type="hidden" name="csrf" value="{e(token)}">'
            + f'<label>Model<select name="model" required>{choices}</select></label>'
            + '<label>Strategy<select name="strategy">'
            + ''.join(f'<option value="{s}"{" selected" if strategy == s else ""}>{s.title()}</option>'
                      for s in ('independent', 'repair')) + '</select></label>' + inputs
            + ('<p>No configured model is currently available. Check worker status or configure a model.</p>'
               if not models else '<button type="submit">Start run</button>') + '</form>')
    return page(problem['title'], f'<p>{e(fixture["set_id"])} v{e(fixture["version"])} · '
                f'Environment: {e(problem["environment"])}</p><h2>Imports</h2>'
                f'<pre>{e(chr(10).join(problem["imports"]))}</pre><h2>Lean statement</h2>'
                f'<pre>{e(problem["statement"])}</pre>{form}<h2>Recent activity</h2>'
                f'<ul>{recent}</ul>')


def validate(fields, model_ids):
    allowed = {'csrf', 'model', 'strategy', 'attempts', 'max_output_tokens', 'generation_timeout_seconds'}
    if set(fields) != allowed or any(len(v) != 1 for v in fields.values()):
        raise ValueError('Invalid form fields; refresh and try again')
    values = {k: v[0] for k, v in fields.items()}
    if values['model'] not in model_ids:
        raise ValueError('Select an available configured model')
    if values['strategy'] not in ('independent', 'repair'):
        raise ValueError('Choose independent or repair strategy')
    for key, maximum in (('attempts', 100), ('max_output_tokens', 32768),
                         ('generation_timeout_seconds', 86400)):
        raw = values[key]
        if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= maximum:
            raise ValueError(f'{key} must be an integer between 1 and {maximum}')
        values[key] = int(raw)
    return values
