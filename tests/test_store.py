"""Unit tests for rns_resolve.store (module A2).

No network, no RNS required. Uses a tmpdir sqlite database and fabricated
record dicts built by hand. If rns_resolve.records is not present yet
(built by another agent), a contract-faithful stub providing record_id is
installed into sys.modules before rns_resolve.store is imported.
"""

import hashlib
import os
import sqlite3
import sys
import tempfile
import time
import types
import unittest

try:
    import rns_resolve.records  # noqa: F401
except Exception:
    stub = types.ModuleType("rns_resolve.records")

    def _canonical_bytes(rec):
        try:
            try:
                import umsgpack
            except ImportError:
                from RNS.vendor import umsgpack
            return umsgpack.packb(
                [1, rec["name"], rec["identity"], rec["app"],
                 rec["aspects"], rec["ts"], rec["ttl"]]
            )
        except Exception:
            # deterministic stdlib fallback; ids only need to be
            # consistent within this test run
            return repr(
                [1, rec["name"], rec["identity"], rec["app"],
                 rec["aspects"], rec["ts"], rec["ttl"]]
            ).encode("utf-8")

    def record_id(rec):
        return hashlib.sha256(_canonical_bytes(rec)).digest()[:16].hex()

    stub.record_id = record_id
    sys.modules["rns_resolve.records"] = stub

from rns_resolve.records import record_id
from rns_resolve.store import Store

ID_A = "aa" * 16
ID_B = "bb" * 16
TGT_A = "cc" * 16
TGT_B = "dd" * 16


def make_rec(name, identity=ID_A, target=TGT_A, ts=None, ttl=3600,
             sig=b"\x01\x02sig", app="nomadnetwork", aspects=None):
    return {
        "v": 1,
        "name": name,
        "identity": identity,
        "app": app,
        "aspects": ["node"] if aspects is None else aspects,
        "target": target,
        "ts": time.time() if ts is None else ts,
        "ttl": ttl,
        "sig": sig,
    }


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "resolve.db")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # -- setup / schema ----------------------------------------------------

    def test_wal_mode(self):
        conn = sqlite3.connect(self.path)
        try:
            mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(mode.lower(), "wal")

    def test_creates_parent_directory(self):
        deep = os.path.join(self.tmp.name, "sub", "dir", "r.db")
        s = Store(deep)
        try:
            self.assertTrue(os.path.exists(deep))
        finally:
            s.close()

    # -- put / upsert ------------------------------------------------------

    def test_put_returns_record_id_and_counts(self):
        rec = make_rec("alpha", ts=1000.0)
        rid = self.store.put(rec)
        self.assertEqual(rid, record_id(rec))
        self.assertEqual(self.store.count(), 1)

    def test_put_same_record_is_upsert(self):
        rec = make_rec("alpha", ts=1000.0, ttl=10 ** 9)
        rid1 = self.store.put(rec)
        rid2 = self.store.put(dict(rec))
        self.assertEqual(rid1, rid2)
        self.assertEqual(self.store.count(), 1)

    def test_put_upsert_preserves_last_used(self):
        rec = make_rec("alpha", ts=1000.0, ttl=10 ** 9)
        rid = self.store.put(rec)
        self.store.touch_use(rid)
        first = self._last_used(rid)
        self.assertIsNotNone(first)
        self.store.put(dict(rec))
        self.assertEqual(self._last_used(rid), first)

    def test_put_changed_record_gets_new_id(self):
        r1 = make_rec("alpha", ts=1000.0, ttl=10 ** 9)
        r2 = make_rec("alpha", ts=2000.0, ttl=10 ** 9)
        rid1 = self.store.put(r1)
        rid2 = self.store.put(r2)
        self.assertNotEqual(rid1, rid2)
        self.assertEqual(self.store.count(), 2)

    # -- resolve -----------------------------------------------------------

    def test_resolve_exact_name_newest_first(self):
        now = time.time()
        old = make_rec("alpha", ts=now - 100, ttl=10 ** 6)
        new = make_rec("alpha", ts=now - 1, ttl=10 ** 6, identity=ID_B,
                       target=TGT_B)
        other = make_rec("beta", ts=now, ttl=10 ** 6)
        for r in (old, new, other):
            self.store.put(r)
        got = self.store.resolve("alpha")
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0]["identity"], ID_B)
        self.assertEqual(got[1]["identity"], ID_A)
        self.assertTrue(all(g["name"] == "alpha" for g in got))

    def test_resolve_excludes_expired(self):
        now = time.time()
        expired = make_rec("alpha", ts=now - 7200, ttl=3600)
        live = make_rec("alpha", ts=now, ttl=3600, identity=ID_B)
        self.store.put(expired)
        self.store.put(live)
        got = self.store.resolve("alpha")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["identity"], ID_B)

    def test_resolve_roundtrips_record_fields(self):
        rec = make_rec("alpha.node", ts=1234.5, ttl=9999,
                       aspects=["node", "page"], sig=b"\x00\xffsig")
        rid = self.store.put(rec)
        # record put with a fresh ts so it is not expired
        rec2 = make_rec("gamma", ts=time.time(), ttl=3600,
                        aspects=["node", "page"], sig=b"\x00\xffsig")
        rid2 = self.store.put(rec2)
        got = self.store.resolve("gamma")[0]
        self.assertEqual(got["id"], rid2)
        self.assertEqual(got["v"], 1)
        self.assertEqual(got["aspects"], ["node", "page"])
        self.assertEqual(got["sig"], b"\x00\xffsig")
        self.assertEqual(got["app"], "nomadnetwork")
        self.assertFalse(got["attested"])

    def test_resolve_unknown_name_empty(self):
        self.assertEqual(self.store.resolve("nope"), [])

    # -- prefix ------------------------------------------------------------

    def test_prefix_matches_and_limit(self):
        now = time.time()
        for i in range(5):
            self.store.put(make_rec("alpha%d" % i, ts=now - i, ttl=10 ** 6))
        self.store.put(make_rec("beta", ts=now, ttl=10 ** 6))
        got = self.store.prefix("alpha")
        self.assertEqual(len(got), 5)
        got3 = self.store.prefix("alpha", limit=3)
        self.assertEqual(len(got3), 3)
        # newest first
        self.assertEqual(got3[0]["name"], "alpha0")

    def test_prefix_excludes_expired(self):
        now = time.time()
        self.store.put(make_rec("alpha1", ts=now - 7200, ttl=3600))
        self.store.put(make_rec("alpha2", ts=now, ttl=3600))
        got = self.store.prefix("alpha")
        self.assertEqual([g["name"] for g in got], ["alpha2"])

    def test_prefix_underscore_is_literal_not_wildcard(self):
        now = time.time()
        self.store.put(make_rec("a_c", ts=now, ttl=10 ** 6))
        self.store.put(make_rec("abc", ts=now, ttl=10 ** 6))
        got = self.store.prefix("a_")
        self.assertEqual([g["name"] for g in got], ["a_c"])

    # -- whois -------------------------------------------------------------

    def test_whois_by_target(self):
        now = time.time()
        self.store.put(make_rec("alpha", target=TGT_A, ts=now, ttl=10 ** 6))
        self.store.put(make_rec("beta", target=TGT_B, ts=now, ttl=10 ** 6))
        got = self.store.whois(TGT_A)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["name"], "alpha")
        self.assertEqual(self.store.whois("ee" * 16), [])

    def test_whois_case_insensitive_input(self):
        now = time.time()
        self.store.put(make_rec("alpha", target=TGT_A, ts=now, ttl=10 ** 6))
        got = self.store.whois(TGT_A.upper())
        self.assertEqual(len(got), 1)

    # -- peer-facing: all_ids / get_many / missing -------------------------

    def test_get_many_returns_requested_and_excludes_attested(self):
        now = time.time()
        signed = make_rec("alpha", ts=now, ttl=10 ** 6)
        attested = make_rec("beta", ts=now, ttl=10 ** 6, sig=None)
        rid_s = self.store.put(signed)
        rid_a = self.store.put(attested)
        got = self.store.get_many([rid_s, rid_a, "ff" * 16])
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["id"], rid_s)
        self.assertEqual(got[0]["sig"], signed["sig"])

    def test_attested_flag_set_when_sig_none(self):
        now = time.time()
        rid = self.store.put(make_rec("beta", ts=now, ttl=10 ** 6, sig=None))
        got = self.store.resolve("beta")
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["attested"])
        self.assertIsNone(got[0]["sig"])
        # attested records still resolve locally, just never go to peers
        self.assertEqual(self.store.get_many([rid]), [])

    def test_all_ids_offers_only_replicable_records(self):
        now = time.time()
        rid_s = self.store.put(make_rec("alpha", ts=now, ttl=10 ** 6))
        rid_a = self.store.put(
            make_rec("beta", ts=now, ttl=10 ** 6, sig=None)
        )
        ids = self.store.all_ids()
        self.assertIn(rid_s, ids)
        self.assertNotIn(rid_a, ids)

    def test_missing_returns_unknown_subset_in_order(self):
        now = time.time()
        rid = self.store.put(make_rec("alpha", ts=now, ttl=10 ** 6))
        unknown1 = "11" * 16
        unknown2 = "22" * 16
        got = self.store.missing([unknown1, rid, unknown2])
        self.assertEqual(got, [unknown1, unknown2])
        self.assertEqual(self.store.missing([rid]), [])
        self.assertEqual(self.store.missing([]), [])

    def test_missing_counts_attested_as_present(self):
        now = time.time()
        rid = self.store.put(make_rec("beta", ts=now, ttl=10 ** 6, sig=None))
        self.assertEqual(self.store.missing([rid]), [])

    def test_get_many_handles_large_id_list(self):
        now = time.time()
        rid = self.store.put(make_rec("alpha", ts=now, ttl=10 ** 6))
        ids = ["%032x" % i for i in range(1200)] + [rid]
        got = self.store.get_many(ids)
        self.assertEqual([g["id"] for g in got], [rid])
        miss = self.store.missing(ids)
        self.assertEqual(len(miss), 1200)

    # -- expire_sweep / count / touch_use ----------------------------------

    def test_expire_sweep_deletes_and_counts(self):
        now = time.time()
        self.store.put(make_rec("old1", ts=now - 7200, ttl=3600))
        self.store.put(make_rec("old2", ts=now - 9000, ttl=3600))
        self.store.put(make_rec("live", ts=now, ttl=3600))
        self.assertEqual(self.store.count(), 3)
        deleted = self.store.expire_sweep()
        self.assertEqual(deleted, 2)
        self.assertEqual(self.store.count(), 1)
        self.assertEqual(self.store.expire_sweep(), 0)

    def test_touch_use_sets_last_used(self):
        rec = make_rec("alpha", ts=time.time(), ttl=3600)
        rid = self.store.put(rec)
        self.assertIsNone(self._last_used(rid))
        before = time.time()
        self.store.touch_use(rid)
        val = self._last_used(rid)
        self.assertIsNotNone(val)
        self.assertGreaterEqual(val, before - 1)
        # touching an unknown id is a no-op, not an error
        self.store.touch_use("ff" * 16)

    # -- helpers -----------------------------------------------------------

    def _last_used(self, rid):
        conn = sqlite3.connect(self.path)
        try:
            row = conn.execute(
                "SELECT last_used FROM records WHERE id=?", (rid,)
            ).fetchone()
        finally:
            conn.close()
        return None if row is None else row[0]


if __name__ == "__main__":
    unittest.main()
