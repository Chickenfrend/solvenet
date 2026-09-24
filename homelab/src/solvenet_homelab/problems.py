"""Problem pages and strictly bounded fixture-run form."""

import re
from html import escape
from urllib.parse import quote

from . import layout


ID = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\Z')
RUN_ID = re.compile(r'[0-9a-f]{32}\Z')


def url(set_id, version, problem_id):
    return f'/problems/{quote(set_id, safe="")}/{version}/{quote(problem_id, safe="")}'


def page(title, content, htmx=False):
    return layout.page(title, content, htmx=htmx)


def list_page(sets, rows, runs, error=None):
    if error:
        return page('Problems', f'<p role="alert">{escape(error)}. Check the coordinator and refresh.</p>')
    items = []
    for fixture, problem in rows:
        path = url(fixture['set_id'], fixture['version'], problem['id'])
        matches = [r for r in runs if r.get('fixture_set_id') == fixture['set_id']
                    and r.get('fixture_version') == fixture['version']
                    and r.get('fixture_problem_id') == problem['id']]
        e = lambda value: escape(str(value), quote=True)
        category = problem.get('category') or fixture['set_id']
        state = e(matches[0]['status']) if matches else 'No recent runs'
        description = problem.get('description')
        recent = matches[0] if matches else None
        run_id = recent.get('run_id') if recent else None
        run_link = (f' · <a href="/runs/{e(run_id)}">View recent run</a>'
                    if isinstance(run_id, str) and RUN_ID.fullmatch(run_id) else '')
        activity = (f'<p>Recent activity: {state}{run_link}</p>'
                    if matches else '<p>Recent activity: No recent runs.</p>')
        items.append(f'''<article class="problem-card"><details><summary><h2 class="problem-heading">
<span>{e(problem['title'])}</span>
<span class="problem-meta">{e(category)} · {state}</span>
<span class="expand-label" aria-hidden="true">Preview</span></h2></summary>
<div class="problem-preview">{f'<p>{e(description)}</p>' if description else ''}
<h3>Imports</h3><pre>{e(chr(10).join(problem['imports']))}</pre>
<h3>Lean statement</h3><pre>{e(problem['statement'])}</pre>
{('<p class="muted">Preview shortened; open the problem for full details.</p>' if problem.get('preview_truncated') else '')}
{activity}<a href="{e(path)}">View problem / start work</a></div></details></article>''')
    return page('Problems', '<p class="lede">Browse Lean problems and start a proof run.</p>'
                + (''.join(items) or '<p class="empty">No problems available. Refresh to check for new fixture sets.</p>'))


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
             + ('<p class="empty">No configured model is currently available. Check worker status or configure a model.</p>'
                if not models else '<button type="submit">Start run</button>') + '</form>')
    return page(problem['title'], f'<p>{e(fixture["set_id"])} v{e(fixture["version"])} · '
                f'Environment: {e(problem["environment"])}</p><h2>Imports</h2>'
                f'<pre>{e(chr(10).join(problem["imports"]))}</pre><h2>Lean statement</h2>'
                f'<pre>{e(problem["statement"])}</pre>{form}<h2>Recent activity</h2>'
                 f'<ul>{recent or "<li>No recent runs for this problem.</li>"}</ul>')


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
