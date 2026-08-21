# Operator dashboard conventions

Applies to `rns_resolve/admin.py`, `rns_resolve/admin_templates.py`, the
`/admin/*` handlers in `rns_resolve/service.py`, and `docs/ADMIN.md`.

## Boundaries

- The dashboard is a **separate process** with no state and no database access.
  It reads resolvers' loopback APIs and their public `/healthz`. If it wants a
  number, the resolver must expose it; do not open the sqlite file.
- Every resolver-side admin endpoint lives on the **private (loopback) port**.
  Nothing that lists, mutates, or reveals records may appear on the public
  health port.
- Access control is two gates: the request must come from loopback or from a
  trusted proxy carrying `X-Authentik-Username`, and every write lands on a
  loopback port that is unreachable off-box. Do not weaken either one to make a
  demo easier, and do not trust a forwarded header from an untrusted peer.
- `/healthz` on the dashboard is the only open route, and it must not include
  resolver record data.

## Structure

The pattern here is dependency injection with pydantic settings:

- `AdminSettings` is a pydantic model built by `from_env`. Add a knob there,
  with a default that matches the deployed stack.
- `ResolverClient` takes an injected `fetch` callable, which is why the tests
  need no sockets. Keep the transport injectable; do not call `urllib`
  directly from a route.
- `Registry` owns the clients and the aggregation. Routes receive it through
  FastAPI `Depends`, so tests can override it.
- Templates are a `DictLoader` in `admin_templates.py` with autoescaping on.
  Keep them self-contained: no CDN, no external font, no remote asset. This
  has to work on an isolated LAN, and an operator page that phones out is a
  design error here.

## Behaviour

- A dead resolver must never break the page. `ResolverClient` turns an
  unreachable host into `{"ok": False, "unreachable": True}` and the card shows
  the error; the other resolvers still render.
- Destructive actions require explicit confirmation in the form, and the reply
  says what actually happened, including the resolver it happened on.
- State the honest caveats in the UI, not only in the docs. Deleting a signed
  record is local and replication can bring it back. A flagged peer is
  evidence, not a verdict.
- Actions redirect (303) back with a message rather than rendering a result
  page, so a refresh does not repeat a write.
- The dashboard reads and triggers. It must never become a second path into
  resolution logic: no route may register a name, alter a record's content, or
  change what `resolve` would answer.
