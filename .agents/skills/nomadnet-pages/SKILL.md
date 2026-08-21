---
name: nomadnet-pages
description: The announced NomadNet node and its micron exec pages (lookup, register, docs). Covers the loopback contract, micron traps, and why the node registers its own name at setup.
---

# Skill: nomadnet-pages

The resolver has a face on the mesh: an announced NomadNet node whose pages let
someone look a name up, register one, and read the docs without installing
anything. Pages are the only part of this project most users will ever touch.

## When to use

- Editing `pages/index.mu`, `pages/register.mu`, `pages/docs.mu`
- Changing the node container or its config
- Touching the private loopback endpoints the pages call
- Adding a page

Also read: `.agents/conventions/admin.md` (same loopback port, different
consumer) and `.agents/skills/trust-invariants/SKILL.md`.

## Contract

- Pages are **stdlib-only Python exec scripts**. They must not import
  `rns_resolve`. They talk to the resolver over loopback HTTP on
  `RESOLVE_PRIVATE_PORT` (8226 for resolver A), which is why the node container
  uses host networking.
- Exec pages need the executable bit. The stack sets `chmod 755` at deploy; a
  page that renders as source is almost always this.
- Field and variable names arrive as `field_*` and `var_*` in the environment.
  `var_q=beacon` is how the lookup form passes a query.
- The node has its **own identity volume**, so its node hash is stable across
  restarts and independent of the resolver's identity.

## Micron traps

- Micron is not markdown. Test what the wire actually carries, not what a
  client renders.
- MeshData head blocks (`# +type: service`) appear as literal `#` lines on the
  raw wire and are discarded by clients at render time. That is correct
  behaviour, not a bug to hide.
- Decorative unicode in a node name is normal on the mesh. `derive_site_name`
  NFKC-folds bold unicode, strips emoji and turns spaces into dashes, which is
  how a node called with Mathematical Sans-Serif Bold letters becomes the
  ASCII name it registers.

## Registration through a page is the fallback, not the flow

The intended flow is **setup-time self-registration**: `rns_resolve.nodereg`
reads the node's own identity and node name, signs, and registers with a
resolver, renewing on an interval. That record is self-certifying, so it
replicates to peers and resolves everywhere.

A registration made through `register.mu` is **attested**: accepted locally,
never replicated. The page says so, and `docs/INTEGRATION.md` says so. Keep
that distinction visible in any copy you write; a user who thinks a page
registration propagates will be confused when a second resolver has never
heard of their name.

`register.mu` requires an identified link, shows the verified identity, derives
the target from it, and lists owned names with per-name unregister links backed
by `GET /owned` and `POST /unregister`.

## Page copy

Users read this over a slow link, on a small screen, sometimes on a terminal.
Short lines. Say what happened and what to do next. No decoration for its own
sake. Run the prose past `.agents/skills/no-ai-slop/SKILL.md` like any other
text in the repo.
