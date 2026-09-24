"""Shared document shell for the private site."""

from html import escape


def page(title, content, section='Problems', htmx=False):
    title = escape(str(title), quote=True)
    links = ''.join(
        f'<a href="{path}"' + (' aria-current="page"' if section == name else '')
        + f'>{name}</a>'
        for name, path in (('Models', '/'), ('Problems', '/problems'))
    )
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · SolveNet</title><link rel="stylesheet" href="/static/site.css">
{'<script src="/static/htmx.min.js" defer></script>' if htmx else ''}</head>
<body><div class="site"><header class="site-header"><a class="brand" href="/">SolveNet</a>
<nav class="site-nav" aria-label="Main navigation">{links}</nav></header>
<main id="main"><h1>{title}</h1>{content}</main></div></body></html>'''
