"""Unit tests for rns_resolve.service handle_request dispatch.

Pure-function tests: fakes and stubs only, no sockets, no RNS. A fake
rns_resolve.records module is installed in sys.modules so the dispatch
logic is exercised without the real records module (which may need RNS).
"""

import hashlib
import re
import sys
import types
import unittest

from rns_resolve import service

HAVE_MSGPACK = service.umsgpack is not None


def pack(obj):
    return service.umsgpack.packb(obj)


def unpack(data):
    return service.umsgpack.unpackb(data)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

NAME_RE = re.compile(r"^[a-z0-9._-]+$")


def make_fake_records(verify_result=True):
    mod = types.ModuleType("rns_resolve.records")
    mod.HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")
    mod.calls = {"verify": [], "derive": [], "normalize": []}

    def normalize_name(s):
        if not isinstance(s, str) or not s:
            raise ValueError("invalid name")
        s = s.lower().strip()
        if not NAME_RE.match(s):
            raise ValueError("invalid name")
        return s

    def record_id(rec):
        key = "|".join([str(rec["v"]), rec["name"], rec["identity"],
                        rec["app"], ",".join(rec["aspects"]),
                        str(rec["ts"]), str(rec["ttl"])])
        return hashlib.sha256(key.encode()).hexdigest()[:32]

    def derive_target(identity_hex, app, aspects):
        key = identity_hex + "|" + app + "|" + ",".join(aspects)
        mod.calls["derive"].append((identity_hex, app, tuple(aspects)))
        return hashlib.sha256(key.encode()).hexdigest()[:32]

    def verify_record(rec, identity):
        mod.calls["verify"].append((dict(rec), identity))
        return mod.verify_result

    mod.verify_result = verify_result
    mod.normalize_name = normalize_name
    mod.record_id = record_id
    mod.derive_target = derive_target
    mod.verify_record = verify_record
    return mod


class FakeStore:
    def __init__(self, records=None):
        self.records = list(records or [])
        self.touched = []
        self.put_calls = []

    def resolve(self, name_norm):
        return [r for r in self.records if r["name"] == name_norm]

    def whois(self, target_hex):
        return [r for r in self.records if r["target"] == target_hex]

    def put(self, rec):
        self.put_calls.append(dict(rec))
        self.records.append(dict(rec))
        return "fakeid"

    def touch_use(self, record_id):
        self.touched.append(record_id)

    def count(self):
        return len(self.records)


class FakeBeacon:
    def __init__(self, candidates=None, available=True):
        self._candidates = list(candidates or [])
        self._available = available
        self.queries = []

    def available(self):
        return self._available

    def candidates(self, q, limit=10):
        self.queries.append((q, limit))
        return self._candidates[:limit]


class FakeIdentity:
    def __init__(self, hash_hex="aa" * 16):
        self.hash = bytes.fromhex(hash_hex)


def make_record(name="gate", identity="bb" * 16, target="cc" * 16,
                ts=1000.0, ttl=86400, sig=b"sigbytes"):
    return {"v": 1, "name": name, "identity": identity,
            "app": "nomadnetwork", "aspects": ["node"], "target": target,
            "ts": ts, "ttl": ttl, "sig": sig}


def make_deps(store=None, beacon=None, sync_handler=None, rate_limiter=None):
    return service.Deps(
        store=store if store is not None else FakeStore(),
        beacon=beacon,
        manifest=service.build_manifest("dd" * 16),
        rate_limiter=rate_limiter if rate_limiter is not None
        else service.RateLimiter(),
        sync_handler=sync_handler,
    )


@unittest.skipUnless(HAVE_MSGPACK, "umsgpack not available")
class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.fake_records = make_fake_records()
        self._saved = sys.modules.get("rns_resolve.records")
        sys.modules["rns_resolve.records"] = self.fake_records

    def tearDown(self):
        if self._saved is not None:
            sys.modules["rns_resolve.records"] = self._saved
        else:
            sys.modules.pop("rns_resolve.records", None)

    def call(self, payload, link_identity, deps):
        return unpack(service.handle_request(pack(payload), link_identity,
                                             deps))


class TestManifest(ServiceTestBase):
    def test_manifest_shape(self):
        deps = make_deps()
        reply = self.call({"op": "__manifest__"}, None, deps)
        self.assertTrue(reply["ok"])
        manifest = reply["manifest"]
        self.assertEqual(manifest["meshapi"], "0.1")
        svc = manifest["service"]
        self.assertEqual(svc["name"], "rns-resolve")
        self.assertEqual(svc["app"], "rnsresolve")
        self.assertEqual(svc["aspect"], "query")
        self.assertEqual(svc["path"], "q")
        self.assertEqual(svc["encoding"], "umsgpack")
        ops = [o["op"] for o in manifest["ops"]]
        for op in ("__manifest__", "resolve", "register", "whois",
                   "sync.offer", "sync.push"):
            self.assertIn(op, ops)

    def test_manifest_version_tolerant(self):
        deps = make_deps()
        # No v field, wrong v field: manifest still served.
        for payload in ({"op": "__manifest__"},
                        {"v": 99, "op": "__manifest__"},
                        {"v": "x", "op": "__manifest__"}):
            reply = self.call(payload, None, deps)
            self.assertTrue(reply["ok"], payload)
            self.assertIn("manifest", reply)

    def test_unknown_op_and_bad_version(self):
        deps = make_deps()
        reply = self.call({"v": 1, "op": "frobnicate"}, None, deps)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["err"], "unknown op")
        reply = self.call({"v": 2, "op": "resolve", "q": "x"}, None, deps)
        self.assertFalse(reply["ok"])

    def test_bad_payload(self):
        deps = make_deps()
        reply = unpack(service.handle_request(b"\x00notmsgpack-garbage",
                                              None, deps))
        self.assertFalse(reply["ok"])


class TestResolve(ServiceTestBase):
    def test_normalization_error(self):
        deps = make_deps()
        reply = self.call({"v": 1, "op": "resolve", "q": "Bad Name!!"},
                          None, deps)
        self.assertFalse(reply["ok"])
        self.assertIn("err", reply)

    def test_registered_and_announced_merge(self):
        rec1 = make_record(name="gate", ts=2000.0)
        rec2 = make_record(name="gate", identity="ee" * 16, ts=1000.0)
        other = make_record(name="other")
        store = FakeStore([rec1, rec2, other])
        cand = {"hash": "ff" * 16, "name": "gateway node", "trust": 1.5,
                "last_seen": "2026-08-18T00:00:00", "announce_count": 12,
                "reachable": True}
        beacon = FakeBeacon([cand])
        deps = make_deps(store=store, beacon=beacon)

        reply = self.call({"v": 1, "op": "resolve", "q": "GATE"}, None, deps)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["q"], "gate")
        self.assertEqual(len(reply["registered"]), 2)
        for pub in reply["registered"]:
            self.assertNotIn("sig", pub)
            self.assertIn("id", pub)
            self.assertEqual(pub["expires"], pub["ts"] + pub["ttl"])
        self.assertEqual(reply["announced"], [cand])
        # touch_use called for each returned registered record
        ids = [p["id"] for p in reply["registered"]]
        self.assertEqual(store.touched, ids)
        # beacon queried with the normalized name
        self.assertEqual(beacon.queries[0][0], "gate")

    def test_beacon_unavailable_degrades_to_empty(self):
        store = FakeStore([make_record(name="gate")])
        beacon = FakeBeacon([{"hash": "ff" * 16}], available=False)
        deps = make_deps(store=store, beacon=beacon)
        reply = self.call({"v": 1, "op": "resolve", "q": "gate"}, None, deps)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["announced"], [])
        self.assertEqual(len(reply["registered"]), 1)

    def test_no_beacon_at_all(self):
        deps = make_deps(store=FakeStore(), beacon=None)
        reply = self.call({"v": 1, "op": "resolve", "q": "gate"}, None, deps)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["announced"], [])
        self.assertEqual(reply["registered"], [])

    def test_limit_applied(self):
        recs = [make_record(name="gate", ts=float(i)) for i in range(5)]
        store = FakeStore(recs)
        deps = make_deps(store=store)
        reply = self.call({"v": 1, "op": "resolve", "q": "gate", "limit": 2},
                          None, deps)
        self.assertEqual(len(reply["registered"]), 2)


class TestRegister(ServiceTestBase):
    def payload(self, **kw):
        base = {"v": 1, "op": "register", "name": "myname",
                "ts": 1234.5, "sig": b"detached-sig"}
        base.update(kw)
        return base

    def test_identify_required(self):
        deps = make_deps()
        reply = self.call(self.payload(), None, deps)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["err"], "identify required")

    def test_register_ok_derives_target_and_verifies(self):
        store = FakeStore()
        deps = make_deps(store=store)
        ident = FakeIdentity("ab" * 16)
        reply = self.call(self.payload(), ident, deps)
        self.assertTrue(reply["ok"], reply)
        rec = reply["record"]
        self.assertEqual(rec["identity"], "ab" * 16)
        self.assertEqual(rec["name"], "myname")
        self.assertEqual(rec["app"], "nomadnetwork")
        self.assertEqual(rec["aspects"], ["node"])
        self.assertNotIn("sig", rec)
        # target derived from the verified identity, not client-supplied
        expected_target = self.fake_records.derive_target(
            "ab" * 16, "nomadnetwork", ["node"])
        self.assertEqual(rec["target"], expected_target)
        # sig verify was called with the built record and the link identity
        self.assertEqual(len(self.fake_records.calls["verify"]), 1)
        verified_rec, verified_ident = self.fake_records.calls["verify"][0]
        self.assertIs(verified_ident, ident)
        self.assertEqual(verified_rec["name"], "myname")
        # stored
        self.assertEqual(len(store.put_calls), 1)
        self.assertEqual(store.put_calls[0]["sig"], b"detached-sig")

    def test_register_bad_signature(self):
        self.fake_records.verify_result = False
        store = FakeStore()
        deps = make_deps(store=store)
        reply = self.call(self.payload(), FakeIdentity(), deps)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["err"], "bad signature")
        self.assertEqual(store.put_calls, [])

    def test_register_invalid_name(self):
        deps = make_deps()
        reply = self.call(self.payload(name="No Spaces Allowed"),
                          FakeIdentity(), deps)
        self.assertFalse(reply["ok"])

    def test_register_missing_sig(self):
        deps = make_deps()
        reply = self.call(self.payload(sig=None), FakeIdentity(), deps)
        self.assertFalse(reply["ok"])

    def test_register_ttl_clamped(self):
        deps = make_deps()
        reply = self.call(self.payload(ttl=1), FakeIdentity(), deps)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["record"]["ttl"], service.TTL_MIN)
        reply = self.call(self.payload(ttl=10**10), FakeIdentity(), deps)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["record"]["ttl"], service.TTL_MAX)


class TestWhois(ServiceTestBase):
    def test_whois_registered(self):
        target = "cc" * 16
        store = FakeStore([make_record(target=target),
                           make_record(name="zed", target="dd" * 16)])
        deps = make_deps(store=store)
        reply = self.call({"v": 1, "op": "whois", "hash": target.upper()},
                          None, deps)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["hash"], target)
        self.assertEqual(len(reply["registered"]), 1)
        self.assertNotIn("sig", reply["registered"][0])

    def test_whois_invalid_hash(self):
        deps = make_deps()
        for bad in ("nothex", "cc" * 15, "", 5):
            reply = self.call({"v": 1, "op": "whois", "hash": bad},
                              None, deps)
            self.assertFalse(reply["ok"], bad)
            self.assertEqual(reply["err"], "invalid hash")


class TestRateLimit(ServiceTestBase):
    def test_rate_limited_after_max(self):
        limiter = service.RateLimiter(max_requests=3, window=60.0)
        deps = make_deps(rate_limiter=limiter)
        payload = {"op": "__manifest__"}
        for _ in range(3):
            reply = self.call(payload, None, deps)
            self.assertTrue(reply["ok"])
        reply = self.call(payload, None, deps)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["err"], "rate limited")

    def test_rate_limit_per_key(self):
        limiter = service.RateLimiter(max_requests=1, window=60.0)
        deps = make_deps(rate_limiter=limiter)
        payload = {"op": "__manifest__"}
        self.assertTrue(self.call(payload, FakeIdentity("aa" * 16),
                                  deps)["ok"])
        # different identity: separate budget
        self.assertTrue(self.call(payload, FakeIdentity("bb" * 16),
                                  deps)["ok"])
        # same identity again: over budget
        reply = self.call(payload, FakeIdentity("aa" * 16), deps)
        self.assertEqual(reply["err"], "rate limited")

    def test_window_slides(self):
        now = [0.0]
        limiter = service.RateLimiter(max_requests=2, window=10.0,
                                      clock=lambda: now[0])
        self.assertTrue(limiter.allow("k"))
        self.assertTrue(limiter.allow("k"))
        self.assertFalse(limiter.allow("k"))
        now[0] = 11.0
        self.assertTrue(limiter.allow("k"))


class TestSync(ServiceTestBase):
    def test_sync_disabled(self):
        deps = make_deps(sync_handler=None)
        for op in ("sync.offer", "sync.push"):
            reply = self.call({"v": 1, "op": op, "ids": []}, None, deps)
            self.assertFalse(reply["ok"])
            self.assertEqual(reply["err"], "sync disabled")

    def test_sync_handler_passthrough(self):
        seen = []

        def fake_sync(op, payload, store):
            seen.append((op, payload["ids"], store))
            return {"ok": True, "want": ["abc"]}

        store = FakeStore()
        deps = make_deps(store=store, sync_handler=fake_sync)
        reply = self.call({"v": 1, "op": "sync.offer", "ids": ["abc", "def"]},
                          None, deps)
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["want"], ["abc"])
        self.assertEqual(seen[0][0], "sync.offer")
        self.assertEqual(seen[0][1], ["abc", "def"])
        self.assertIs(seen[0][2], store)

    def test_sync_handler_none_reply_is_unknown_op(self):
        deps = make_deps(sync_handler=lambda op, payload, store: None)
        reply = self.call({"v": 1, "op": "sync.bogus"}, None, deps)
        self.assertFalse(reply["ok"])
        self.assertEqual(reply["err"], "unknown op")


if __name__ == "__main__":
    unittest.main()
