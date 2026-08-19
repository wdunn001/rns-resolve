"""Unit tests for rns_resolve.records (stdlib unittest, no network).

RNS-dependent paths are guarded with skipUnless. msgpack-dependent paths
(canonical_bytes and friends) are guarded on msgpack availability, which
is satisfied by either the standalone umsgpack package or the RNS
vendored copy."""

import unittest

from rns_resolve import records
from rns_resolve.records import (
    HASH_RE,
    TTL_DEFAULT,
    TTL_MAX,
    TTL_MIN,
    clamp_ttl,
    make_record,
    normalize_name,
)


def _have_rns():
    try:
        import RNS  # noqa: F401
        return True
    except ImportError:
        return False


def _have_msgpack():
    try:
        records._msgpack()
        return True
    except ImportError:
        return False


HAVE_RNS = _have_rns()
HAVE_MSGPACK = _have_msgpack()

IDENTITY_HEX = "00112233445566778899aabbccddeeff"


class TestHashRe(unittest.TestCase):
    def test_matches_32_hex(self):
        self.assertTrue(HASH_RE.match("0" * 32))
        self.assertTrue(HASH_RE.match("abcdef0123456789" * 2))
        self.assertTrue(HASH_RE.match("ABCDEF0123456789" * 2))

    def test_rejects_wrong_length(self):
        self.assertIsNone(HASH_RE.match("0" * 31))
        self.assertIsNone(HASH_RE.match("0" * 33))
        self.assertIsNone(HASH_RE.match(""))

    def test_rejects_non_hex(self):
        self.assertIsNone(HASH_RE.match("g" * 32))
        self.assertIsNone(HASH_RE.match("0" * 30 + "zz"))
        self.assertIsNone(HASH_RE.match("0" * 31 + " "))


class TestNormalizeName(unittest.TestCase):
    # --- accepted forms ---

    def test_simple(self):
        self.assertEqual(normalize_name("alice"), "alice")

    def test_lowercased(self):
        self.assertEqual(normalize_name("Alice"), "alice")
        self.assertEqual(normalize_name("ALICE.NODE"), "alice.node")

    def test_digits_and_allowed_punct(self):
        self.assertEqual(normalize_name("node-42_x.y0"), "node-42_x.y0")

    def test_underscore_edges_allowed(self):
        # Only "-" and "." are forbidden at label edges.
        self.assertEqual(normalize_name("_alice_"), "_alice_")

    def test_interior_hyphen_ok(self):
        self.assertEqual(normalize_name("a-b.c-d"), "a-b.c-d")

    def test_surrounding_whitespace_stripped(self):
        self.assertEqual(normalize_name("  alice \n"), "alice")

    def test_single_char_label(self):
        self.assertEqual(normalize_name("a"), "a")
        self.assertEqual(normalize_name("a.b.c"), "a.b.c")

    # --- label depth cap 3 ---

    def test_three_labels_ok(self):
        self.assertEqual(normalize_name("a.b.c"), "a.b.c")

    def test_four_labels_rejected(self):
        with self.assertRaises(ValueError):
            normalize_name("a.b.c.d")

    # --- label length cap 32 ---

    def test_label_32_ok(self):
        label = "x" * 32
        self.assertEqual(normalize_name(label), label)

    def test_label_33_rejected(self):
        with self.assertRaises(ValueError):
            normalize_name("x" * 33)

    def test_long_label_in_second_position_rejected(self):
        with self.assertRaises(ValueError):
            normalize_name("ok." + "x" * 33)

    # --- total length cap 64 ---

    def test_total_64_ok(self):
        # 32 + 1 + 31 = 64 chars, 2 labels of legal length
        name = "x" * 32 + "." + "y" * 31
        self.assertEqual(len(name), 64)
        self.assertEqual(normalize_name(name), name)

    def test_total_65_rejected(self):
        name = "x" * 32 + "." + "y" * 32  # 65 chars, labels individually legal
        self.assertEqual(len(name), 65)
        with self.assertRaises(ValueError):
            normalize_name(name)

    # --- empty / degenerate ---

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            normalize_name("")

    def test_whitespace_only_rejected(self):
        with self.assertRaises(ValueError):
            normalize_name("   ")

    def test_non_string_rejected(self):
        for bad in (None, 5, b"alice", ["a"]):
            with self.assertRaises(ValueError):
                normalize_name(bad)

    def test_empty_labels_rejected(self):
        for bad in (".", "a..b", ".a", "a.", "..", "a.b."):
            with self.assertRaises(ValueError):
                normalize_name(bad)

    # --- edge chars per label ---

    def test_leading_trailing_hyphen_rejected(self):
        for bad in ("-a", "a-", "-a-", "a.-b", "a.b-", "-"):
            with self.assertRaises(ValueError):
                normalize_name(bad)

    # --- bad characters ---

    def test_bad_ascii_chars_rejected(self):
        for bad in ("a b", "a/b", "a@b", "a:b", "a+b", "a!b", "a,b",
                    "a\tb", "a\x00b", "a'b", 'a"b', "a`b", "a#b", "a%b"):
            with self.assertRaises(ValueError):
                normalize_name(bad)

    def test_unicode_rejected(self):
        for bad in ("caf\xe9",              # cafe with e-acute
                    "\xfcber",              # uber with u-umlaut
                    "\u65e5\u672c",           # CJK
                    "na\xefve",             # i-diaeresis
                    "\U0001f600"):            # emoji
            with self.assertRaises(ValueError):
                normalize_name(bad)

    def test_fullwidth_not_folded(self):
        # NFC (not NFKC) is specified: fullwidth letters stay non-ASCII
        # and must be rejected, not silently folded to ascii.
        with self.assertRaises(ValueError):
            normalize_name("\uff41\uff42\uff43")  # fullwidth "abc"

    def test_nfc_composed_and_decomposed_agree(self):
        # "e" + combining acute composes under NFC to the same rejected
        # code point as precomposed e-acute: both must fail identically.
        composed = "caf\xe9"
        decomposed = "cafe\u0301"
        self.assertEqual(len(composed), 4)
        self.assertEqual(len(decomposed), 5)
        with self.assertRaises(ValueError):
            normalize_name(composed)
        with self.assertRaises(ValueError):
            normalize_name(decomposed)

    def test_combining_mark_alone_rejected(self):
        with self.assertRaises(ValueError):
            normalize_name("a\u0301")

    def test_unicode_dot_lookalikes_rejected(self):
        # One-dot leader / ideographic full stop must not act as label
        # separators.
        for bad in ("a\u2024b", "a\u3002b"):
            with self.assertRaises(ValueError):
                normalize_name(bad)


class TestClampTtl(unittest.TestCase):
    def test_default_on_none(self):
        self.assertEqual(clamp_ttl(None), TTL_DEFAULT)

    def test_in_range_passthrough(self):
        self.assertEqual(clamp_ttl(7200), 7200)
        self.assertEqual(clamp_ttl(TTL_MIN), TTL_MIN)
        self.assertEqual(clamp_ttl(TTL_MAX), TTL_MAX)

    def test_clamp_low(self):
        self.assertEqual(clamp_ttl(0), TTL_MIN)
        self.assertEqual(clamp_ttl(-5), TTL_MIN)
        self.assertEqual(clamp_ttl(TTL_MIN - 1), TTL_MIN)

    def test_clamp_high(self):
        self.assertEqual(clamp_ttl(TTL_MAX + 1), TTL_MAX)
        self.assertEqual(clamp_ttl(10 ** 12), TTL_MAX)

    def test_bad_ttl_raises(self):
        with self.assertRaises(ValueError):
            clamp_ttl("soon")
        with self.assertRaises(ValueError):
            clamp_ttl([])

    def test_constants(self):
        self.assertEqual(TTL_MIN, 3600)
        self.assertEqual(TTL_MAX, 365 * 86400)
        self.assertEqual(TTL_DEFAULT, 30 * 86400)


class TestMakeRecord(unittest.TestCase):
    def test_defaults(self):
        rec = make_record("Alice", IDENTITY_HEX)
        self.assertEqual(rec["v"], 1)
        self.assertEqual(rec["name"], "alice")
        self.assertEqual(rec["identity"], IDENTITY_HEX)
        self.assertEqual(rec["app"], "nomadnetwork")
        self.assertEqual(rec["aspects"], ["node"])
        self.assertEqual(rec["target"], "")
        self.assertEqual(rec["ttl"], TTL_DEFAULT)
        self.assertIsNone(rec["sig"])
        self.assertIsInstance(rec["ts"], float)

    def test_identity_lowercased(self):
        rec = make_record("alice", IDENTITY_HEX.upper())
        self.assertEqual(rec["identity"], IDENTITY_HEX)

    def test_bad_identity_rejected(self):
        with self.assertRaises(ValueError):
            make_record("alice", "nothex")
        with self.assertRaises(ValueError):
            make_record("alice", "00" * 15)

    def test_ttl_clamped(self):
        self.assertEqual(make_record("a", IDENTITY_HEX, ttl=1)["ttl"], TTL_MIN)
        self.assertEqual(
            make_record("a", IDENTITY_HEX, ttl=10 ** 12)["ttl"], TTL_MAX)

    def test_name_normalized(self):
        with self.assertRaises(ValueError):
            make_record("bad name!", IDENTITY_HEX)


@unittest.skipUnless(HAVE_MSGPACK, "no msgpack implementation available")
class TestCanonicalBytes(unittest.TestCase):
    def _rec(self, **kw):
        base = dict(name="alice", identity_hash_hex=IDENTITY_HEX,
                    ts=1700000000.0, ttl=86400)
        base.update(kw)
        return make_record(**base)

    def test_deterministic(self):
        a = records.canonical_bytes(self._rec())
        b = records.canonical_bytes(self._rec())
        self.assertEqual(a, b)
        self.assertIsInstance(a, bytes)

    def test_excludes_target_and_sig(self):
        a = self._rec()
        b = self._rec()
        b["target"] = "ff" * 16
        b["sig"] = b"\x01\x02\x03"
        self.assertEqual(records.canonical_bytes(a), records.canonical_bytes(b))

    def test_field_changes_change_bytes(self):
        base = records.canonical_bytes(self._rec())
        for kw in (dict(name="bob"),
                   dict(identity_hash_hex="ff" * 16),
                   dict(app="other"),
                   dict(aspects=["page"]),
                   dict(ts=1700000001.0),
                   dict(ttl=7200)):
            self.assertNotEqual(base, records.canonical_bytes(self._rec(**kw)))

    def test_roundtrip_shape(self):
        umsgpack = records._msgpack()
        rec = self._rec()
        out = umsgpack.unpackb(records.canonical_bytes(rec))
        self.assertEqual(list(out), [1, "alice", IDENTITY_HEX, "nomadnetwork",
                                     ["node"], 1700000000.0, 86400])


@unittest.skipUnless(HAVE_MSGPACK, "no msgpack implementation available")
class TestRecordId(unittest.TestCase):
    def test_shape(self):
        rec = make_record("alice", IDENTITY_HEX, ts=1.0, ttl=86400)
        rid = records.record_id(rec)
        self.assertEqual(len(rid), 32)
        self.assertTrue(HASH_RE.match(rid))

    def test_stable_and_sensitive(self):
        a = make_record("alice", IDENTITY_HEX, ts=1.0, ttl=86400)
        b = make_record("alice", IDENTITY_HEX, ts=1.0, ttl=86400)
        c = make_record("bob", IDENTITY_HEX, ts=1.0, ttl=86400)
        self.assertEqual(records.record_id(a), records.record_id(b))
        self.assertNotEqual(records.record_id(a), records.record_id(c))

    def test_ignores_target_and_sig(self):
        a = make_record("alice", IDENTITY_HEX, ts=1.0, ttl=86400)
        b = dict(a, target="ff" * 16, sig=b"sig")
        self.assertEqual(records.record_id(a), records.record_id(b))


class TestDeriveTargetValidation(unittest.TestCase):
    def test_bad_hex_rejected_without_rns(self):
        # Validation happens before the lazy RNS import.
        with self.assertRaises(ValueError):
            records.derive_target("nothex", "nomadnetwork", ["node"])
        with self.assertRaises(ValueError):
            records.derive_target("00" * 15, "nomadnetwork", ["node"])


@unittest.skipUnless(HAVE_RNS, "RNS not installed")
class TestDeriveTargetRns(unittest.TestCase):
    def test_matches_rns_destination_hash(self):
        import RNS
        expected = RNS.Destination.hash(
            bytes.fromhex(IDENTITY_HEX), "rnsresolve", "query").hex()
        got = records.derive_target(IDENTITY_HEX, "rnsresolve", ["query"])
        self.assertEqual(got, expected)
        self.assertTrue(HASH_RE.match(got))
        self.assertEqual(got, got.lower())

    def test_aspects_matter(self):
        a = records.derive_target(IDENTITY_HEX, "nomadnetwork", ["node"])
        b = records.derive_target(IDENTITY_HEX, "nomadnetwork", ["page"])
        c = records.derive_target(IDENTITY_HEX, "other", ["node"])
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, c)

    def test_multiple_aspects(self):
        import RNS
        expected = RNS.Destination.hash(
            bytes.fromhex(IDENTITY_HEX), "app", "a", "b").hex()
        self.assertEqual(
            records.derive_target(IDENTITY_HEX, "app", ["a", "b"]), expected)


@unittest.skipUnless(HAVE_RNS, "RNS not installed")
class TestSignVerify(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import RNS
        cls.identity = RNS.Identity()
        cls.other = RNS.Identity()

    def _rec(self):
        return make_record("alice", self.identity.hash.hex(),
                           ts=1700000000.0, ttl=86400)

    def test_roundtrip(self):
        rec = self._rec()
        rec["sig"] = records.sign_record(rec, self.identity)
        self.assertIsInstance(rec["sig"], bytes)
        self.assertTrue(records.verify_record(rec, self.identity))

    def test_tamper_fails(self):
        rec = self._rec()
        rec["sig"] = records.sign_record(rec, self.identity)
        rec["name"] = "mallory"
        self.assertFalse(records.verify_record(rec, self.identity))

    def test_wrong_identity_fails(self):
        rec = self._rec()
        rec["sig"] = records.sign_record(rec, self.identity)
        self.assertFalse(records.verify_record(rec, self.other))

    def test_missing_sig_fails(self):
        rec = self._rec()
        self.assertFalse(records.verify_record(rec, self.identity))
        rec["sig"] = b""
        self.assertFalse(records.verify_record(rec, self.identity))

    def test_garbage_sig_fails(self):
        rec = self._rec()
        rec["sig"] = b"not a real signature"
        self.assertFalse(records.verify_record(rec, self.identity))

    def test_verify_never_raises(self):
        # Broken record shapes return False rather than raising.
        self.assertFalse(records.verify_record({}, self.identity))
        self.assertFalse(records.verify_record({"sig": b"x"}, self.identity))
        self.assertFalse(records.verify_record({"sig": b"x"}, None))


if __name__ == "__main__":
    unittest.main()
