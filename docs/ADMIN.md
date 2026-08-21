# Operator dashboard and management

`rns_resolve.admin` is a small web app for the person who runs resolvers. It
answers the questions the `/healthz` JSON could not: what is in the store,
who registered it, is replication actually working, what are clients asking
for, and it gives you the handful of actions worth having a button for
(sync a peer now, announce now, run an audit round, delete a record).

It is a separate process from the resolver, holds no state, and talks only
to resolvers' loopback APIs. One dashboard fronts any number of resolvers.

```
browser --https--> Caddy (Authentik forward-auth) --> admin :8229
                                                        |
                             127.0.0.1:8226 /admin/*  <-+-> resolver A
                             127.0.0.1:8228 /admin/*      resolver B
```

## Pages

| Path | What it shows |
|---|---|
| `/` | One card per resolver: up/RNS ready, dest and identity, record count, uptime, announce cadence, request counters by op, recent queries, per-peer sync state (last result, next due, backoff), withholding-audit state and suspects. Buttons: announce now, sync all peers, sync one peer, audit now. |
| `/records` | Every record across resolvers, newest first. Filter by substring (name, target or registrant), by resolver, live-only or including expired. Shows signed vs attested, whether the record carries a bound pubkey, registered/expires/last-used, and a confirm-then-delete control. |
| `/lookup` | Run a name through a resolver's own `/resolve`, exactly as a client would see it: registered records first, then Beacon announce candidates with their trust evidence. Use it to check what a name actually returns before telling someone to use it. |
| `/api/overview` | The dashboard's data as JSON, for scripting or a status page. |
| `/healthz` | Unauthenticated liveness for Gatus (no resolver data in the reply). |

## Access control

The app is internal-only and gated twice.

1. It accepts a request only from **loopback** (an operator on the box) or
   from a **trusted proxy** (`RESOLVE_ADMIN_TRUSTED_PROXIES`, default the
   .88 Caddy) **carrying `X-Authentik-Username`**. A LAN client that hits
   :8229 directly gets 403 even if it forges the header, because the peer
   address is not a trusted proxy.
2. Every write goes to a resolver's **127.0.0.1** private port, which is not
   reachable off-box at all. The dashboard cannot be a remote-control lever
   for anyone who does not already have the box.

`/healthz` is the only open route so a monitor can probe it.

## Configuration

| Env | Default | Meaning |
|---|---|---|
| `RESOLVE_ADMIN_PORT` | `8229` | listen port |
| `RESOLVE_ADMIN_HOST` | `0.0.0.0` | listen address |
| `RESOLVE_ADMIN_RESOLVERS` | `A=http://127.0.0.1:8225\|http://127.0.0.1:8226,B=...8227\|...8228` | `NAME=health_url\|private_url` list. The private url defaults to the health port plus one, matching the stack convention. |
| `RESOLVE_ADMIN_TRUSTED_PROXIES` | `192.168.1.88` | proxies whose Authentik-identified requests are accepted |
| `RESOLVE_ADMIN_NODE_HASH` / `_NODE_NAME` | unset | the NomadNet node to display alongside the resolvers |
| `RESOLVE_ADMIN_TIMEOUT_S` | `8` | per-request timeout to a resolver |
| `RESOLVE_ADMIN_PAGE_SIZE` | `200` | default records page size |

Install and run:

```
pip install 'rns-resolve[admin]'
RESOLVE_ADMIN_RESOLVERS='A=http://127.0.0.1:8225' python -m rns_resolve.admin
```

## Resolver-side surface it depends on

The dashboard is a client of endpoints the resolver now exposes on its
**private (loopback) port**, alongside the existing `/resolve`, `/register`,
`/unregister` and `/owned` used by the NomadNet pages:

| Endpoint | Purpose |
|---|---|
| `GET /admin/status` | service identity and config, uptime, announce bookkeeping, per-peer sync state, audit state, request metrics |
| `GET /admin/records?q=&limit=&offset=&expired=` | paged record listing with operator-only fields (id, ttl, expiry, attested, pubkey bound, last used) |
| `POST /admin/records/delete {"id"}` | delete one record by id, regardless of registrant |
| `POST /admin/sync {"peer"?}` | sync one peer now, or all |
| `POST /admin/announce` | announce the resolver destination now |
| `POST /admin/audit` | run one withholding-audit round now (when the build has the MOFU audit) |

Two honest caveats the UI states as well:

* **Deleting a signed record is local and can be undone by replication.** A
  self-certifying record you delete here comes back on the next sync from a
  peer that still holds it. To retire a name, delete it on every resolver
  that holds it, or let it expire; the registrant is the only party who can
  authoritatively unregister it.
* **Flagged peers are evidence, not a verdict.** Withholding and being
  partitioned look identical from inside. The dashboard surfaces suspects so
  an operator can decide; nothing is auto-banned. With only two resolvers
  there is no majority, so the audit repairs gaps but cannot accuse.

## Tests

`tests/test_admin.py` covers settings parsing, the resolver client (with an
injected transport, so no sockets), card shaping, registry aggregation
including a dead resolver, the access-control rule, and every route through
`fastapi.testclient`. `tests/test_admin_api.py` covers the resolver-side
store listing/deletion, metrics, the `admin_*` handlers and the scheduler's
sync state. FastAPI and jinja2 are an optional extra; the app tests skip
without them, the logic tests always run.
