# rns-resolve agent overview

Project brief for automated agents and contributors. Conventions and task
skills live under `.agents/`. This file is the durable source of truth for
architecture and invariants.

## What this project is

Human-readable name resolution for Reticulum, built so that adopting it costs
a client one branch in its URL parser and gives a resolver no authority it did
not earn. There is no root zone, no registrar, and no name that a resolver can
mint on someone else's behalf.

Three layers, in the order a client should try them:

1. **Classify.** A 32-hex string is already a destination hash. It is returned
   as-is and never sent to a resolver.
2. **Petnames.** A local, user-owned table (`~/.rns_resolve/petnames.json`)
   with trust-on-first-use pinning. A pinned name resolves with no network at
   all, and a changed hash warns rather than silently following.
3. **Resolver.** Only for input that never looked like a hash. The resolver
   answers with registered records first, then, optionally, announce-heard
   candidates carrying their evidence so the caller can choose.

Source: `wdunn001/rns-resolve` (Forgejo canonical, GitHub public mirror).

## Runtime shape

```
client (CLI, NomadNet patch, MeshChat)
    |  RNS Link -> Request on aspect rnsresolve.query, path "q", umsgpack
    v
ResolveService (rns_resolve/service.py)
    +-- Store           sqlite records table (rns_resolve/store.py)
    +-- records         canonical bytes, signatures, pubkey binding
    +-- BeaconSource    READ-ONLY announce candidates from the Beacon DB
    +-- PeerScheduler   offer/want/push replication + withholding audit
    +-- HTTP :8225      /healthz, public, no record data
    +-- HTTP :8226      loopback only: /resolve /register /unregister /owned
                        and /admin/* for the dashboard
         ^
         |  loopback HTTP
    NomadNet node pages (pages/*.mu)        admin dashboard (rns_resolve/admin.py)
```

The live deployment runs two mutually peered resolvers plus an announced
NomadNet node, in `homelab-compose/rns-resolve-stack`. Resolver A is
Beacon-backed; resolver B is registered-records-only on purpose, which proves
the service is useful without a crawler index.

## Wire operations

| Op | Meaning |
| --- | --- |
| `__manifest__` | MeshAPI 0.1 self-description. Version tolerant, answered before the version check. |
| `resolve` | Registered records for a name, then Beacon announce candidates ranked with trust evidence. |
| `register` | Requires an identified link. The target is **derived** from the caller's identity hash, so a caller can only register names for destinations it owns. |
| `whois` | Reverse lookup: names claiming a target. |
| `sync.offer` / `sync.push` | LXMPeer-shaped replication. Off unless `RESOLVE_PEERS` is set. Requires an identified link and a peering stamp. |
| `sync.inventory` / `sync.fetch` | Withholding audit: list what we hold, serve records by id. Same stamp gate and caps. |

## Storage

`records` in sqlite (WAL, autocommit, one lock): `id, name, identity, app,
aspects, target, ts, ttl, sig, attested, last_used, pubkey`.

- `sig` present means **self-certifying**: signed by the registrant, verifiable
  by anyone, replicable to peers.
- `sig` NULL means **attested**: accepted locally through the node page. Never
  replicated, never offered to a peer.
- `pubkey` embeds the registrant's public key, hash-bound to the identity
  field. It exists because `Identity.recall` only knows identities that have
  announced, so a registrant who never announced was unverifiable by a peer.
  Peers verify standalone first and fall back to recall.

## Ports and environment

| Env | Default | Meaning |
| --- | --- | --- |
| `RESOLVE_DB` | `/data/resolve.db` | sqlite record store |
| `RESOLVE_RNS_CONFIG` | `/config` | Reticulum config dir. Holds `resolve_identity`, the trust anchor. |
| `RESOLVE_HEALTH_PORT` | `8225` | public `/healthz`. No record data in the reply. |
| `RESOLVE_PRIVATE_PORT` | `8226` | loopback only: node pages and the dashboard |
| `RESOLVE_PEERS` | empty | peer resolver hashes. Empty disables replication. |
| `RESOLVE_PEERING_COST` | `18` | LXStamper peering cost. 0 disables stamping. |
| `RESOLVE_SYNC_FROM` | unset | inbound sync allowlist of identity hashes |
| `RESOLVE_AUDIT_INTERVAL` / `_GRACE` / `_STRIKES` | 3600 / 1800 / 3 | withholding audit cadence and patience |
| `BEACON_DB_*` | unset | optional read-only announce-candidate source |
| `RESOLVE_ADMIN_*` | see `docs/ADMIN.md` | dashboard process |

The identity file in the config volume **is** the service's identity. Losing
or regenerating it changes the destination hash every client has pinned, which
is indistinguishable from an impostor. Never regenerate it to fix a problem.

## Invariants

1. Resolvers answer only queries RNS itself cannot express. Hash-shaped input
   never reaches a resolver. See `skills/trust-invariants/SKILL.md`.
2. `register` derives the target from the caller's identity. No code path may
   accept a caller-supplied target.
3. Attested records never replicate. Only signed records are offered, pushed,
   fetched, or served to a peer.
4. Beacon access is read-only, and the service degrades to registered-only
   when the Beacon database is unreachable rather than failing.
5. Announce candidates are returned with evidence and are never auto-selected
   on a client's behalf.
6. A flagged peer is disclosure, not enforcement. Nothing is auto-banned,
   because withholding and being partitioned look identical from inside.
7. The public health port exposes no record data. Record listings live behind
   the loopback port only.

## Known traps

- `Identity.load_public_key` returns `None` **on success**. Judge the result by
  `ident.hash`, never by the return value.
- `Transport.request_path()` takes fixed-length bytes. A name cannot be carried
  as a path request, so never encode one into it. That is a structural reason
  the trust boundary holds, not just a policy.
- Mesh announce names are decorative unicode (for example Mathematical
  Sans-Serif Bold). SQL `ILIKE` can never match an ASCII query against them.
  `beacon_source` pulls candidates and matches in Python on NFKC casefold.
- A stamped resolver rejects an unstamped peer, so both ends of a peering pair
  must ship together.
- Two resolvers give the withholding audit no majority. It repairs gaps but
  cannot accuse until a third joins.

## Commands

```
py -3 -m pytest tests -q          # full suite, no network, no RNS needed
py -3 -m pytest tests/test_admin.py -q
pip install -e '.[service,admin]' # service extras: psycopg2-binary, lxmf
python -m rns_resolve.service     # a resolver
python -m rns_resolve.admin       # the operator dashboard
python -m rns_resolve <name>      # the client
```

On the Windows dev box the interpreter is `py -3`, not `python`.
