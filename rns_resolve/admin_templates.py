"""Jinja2 templates for the rns-resolve operator dashboard, kept in-package
(a dict loader) so the image needs no template directory and the package
installs cleanly from a wheel. Self-contained HTML/CSS, no external assets:
the dashboard must work on an isolated LAN."""
from __future__ import annotations

from typing import Any, Callable

_BASE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} admin</title>
<style>
:root{--bg:#f6f7f9;--fg:#1d2330;--mut:#5b6475;--card:#fff;--line:#d9dee7;--ok:#1f8f4e;--bad:#c0392b;--warn:#b7791f;--acc:#2b5fd9;--chip:#eef1f6}
@media (prefers-color-scheme:dark){:root{--bg:#0f1216;--fg:#e6e9ef;--mut:#9aa4b5;--card:#171b22;--line:#2a313d;--ok:#3ccf7a;--bad:#ff6b5b;--warn:#f0b44c;--acc:#6f9bff;--chip:#222938}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:18px;align-items:center;padding:12px 20px;border-bottom:1px solid var(--line);background:var(--card)}
header h1{font-size:17px;margin:0}header nav a{margin-right:14px;color:var(--acc);text-decoration:none}header .u{margin-left:auto;color:var(--mut);font-size:13px}
main{padding:18px 20px;max-width:1400px;margin:0 auto}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card h2{margin:0 0 8px;font-size:16px;display:flex;align-items:center;gap:10px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;background:var(--bad)}.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:3px 14px;font-size:14px}.kv dt{color:var(--mut)}.kv dd{margin:0;word-break:break-all}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13.5px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--mut);font-weight:600}
.chip{display:inline-block;padding:1px 8px;border-radius:999px;background:var(--chip);font-size:12px;margin-right:4px}.chip.ok{color:var(--ok)}.chip.bad{color:var(--bad)}.chip.warn{color:var(--warn)}
.msg{background:var(--chip);border:1px solid var(--line);padding:8px 12px;border-radius:8px;margin-bottom:14px}
form.inline{display:inline}button,input[type=submit]{font:inherit;padding:4px 10px;border-radius:6px;border:1px solid var(--line);background:var(--chip);color:var(--fg);cursor:pointer}
button.danger{border-color:var(--bad);color:var(--bad)}input[type=text],select{font:inherit;padding:5px 8px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
.muted{color:var(--mut)}.actions{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}.sub{font-size:12.5px;color:var(--mut)}
details summary{cursor:pointer;color:var(--acc)}
</style></head><body>
<header><h1>{{ title }} admin</h1>
<nav><a href="/">Dashboard</a><a href="/records">Records</a><a href="/lookup">Lookup</a><a href="/api/overview">JSON</a></nav>
<span class="u">{{ user }}</span></header>
<main>
{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
{% block body %}{% endblock %}
</main></body></html>"""

_DASHBOARD = r"""{% extends "base" %}{% block body %}
<p class="sub">{{ overview.up }}/{{ overview.total }} resolvers up, {{ overview.total_records }} records total, generated {{ overview.generated_at }}.
{% if overview.node_hash %} NomadNet node {{ overview.node_name or '' }} <code>{{ overview.node_hash }}</code>.{% endif %}</p>
<div class="grid">
{% for r in overview.resolvers %}
<div class="card">
  <h2><span class="dot {{ 'ok' if r.up and r.rns_ready else ('warn' if r.up else '') }}"></span>Resolver {{ r.name }}
    {% if r.beacon_db %}<span class="chip ok">beacon db</span>{% else %}<span class="chip">registered-only</span>{% endif %}
    {% if r.peering %}<span class="chip">peered</span>{% endif %}</h2>
  {% if not r.up %}<p class="chip bad">{{ r.error }}</p>{% endif %}
  {% if r.status_error %}<p class="sub">status: {{ r.status_error }}</p>{% endif %}
  <dl class="kv">
    <dt>dest</dt><dd><code>{{ r.dest }}</code></dd>
    <dt>identity</dt><dd><code>{{ r.identity or '' }}</code></dd>
    <dt>records</dt><dd>{{ r.records }}</dd>
    <dt>uptime</dt><dd>{{ r.uptime }} <span class="sub">since {{ r.started_at }}</span></dd>
    <dt>announce</dt><dd>{{ r.last_announce }} <span class="sub">({{ r.announce_count }} total, every {{ r.announce_interval }})</span></dd>
    <dt>requests</dt><dd>{{ r.metrics_total }} <span class="sub">{% for op,n in r.metrics_ops %}{{ op }}={{ n }} {% endfor %}</span></dd>
    <dt>last sweep</dt><dd>{{ r.last_sweep_expired }} expired</dd>
    <dt>peering cost</dt><dd>{{ r.config.peering_cost }}{% if r.config.sync_from %} <span class="sub">allowlist {{ r.config.sync_from|length }}</span>{% endif %}</dd>
  </dl>
  {% if r.peers %}
  <table><thead><tr><th>peer</th><th>last sync</th><th>result</th><th>next</th><th></th></tr></thead><tbody>
  {% for p in r.peers %}<tr>
    <td><code title="{{ p.peer }}">{{ p.short }}</code>{% if p.backoff %} <span class="chip warn">backoff</span>{% endif %}</td>
    <td>{{ p.last_at }}<div class="sub">ok {{ p.last_ok }}</div></td>
    <td>{% if p.last_ok_flag %}<span class="chip ok">ok</span>{% elif p.last_error or p.last_at != 'never' %}<span class="chip bad">failed</span>{% endif %} <span class="sub">{{ p.last_summary }}{% if p.last_error %} {{ p.last_error }}{% endif %}</span></td>
    <td>{{ p.due_in }} <span class="sub">/ {{ p.interval }}</span></td>
    <td><form class="inline" method="post" action="/actions/sync"><input type="hidden" name="resolver" value="{{ r.name }}"><input type="hidden" name="peer" value="{{ p.peer }}"><button>sync now</button></form></td>
  </tr>{% endfor %}</tbody></table>
  {% endif %}
  {% if r.audit %}<details><summary>withholding audit</summary>
    <div class="sub">{{ r.audit.peers }} peers, {{ r.audit.strikes_required }} strikes to flag.
    {% if r.audit.last_round %}Last round: {{ r.audit.last_round.peers_answering }} answered, pulled {{ r.audit.last_round.pulled }}, flagged {{ r.audit.last_round.flagged|length }}.{% else %}No round yet.{% endif %}</div>
    {% if r.suspects %}<ul>{% for s in r.suspects %}<li><code>{{ s.peer }}</code> {{ s.info }}</li>{% endfor %}</ul>{% else %}<div class="sub">No suspects.</div>{% endif %}
  </details>{% endif %}
  {% if r.recent %}<details><summary>recent requests</summary><table><tbody>
    {% for q in r.recent %}<tr><td class="sub">{{ q.age }}</td><td>{{ q.op }}</td><td><code>{{ q.q }}</code></td></tr>{% endfor %}
  </tbody></table></details>{% endif %}
  <div class="actions">
    <form class="inline" method="post" action="/actions/announce"><input type="hidden" name="resolver" value="{{ r.name }}"><button>announce now</button></form>
    {% if r.peering %}<form class="inline" method="post" action="/actions/sync"><input type="hidden" name="resolver" value="{{ r.name }}"><button>sync all peers</button></form>{% endif %}
    {% if r.audit %}<form class="inline" method="post" action="/actions/audit"><input type="hidden" name="resolver" value="{{ r.name }}"><button>audit now</button></form>{% endif %}
    <a class="chip" href="/records?resolver={{ r.name }}">records</a>
  </div>
</div>
{% endfor %}
</div>
{% endblock %}"""

_RECORDS = r"""{% extends "base" %}{% block body %}
<form method="get" action="/records" class="card" style="margin-bottom:14px">
  <input type="text" name="q" value="{{ q }}" placeholder="name, target or identity substring">
  <select name="resolver"><option value="">all resolvers</option>{% for n in resolver_names %}<option value="{{ n }}" {{ 'selected' if n == resolver else '' }}>{{ n }}</option>{% endfor %}</select>
  <select name="expired"><option value="1" {{ 'selected' if expired != '0' else '' }}>include expired</option><option value="0" {{ 'selected' if expired == '0' else '' }}>live only</option></select>
  <button>filter</button>
  <span class="sub">{{ total }} matching, showing {{ records|length }} (offset {{ offset }}, page {{ limit }})</span>
  {% if offset > 0 %}<a class="chip" href="/records?q={{ q|urlencode }}&resolver={{ resolver }}&expired={{ expired }}&limit={{ limit }}&offset={{ [offset - limit, 0]|max }}">prev</a>{% endif %}
  {% if offset + limit < total %}<a class="chip" href="/records?q={{ q|urlencode }}&resolver={{ resolver }}&expired={{ expired }}&limit={{ limit }}&offset={{ offset + limit }}">next</a>{% endif %}
</form>
{% for name, err in errors.items() %}<p class="chip bad">{{ name }}: {{ err }}</p>{% endfor %}
<div class="card" style="overflow-x:auto">
<table><thead><tr><th>name</th><th>resolver</th><th>target</th><th>registrant</th><th>kind</th><th>registered</th><th>expires</th><th>last used</th><th>id</th><th></th></tr></thead><tbody>
{% for r in records %}<tr>
  <td><strong>{{ r.name }}</strong></td>
  <td>{{ r.resolver }}</td>
  <td><code title="{{ r.target }}">{{ r.short_target }}</code> <span class="sub">{{ r.app }}{% if r.aspects %}/{{ r.aspects|join('.') }}{% endif %}</span></td>
  <td><code title="{{ r.identity }}">{{ r.short_identity }}</code></td>
  <td>{% if r.attested %}<span class="chip warn">attested</span>{% else %}<span class="chip ok">signed</span>{% endif %}{% if r.pubkey_bound %}<span class="chip">pubkey</span>{% endif %}{% if r.expired %}<span class="chip bad">expired</span>{% endif %}</td>
  <td>{{ r.registered }}</td><td>{{ r.expires }}</td><td>{{ r.last_used }}</td>
  <td><code title="{{ r.id }}">{{ r.short_id }}</code></td>
  <td><details><summary>delete</summary>
    <form method="post" action="/actions/delete"><input type="hidden" name="resolver" value="{{ r.resolver }}"><input type="hidden" name="record_id" value="{{ r.id }}"><input type="hidden" name="back" value="/records?q={{ q|urlencode }}&resolver={{ resolver }}&expired={{ expired }}">
    <label><input type="checkbox" name="confirm" value="yes"> really delete from {{ r.resolver }}</label> <button class="danger">delete</button>
    {% if not r.attested %}<div class="sub">signed records can come back from a peer on the next sync; delete on every resolver that holds it.</div>{% endif %}</form>
  </details></td>
</tr>{% else %}<tr><td colspan="10" class="muted">no records</td></tr>{% endfor %}
</tbody></table></div>
{% endblock %}"""

_LOOKUP = r"""{% extends "base" %}{% block body %}
<form method="get" action="/lookup" class="card" style="margin-bottom:14px">
  <input type="text" name="q" value="{{ q }}" placeholder="name to resolve (what a client would type)" size="40">
  <select name="resolver"><option value="">every resolver</option>{% for n in resolver_names %}<option value="{{ n }}" {{ 'selected' if n == resolver else '' }}>{{ n }}</option>{% endfor %}</select>
  <button>resolve</button>
  <span class="sub">Runs the resolver's own /resolve on its loopback API: registered records first, then Beacon announce candidates ranked with trust evidence (resolver A only).</span>
</form>
{% for res in results %}
<div class="card" style="margin-bottom:14px"><h2>{{ res.resolver }}</h2>
{% set rep = res.reply %}
{% if not rep.ok %}<p class="chip bad">{{ rep.err or 'unavailable' }}</p>{% else %}
<table><thead><tr><th>name</th><th>target</th><th>source</th><th>trust / evidence</th></tr></thead><tbody>
{% for m in rep.get('results', rep.get('matches', rep.get('records', []))) %}<tr>
  <td><strong>{{ m.name }}</strong></td><td><code>{{ m.target }}</code></td>
  <td>{{ m.source or m.kind or ('record' if m.sig is defined else '') }}</td>
  <td class="sub">{% for k, v in m.items() if k not in ('name','target','source','kind') %}{{ k }}={{ v }} {% endfor %}</td>
</tr>{% else %}<tr><td colspan="4" class="muted">no match</td></tr>{% endfor %}
</tbody></table>{% endif %}</div>
{% endfor %}
{% endblock %}"""

TEMPLATES = {"base": _BASE, "dashboard": _DASHBOARD, "records": _RECORDS, "lookup": _LOOKUP}


def renderer() -> Callable[..., str]:
    """Return ``render(name, **ctx) -> str`` over the in-package templates."""
    from jinja2 import DictLoader, Environment, select_autoescape

    env = Environment(loader=DictLoader(TEMPLATES),
                      autoescape=select_autoescape(default=True, default_for_string=True))

    def render(name: str, **ctx: Any) -> str:
        return env.get_template(name).render(**ctx)

    return render
