# Core conventions

- Read `.agents/overview.md` for layout, ports, invariants, and known traps.
- Mesh-facing work: read `.agents/conventions/reticulum-zen.md` and the gates
  in `.agents/skills/trust-invariants/SKILL.md` first.
- Run `py -3 -m pytest tests -q` before saying a change works. The suite needs
  no network and no RNS, so there is no excuse for skipping it.
- Minimal diffs. Match nearby style. The codebase is stdlib-first Python with
  small seams for testing; keep it that way.
- No emojis in repo text, source, or commit messages. Decorative unicode
  belongs to mesh announce names, not to this repository.
- No emdashes or semicolons in comments or docs you write.
- No backticks inside code comments. Prefer plain words or quoted identifiers.
- No TODO or FIXME noise. Either fix it or write it down in the right doc.
- Do not commit or push unless asked.
- Do not create markdown docs unless asked, except agent guidance under
  `.agents/` when requested.
- Do not generate exploit proof-of-concepts, malware, or attack tooling.

## Dependencies

The core package depends on `rns` and nothing else. Optional extras are
declared in `pyproject.toml`:

- `service` adds `psycopg2-binary` (Beacon reads) and `lxmf` (LXStamper).
- `admin` adds `fastapi`, `uvicorn`, `jinja2`, `pydantic`.

A module that needs an extra must import it lazily or guard the import, so a
client install stays small and a missing extra degrades to a clear message
rather than an ImportError at startup. `rns_resolve/admin.py` shows the shape.

Do not add a dependency to make something marginally shorter. Prefer LXMF's or
RNS's own implementation over a reimplementation when the semantics must match
theirs, which is why peering stamps use LXMF's `LXStamper` directly.

## Errors and failure

- Handlers return `{"ok": False, "err": "..."}`; they do not raise across the
  wire. `_err` in `service.py` is the one shape.
- Degrade, do not fail. An unreachable Beacon database means registered-only
  answers. An unreachable peer means backoff, not a crash.
- Never let an operator or diagnostic path change resolution behaviour.

## Prose

Before writing or editing prose, read `.agents/skills/no-ai-slop/SKILL.md` and
self-check against `.agents/skills/no-ai-slop/references/ai-writing-detection.md`.
The 24 rules are in `.agents/skills/no-ai-slop/references/rules.md`.

Say what the thing is. Headings name the section contents. Every claim ends on
a checkable detail (a path, an op name, a port, a measured quantity). Do not
invent numbers, quotes, or incidents. This applies to page copy in `pages/`,
which users read over the mesh, as much as to the README.

Public commit messages and docs describe the change, not internal editorial
policy. A subject line like "docs: formatting cleanup" is right; naming a
private style rule in a public log is not.
