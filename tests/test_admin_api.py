"""Unit tests for the resolver-side operator surface that the dashboard uses:
Store.list_records / delete_id, service.Metrics, the service.admin_* handlers,
and PeerScheduler.state / sync_now.

Same discipline as the rest of the suite: no sockets, no RNS. The service
handlers take a fake svc/deps, and the scheduler's sync_peer is stubbed.
"""

import time
import types
import unittest

from rns_resolve import peers, service, store as store_mod


def make_rec(name="alice", sig=b"sigbytes", identity="ab" * 16,
             target="cd" * 16, ts=None, ttl=3600, pubkey=b"pk"):
    return {
        "v": 1,
        "name": name,
        "identity": identity,
        "app": "nomadnetwork",
        "aspects": ["node"],
        "target": target,
        "ts": time.time() if ts is None else ts,
        "ttl": ttl,
        "sig": sig,
        "pubkey": pubkey,
    }


class StoreAdminTest(unittest.TestCase):
    def setUp(self):
        self.store = store_mod.Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_list_records_newest_first_with_total(self):
        now = time.time()
        self.store.put(make_rec("older", ts=now - 100))
        self.store.put(make_rec("newer", ts=now))
        recs, total = self.store.list_records()
        self.assertEqual(total, 2)
        self.assertEqual([r["name"] for r in recs], ["newer", "older"])

    def test_list_records_filters_on_name_target_and_identity(self):
        self.store.put(make_rec("beacon", target="7d" * 16, identity="11" * 16))
        self.store.put(make_rec("signal-fire", target="ca" * 16, identity="22" * 16))
        for q, expect in (("beac", ["beacon"]), ("7d7d", ["beacon"]),
                          ("2222", ["signal-fire"]), ("zzz", [])):
            recs, total = self.store.list_records(q=q)
            self.assertEqual([r["name"] for r in recs], expect, q)
            self.assertEqual(total, len(expect), q)

    def test_list_records_can_exclude_expired(self):
        now = time.time()
        self.store.put(make_rec("live", ts=now, ttl=3600))
        self.store.put(make_rec("dead", ts=now - 7200, ttl=3600))
        recs, total = self.store.list_records(include_expired=False)
        self.assertEqual([r["name"] for r in recs], ["live"])
        self.assertEqual(total, 1)
        recs, total = self.store.list_records(include_expired=True)
        self.assertEqual(total, 2)

    def test_list_records_paginates(self):
        now = time.time()
        for i in range(5):
            self.store.put(make_rec(f"n{i}", ts=now - i))
        page1, total = self.store.list_records(limit=2, offset=0)
        page2, _ = self.store.list_records(limit=2, offset=2)
        self.assertEqual(total, 5)
        self.assertEqual([r["name"] for r in page1], ["n0", "n1"])
        self.assertEqual([r["name"] for r in page2], ["n2", "n3"])

    def test_list_records_includes_attested_records(self):
        # unlike all_ids/get_many (peer-facing), the operator sees everything
        self.store.put(make_rec("attested-one", sig=None))
        recs, total = self.store.list_records()
        self.assertEqual(total, 1)
        self.assertTrue(recs[0]["attested"])

    def test_delete_id_removes_one_record_regardless_of_owner(self):
        rid = self.store.put(make_rec("beacon", identity="11" * 16))
        self.store.put(make_rec("other", identity="22" * 16))
        self.assertEqual(self.store.delete_id(rid), 1)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.delete_id(rid), 0)

    def test_delete_id_of_unknown_id_is_zero_not_an_error(self):
        self.assertEqual(self.store.delete_id("nope"), 0)


class MetricsTest(unittest.TestCase):
    def test_counts_ops_and_keeps_recent_queries(self):
        m = service.Metrics(recent=3)
        m.note("resolve", {"q": "beacon"})
        m.note("resolve", {"q": "signal-fire"})
        m.note("whois", {"target": "ab" * 16})
        snap = m.snapshot()
        self.assertEqual(snap["total"], 3)
        self.assertEqual(snap["ops"], {"resolve": 2, "whois": 1})
        self.assertEqual(snap["recent"][0]["op"], "whois")
        self.assertEqual(snap["recent"][-1]["q"], "beacon")

    def test_recent_is_bounded(self):
        m = service.Metrics(recent=2)
        for i in range(5):
            m.note("resolve", {"q": f"n{i}"})
        self.assertEqual(len(m.snapshot()["recent"]), 2)

    def test_note_tolerates_a_payload_without_a_query(self):
        m = service.Metrics()
        m.note("sync.offer", {"ids": ["a"]})
        m.note("register", None)
        snap = m.snapshot()
        self.assertEqual(snap["total"], 2)
        self.assertIsNone(snap["recent"][0]["q"])

    def test_long_queries_are_truncated(self):
        m = service.Metrics()
        m.note("resolve", {"q": "x" * 500})
        self.assertEqual(len(m.snapshot()["recent"][0]["q"]), 64)


class FakeStore:
    def __init__(self, records=None, total=0):
        self._records = list(records or [])
        self._total = total or len(self._records)
        self.deleted = []
        self.list_calls = []

    def count(self):
        return len(self._records)

    def list_records(self, q=None, limit=200, offset=0, include_expired=True):
        self.list_calls.append((q, limit, offset, include_expired))
        return self._records, self._total

    def delete_id(self, rid):
        self.deleted.append(rid)
        return 1 if any(r.get("id") == rid for r in self._records) else 0


class FakeScheduler:
    def __init__(self, peer_hashes=("ca87",), fail=False):
        self.peer_hashes = list(peer_hashes)
        self.synced = []
        self.audited = 0
        self.fail = fail

    def sync_now(self, peer):
        if peer not in self.peer_hashes:
            raise ValueError("unknown peer")
        self.synced.append(peer)
        return {"ok": not self.fail, "pushed": 1, "accepted": 1}

    def state(self):
        return [{"peer": p, "interval_s": 900, "due_in_s": 10} for p in self.peer_hashes]

    def audit_state(self):
        return {"peers": len(self.peer_hashes), "strikes_required": 3,
                "last_round": None, "suspects": {}}

    def audit_peers(self):
        self.audited += 1
        return {"peers_answering": 1, "pulled": 0, "flagged": {}}


class FakeDest:
    def __init__(self, boom=False):
        self.announces = 0
        self.boom = boom

    def announce(self):
        if self.boom:
            raise RuntimeError("no path")
        self.announces += 1


def fake_svc(store=None, scheduler=None, dest=None, **kw):
    deps = service.Deps(store=store or FakeStore(), sync_handler=None)
    svc = types.SimpleNamespace(
        deps=deps, dest_hex="5f38", identity=None, rns_ready=True,
        peer_scheduler=scheduler, destination=dest,
        started_at=time.time() - 60, last_announce_at=None, announce_count=0,
        last_sweep_expired=0, db_path="/data/resolve.db", rns_configdir="/config",
        health_port=8225, private_port=8226, peer_hashes=["ca87"],
        peering_cost=18, sync_from=None)
    for k, v in kw.items():
        setattr(svc, k, v)
    return svc


class AdminStatusTest(unittest.TestCase):
    def test_status_reports_service_peers_and_metrics(self):
        sched = FakeScheduler()
        svc = fake_svc(store=FakeStore([{"id": "a"}]), scheduler=sched)
        svc.deps.metrics.note("resolve", {"q": "beacon"})
        out = service.admin_status(svc)
        self.assertTrue(out["ok"])
        self.assertEqual(out["dest"], "5f38")
        self.assertEqual(out["records"], 1)
        self.assertEqual(out["config"]["peers"], ["ca87"])
        self.assertEqual(out["config"]["peering_cost"], 18)
        self.assertEqual(out["peer_sync"][0]["peer"], "ca87")
        self.assertEqual(out["peer_audit"]["strikes_required"], 3)
        self.assertEqual(out["metrics"]["total"], 1)
        self.assertGreaterEqual(out["uptime_s"], 59)

    def test_status_without_peering_has_no_peer_sections(self):
        out = service.admin_status(fake_svc())
        self.assertEqual(out["peer_sync"], [])
        self.assertIsNone(out["peer_audit"])

    def test_status_survives_a_broken_store(self):
        class Boom:
            def count(self):
                raise RuntimeError("db gone")
        out = service.admin_status(fake_svc(store=Boom()))
        self.assertTrue(out["ok"])
        self.assertEqual(out["records"], 0)


class AdminRecordsTest(unittest.TestCase):
    def rec(self, **kw):
        base = {"id": "rec1", "name": "beacon", "identity": "11" * 16,
                "app": "nomadnetwork", "aspects": ["node"], "target": "7d" * 16,
                "ts": time.time() - 10, "ttl": 3600, "sig": b"s", "pubkey": b"pk",
                "attested": 0, "last_used": None}
        base.update(kw)
        return base

    def test_records_returns_admin_view_fields(self):
        store = FakeStore([self.rec()])
        out = service.admin_records({}, service.Deps(store=store, sync_handler=None))
        self.assertTrue(out["ok"])
        r = out["records"][0]
        self.assertEqual(r["id"], "rec1")
        self.assertTrue(r["pubkey_bound"])
        self.assertFalse(r["attested"])
        self.assertFalse(r["expired"])
        self.assertAlmostEqual(r["expires_at"], r["ts"] + r["ttl"], places=3)

    def test_expired_record_is_marked(self):
        store = FakeStore([self.rec(ts=time.time() - 7200, ttl=3600)])
        out = service.admin_records({}, service.Deps(store=store, sync_handler=None))
        self.assertTrue(out["records"][0]["expired"])

    def test_attested_record_is_marked(self):
        store = FakeStore([self.rec(sig=None, attested=1)])
        out = service.admin_records({}, service.Deps(store=store, sync_handler=None))
        self.assertTrue(out["records"][0]["attested"])

    def test_query_params_reach_the_store(self):
        store = FakeStore([])
        service.admin_records({"q": ["beacon"], "limit": ["10"], "offset": ["5"],
                               "expired": ["0"]},
                              service.Deps(store=store, sync_handler=None))
        self.assertEqual(store.list_calls[0], ("beacon", 10, 5, False))

    def test_bad_limit_is_rejected(self):
        out = service.admin_records({"limit": ["abc"]},
                                    service.Deps(store=FakeStore(), sync_handler=None))
        self.assertFalse(out["ok"])

    def test_store_without_listing_support_says_so(self):
        deps = service.Deps(store=object(), sync_handler=None)
        out = service.admin_records({}, deps)
        self.assertFalse(out["ok"])


class AdminActionsTest(unittest.TestCase):
    def test_delete_requires_an_id(self):
        deps = service.Deps(store=FakeStore(), sync_handler=None)
        self.assertFalse(service.admin_delete({}, deps)["ok"])
        self.assertFalse(service.admin_delete({"id": 5}, deps)["ok"])

    def test_delete_removes_and_reports(self):
        store = FakeStore([{"id": "rec1"}])
        deps = service.Deps(store=store, sync_handler=None)
        out = service.admin_delete({"id": "rec1"}, deps)
        self.assertTrue(out["ok"])
        self.assertEqual(out["removed"], 1)
        self.assertEqual(store.deleted, ["rec1"])

    def test_delete_of_a_missing_record_is_not_found(self):
        deps = service.Deps(store=FakeStore([]), sync_handler=None)
        out = service.admin_delete({"id": "nope"}, deps)
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["err"])

    def test_sync_all_peers(self):
        sched = FakeScheduler(["p1", "p2"])
        out = service.admin_sync({}, fake_svc(scheduler=sched))
        self.assertTrue(out["ok"])
        self.assertEqual(sorted(sched.synced), ["p1", "p2"])

    def test_sync_one_peer(self):
        sched = FakeScheduler(["p1", "p2"])
        out = service.admin_sync({"peer": "p2"}, fake_svc(scheduler=sched))
        self.assertTrue(out["ok"])
        self.assertEqual(sched.synced, ["p2"])

    def test_sync_unknown_peer_is_an_error(self):
        out = service.admin_sync({"peer": "zz"}, fake_svc(scheduler=FakeScheduler()))
        self.assertFalse(out["ok"])

    def test_sync_without_peering_is_an_error(self):
        out = service.admin_sync({}, fake_svc(scheduler=None))
        self.assertFalse(out["ok"])
        self.assertIn("peering disabled", out["err"])

    def test_announce_marks_the_service(self):
        dest = FakeDest()
        svc = fake_svc(dest=dest)
        out = service.admin_announce(svc)
        self.assertTrue(out["ok"])
        self.assertEqual(dest.announces, 1)
        self.assertEqual(svc.announce_count, 1)
        self.assertIsNotNone(svc.last_announce_at)

    def test_announce_failure_is_reported(self):
        out = service.admin_announce(fake_svc(dest=FakeDest(boom=True)))
        self.assertFalse(out["ok"])

    def test_announce_without_rns_is_an_error(self):
        svc = fake_svc(dest=None)
        self.assertFalse(service.admin_announce(svc)["ok"])

    def test_audit_runs_a_round(self):
        sched = FakeScheduler()
        out = service.admin_audit(fake_svc(scheduler=sched))
        self.assertTrue(out["ok"])
        self.assertEqual(sched.audited, 1)

    def test_audit_unsupported_when_the_scheduler_lacks_it(self):
        out = service.admin_audit(fake_svc(scheduler=object()))
        self.assertFalse(out["ok"])


class DispatchMetricsTest(unittest.TestCase):
    """handle_request should count ops without changing any reply."""

    def test_resolve_is_counted(self):
        if service.umsgpack is None:
            self.skipTest("umsgpack not available")
        store = FakeStore()
        store.resolve = lambda name: []
        store.prefix = lambda name, limit=10: []
        deps = service.Deps(store=store, sync_handler=None)
        payload = service.umsgpack.packb({"v": 1, "op": "resolve", "q": "beacon"})
        service.handle_request(payload, None, deps)
        snap = deps.metrics.snapshot()
        self.assertEqual(snap["ops"].get("resolve"), 1)
        self.assertEqual(snap["recent"][0]["q"], "beacon")

    def test_manifest_is_not_counted(self):
        if service.umsgpack is None:
            self.skipTest("umsgpack not available")
        deps = service.Deps(store=FakeStore(), sync_handler=None)
        service.handle_request(service.umsgpack.packb({"op": "__manifest__"}), None, deps)
        self.assertEqual(deps.metrics.snapshot()["total"], 0)


class SchedulerStateTest(unittest.TestCase):
    def scheduler(self, result=None, boom=False):
        sched = peers.PeerScheduler(store=None, peer_hashes=["p1", "p2"], rns_owner=None)
        calls = []

        def fake_sync(peer):
            calls.append(peer)
            if boom:
                raise RuntimeError("link failed")
            return dict(result or {"ok": True, "offered": 3, "pushed": 1,
                                   "accepted": 1, "rejected": 0})

        sched.sync_peer = fake_sync
        sched._note_failure = lambda peer: None
        return sched, calls

    def test_state_lists_every_peer_before_any_sync(self):
        sched, _ = self.scheduler()
        st = sched.state()
        self.assertEqual([s["peer"] for s in st], ["p1", "p2"])
        self.assertIsNone(st[0]["last"])
        self.assertIsNone(st[0]["last_ok_at"])
        self.assertFalse(st[0]["backoff"])

    def test_sync_now_records_the_outcome(self):
        sched, calls = self.scheduler()
        out = sched.sync_now("p1")
        self.assertTrue(out["ok"])
        self.assertEqual(calls, ["p1"])
        st = {s["peer"]: s for s in sched.state()}
        self.assertIsNotNone(st["p1"]["last"])
        self.assertEqual(st["p1"]["last"]["result"]["pushed"], 1)
        self.assertIsNotNone(st["p1"]["last_ok_at"])
        self.assertIsNone(st["p2"]["last"])

    def test_failed_sync_is_recorded_without_a_last_ok(self):
        sched, _ = self.scheduler(result={"ok": False})
        sched.sync_now("p1")
        st = {s["peer"]: s for s in sched.state()}
        self.assertFalse(st["p1"]["last"]["result"]["ok"])
        self.assertIsNone(st["p1"]["last_ok_at"])

    def test_raising_sync_is_captured_not_propagated(self):
        sched, _ = self.scheduler(boom=True)
        out = sched.sync_now("p1")
        self.assertFalse(out["ok"])
        self.assertIn("link failed", out["error"])

    def test_sync_now_rejects_an_unconfigured_peer(self):
        sched, _ = self.scheduler()
        with self.assertRaises(ValueError):
            sched.sync_now("nope")

    def test_backoff_shows_in_state(self):
        sched, _ = self.scheduler()
        sched._interval["p1"] = peers.SYNC_INTERVAL * 4
        st = {s["peer"]: s for s in sched.state()}
        self.assertTrue(st["p1"]["backoff"])
        self.assertFalse(st["p2"]["backoff"])


if __name__ == "__main__":
    unittest.main()
