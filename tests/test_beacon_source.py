"""Tests for rns_resolve.beacon_source (module A3).

No network, no real Postgres: psycopg2 is mocked via sys.modules
injection. Verifies ranking math, graceful degradation on connect and
query failure, the one-retry rule, and SQL parameterization.
"""

import importlib
import math
import re
import sys
import time
import types
import unittest
from datetime import datetime, timedelta, timezone

from rns_resolve import beacon_source
from rns_resolve.beacon_source import (
    BeaconSource,
    HALFLIFE_SECONDS,
    REACHABLE_FACTOR,
    UNREACHABLE_FACTOR,
    WHOLE_WORD_BONUS,
    recency_decay,
)

ENV = {
    "BEACON_DB_HOST": "beacon-db.invalid",
    "BEACON_DB_PORT": "5432",
    "BEACON_DB_NAME": "beacon",
    "BEACON_DB_USER": "reader",
    "BEACON_DB_PASSWORD": "secret",
}


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    def execute(self, sql, params=None):
        self.conn.mod.executed.append((sql, params))
        if self.conn.mod.execute_failures > 0:
            self.conn.mod.execute_failures -= 1
            raise self.conn.mod.Error("query failed")

    def fetchall(self):
        return list(self.conn.mod.rows)

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, mod):
        self.mod = mod
        self.closed = False
        self.autocommit = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


def make_fake_psycopg2(rows=None, connect_failures=0, execute_failures=0):
    mod = types.ModuleType("psycopg2")
    mod.rows = rows or []
    mod.connect_failures = connect_failures
    mod.execute_failures = execute_failures
    mod.executed = []
    mod.connect_calls = []
    mod.Error = type("Error", (Exception,), {})

    def connect(**kwargs):
        mod.connect_calls.append(kwargs)
        if mod.connect_failures > 0:
            mod.connect_failures -= 1
            raise mod.Error("connect failed")
        return FakeConn(mod)

    mod.connect = connect
    return mod


class BeaconSourceTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = sys.modules.get("psycopg2")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            sys.modules.pop("psycopg2", None)
        else:
            sys.modules["psycopg2"] = self._saved

    def install(self, mod):
        sys.modules["psycopg2"] = mod
        return mod


class TestImportWithoutPsycopg2(BeaconSourceTestCase):
    def test_module_imports_and_degrades_without_psycopg2(self):
        # A None entry in sys.modules makes "import psycopg2" raise
        # ImportError, proving there is no import at module top level
        # and that a configured source degrades cleanly.
        sys.modules["psycopg2"] = None
        importlib.reload(beacon_source)
        src = beacon_source.BeaconSource(env=dict(ENV))
        self.assertEqual(src.candidates("anything"), [])
        self.assertFalse(src.available())

    def test_unconfigured_env_never_touches_db(self):
        sys.modules["psycopg2"] = None
        src = BeaconSource(env={})
        self.assertFalse(src.available())
        self.assertEqual(src.candidates("alpha"), [])


class TestRecencyDecay(unittest.TestCase):
    def test_now_is_one(self):
        now = time.time()
        self.assertAlmostEqual(recency_decay(now, now=now), 1.0, places=9)

    def test_one_halflife_is_half(self):
        now = time.time()
        self.assertAlmostEqual(
            recency_decay(now - HALFLIFE_SECONDS, now=now), 0.5, places=9
        )

    def test_two_halflives_is_quarter(self):
        now = time.time()
        self.assertAlmostEqual(
            recency_decay(now - 2 * HALFLIFE_SECONDS, now=now), 0.25, places=9
        )

    def test_none_and_garbage_are_zero(self):
        self.assertEqual(recency_decay(None), 0.0)
        self.assertEqual(recency_decay("not-a-date"), 0.0)
        self.assertEqual(recency_decay(object()), 0.0)

    def test_datetime_inputs(self):
        now_dt = datetime.now(timezone.utc)
        now = now_dt.timestamp()
        old = now_dt - timedelta(seconds=HALFLIFE_SECONDS)
        self.assertAlmostEqual(recency_decay(old, now=now), 0.5, places=6)
        # Naive datetimes are treated as UTC.
        naive = old.replace(tzinfo=None)
        self.assertAlmostEqual(recency_decay(naive, now=now), 0.5, places=6)

    def test_future_clamps_to_one(self):
        now = time.time()
        self.assertEqual(recency_decay(now + 999999, now=now), 1.0)


class TestRankingMath(BeaconSourceTestCase):
    def test_trust_formula_and_ordering(self):
        now_dt = datetime.now(timezone.utc)
        week_ago = now_dt - timedelta(seconds=HALFLIFE_SECONDS)
        rows = [
            ("A" * 32, "quasarke gateway", now_dt, 100, True),
            ("b" * 32, "quasarke gateway", now_dt, 100, False),
            ("c" * 32, "quasarke gateway", week_ago, 100, True),
        ]
        self.install(make_fake_psycopg2(rows=rows))
        src = BeaconSource(env=dict(ENV))
        out = src.candidates("gateway", limit=10)
        self.assertEqual(len(out), 3)
        self.assertTrue(src.available())

        base = math.log(1 + 100) * WHOLE_WORD_BONUS
        expected = {
            "a" * 32: base * 1.0 * REACHABLE_FACTOR,
            "b" * 32: base * 1.0 * UNREACHABLE_FACTOR,
            "c" * 32: base * 0.5 * REACHABLE_FACTOR,
        }
        for cand in out:
            self.assertAlmostEqual(
                cand["trust"], expected[cand["hash"]], places=2
            )
        # reachable fresh > unreachable fresh > reachable week-old
        # (1.15 > 0.85 > 0.575)
        self.assertEqual(
            [c["hash"] for c in out], ["a" * 32, "b" * 32, "c" * 32]
        )

    def test_announce_count_uses_log1p(self):
        now_dt = datetime.now(timezone.utc)
        rows = [
            ("a" * 32, "mynodeone", now_dt, 0, True),
            ("b" * 32, "mynodetwo", now_dt, 7, True),
        ]
        self.install(make_fake_psycopg2(rows=rows))
        src = BeaconSource(env=dict(ENV))
        out = src.candidates("node", limit=10)
        by_hash = {c["hash"]: c for c in out}
        # announce_count 0 -> ln(1) = 0 -> trust exactly 0.
        self.assertEqual(by_hash["a" * 32]["trust"], 0.0)
        self.assertAlmostEqual(
            by_hash["b" * 32]["trust"],
            math.log(8) * REACHABLE_FACTOR,
            places=2,
        )

    def test_whole_word_bonus_beats_plain_substring(self):
        now_dt = datetime.now(timezone.utc)
        rows = [
            ("a" * 32, "alphanode hub", now_dt, 50, True),
            ("b" * 32, "alpha node hub", now_dt, 50, True),
        ]
        self.install(make_fake_psycopg2(rows=rows))
        src = BeaconSource(env=dict(ENV))
        out = src.candidates("alpha", limit=10)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["hash"], "b" * 32)
        self.assertAlmostEqual(
            out[0]["trust"], out[1]["trust"] * WHOLE_WORD_BONUS, places=6
        )

    def test_case_insensitive_substring_and_shape(self):
        now_dt = datetime.now(timezone.utc)
        rows = [("A1" * 16, "QuAsArKe Node", now_dt, 3, True)]
        self.install(make_fake_psycopg2(rows=rows))
        src = BeaconSource(env=dict(ENV))
        out = src.candidates("quasarke", limit=5)
        self.assertEqual(len(out), 1)
        cand = out[0]
        self.assertEqual(
            sorted(cand.keys()),
            [
                "announce_count",
                "hash",
                "last_seen",
                "name",
                "reachable",
                "trust",
            ],
        )
        self.assertEqual(cand["hash"], "a1" * 16)  # lowercased
        self.assertEqual(cand["name"], "QuAsArKe Node")
        self.assertEqual(cand["announce_count"], 3)
        self.assertIs(cand["reachable"], True)
        self.assertIsInstance(cand["trust"], float)
        self.assertEqual(cand["last_seen"], now_dt.isoformat())

    def test_limit_respected(self):
        now_dt = datetime.now(timezone.utc)
        rows = [
            ("%032x" % i, "node %d" % i, now_dt, i + 1, True)
            for i in range(20)
        ]
        self.install(make_fake_psycopg2(rows=rows))
        src = BeaconSource(env=dict(ENV))
        self.assertEqual(len(src.candidates("node", limit=3)), 3)

    def test_empty_query_returns_empty(self):
        mod = self.install(make_fake_psycopg2(rows=[]))
        src = BeaconSource(env=dict(ENV))
        self.assertEqual(src.candidates(""), [])
        self.assertEqual(src.candidates("   "), [])
        self.assertEqual(mod.executed, [])


class TestDegradation(BeaconSourceTestCase):
    def test_connect_failure_returns_empty_and_unavailable(self):
        mod = self.install(make_fake_psycopg2(connect_failures=99))
        src = BeaconSource(env=dict(ENV))
        self.assertEqual(src.candidates("alpha"), [])
        self.assertFalse(src.available())
        # Lazy connect with one retry: exactly two connect attempts.
        self.assertEqual(len(mod.connect_calls), 2)

    def test_query_failure_retries_once_then_succeeds(self):
        now_dt = datetime.now(timezone.utc)
        rows = [("a" * 32, "alpha", now_dt, 5, True)]
        mod = self.install(
            make_fake_psycopg2(rows=rows, execute_failures=1)
        )
        src = BeaconSource(env=dict(ENV))
        out = src.candidates("alpha")
        self.assertEqual(len(out), 1)
        self.assertTrue(src.available())
        self.assertEqual(len(mod.connect_calls), 2)
        # One failed execute, then the two-query success pass (ILIKE + top).
        self.assertEqual(len(mod.executed), 3)

    def test_persistent_query_failure_gives_up_after_retry(self):
        mod = self.install(make_fake_psycopg2(execute_failures=99))
        src = BeaconSource(env=dict(ENV))
        self.assertEqual(src.candidates("alpha"), [])
        self.assertFalse(src.available())
        self.assertEqual(len(mod.executed), 2)

    def test_availability_flips_back_on_recovery(self):
        mod = self.install(make_fake_psycopg2(connect_failures=2))
        src = BeaconSource(env=dict(ENV))
        self.assertEqual(src.candidates("alpha"), [])
        self.assertFalse(src.available())
        # Backend recovers.
        now_dt = datetime.now(timezone.utc)
        mod.rows = [("a" * 32, "alpha", now_dt, 5, True)]
        out = src.candidates("alpha")
        self.assertEqual(len(out), 1)
        self.assertTrue(src.available())

    def test_lazy_connect_only_on_first_use(self):
        mod = self.install(make_fake_psycopg2(rows=[]))
        src = BeaconSource(env=dict(ENV))
        self.assertEqual(mod.connect_calls, [])
        src.candidates("alpha")
        self.assertEqual(len(mod.connect_calls), 1)
        src.candidates("beta")  # connection reused
        self.assertEqual(len(mod.connect_calls), 1)


class TestSqlParameterization(BeaconSourceTestCase):
    def test_no_literal_percent_in_sql_text(self):
        mod = self.install(make_fake_psycopg2(rows=[]))
        src = BeaconSource(env=dict(ENV))
        src.candidates("alpha")
        # Two queries per call: the ILIKE pre-filter and the top-nodes pull.
        self.assertEqual(len(mod.executed), 2)
        for sql, params in mod.executed:
            # Every % in the SQL text must be a %s placeholder.
            self.assertEqual(re.findall(r"%(?!s)", sql), [])

    def test_query_travels_as_bound_parameter(self):
        mod = self.install(make_fake_psycopg2(rows=[]))
        src = BeaconSource(env=dict(ENV))
        evil = "x'; DROP TABLE nodes; --"
        src.candidates(evil)
        sql, params = mod.executed[0]
        self.assertNotIn(evil, sql)
        self.assertIsInstance(params, tuple)
        self.assertEqual(len(params), 2)
        self.assertIn(evil, params[0])
        self.assertTrue(params[0].startswith("%"))
        self.assertTrue(params[0].endswith("%"))
        self.assertIsInstance(params[1], int)

    def test_like_wildcards_in_query_are_escaped(self):
        mod = self.install(make_fake_psycopg2(rows=[]))
        src = BeaconSource(env=dict(ENV))
        src.candidates("50%_done")
        sql, params = mod.executed[0]
        self.assertEqual(params[0], "%50\\%\\_done%")

    def test_connect_uses_env_credentials(self):
        mod = self.install(make_fake_psycopg2(rows=[]))
        src = BeaconSource(env=dict(ENV))
        src.candidates("alpha")
        kw = mod.connect_calls[0]
        self.assertEqual(kw["host"], ENV["BEACON_DB_HOST"])
        self.assertEqual(kw["port"], 5432)
        self.assertEqual(kw["dbname"], ENV["BEACON_DB_NAME"])
        self.assertEqual(kw["user"], ENV["BEACON_DB_USER"])
        self.assertEqual(kw["password"], ENV["BEACON_DB_PASSWORD"])


if __name__ == "__main__":
    unittest.main()
