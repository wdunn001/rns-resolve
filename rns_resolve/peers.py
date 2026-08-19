"""Replication skeleton for rns-resolve, shaped after LXMF's LXMPeer.

Offer/want/push over the resolver's own request path "q":

  {"v":1, "op":"sync.offer", "ids":[...]}      -> {"ok":True, "want":[subset]}
  {"v":1, "op":"sync.push", "records":[...]}   -> {"ok":True, "accepted":n, "rejected":n}

Direction note: the scheduler OFFERS our record ids to a peer, the peer
replies with the subset it wants, and we PUSH those records to it. So
sync_peer() fills in records the PEER is missing.

Acceptance rule for pushed records (handle_sync, "sync.push"):
  - only self-certifying records (sig present) are accepted
  - the registrant identity must be recallable via RNS.Identity.recall
  - the detached signature must verify against that identity
  - the target is RE-DERIVED from identity+app+aspects; the pushed
    "target" field is never trusted
Anything failing any step is counted as rejected.

Resolver-attested records (sig None, attested=1) are never pushed:
Store.get_many excludes them, and sync_peer filters defensively.

All RNS and records.py calls are isolated in small module-level
functions / small methods so unit tests can stub them without RNS
installed.
"""

import threading
import time

# ---------------------------------------------------------------------------
# Small seams. Tests monkeypatch these module-level functions. Real imports
# are lazy so this module loads without RNS (and before records.py exists
# at build time, though it is required at runtime).
# ---------------------------------------------------------------------------


def _record_id(rec):
    from . import records
    return records.record_id(rec)


def _verify_record(rec, identity):
    from . import records
    return records.verify_record(rec, identity)


def _derive_target(identity_hash_hex, app, aspects):
    from . import records
    return records.derive_target(identity_hash_hex, app, aspects)


def _verify_standalone(rec):
    from . import records
    return records.verify_record_standalone(rec)


def _lxstamper():
    """LXMF's LXStamper module, or None. We import LXMF's implementation
    rather than reimplementing it: same workblock construction, same
    peering-key semantics (key material = receiver_identity.hash +
    sender_identity.hash, WORKBLOCK_EXPAND_ROUNDS_PEERING), so an audit of
    LXMF's stamps is an audit of ours."""
    try:
        from LXMF import LXStamper
        return LXStamper
    except Exception:
        return None


def _recall_identity(identity_hash_hex):
    """Recall a known RNS Identity by its hash. None if unknown."""
    import RNS
    try:
        return RNS.Identity.recall(bytes.fromhex(identity_hash_hex))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pure sync handler (wired into service.py's request handler)
# ---------------------------------------------------------------------------


# Mirrors LXMRouter.PEERING_COST / MAX_PEERING_COST.
DEFAULT_PEERING_COST = 18
MAX_PEERING_COST = 26
# Cheap DoS guards on sync payload sizes (LXMF budgets sync sizes via
# propagation_sync_limit; ours are simple hard caps).
MAX_OFFER_IDS = 5000
MAX_PUSH_RECORDS = 500
# sync.fetch: how many records one pull may ask for.
MAX_FETCH_IDS = 200

# --- MOFU (majority-of-peers) withholding audit ----------------------------
# A peer can pass every authentication check and still lie by OMISSION: it
# simply never offers records it holds. Signatures cannot catch that, because
# nothing is forged. The only way to see an omission is to compare a peer's
# claimed inventory against what its fellow peers hold.
#
# So each audit round asks every peer for its record-id inventory, forms the
# union as the "known universe", and calls a record EXPECTED when a majority
# of responding parties (peers plus ourselves) hold it. A peer that lacks an
# expected record is missing something its fellows agree exists.
#
# Two guards keep normal propagation lag from looking like malice:
#   GRACE   a record must be older than this before its absence counts, so a
#           just-registered record in flight is never evidence.
#   STRIKES the same peer must lack the same expected record across this many
#           CONSECUTIVE audits before it is flagged. One slow round is noise.
#
# The audit REPAIRS as well as accuses: anything in the universe we do not
# hold is pulled from a peer that does and validated exactly like a push, so
# one honest peer is enough to defeat another's withholding. Detection alone
# would be a complaint; the pull is the actual defence.
#
# Flagging is disclosure, never enforcement. A flagged peer is reported, not
# banned: withholding and being partitioned look identical from here, and the
# design's whole stance is to publish evidence and let a human decide.
AUDIT_INTERVAL = 60 * 60         # seconds between audit rounds per scheduler
AUDIT_GRACE = 30 * 60            # record age before its absence counts
AUDIT_STRIKES = 3                # consecutive rounds before a peer is flagged


def _sync_gate(payload, ctx):
    """Shared admission checks for sync ops. Returns an error reply dict,
    or None when the caller may proceed. Semantics follow LXMF's
    offer_request: identified link required, per-pair peering key validated
    at the receiver's advertised cost. Divergence from LXMF (documented in
    docs/LXMPEER-GAPS.md): we validate the key on every sync op instead of
    caching per-link validation state, because validation is one cheap
    25-round workblock and our handler is deliberately stateless."""
    ctx = ctx or {}
    cost = int(ctx.get("peering_cost") or 0)
    link_identity = ctx.get("link_identity")
    allowed = ctx.get("allowed_sync_identities")

    if allowed is not None:
        rid = None
        try:
            rid = link_identity.hash.hex()
        except Exception:
            pass
        if rid is None or rid not in allowed:
            return {"ok": False, "err": "not allowed"}

    if cost <= 0:
        return None

    if link_identity is None:
        return {"ok": False, "err": "identify required", "cost": cost}
    stamper = _lxstamper()
    if stamper is None:
        return {"ok": False, "err": "stamps unavailable"}
    key = payload.get("key")
    if not isinstance(key, (bytes, bytearray)):
        return {"ok": False, "err": "peering key required", "cost": cost}
    self_hash = ctx.get("self_identity_hash")
    try:
        peering_id = bytes(self_hash) + bytes(link_identity.hash)
    except Exception:
        return {"ok": False, "err": "stamps unavailable"}
    if not stamper.validate_peering_key(peering_id, bytes(key), cost):
        return {"ok": False, "err": "invalid peering key", "cost": cost}
    return None


def expected_records(inventories, own_ids):
    """Ids a MAJORITY of responding parties hold (peers plus ourselves).

    inventories: {peer_hex: set(ids)} for peers that ANSWERED this round.
    A peer that did not answer is absent from the vote entirely, so an
    unreachable peer neither accuses nor excuses anyone.
    """
    own_ids = set(own_ids or ())
    voters = len(inventories) + 1          # +1 for ourselves
    if voters < 2:
        return set()                       # nobody to compare against
    threshold = voters // 2 + 1            # strict majority
    universe = set(own_ids)
    for ids in inventories.values():
        universe |= set(ids)
    expected = set()
    for rid in universe:
        holders = sum(1 for ids in inventories.values() if rid in ids)
        if rid in own_ids:
            holders += 1
        if holders >= threshold:
            expected.add(rid)
    return expected


def withholding_candidates(inventories, own_ids, ages, now=None,
                           grace=AUDIT_GRACE):
    """{peer_hex: set(expected ids that peer lacks)} for THIS round.

    ages: {record_id: ts} for records whose age we know. An id whose age is
    unknown is treated as too young to count, because we cannot prove it has
    been around long enough to have propagated.
    """
    now = time.time() if now is None else now
    ages = ages or {}
    expected = expected_records(inventories, own_ids)
    settled = {rid for rid in expected
               if rid in ages and (now - float(ages[rid])) >= grace}
    out = {}
    for peer, ids in inventories.items():
        lacking = settled - set(ids)
        if lacking:
            out[peer] = lacking
    return out


class WithholdingAudit:
    """Tracks per-peer omissions across rounds and flags sustained ones."""

    def __init__(self, strikes=AUDIT_STRIKES):
        self.strikes_required = strikes
        # {peer_hex: {record_id: consecutive_rounds_missing}}
        self._strikes = {}
        self._last_round = {}

    def record_round(self, candidates):
        """Fold one round's candidates in. Returns {peer: flagged_ids}."""
        flagged = {}
        peers = set(self._strikes) | set(candidates)
        for peer in peers:
            missing = set(candidates.get(peer, ()))
            counts = self._strikes.get(peer, {})
            updated = {}
            for rid in missing:
                updated[rid] = counts.get(rid, 0) + 1
            # ids no longer missing reset to zero by simply being dropped
            self._strikes[peer] = updated
            self._last_round[peer] = sorted(missing)
            hits = {rid for rid, n in updated.items()
                    if n >= self.strikes_required}
            if hits:
                flagged[peer] = hits
        return flagged

    def state(self):
        """Serializable view for /healthz and operators."""
        out = {}
        for peer, counts in self._strikes.items():
            if not counts:
                continue
            out[peer] = {
                "missing_now": len(counts),
                "max_consecutive_rounds": max(counts.values()),
                "flagged": max(counts.values()) >= self.strikes_required,
                "sample": sorted(counts)[:5],
            }
        return out


def handle_sync(op, payload, store, ctx=None):
    """Handle a sync op. Returns a reply dict, or None if op is not a
    sync op. Pure function apart from store access. ctx (optional) carries
    the admission context built by the service: link_identity,
    self_identity_hash (bytes), peering_cost (int, 0 = open),
    allowed_sync_identities (set of hex identity hashes, None = open)."""

    if op == "sync.offer":
        gate = _sync_gate(payload, ctx)
        if gate is not None:
            return gate
        ids = payload.get("ids")
        if not isinstance(ids, list):
            return {"ok": False, "err": "bad offer"}
        if len(ids) > MAX_OFFER_IDS:
            return {"ok": False, "err": "offer too large"}
        clean = [i for i in ids if isinstance(i, str)]
        want = store.missing(clean)
        return {"ok": True, "want": want}

    if op == "sync.inventory":
        # Answer with the ids of every replicable record we hold. This is
        # what makes withholding visible: a peer's fellows can compare what
        # it claims to have against what they have. Attested records are
        # excluded (all_ids already does that) because they never replicate
        # and their absence elsewhere is correct, not suspicious.
        gate = _sync_gate(payload, ctx)
        if gate is not None:
            return gate
        ids = store.all_ids()
        return {"ok": True, "ids": ids, "count": len(ids)}

    if op == "sync.fetch":
        # Serve specific records by id, so a peer can PULL what it is
        # missing instead of waiting to be told. This is the repair half of
        # the audit: one honest holder defeats another peer's omission.
        gate = _sync_gate(payload, ctx)
        if gate is not None:
            return gate
        ids = payload.get("ids")
        if not isinstance(ids, list):
            return {"ok": False, "err": "bad fetch"}
        if len(ids) > MAX_FETCH_IDS:
            return {"ok": False, "err": "fetch too large"}
        clean = [i for i in ids if isinstance(i, str)]
        recs = [r for r in store.get_many(clean) if r.get("sig") is not None]
        return {"ok": True, "records": recs}

    if op == "sync.push":
        gate = _sync_gate(payload, ctx)
        if gate is not None:
            return gate
        recs = payload.get("records")
        if not isinstance(recs, list):
            return {"ok": False, "err": "bad push"}
        if len(recs) > MAX_PUSH_RECORDS:
            return {"ok": False, "err": "push too large"}
        accepted = 0
        rejected = 0
        for rec in recs:
            if _accept_pushed_record(rec, store):
                accepted += 1
            else:
                rejected += 1
        return {"ok": True, "accepted": accepted, "rejected": rejected}

    return None


def _accept_pushed_record(rec, store):
    """Validate one pushed record and store it. True if accepted."""
    try:
        if not isinstance(rec, dict):
            return False
        if rec.get("sig") is None:
            # Attested or unsigned records are never replicable.
            return False
        identity_hex = rec.get("identity")
        if not isinstance(identity_hex, str):
            return False
        # Preferred path: the record carries its registrant's public key and
        # verifies standalone (self-certifying; no prior knowledge needed).
        # Fallback: an announce-known identity via RNS.Identity.recall.
        if rec.get("pubkey"):
            if not _verify_standalone(rec):
                return False
        else:
            identity = _recall_identity(identity_hex)
            if identity is None:
                return False
            if not _verify_record(rec, identity):
                return False
        # Never trust the pushed target field; re-derive it.
        rec = dict(rec)
        rec["target"] = _derive_target(
            identity_hex, rec.get("app"), rec.get("aspects"))
        store.put(rec)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

SYNC_INTERVAL = 15 * 60          # base interval between syncs per peer
MAX_BACKOFF = 4 * 60 * 60        # cap for exponential backoff
LINK_TIMEOUT = 30                # seconds to wait for path/link/request


class PeerScheduler:
    """Periodically syncs our self-certifying records to peer resolvers.

    One daemon thread; each peer is synced every SYNC_INTERVAL seconds.
    An unreachable peer has its interval doubled (exponential backoff)
    up to MAX_BACKOFF; a successful sync resets it to SYNC_INTERVAL.
    """

    def __init__(self, store, peer_hashes, rns_owner, audit_interval=None,
                 audit_grace=None, audit_strikes=None):
        self.store = store
        self.peer_hashes = list(peer_hashes)
        self.rns_owner = rns_owner
        self.audit_interval = int(audit_interval or AUDIT_INTERVAL)
        self.audit_grace = int(audit_grace or AUDIT_GRACE)
        self._stop = threading.Event()
        self._thread = None
        self._interval = {h: SYNC_INTERVAL for h in self.peer_hashes}
        self._next_due = {h: time.time() for h in self.peer_hashes}
        # Per-peer peering keys, LXMPeer-style: {peer_hex: (key, value, cost)}.
        # Cheap to regenerate (25-round workblock), so memory-only.
        self._peer_keys = {}
        # MOFU withholding audit
        self.audit = WithholdingAudit(
            strikes=int(audit_strikes or AUDIT_STRIKES))
        self._next_audit = time.time() + self.audit_interval
        self._last_audit = None

    # -- lifecycle ----------------------------------------------------------

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="rns-resolve-peers", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self):
        while not self._stop.is_set():
            now = time.time()
            for peer in self.peer_hashes:
                if self._stop.is_set():
                    break
                if now >= self._next_due.get(peer, 0):
                    try:
                        self.sync_peer(peer)
                    except Exception:
                        self._note_failure(peer)
            if not self._stop.is_set() and time.time() >= self._next_audit:
                try:
                    self.audit_peers()
                except Exception:
                    pass
                self._next_audit = time.time() + self.audit_interval
            self._stop.wait(30)

    # -- backoff math -------------------------------------------------------

    def interval_for(self, peer):
        return self._interval.get(peer, SYNC_INTERVAL)

    def _note_success(self, peer):
        self._interval[peer] = SYNC_INTERVAL
        self._next_due[peer] = time.time() + SYNC_INTERVAL

    def _note_failure(self, peer):
        cur = self._interval.get(peer, SYNC_INTERVAL)
        self._interval[peer] = min(cur * 2, MAX_BACKOFF)
        self._next_due[peer] = time.time() + self._interval[peer]

    # -- one sync round -----------------------------------------------------

    def sync_peer(self, hash_hex):
        """Offer our ids, learn what the peer wants, push those records.

        Returns {"ok":bool, "offered":n, "pushed":n, "accepted":n,
        "rejected":n} (counts are 0 on failure)."""
        result = {"ok": False, "offered": 0, "pushed": 0,
                  "accepted": 0, "rejected": 0}
        link = None
        try:
            ids = self.store.all_ids()
            result["offered"] = len(ids)

            link = self._open_link(hash_hex)
            if link is None:
                self._note_failure(hash_hex)
                return result

            offer = {"v": 1, "op": "sync.offer", "ids": ids}
            key = self._cached_key(hash_hex)
            if key is not None:
                offer["key"] = key
            offer_reply = self._request(link, offer)

            # Cost self-negotiation, mirroring LXMPeer's regenerate-on-
            # mismatch: a stamped-peering resolver answers a keyless or
            # underweight offer with its cost; we generate a key for that
            # cost and retry once.
            if (isinstance(offer_reply, dict) and not offer_reply.get("ok")
                    and "peering key" in str(offer_reply.get("err", ""))
                    and offer_reply.get("cost")):
                key = self._ensure_peering_key(
                    hash_hex, int(offer_reply["cost"]))
                if key is not None:
                    offer["key"] = key
                    offer_reply = self._request(link, offer)

            if not isinstance(offer_reply, dict) or not offer_reply.get("ok"):
                self._note_failure(hash_hex)
                return result

            want = offer_reply.get("want") or []
            if want:
                recs = self.store.get_many(want)
                # Store.get_many already excludes attested records;
                # defensively drop anything unsigned regardless.
                recs = [r for r in recs if r.get("sig") is not None]
                result["pushed"] = len(recs)
                if recs:
                    push = {"v": 1, "op": "sync.push", "records": recs}
                    if offer.get("key") is not None:
                        push["key"] = offer["key"]
                    push_reply = self._request(link, push)
                    if (not isinstance(push_reply, dict)
                            or not push_reply.get("ok")):
                        self._note_failure(hash_hex)
                        return result
                    result["accepted"] = int(push_reply.get("accepted", 0))
                    result["rejected"] = int(push_reply.get("rejected", 0))

            result["ok"] = True
            self._note_success(hash_hex)
            return result
        except Exception:
            self._note_failure(hash_hex)
            return result
        finally:
            if link is not None:
                self._close_link(link)

    # -- MOFU withholding audit ---------------------------------------------

    def fetch_inventory(self, hash_hex):
        """Ask one peer what record ids it holds. None if it did not answer."""
        link = None
        try:
            link = self._open_link(hash_hex)
            if link is None:
                return None
            payload = {"v": 1, "op": "sync.inventory"}
            key = self._cached_key(hash_hex)
            if key is not None:
                payload["key"] = key
            reply = self._request(link, payload)
            if (isinstance(reply, dict) and not reply.get("ok")
                    and "peering key" in str(reply.get("err", ""))
                    and reply.get("cost")):
                key = self._ensure_peering_key(hash_hex, int(reply["cost"]))
                if key is not None:
                    payload["key"] = key
                    reply = self._request(link, payload)
            if not isinstance(reply, dict) or not reply.get("ok"):
                return None
            ids = reply.get("ids")
            return set(i for i in ids if isinstance(i, str)) if isinstance(ids, list) else None
        except Exception:
            return None
        finally:
            if link is not None:
                self._close_link(link)

    def pull_records(self, hash_hex, ids):
        """Pull specific records from a peer and store the valid ones.

        This is the repair half of the audit. Pulled records go through the
        same validation as pushed ones, so a peer we are repairing FROM can
        no more forge a record than one pushing to us. Returns accepted count.
        """
        ids = [i for i in ids if isinstance(i, str)][:MAX_FETCH_IDS]
        if not ids:
            return 0
        link = None
        try:
            link = self._open_link(hash_hex)
            if link is None:
                return 0
            payload = {"v": 1, "op": "sync.fetch", "ids": ids}
            key = self._cached_key(hash_hex)
            if key is not None:
                payload["key"] = key
            reply = self._request(link, payload)
            if not isinstance(reply, dict) or not reply.get("ok"):
                return 0
            accepted = 0
            for rec in (reply.get("records") or []):
                if _accept_pushed_record(rec, self.store):
                    accepted += 1
            return accepted
        except Exception:
            return 0
        finally:
            if link is not None:
                self._close_link(link)

    def audit_peers(self):
        """One MOFU round: inventory every peer, repair our gaps, then flag
        peers that persistently lack what their fellows agree exists."""
        inventories = {}
        for peer in self.peer_hashes:
            if self._stop.is_set():
                break
            inv = self.fetch_inventory(peer)
            if inv is not None:
                inventories[peer] = inv
        result = {"peers_answering": len(inventories), "pulled": 0,
                  "flagged": {}, "candidates": {}}
        if not inventories:
            self._last_audit = result
            return result

        own = set(self.store.all_ids())

        # Repair first: anything a peer holds that we do not, pull and verify.
        # Doing this BEFORE judging also means we stop accusing others of
        # gaps we are about to fill ourselves.
        for peer, ids in inventories.items():
            gap = list(ids - own)
            if gap:
                got = self.pull_records(peer, gap)
                result["pulled"] += got
                if got:
                    own = set(self.store.all_ids())

        ages = {r.get("id"): r.get("ts")
                for r in self.store.get_many(sorted(own))
                if r.get("id")}
        candidates = withholding_candidates(inventories, own, ages,
                                            grace=self.audit_grace)
        result["candidates"] = {p: sorted(v) for p, v in candidates.items()}
        flagged = self.audit.record_round(candidates)
        result["flagged"] = {p: sorted(v) for p, v in flagged.items()}
        self._last_audit = result
        return result

    def audit_state(self):
        """Serializable audit view for /healthz."""
        return {
            "peers": len(self.peer_hashes),
            "strikes_required": self.audit.strikes_required,
            "last_round": self._last_audit,
            "suspects": self.audit.state(),
        }

    # -- peering keys (LXMPeer semantics via LXMF's own LXStamper) ---------

    def _own_identity(self):
        try:
            get = getattr(self.rns_owner, "get_identity", None)
            return get() if get else None
        except Exception:
            return None

    def _cached_key(self, peer_hex):
        entry = self._peer_keys.get(peer_hex)
        return entry[0] if entry else None

    def _ensure_peering_key(self, peer_hex, cost):
        """Generate (or reuse) a peering key for this peer at this cost.
        Key material = peer_identity.hash + own_identity.hash, exactly as
        LXMPeer.generate_peering_key builds it. Returns key bytes or None."""
        entry = self._peer_keys.get(peer_hex)
        if entry and entry[2] >= cost and entry[1] >= cost:
            return entry[0]
        stamper = _lxstamper()
        own = self._own_identity()
        if stamper is None or own is None:
            return None
        peer_identity_hash = self._peer_identity_hash(peer_hex)
        if peer_identity_hash is None:
            return None
        try:
            key, value = stamper.generate_stamp(
                peer_identity_hash + own.hash, cost,
                expand_rounds=stamper.WORKBLOCK_EXPAND_ROUNDS_PEERING)
        except Exception:
            return None
        if not key or value < cost:
            return None
        self._peer_keys[peer_hex] = (key, value, cost)
        return key

    def _peer_identity_hash(self, peer_hex):
        try:
            import RNS
            identity = RNS.Identity.recall(bytes.fromhex(peer_hex))
            return identity.hash if identity else None
        except Exception:
            return None

    # -- RNS seams (stubbed in tests) --------------------------------------

    def _open_link(self, hash_hex):
        """Resolve the path to a peer and open a Link. None on failure."""
        import RNS
        dest_bytes = bytes.fromhex(hash_hex)
        if not RNS.Transport.has_path(dest_bytes):
            RNS.Transport.request_path(dest_bytes)
            deadline = time.time() + LINK_TIMEOUT
            while (not RNS.Transport.has_path(dest_bytes)
                   and time.time() < deadline):
                time.sleep(0.2)
            if not RNS.Transport.has_path(dest_bytes):
                return None
        identity = RNS.Identity.recall(dest_bytes)
        if identity is None:
            return None
        destination = RNS.Destination(
            identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
            "rnsresolve", "query")
        link = RNS.Link(destination)
        deadline = time.time() + LINK_TIMEOUT
        while (link.status != RNS.Link.ACTIVE
               and time.time() < deadline):
            time.sleep(0.2)
        if link.status != RNS.Link.ACTIVE:
            return None
        # Identify so the peer can gate syncs (stamps and allowlists both
        # key on the remote identity, as in LXMF's offer_request).
        own = self._own_identity()
        if own is not None:
            try:
                link.identify(own)
            except Exception:
                pass
        return link

    def _request(self, link, payload):
        """Send one msgpack request over the link, return the decoded
        reply dict, or None on failure/timeout."""
        try:
            import umsgpack
        except ImportError:
            from RNS.vendor import umsgpack

        done = threading.Event()
        box = {}

        def _on_response(receipt):
            try:
                box["reply"] = umsgpack.unpackb(receipt.response)
            except Exception:
                box["reply"] = None
            done.set()

        def _on_failed(receipt):
            box["reply"] = None
            done.set()

        link.request(
            "q", umsgpack.packb(payload),
            response_callback=_on_response,
            failed_callback=_on_failed,
            timeout=LINK_TIMEOUT)
        if not done.wait(LINK_TIMEOUT + 5):
            return None
        return box.get("reply")

    def _close_link(self, link):
        try:
            link.teardown()
        except Exception:
            pass


def start_scheduler(store, peer_hashes, rns_owner, audit_interval=None,
                    audit_grace=None, audit_strikes=None):
    """Convenience wiring for service.py: build, start, and return a
    PeerScheduler. peer_hashes may be an iterable of 32-hex strings."""
    scheduler = PeerScheduler(store, peer_hashes, rns_owner,
                              audit_interval=audit_interval,
                              audit_grace=audit_grace,
                              audit_strikes=audit_strikes)
    scheduler.start()
    return scheduler
