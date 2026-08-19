# rns-resolve, module contracts (v0 prototype)

Human-readable name resolution for Reticulum. A resolver is an ordinary RNS
destination; clients consult it ONLY for input that never matched the
destination-hash pattern. This file is the binding interface contract for all
modules. Do not deviate from signatures or wire shapes without updating this
file.

## Global conventions

- Python 3.11+, stdlib-first. Allowed third-party deps: `rns` (RNS),
  umsgpack (import fallback: `import umsgpack` then
  `from RNS.vendor import umsgpack`), `psycopg2` (beacon_source only).
- Wire encoding: msgpack everywhere (family convention, matches RNS/LXMF).
- A destination hash is 16 bytes, rendered as 32 lowercase hex chars.
  `HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")` (defined in records.py,
  imported everywhere else).
- Service identity: app name `rnsresolve`, aspect `query`, request path `q`.
- Env vars (service): `RESOLVE_DB` (default `/data/resolve.db`),
  `RESOLVE_HEALTH_PORT` (default 8225), `RESOLVE_PRIVATE_PORT` (default
  8226, binds 127.0.0.1 ONLY), `RESOLVE_RNS_CONFIG` (default `/config`),
  `RESOLVE_PEERS` (comma-separated peer resolver dest hashes, default empty
  = replication off), `BEACON_DB_HOST/PORT/NAME/USER/PASSWORD` (optional;
  absent or unreachable → announce candidates degrade to empty, service
  stays up).
- NEVER hardcode internal IPs anywhere in this repo. Backends come from env.
- No em dashes in any file (house style). ASCII quotes in code.

## Trust model (summary all modules must respect)

1. Resolvers are consulted only for non-hash-shaped input. A valid
   32-hex-char string is NEVER sent to a resolver by the client.
2. `register` proves ownership by DERIVATION: the service computes the
   target destination hash from the caller's own verified identity hash
   plus the declared app/aspects. You can only register names for
   destinations your identity generates. No forged targets possible.
3. Records registered over an identified RNS Link with a client-side
   detached signature are SELF-CERTIFYING (replicable). Records registered
   via the local NomadNet page (no client key available) are
   RESOLVER-ATTESTED (sig=None, never replicated to peers).
4. resolve answers are ranked CANDIDATES with evidence, never one
   authoritative answer. The client pins locally (TOFU).

## Record shape (records.py owns this)

A record is a plain dict:

```python
{
  "v": 1,
  "name": str,        # normalized (see below)
  "identity": str,    # 32 hex chars, registrant identity hash
  "app": str,         # e.g. "nomadnetwork" (default)
  "aspects": list,    # e.g. ["node"] (default)
  "target": str,      # 32 hex chars, DERIVED dest hash (service fills)
  "ts": float,        # registration unix time (service fills at accept)
  "ttl": int,         # seconds; default 30*86400; clamp 3600..365*86400
  "sig": bytes|None,  # detached signature over canonical_bytes(), or None
}
```

- Name normalization (`normalize_name(s) -> str`, raises `ValueError`):
  lowercase, NFC unicode normalize, allowed chars `[a-z0-9._-]`, labels
  split on `.`, max 3 labels, each label 1..32 chars, total <= 64 chars,
  no leading/trailing `-` or `.` per label.
- `canonical_bytes(rec) -> bytes`: msgpack of the list
  `[1, name, identity, app, aspects, ts, ttl]` (EXCLUDES target and sig;
  target is derived, sig covers the rest).
- `record_id(rec) -> str`: sha256(canonical_bytes)[:16].hex(), the
  fixed-width diff key used by store + peers.
- `derive_target(identity_hash_hex, app, aspects) -> str`: uses
  `RNS.Destination.hash(bytes.fromhex(identity_hash_hex), app, *aspects)`
  and returns 32-hex. (This call pattern with raw identity-hash bytes is
  proven elsewhere in this ecosystem.) Import RNS lazily inside the
  function so records.py works without RNS installed (tests).
- `sign_record(rec, identity) -> bytes` (client side, has private key) and
  `verify_record(rec, identity) -> bool` (identity = RNS.Identity with
  public key; returns False on any exception). Lazy-import RNS.

## store.py

```python
class Store:
    def __init__(self, path: str): ...          # sqlite3, autocommit, WAL
    def put(self, rec: dict) -> str             # upsert by record_id; returns id
    def resolve(self, name_norm: str) -> list[dict]   # non-expired, newest ts first
    def prefix(self, name_prefix: str, limit=10) -> list[dict]
    def whois(self, target_hex: str) -> list[dict]
    def all_ids(self) -> list[str]              # for peer offers
    def get_many(self, ids: list[str]) -> list[dict]
    def missing(self, ids: list[str]) -> list[str]    # subset not in store
    def expire_sweep(self) -> int               # delete past ts+ttl; count
    def count(self) -> int
    def touch_use(self, record_id: str) -> None # bumps last_used (lease renewal signal)
```

Schema: `records(id TEXT PRIMARY KEY, name TEXT, identity TEXT, app TEXT,
aspects TEXT, target TEXT, ts REAL, ttl INTEGER, sig BLOB, attested INTEGER,
last_used REAL)`. `attested=1` means resolver-attested (sig is NULL); these
are excluded from `get_many` results handed to peers.
Index on name and on target.

## beacon_source.py

```python
class BeaconSource:
    def __init__(self, env: dict | None = None): ...  # reads BEACON_DB_* from env
    def available(self) -> bool
    def candidates(self, q: str, limit: int = 10) -> list[dict]
```

Read-only SELECTs against the `nodes` table:
`nodes(dest_hash text pk, name text, first_seen tz, last_seen tz,
announce_count int, last_crawled tz, reachable bool, last_reachable tz)`.
Candidate dict: `{"hash": str32hex, "name": str, "trust": float,
"last_seen": iso_str, "announce_count": int, "reachable": bool}`.
Match: case-insensitive substring AND whole-word bonus on `name`; rank by
`trust = ln(1+announce_count) * recency_decay(last_seen, halflife=7d)
* (1.15 if reachable else 0.85)`. Connection lazily opened, one retry,
every failure path returns [] and flips available() False until next call.
psycopg2 GOTCHA: no literal `%` in SQL text (use %% or avoid).

## service.py (daemon `resolved`)

- Attaches to the local RNS instance via the config dir at
  `RESOLVE_RNS_CONFIG` (the deploy config carries a TCPClientInterface to
  the host's local hub; service code just does
  `RNS.Reticulum(configdir=...)`). Persistent identity file at
  `<RESOLVE_RNS_CONFIG>/resolve_identity` (create if absent).
- Destination: `RNS.Destination(identity, IN, SINGLE, "rnsresolve",
  "query")`, `register_request_handler("q", handler,
  allow=RNS.Destination.ALLOW_ALL)`; announce at start + every 30 min.
  Set a link established callback so links can `identify()`.
- Request handler receives msgpack payload, returns msgpack reply.
  Ops (envelope `{"v":1, "op": ...}` except `__manifest__` which is
  version-tolerant and checked FIRST):
  - `{"op":"__manifest__"}` → `{"ok":True,"manifest":MANIFEST}` where
    MANIFEST follows the MeshAPI 0.1 shape:
    `{"meshapi":"0.1","service":{"name":"rns-resolve","summary":...,
    "app":"rnsresolve","aspect":"query","path":"q","dest":<hex>,
    "encoding":"umsgpack","source":"https://github.com/wdunn001/rns-resolve"},
    "ops":[...]}` with request field types in the MeshAPI type
    mini-language (`str!`, `int?` etc).
  - `{"v":1,"op":"resolve","q":str,"limit":int?}` → normalize q; reply
    `{"ok":True,"q":q_norm,"registered":[record_public...],
    "announced":[candidate...]}`. record_public = record minus sig, plus
    `{"id":record_id,"expires":ts+ttl}`. Also `touch_use` each returned
    registered record. If q fails normalization → `{"ok":False,"err":...}`.
  - `{"v":1,"op":"register","name":str,"app":str?,"aspects":list?,
    "ts":float,"ttl":int?,"sig":bytes}`, REQUIRES identified link
    (`link.get_remote_identity()`; if None →
    `{"ok":False,"err":"identify required"}`). Build rec with identity =
    remote identity hash hex; verify sig with the remote Identity; derive
    target; store; reply `{"ok":True,"record":record_public}`.
  - `{"v":1,"op":"whois","hash":str}` → registered records for target +
    (if beacon available) the announce-name row for that hash.
  - Per-link rate limit: max 30 requests/min, then `{"ok":False,
    "err":"rate limited"}`.
- Health HTTP (0.0.0.0:RESOLVE_HEALTH_PORT) GET `/healthz`:
  `{"status":"ok","rns_ready":bool,"records":int,"beacon_db":bool,
  "dest":<hex>}` HTTP 200 when rns_ready else 503.
- Private HTTP (127.0.0.1:RESOLVE_PRIVATE_PORT), for colocated NomadNet
  exec pages only: GET `/resolve?q=...&limit=` → same shape as resolve op
  (JSON); POST `/register` JSON `{"name":..., "identity": hex32,
  "app"?, "aspects"?}` → attested record (sig None, attested=1), reply
  JSON like register op. POST `/unregister` JSON `{"name","identity"}` →
  deletes matching records owned by that identity.
- Startup: Store, BeaconSource, optional peers (peers.start_scheduler(...)
  only if RESOLVE_PEERS non-empty), expire_sweep hourly in the announce
  loop thread.
- main() entry: `python -m rns_resolve.service`.

## client.py + __main__.py

CLI: `python -m rns_resolve <query> [--resolver HEX32] [--config DIR]
[--rns-config DIR] [--register NAME] [--app APP] [--aspects a,b]
[--ttl SECONDS] [--pin N] [--repin] [--json] [--timeout SECS]`

Flow for `<query>`:
1. If HASH_RE matches → print it back with kind "hash" and exit 0 (never
   query the resolver; this is the trust invariant).
2. petnames.get(normalized) hit → print pinned hash, kind "petname". Done,
   zero network.
3. Miss → require `--resolver` (or env `RESOLVE_RESOLVER`); RNS init with
   `--rns-config` dir; `RNS.Transport.request_path` + wait for the
   RESOLVER's real dest (that is a legitimate hash lookup); open Link;
   send `resolve` op via `link.request("q", msgpack_bytes)`; collect reply.
4. Print ranked candidates (registered first, then announced) as a table:
   idx, kind, name, hash, evidence (expires/trust/last_seen).
5. `--pin N` pins candidate N into petnames. Auto-pin ONLY when there is
   exactly one registered candidate and zero announced conflicts.
6. TOFU: if the name is already pinned and a fresh resolve (forced by
   `--repin`-less explicit resolve when pinned names skip the network, so only on `--repin`) returns a different hash → print a loud
   `NAME/HASH CHANGED` warning; require `--repin` to overwrite.

`--register NAME`: identify on the link (client identity file at
`<--config>/client_identity`, auto-create), build rec (ts=time.time(),
ttl), sign with own identity, send register op, print stored record.

Client NEVER dials any hardcoded host: RNS config dir is the user's own.

## petnames.py

```python
class PetnameTable:
    def __init__(self, path: str | None = None): ...  # default ~/.rns_resolve/petnames.json
    def get(self, name_norm: str) -> dict | None      # {"hash","source","first_seen","last_verified"}
    def pin(self, name_norm: str, hash_hex: str, source: str) -> None
    def unpin(self, name_norm: str) -> bool
    def changed(self, name_norm: str, hash_hex: str) -> bool  # pinned and different
    def all(self) -> dict
```
Atomic writes (tmp file + os.replace is fine here, plain file path).
Corrupt/missing file → start empty, never crash.

## peers.py (replication skeleton, LXMPeer-shaped)

Offer/want over the same request path "q" using ops:
- `{"v":1,"op":"sync.offer","ids":[...]}` → `{"ok":True,"want":[subset]}`
- `{"v":1,"op":"sync.push","records":[rec_with_sig...]}` →
  `{"ok":True,"accepted":n,"rejected":n}`
Acceptance rule: only self-certifying records (sig present); verify via
`RNS.Identity.recall(bytes.fromhex(rec["identity"]))`; unrecallable
identity → reject (count it). Re-derive target before storing (never trust
the pushed target field). attested records are never pushed.

```python
class PeerScheduler:
    def __init__(self, store, peer_hashes: list[str], rns_owner): ...
    def start(self): ...   # daemon thread, sync each peer every 15 min
    def sync_peer(self, hash_hex) -> dict  # {"offered":n,"pushed":n,...}
    def stop(self): ...
```
Backoff: unreachable peer → double interval up to 4h; reset on success.
Service wires the two sync ops into its handler by calling
`peers.handle_sync(op, payload, store)` (pure function, returns reply dict
or None if not a sync op).

## pages/ (NomadNet exec pages, run ON the resolver node)

`pages/index.mu` (executable): lookup form. Reads `field_q` OR `var_q`,
calls `http://127.0.0.1:8226/resolve?q=`, renders ranked results in micron.
`pages/register.mu` (executable): shows the caller's verified
`remote_identity` env value; field for desired name (+ optional app/aspect
preset choices as links); POSTs to `/register` with that identity; renders
the stored record incl derived target; also an unregister link for names
owned by that identity.
Micron client-compat laws (HARD, from production scars): no literal
link-vars on submit links (enumerate bare field names + a `step` text
marker); no field named `name` (use `rname`, map server-side...); actually
the HTTP API uses "name" internally, only the FORM FIELD must be `rname`;
no decorative square brackets in output; escape backticks in any echoed
user/backend text (`s.replace("`","\\`")`); guard lines starting with
`-`/`#`/`>` with a leading `\`. Read `var_x` first then `field_x`.
Both pages: `try: from beaconrum import track; track("rns-resolve", ...)
except Exception: pass`.
Python for pages: self-contained stdlib only (urllib), shebang
`#!/usr/bin/env python3`, executable bit documented in deploy README.

## patches/ + docs/ (integration deliverables)

- `patches/nomadnet-browser-resolve.patch`: unified diff against current
  upstream `nomadnet/ui/textui/Browser.py` (fetch the file from
  https://raw.githubusercontent.com/markqvist/NomadNet/master/nomadnet/ui/textui/Browser.py
  to build an accurate diff): in BOTH url-parse sites, the
  `else: raise ValueError("Malformed URL")` branch first attempts
  `rns_resolve` (petname table, then resolver query if
  `RNS_RESOLVE_RESOLVER` env set), falling back to the original raise.
  Import guarded so NomadNet without rns_resolve behaves exactly as today.
- `docs/INTEGRATION.md`: how a client app adopts resolution (the
  classify/petname/resolver/TOFU order), the trust invariants, MeshChat
  notes (its CustomDestinationDisplayName table is the petname template;
  its parse site NOT yet located, verify before assuming), and the
  resolver-operator guide (env vars, ports, peering).
- `README.md` (repo root): what/why (naming for Reticulum without a
  registrar), the trust model, quickstart for users (client) and operators
  (service), link to Discussion #965 context, MIT license note, explicit
  "prototype" framing. No internal hostnames/IPs.

## tests/

Each module agent writes `tests/test_<module>.py` (stdlib unittest, no
network, no RNS required: skipUnless(RNS importable) for RNS-dependent
paths; sqlite in tmpdir; petnames in tmpdir). `python -m unittest
discover tests` must pass with only stdlib + (optionally) rns installed.

## File ownership (one agent per line, do not touch other files)

- A1: rns_resolve/records.py, tests/test_records.py
- A2: rns_resolve/store.py, tests/test_store.py
- A3: rns_resolve/beacon_source.py, tests/test_beacon_source.py (mock psycopg2)
- A4: rns_resolve/service.py, tests/test_service.py (handler logic w/ fakes)
- A5: rns_resolve/client.py, rns_resolve/__main__.py, tests/test_client.py
- A6: rns_resolve/petnames.py, tests/test_petnames.py
- A7: rns_resolve/peers.py, tests/test_peers.py
- A8: pages/index.mu, pages/register.mu
- A9: patches/nomadnet-browser-resolve.patch, docs/INTEGRATION.md, README.md, LICENSE (MIT, wdunn001)
