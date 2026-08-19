"""Unit tests for rns_resolve.peers (A7).

No network, no RNS required: all RNS/records calls go through small
module-level seams (_recall_identity, _verify_record, _derive_target)
and PeerScheduler methods (_open_link, _request, _close_link) which are
stubbed here.
"""

import importlib.util
import unittest

from rns_resolve import peers

HAVE_RNS = importlib.util.find_spec("RNS") is not None


class FakeStore:
    """Minimal store double implementing the parts peers.py uses.

    Records with attested truthy (sig None) are excluded from get_many,
    matching the real Store contract."""

    def __init__(self, records=None):
        # records: dict id -> rec
        self.records = dict(records or {})
        self.put_calls = []
        self.get_many_calls = []

    def put(self, rec):
        self.put_calls.append(rec)
        rid = "id-" + rec["name"]
        self.records[rid] = rec
        return rid

    def all_ids(self):
        return list(self.records.keys())

    def get_many(self, ids):
        self.get_many_calls.append(list(ids))
        out = []
        for i in ids:
            rec = self.records.get(i)
            if rec is None:
                continue
            if rec.get("attested"):
                continue  # contract: attested excluded from peer handoff
            out.append(rec)
        return out

    def missing(self, ids):
        return [i for i in ids if i not in self.records]


def make_rec(name="alice", sig=b"sigbytes", identity="ab" * 16,
             target="cd" * 16, attested=0):
    return {
        "v": 1,
        "name": name,
        "identity": identity,
        "app": "nomadnetwork",
        "aspects": ["node"],
        "target": target,
        "ts": 1000.0,
        "ttl": 86400,
        "sig": sig,
        "attested": attested,
    }


class SeamPatcher:
    """Mixin: monkeypatch the peers module seams, restore on teardown."""

    def patch_seams(self, recall=None, verify=None, derive=None):
        self._saved = (peers._recall_identity, peers._verify_record,
                       peers._derive_target)
        if recall is not None:
            peers._recall_identity = recall
        if verify is not None:
            peers._verify_record = verify
        if derive is not None:
            peers._derive_target = derive
        self.addCleanup(self._restore_seams)

    def _restore_seams(self):
        (peers._recall_identity, peers._verify_record,
         peers._derive_target) = self._saved


class TestHandleSyncOffer(unittest.TestCase):

    def test_non_sync_op_returns_none(self):
        self.assertIsNone(peers.handle_sync("resolve", {}, FakeStore()))
        self.assertIsNone(peers.handle_sync("register", {}, FakeStore()))

    def test_offer_replies_missing_subset(self):
        store = FakeStore({"have1": make_rec("a"), "have2": make_rec("b")})
        payload = {"ids": ["have1", "nope1", "have2", "nope2"]}
        reply = peers.handle_sync("sync.offer", payload, store)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["want"], ["nope1", "nope2"])

    def test_offer_bad_shape_rejected(self):
        reply = peers.handle_sync("sync.offer", {"ids": "notalist"},
                                  FakeStore())
        self.assertFalse(reply["ok"])

    def test_offer_filters_non_string_ids(self):
        store = FakeStore()
        reply = peers.handle_sync(
            "sync.offer", {"ids": ["good", 42, None]}, store)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["want"], ["good"])


class TestHandleSyncPush(SeamPatcher, unittest.TestCase):

    def test_push_bad_shape_rejected(self):
        reply = peers.handle_sync("sync.push", {"records": "nope"},
                                  FakeStore())
        self.assertFalse(reply["ok"])

    def test_rejects_sig_none(self):
        # Even if identity would be recallable and verify would pass,
        # a sig-less (attested-shaped) record must be rejected.
        self.patch_seams(recall=lambda h: object(),
                         verify=lambda rec, ident: True,
                         derive=lambda i, a, asp: "ee" * 16)
        store = FakeStore()
        reply = peers.handle_sync(
            "sync.push", {"records": [make_rec(sig=None)]}, store)
        self.assertEqual(reply["accepted"], 0)
        self.assertEqual(reply["rejected"], 1)
        self.assertEqual(store.put_calls, [])

    def test_rejects_unrecallable_identity(self):
        self.patch_seams(recall=lambda h: None,
                         verify=lambda rec, ident: True,
                         derive=lambda i, a, asp: "ee" * 16)
        store = FakeStore()
        reply = peers.handle_sync(
            "sync.push", {"records": [make_rec()]}, store)
        self.assertEqual(reply["accepted"], 0)
        self.assertEqual(reply["rejected"], 1)
        self.assertEqual(store.put_calls, [])

    def test_rejects_bad_signature(self):
        self.patch_seams(recall=lambda h: object(),
                         verify=lambda rec, ident: False,
                         derive=lambda i, a, asp: "ee" * 16)
        store = FakeStore()
        reply = peers.handle_sync(
            "sync.push", {"records": [make_rec()]}, store)
        self.assertEqual(reply["accepted"], 0)
        self.assertEqual(reply["rejected"], 1)
        self.assertEqual(store.put_calls, [])

    def test_accepts_valid_and_rederives_target(self):
        derived = "ee" * 16
        seen = {}

        def fake_derive(identity_hex, app, aspects):
            seen["args"] = (identity_hex, app, aspects)
            return derived

        self.patch_seams(recall=lambda h: object(),
                         verify=lambda rec, ident: True,
                         derive=fake_derive)
        store = FakeStore()
        pushed = make_rec(target="99" * 16)  # forged target field
        reply = peers.handle_sync("sync.push", {"records": [pushed]}, store)
        self.assertEqual(reply["accepted"], 1)
        self.assertEqual(reply["rejected"], 0)
        self.assertEqual(len(store.put_calls), 1)
        stored = store.put_calls[0]
        # target field from the wire is never trusted
        self.assertEqual(stored["target"], derived)
        self.assertEqual(seen["args"],
                         (pushed["identity"], "nomadnetwork", ["node"]))
        # the caller's dict is not mutated
        self.assertEqual(pushed["target"], "99" * 16)

    def test_mixed_batch_counts(self):
        self.patch_seams(
            recall=lambda h: object(),
            verify=lambda rec, ident: rec["name"] != "badsig",
            derive=lambda i, a, asp: "ee" * 16)
        store = FakeStore()
        batch = [make_rec("good1"), make_rec("badsig"),
                 make_rec("nosig", sig=None), "not-a-dict",
                 make_rec("good2")]
        reply = peers.handle_sync("sync.push", {"records": batch}, store)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["accepted"], 2)
        self.assertEqual(reply["rejected"], 3)

    def test_store_put_exception_counts_rejected(self):
        self.patch_seams(recall=lambda h: object(),
                         verify=lambda rec, ident: True,
                         derive=lambda i, a, asp: "ee" * 16)
        store = FakeStore()

        def boom(rec):
            raise RuntimeError("disk full")
        store.put = boom
        reply = peers.handle_sync(
            "sync.push", {"records": [make_rec()]}, store)
        self.assertEqual(reply["accepted"], 0)
        self.assertEqual(reply["rejected"], 1)


class StubbedScheduler(peers.PeerScheduler):
    """PeerScheduler with the RNS seams replaced by scripted fakes."""

    def __init__(self, store, peer_hashes, link=None, replies=None):
        super().__init__(store, peer_hashes, rns_owner=None)
        self.fake_link = link
        self.replies = list(replies or [])
        self.requests = []
        self.closed = []

    def _open_link(self, hash_hex):
        return self.fake_link

    def _request(self, link, payload):
        self.requests.append(payload)
        if self.replies:
            return self.replies.pop(0)
        return None

    def _close_link(self, link):
        self.closed.append(link)


class TestBackoff(unittest.TestCase):

    PEER = "aa" * 16

    def make(self):
        return StubbedScheduler(FakeStore(), [self.PEER])

    def test_base_interval(self):
        s = self.make()
        self.assertEqual(s.interval_for(self.PEER), 15 * 60)

    def test_failure_doubles_up_to_cap(self):
        s = self.make()
        expected = [1800, 3600, 7200, 14400, 14400, 14400]
        for want in expected:
            s._note_failure(self.PEER)
            self.assertEqual(s.interval_for(self.PEER), want)
        self.assertEqual(peers.MAX_BACKOFF, 4 * 60 * 60)

    def test_success_resets(self):
        s = self.make()
        for _ in range(5):
            s._note_failure(self.PEER)
        self.assertEqual(s.interval_for(self.PEER), peers.MAX_BACKOFF)
        s._note_success(self.PEER)
        self.assertEqual(s.interval_for(self.PEER), 15 * 60)


class TestSyncPeer(unittest.TestCase):

    PEER = "bb" * 16

    def test_offer_then_push_flow(self):
        store = FakeStore({
            "id1": make_rec("one"),
            "id2": make_rec("two"),
            "id3": make_rec("three"),
        })
        sched = StubbedScheduler(
            store, [self.PEER], link=object(),
            replies=[
                {"ok": True, "want": ["id1", "id3"]},
                {"ok": True, "accepted": 2, "rejected": 0},
            ])
        result = sched.sync_peer(self.PEER)
        self.assertTrue(result["ok"])
        self.assertEqual(result["offered"], 3)
        self.assertEqual(result["pushed"], 2)
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(result["rejected"], 0)
        # first request is the offer with OUR ids (we offer, peer wants,
        # we push: that is the direction)
        offer = sched.requests[0]
        self.assertEqual(offer["op"], "sync.offer")
        self.assertEqual(sorted(offer["ids"]), ["id1", "id2", "id3"])
        push = sched.requests[1]
        self.assertEqual(push["op"], "sync.push")
        self.assertEqual([r["name"] for r in push["records"]],
                         ["one", "three"])
        # link closed, backoff reset
        self.assertEqual(len(sched.closed), 1)
        self.assertEqual(sched.interval_for(self.PEER), 15 * 60)

    def test_attested_records_never_pushed(self):
        store = FakeStore({
            "signed": make_rec("signed"),
            "attested": make_rec("localonly", sig=None, attested=1),
        })
        sched = StubbedScheduler(
            store, [self.PEER], link=object(),
            replies=[
                # peer wants both ids, including the attested one
                {"ok": True, "want": ["signed", "attested"]},
                {"ok": True, "accepted": 1, "rejected": 0},
            ])
        result = sched.sync_peer(self.PEER)
        self.assertTrue(result["ok"])
        push = sched.requests[1]
        names = [r["name"] for r in push["records"]]
        self.assertEqual(names, ["signed"])
        for r in push["records"]:
            self.assertIsNotNone(r["sig"])
        self.assertEqual(result["pushed"], 1)

    def test_defensive_filter_even_if_store_leaks_unsigned(self):
        # Belt and braces: even if a store returned an unsigned record
        # from get_many, sync_peer must not push it.
        store = FakeStore({"leak": make_rec("leak", sig=None, attested=0)})
        sched = StubbedScheduler(
            store, [self.PEER], link=object(),
            replies=[{"ok": True, "want": ["leak"]}])
        result = sched.sync_peer(self.PEER)
        self.assertTrue(result["ok"])
        self.assertEqual(result["pushed"], 0)
        # no push request was sent at all
        self.assertEqual(len(sched.requests), 1)

    def test_empty_want_skips_push(self):
        store = FakeStore({"id1": make_rec("one")})
        sched = StubbedScheduler(
            store, [self.PEER], link=object(),
            replies=[{"ok": True, "want": []}])
        result = sched.sync_peer(self.PEER)
        self.assertTrue(result["ok"])
        self.assertEqual(result["pushed"], 0)
        self.assertEqual(len(sched.requests), 1)

    def test_unreachable_peer_backs_off(self):
        store = FakeStore({"id1": make_rec("one")})
        sched = StubbedScheduler(store, [self.PEER], link=None)
        result = sched.sync_peer(self.PEER)
        self.assertFalse(result["ok"])
        self.assertEqual(sched.interval_for(self.PEER), 1800)
        result = sched.sync_peer(self.PEER)
        self.assertFalse(result["ok"])
        self.assertEqual(sched.interval_for(self.PEER), 3600)

    def test_failed_offer_reply_backs_off(self):
        store = FakeStore({"id1": make_rec("one")})
        sched = StubbedScheduler(
            store, [self.PEER], link=object(), replies=[None])
        result = sched.sync_peer(self.PEER)
        self.assertFalse(result["ok"])
        self.assertEqual(sched.interval_for(self.PEER), 1800)
        # link still torn down on failure
        self.assertEqual(len(sched.closed), 1)

    def test_start_stop_idempotent(self):
        sched = StubbedScheduler(FakeStore(), [])
        sched.start()
        first = sched._thread
        sched.start()
        self.assertIs(sched._thread, first)
        self.assertTrue(first.daemon)
        sched.stop()
        self.assertIsNone(sched._thread)
        sched.stop()  # second stop is a no-op


@unittest.skipUnless(HAVE_RNS, "RNS not installed")
class TestRnsSeams(unittest.TestCase):

    def test_recall_unknown_identity_returns_none(self):
        # An identity hash nobody has announced is not recallable.
        self.assertIsNone(peers._recall_identity("00" * 16))

    def test_recall_bad_hex_returns_none(self):
        self.assertIsNone(peers._recall_identity("not-hex"))


if __name__ == "__main__":
    unittest.main()


class FakeStamper:
    """Stub with LXStamper's surface."""
    WORKBLOCK_EXPAND_ROUNDS_PEERING = 25

    def __init__(self, valid=True, gen_value=99):
        self.valid = valid
        self.gen_value = gen_value
        self.validated = []
        self.generated = []

    def validate_peering_key(self, peering_id, key, cost):
        self.validated.append((bytes(peering_id), bytes(key), cost))
        return self.valid

    def generate_stamp(self, material, cost, expand_rounds=None):
        self.generated.append((bytes(material), cost, expand_rounds))
        return b"stamp-key", self.gen_value


class FakeIdentity:
    def __init__(self, h):
        self.hash = h


class SyncGateTest(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        self.stamper = FakeStamper()
        self._orig = peers._lxstamper
        peers._lxstamper = lambda: self.stamper

    def tearDown(self):
        peers._lxstamper = self._orig

    def ctx(self, **kw):
        base = {"self_identity_hash": b"S" * 16,
                "peering_cost": 18,
                "allowed_sync_identities": None,
                "link_identity": FakeIdentity(b"R" * 16)}
        base.update(kw)
        return base

    def test_cost_zero_is_open(self):
        reply = peers.handle_sync(
            "sync.offer", {"ids": []}, self.store,
            self.ctx(peering_cost=0, link_identity=None))
        self.assertTrue(reply["ok"])

    def test_unidentified_rejected_with_cost(self):
        reply = peers.handle_sync(
            "sync.offer", {"ids": []}, self.store, self.ctx(link_identity=None))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["cost"], 18)
        self.assertIn("identify", reply["err"])

    def test_missing_key_advertises_cost(self):
        reply = peers.handle_sync(
            "sync.offer", {"ids": []}, self.store, self.ctx())
        self.assertFalse(reply["ok"])
        self.assertIn("peering key required", reply["err"])
        self.assertEqual(reply["cost"], 18)

    def test_valid_key_admits_and_uses_lxmf_material_order(self):
        reply = peers.handle_sync(
            "sync.offer", {"ids": [], "key": b"k"}, self.store, self.ctx())
        self.assertTrue(reply["ok"])
        peering_id, key, cost = self.stamper.validated[0]
        # LXMF offer_request: self.identity.hash + remote_identity.hash
        self.assertEqual(peering_id, b"S" * 16 + b"R" * 16)
        self.assertEqual(cost, 18)

    def test_invalid_key_rejected(self):
        self.stamper.valid = False
        reply = peers.handle_sync(
            "sync.offer", {"ids": [], "key": b"k"}, self.store, self.ctx())
        self.assertFalse(reply["ok"])
        self.assertIn("invalid peering key", reply["err"])

    def test_push_gated_too(self):
        reply = peers.handle_sync(
            "sync.push", {"records": []}, self.store, self.ctx())
        self.assertFalse(reply["ok"])
        self.assertIn("peering key required", reply["err"])

    def test_allowlist_blocks_unknown_identity(self):
        reply = peers.handle_sync(
            "sync.offer", {"ids": [], "key": b"k"}, self.store,
            self.ctx(allowed_sync_identities={"aa" * 16}))
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["err"], "not allowed")

    def test_allowlist_admits_listed_identity(self):
        rid = (b"R" * 16).hex()
        reply = peers.handle_sync(
            "sync.offer", {"ids": [], "key": b"k"}, self.store,
            self.ctx(allowed_sync_identities={rid}))
        self.assertTrue(reply["ok"])

    def test_offer_size_cap(self):
        ids = ["x" * 32] * (peers.MAX_OFFER_IDS + 1)
        reply = peers.handle_sync(
            "sync.offer", {"ids": ids, "key": b"k"}, self.store, self.ctx())
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["err"], "offer too large")

    def test_push_size_cap(self):
        recs = [{}] * (peers.MAX_PUSH_RECORDS + 1)
        reply = peers.handle_sync(
            "sync.push", {"records": recs, "key": b"k"}, self.store, self.ctx())
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["err"], "push too large")

    def test_stamps_unavailable_when_lxmf_missing(self):
        peers._lxstamper = lambda: None
        reply = peers.handle_sync(
            "sync.offer", {"ids": [], "key": b"k"}, self.store, self.ctx())
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["err"], "stamps unavailable")


class PeeringKeyNegotiationTest(unittest.TestCase):
    def setUp(self):
        self.stamper = FakeStamper(gen_value=20)
        self._orig = peers._lxstamper
        peers._lxstamper = lambda: self.stamper

    def tearDown(self):
        peers._lxstamper = self._orig

    def _scheduler(self):
        class Owner:
            def get_identity(self):
                return FakeIdentity(b"O" * 16)
        sched = peers.PeerScheduler(FakeStore(), ["ab" * 16], Owner())
        sched._peer_identity_hash = lambda peer_hex: b"P" * 16
        return sched

    def test_ensure_key_uses_lxmpeer_material_order_and_caches(self):
        sched = self._scheduler()
        key = sched._ensure_peering_key("ab" * 16, 18)
        self.assertEqual(key, b"stamp-key")
        material, cost, rounds = self.stamper.generated[0]
        # LXMPeer.generate_peering_key: peer_identity.hash + own_identity.hash
        self.assertEqual(material, b"P" * 16 + b"O" * 16)
        self.assertEqual(cost, 18)
        self.assertEqual(rounds, 25)
        # Cached: second call generates nothing new.
        sched._ensure_peering_key("ab" * 16, 18)
        self.assertEqual(len(self.stamper.generated), 1)

    def test_underweight_key_not_cached(self):
        self.stamper.gen_value = 5
        sched = self._scheduler()
        self.assertIsNone(sched._ensure_peering_key("ab" * 16, 18))

    def test_offer_retry_on_cost_advert(self):
        sched = self._scheduler()
        replies = [
            {"ok": False, "err": "peering key required", "cost": 18},
            {"ok": True, "want": []},
        ]
        sent = []
        sched._open_link = lambda h: object()
        sched._close_link = lambda l: None
        def fake_request(link, payload):
            sent.append(dict(payload))
            return replies[len(sent) - 1]
        sched._request = fake_request
        result = sched.sync_peer("ab" * 16)
        self.assertTrue(result["ok"])
        self.assertNotIn("key", sent[0])
        self.assertEqual(sent[1]["key"], b"stamp-key")
