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

    def __init__(self, store, peer_hashes, rns_owner):
        self.store = store
        self.peer_hashes = list(peer_hashes)
        self.rns_owner = rns_owner
        self._stop = threading.Event()
        self._thread = None
        self._interval = {h: SYNC_INTERVAL for h in self.peer_hashes}
        self._next_due = {h: time.time() for h in self.peer_hashes}
        # Per-peer peering keys, LXMPeer-style: {peer_hex: (key, value, cost)}.
        # Cheap to regenerate (25-round workblock), so memory-only.
        self._peer_keys = {}

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


def start_scheduler(store, peer_hashes, rns_owner):
    """Convenience wiring for service.py: build, start, and return a
    PeerScheduler. peer_hashes may be an iterable of 32-hex strings."""
    scheduler = PeerScheduler(store, peer_hashes, rns_owner)
    scheduler.start()
    return scheduler
