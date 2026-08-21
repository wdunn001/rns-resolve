---
name: operator-dashboard
description: Adding a panel, an action, or a resolver-side /admin endpoint to the operator dashboard. Covers the injection pattern, the two access gates, and what must never appear on the public port.
---

# Skill: operator-dashboard

`rns_resolve.admin` is the page an operator opens when something looks wrong.
It answers what `/healthz` cannot: what is in the store, whether replication is
working, what clients are asking for, and it offers the few actions worth a
button.

## When to use

- Adding a panel, a column, or an action to the dashboard
- Adding a resolver-side `/admin/*` endpoint
- Changing access control or deployment of the admin process

Also read: `.agents/conventions/admin.md`, `docs/ADMIN.md`.

## Architecture in one line

A stateless FastAPI process talks to resolvers' loopback APIs; it never opens
the database and never joins the mesh.

```
browser -> Caddy (Authentik forward-auth) -> admin :8229 -> 127.0.0.1:8226 /admin/*
```

## Adding a datum end to end

1. **Resolver side.** Expose it from `admin_status` (or a new handler) in
   `service.py`. If the value comes from a live object, read it defensively:
   the dashboard must render when a collaborator is missing or broken.
2. **Store side.** If it needs SQL, add a method to `store.py`. Operator
   queries may include attested and expired records, unlike the peer-facing
   queries, which must not.
3. **Client side.** Add a call to `ResolverClient` using the private base url.
4. **Shape it.** Extend `resolver_card` or the records view. Formatting helpers
   (`_age`, `_dur`, `_stamp`) exist so the template holds no logic.
5. **Template.** Edit the `DictLoader` templates in `admin_templates.py`. Self
   contained only: no CDN, no remote font, no external image.
6. **Test both sides.** `tests/test_admin_api.py` for the resolver handler and
   store method with fakes, `tests/test_admin.py` for the client, the card and
   the route with an injected transport.

## Adding an action

Actions are POST form routes that call one resolver method and redirect (303)
back with a message. Rules:

- Destructive actions require an explicit confirmation field. An unconfirmed
  post must not reach the resolver at all, and the test proves it.
- The message says what happened, on which resolver, with counts when there
  are counts.
- If the outcome can be undone by the system itself, say so in the UI. Deleting
  a signed record is local; a peer that still holds it will push it back on the
  next sync.
- Never add an action that registers a name, edits record content, or changes
  what `resolve` would answer. The dashboard reads and triggers; it is not a
  second write path into the namespace.

## The two gates, and why both

1. **Peer plus identity.** A request is accepted from loopback, or from a
   trusted proxy (`RESOLVE_ADMIN_TRUSTED_PROXIES`) carrying
   `X-Authentik-Username`. A LAN client hitting the port directly is refused
   even if it forges the header, because its peer address is not a trusted
   proxy. Test: `AccessControlTest.test_lan_client_is_rejected_even_with_a_forged_header`.
2. **Loopback writes.** Every mutating call lands on a resolver's 127.0.0.1
   port, which is unreachable off-box by construction. Even a total failure of
   gate 1 does not hand a remote party a lever the box does not already give
   them.

Do not weaken either gate for convenience, and do not put a record listing on
the public health port to save a hop.

## Multiple resolvers

The dashboard is built for more than one resolver, because the live deployment
runs a mutually peered pair and the design expects a third. A dead resolver
must degrade to a card with an error, never to a broken page. When adding an
aggregation, decide what it means when one resolver is missing, and make that
visible rather than silently averaging it away.
