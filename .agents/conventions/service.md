# Service conventions

Applies when editing `rns_resolve/*.py` other than `admin*.py`.

## Shape

- `service.py` holds pure request handlers plus the RNS and HTTP wiring. The
  handlers take `(payload, deps)` and return a plain dict, so they are testable
  with fakes and no sockets. Keep new ops in that shape.
- `Deps` carries the store, Beacon source, manifest, rate limiter, sync handler
  and metrics. Add a collaborator to `Deps` rather than reaching for a module
  global.
- `store.py` owns all SQL. No SQL anywhere else. Every method takes and returns
  plain dicts, holds `self._lock` for the statement, and never leaks a cursor.
- `records.py` owns canonical bytes, signing, and verification. Any change to
  what goes into a record's canonical form changes every record id in the
  world, so treat it as a wire-format change.
- `peers.py` owns replication and the audit. It reaches RNS only through small
  seams (`_open_link`, `_request`, `_close_link`, `_recall_identity`,
  `_verify_record`, `_derive_target`) so tests can stub them.
- `beacon_source.py` is read-only. It must never write to the Beacon database
  and must degrade to returning nothing when the database is unreachable.

## Rules

- Version check: every op except `__manifest__` requires `v == 1`.
  `__manifest__` stays version tolerant so a future client can introspect us.
- Rate limiting is keyed on the link identity and applied before any work.
- Signature verification order is standalone first (using the record's embedded
  pubkey, hash-bound to the identity field), then `Identity.recall` as a
  fallback. `Identity.load_public_key` returns `None` on success, so judge by
  `ident.hash`.
- `register` derives the target with
  `RNS.Destination.hash(bytes.fromhex(rid), app, *aspects)`. There is no other
  acceptable source for a target.
- Anything that could replicate must be signed. `Store.all_ids` and
  `get_many` already exclude attested records; keep new peer-facing queries
  consistent with that and do not widen them.
- Long-running loops (announce, sweep, peer sync) live on daemon threads and
  wait on `self._stop`, so a stop is prompt and a crash in one loop does not
  take the process down.
- Sweep and announce update the operator bookkeeping fields
  (`last_announce_at`, `announce_count`, `last_sweep_expired`) because the
  dashboard reports them. Keep them accurate if you touch those loops.

## Adding an op

1. Write the handler as a pure function next to its peers in `service.py`.
2. Dispatch it in `_handle_request_dict`, after the version check unless it has
   a specific reason to be tolerant.
3. Decide replication and trust: does it read records, expose ids, or accept
   records? If it crosses a peer boundary it needs the stamp gate and payload
   caps that `sync.*` uses.
4. Add it to the manifest if a client should discover it.
5. Test it with fakes, including the rejection paths, not only the happy path.
