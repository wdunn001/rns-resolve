# Integrating rns-resolve

How a Reticulum client application adopts human-readable names, and how an
operator runs a resolver. This document describes the v0 prototype; see
[Reticulum Discussion #965](https://github.com/markqvist/Reticulum/discussions/965)
for the wider design conversation.

## The adoption order (do these in exactly this order)

Any client integration follows one fixed pipeline for user-supplied
destination input:

1. **Classify.** If the input matches the destination-hash pattern
   (`^[0-9a-fA-F]{32}$`, i.e. `rns_resolve.records.HASH_RE`), it IS a hash.
   Use it directly. Stop. A hash-shaped input is never sent to any
   resolver under any circumstances.
2. **Petnames.** Normalize the input with
   `rns_resolve.records.normalize_name` and look it up in the local
   `rns_resolve.petnames.PetnameTable`. A hit returns the pinned hash with
   zero network traffic. Stop.
3. **Resolver.** Only on a petname miss, and only if the user has
   configured a resolver (client CLI: `--resolver` or env
   `RESOLVE_RESOLVER`; NomadNet patch: env `RNS_RESOLVE_RESOLVER`), open a
   link to that resolver destination and send the `resolve` op. Note that
   looking up the path to the resolver's own destination hash is a
   legitimate hash lookup, not a name resolution.
4. **Present candidates, then TOFU.** Show the ranked candidates
   (registered records first, then announce-derived candidates) with their
   evidence. Let the user pick; pin the pick into the petname table.
   Auto-pin only when there is exactly one registered candidate and zero
   announced conflicts. Once pinned, resolution is local until the user
   explicitly re-resolves; if a re-resolve returns a different hash than
   the pin, warn loudly and require explicit confirmation (`--repin`)
   before overwriting.

## Trust invariants your integration must keep

- Never send hash-shaped input to a resolver. The classify step is the
  entire defense against a resolver rewriting destinations the user
  already knows.
- Never treat a resolve answer as authoritative. It is evidence-ranked
  candidates from one resolver's point of view.
- Never silently update a pinned name. A changed hash for a pinned name is
  a loud event, not a background refresh.
- Registration targets are derived, not declared. If you build a
  registration UI, send the identity and the app/aspects; the service
  computes the target with
  `RNS.Destination.hash(identity_hash_bytes, app, *aspects)`. Do not
  invent UI for choosing an arbitrary target hash; the protocol has no
  such field.

## Operator guide

### Environment

| Variable | Default | Notes |
| --- | --- | --- |
| `RESOLVE_DB` | `/data/resolve.db` | sqlite3 store, WAL mode |
| `RESOLVE_RNS_CONFIG` | `/config` | Reticulum config dir; the service does `RNS.Reticulum(configdir=...)` and keeps its identity at `<dir>/resolve_identity` |
| `RESOLVE_HEALTH_PORT` | `8225` | binds 0.0.0.0; `GET /healthz` returns JSON status, HTTP 200 when RNS is ready, else 503 |
| `RESOLVE_PRIVATE_PORT` | `8226` | binds 127.0.0.1 ONLY; JSON API (`GET /resolve`, `POST /register`, `POST /unregister`) for colocated NomadNet exec pages. Never expose this port off-host: records created here are resolver-attested on your resolver's say-so |
| `RESOLVE_PEERS` | empty | comma-separated destination hashes of peer resolvers; empty disables replication |
| `BEACON_DB_HOST/PORT/NAME/USER/PASSWORD` | unset | optional read-only Postgres source of announce-observed names; absent or unreachable degrades announced candidates to empty, service stays up |

Backends always come from environment variables. Never bake addresses into
images or code.

### Ports

- `8225` is a health endpoint safe to expose to your monitoring.
- `8226` is a trusted local control surface. It exists because NomadNet
  exec pages run on the same host and have the visitor's verified
  `remote_identity` available but no way to produce a client-side
  signature. Keep it loopback.

### Peering (`RESOLVE_PEERS`)

Set `RESOLVE_PEERS` to a comma-separated list of peer resolver destination
hashes to enable LXMF-propagation-style replication:

- Every 15 minutes per peer, the scheduler offers its record ids
  (`sync.offer`), learns which the peer wants, and pushes them
  (`sync.push`). Unreachable peers back off up to 4 hours.
- Only self-certifying records (signature present) are ever pushed.
  Resolver-attested records (registered via the local page, `sig=None`)
  never leave the resolver that attested them.
- A receiving resolver verifies each pushed record's signature against the
  registrant identity (which must be recallable via
  `RNS.Identity.recall`) and re-derives the target itself. The pushed
  target field is never trusted.

Peering is optional and off by default. A standalone resolver is a
perfectly valid deployment.

### Registering a node's name (the intended flow)

Registration belongs in node setup, not in a page visit. A NomadNet node
already holds both halves a registration needs, its identity file and its
configured `node_name`, so claiming the name is one command in the node's
deploy (or a small sidecar that renews the lease):

```sh
python -m rns_resolve.nodereg \
    --identity /path/to/nomadnet/storage/identity \
    --nomadnet-config /path/to/nomadnet/config \
    --resolver <resolver hash> \
    --interval 86400   # optional: renew daily; omit for one-shot
```

The name derives from `node_name` automatically (decorative unicode and
emoji fold away: a node announcing as bold `RNS-RESOLVE` with a compass
registers as `rns-resolve`; override with `--name`). Because the node's
own private key signs the record, setup registrations are self-certifying
and replicate between resolvers. The `register.mu` page remains the manual
fallback for visitors without their keys at hand; those records are
resolver-attested and never replicate.

### NomadNet pages

`pages/index.mu` (lookup) and `pages/register.mu` (attested registration)
are executable micron pages that call the loopback API on port 8226. Serve
them from the same host as the resolver daemon. See the deploy README for
the executable-bit and pagesdir details.

## NomadNet browser patch

`patches/nomadnet-browser-resolve.patch` is a unified diff against upstream
`nomadnet/ui/textui/Browser.py` (fetched from `markqvist/NomadNet` master,
2026-08-18). Apply from a NomadNet checkout root:

```sh
git apply patches/nomadnet-browser-resolve.patch
# or: patch -p1 < patches/nomadnet-browser-resolve.patch
```

What it does:

- Adds a guarded import of `rns_resolve` at the top of the module. If the
  package is not installed, a flag stays False and every added code path is
  inert: stock NomadNet behavior is byte-for-byte identical at runtime.
- Both URL-parse sites in the file (`parse_url` and `retrieve_url` contain
  duplicated parsing logic) gain a fallback in their
  `else: raise ValueError("Malformed URL")` branches, for both the
  bare-name form (`myname`) and the name-with-path form (`myname:/page/x.mu`).
  These branches are only reached when the input already failed the
  destination-hash shape check, so the classify invariant holds by
  construction.
- The fallback checks the local petname table first (zero network). Only
  if that misses AND the `RNS_RESOLVE_RESOLVER` environment variable is
  set does it attempt a resolver query, by calling
  `rns_resolve.client.resolve_name(name, resolver_hash)` if the client
  module exposes it (the lookup is via `getattr`, so an older or partial
  install degrades cleanly). Any failure anywhere falls through to the
  original `ValueError("Malformed URL")`.

The patch never auto-pins from the browser path. Pinning stays an explicit
CLI action so the TOFU decision is always the user's.

## MeshChat notes

[Reticulum MeshChat](https://github.com/liamcottle/reticulum-meshchat)
already ships the right local half of this design: its
`CustomDestinationDisplayName` table maps a destination hash to a
user-chosen display name. That is exactly a petname table, and it is the
template an rns-resolve integration should extend rather than replace:

- Local pins (step 2 of the adoption order) could read and write the same
  table, so names the user already assigned keep working.
- A resolver query (step 3) would only fire for typed input that is not
  hash-shaped and has no local display-name entry.

**Unverified:** we have not yet located MeshChat's equivalent URL/input
parse site (the code path where a typed non-hash destination is rejected),
so no patch is provided and the integration point above is a hypothesis.
Verify against MeshChat's actual source before assuming any of this
applies. Contributions welcome.

## Prototype caveats

This is a v0 prototype. The record format, ranking heuristics, replication
acceptance rules, and CLI flags are all subject to change. Treat any
deployment as an experiment. MIT licensed; see `LICENSE`.
