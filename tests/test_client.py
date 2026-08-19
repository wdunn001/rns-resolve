"""Tests for rns_resolve.client (module A5).

No network. resolve_remote is stubbed in all CLI-flow tests. RNS-dependent
paths are behind skipUnless. If sibling modules records.py / petnames.py
are not present yet (parallel agents own them), contract-conformant stubs
are installed into sys.modules so this module tests client logic in
isolation; when the real modules exist they are used instead.
"""

import io
import json
import os
import re
import sys
import tempfile
import types
import unicodedata
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock


def _install_records_stub():
    mod = types.ModuleType("rns_resolve.records")
    mod.HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")

    def normalize_name(s):
        if not isinstance(s, str):
            raise ValueError("name must be a string")
        s = unicodedata.normalize("NFC", s).lower()
        if not 1 <= len(s) <= 64:
            raise ValueError("name length out of range")
        labels = s.split(".")
        if len(labels) > 3:
            raise ValueError("too many labels")
        for label in labels:
            if not 1 <= len(label) <= 32:
                raise ValueError("label length out of range")
            if not re.fullmatch(r"[a-z0-9_-]+", label):
                raise ValueError("invalid characters in label")
            if label[0] == "-" or label[-1] == "-":
                raise ValueError("label starts or ends with -")
        return s

    def sign_record(rec, identity):
        raise RuntimeError("stub sign_record should not be called in tests")

    mod.normalize_name = normalize_name
    mod.sign_record = sign_record
    sys.modules["rns_resolve.records"] = mod


def _install_petnames_stub():
    mod = types.ModuleType("rns_resolve.petnames")

    class PetnameTable:
        def __init__(self, path=None):
            if path is None:
                path = os.path.join(os.path.expanduser("~"),
                                    ".rns_resolve", "petnames.json")
            self.path = path
            self._data = {}
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    self._data = loaded
            except Exception:
                self._data = {}

        def _save(self):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)

        def get(self, name_norm):
            return self._data.get(name_norm)

        def pin(self, name_norm, hash_hex, source):
            import time as _time
            now = _time.time()
            prev = self._data.get(name_norm)
            self._data[name_norm] = {
                "hash": hash_hex,
                "source": source,
                "first_seen": prev["first_seen"] if prev else now,
                "last_verified": now,
            }
            self._save()

        def unpin(self, name_norm):
            if name_norm in self._data:
                del self._data[name_norm]
                self._save()
                return True
            return False

        def changed(self, name_norm, hash_hex):
            entry = self._data.get(name_norm)
            return entry is not None and entry["hash"] != hash_hex

        def all(self):
            return dict(self._data)

    mod.PetnameTable = PetnameTable
    sys.modules["rns_resolve.petnames"] = mod


try:
    import rns_resolve.records  # noqa: F401
except ImportError:
    _install_records_stub()
try:
    import rns_resolve.petnames  # noqa: F401
except ImportError:
    _install_petnames_stub()

from rns_resolve import client  # noqa: E402

try:
    import RNS  # noqa: F401
    HAVE_RNS = True
except Exception:
    HAVE_RNS = False


H1 = "aa" * 16
H2 = "bb" * 16
H3 = "cc" * 16
RESOLVER = "dd" * 16


def make_reply(registered=(), announced=(), q="mynode"):
    return {"ok": True, "q": q,
            "registered": list(registered), "announced": list(announced)}


def reg(target, name="mynode"):
    return {"id": "00" * 8, "name": name, "identity": "ee" * 16,
            "app": "nomadnetwork", "aspects": ["node"], "target": target,
            "ts": 1000.0, "ttl": 86400, "expires": 1000.0 + 86400}


def ann(hash_hex, name="mynode", trust=1.0):
    return {"hash": hash_hex, "name": name, "trust": trust,
            "last_seen": "2026-08-17T00:00:00Z", "announce_count": 3,
            "reachable": True}


class FakePetnames:
    def __init__(self):
        self.data = {}

    def get(self, name_norm):
        return self.data.get(name_norm)

    def pin(self, name_norm, hash_hex, source):
        self.data[name_norm] = {"hash": hash_hex, "source": source,
                                "first_seen": 0.0, "last_verified": 0.0}


class ClassifyTests(unittest.TestCase):
    def test_hash_input(self):
        pet = FakePetnames()
        self.assertEqual(client.classify(H1, pet), ("hash", H1))

    def test_hash_input_uppercase_lowered(self):
        pet = FakePetnames()
        kind, value = client.classify(H1.upper(), pet)
        self.assertEqual(kind, "hash")
        self.assertEqual(value, H1)

    def test_hash_wins_over_petnames(self):
        # A 32-hex string is a hash even if someone pinned it as a name.
        pet = FakePetnames()
        pet.data[H1] = {"hash": H2, "source": "manual",
                        "first_seen": 0.0, "last_verified": 0.0}
        self.assertEqual(client.classify(H1, pet), ("hash", H1))

    def test_wrong_length_hex_is_a_name_not_a_hash(self):
        # 30 hex chars does not match HASH_RE, so it is a name (miss).
        pet = FakePetnames()
        kind, value = client.classify("a" * 30, pet)
        self.assertEqual(kind, "miss")
        self.assertEqual(value, "a" * 30)

    def test_petname_hit(self):
        pet = FakePetnames()
        pet.pin("mynode", H2, "manual")
        self.assertEqual(client.classify("MyNode", pet), ("petname", H2))

    def test_miss_returns_normalized(self):
        pet = FakePetnames()
        self.assertEqual(client.classify("MyNode", pet), ("miss", "mynode"))

    def test_invalid_name_raises(self):
        pet = FakePetnames()
        with self.assertRaises(ValueError):
            client.classify("not a name!", pet)

    def test_too_many_labels_raises(self):
        pet = FakePetnames()
        with self.assertRaises(ValueError):
            client.classify("a.b.c.d", pet)


class ChooseCandidateTests(unittest.TestCase):
    def test_auto_one_registered_zero_announced(self):
        cand, source = client.choose_candidate(make_reply([reg(H1)]))
        self.assertEqual(source, "auto")
        self.assertEqual(client.candidate_hash(cand), H1)

    def test_no_auto_with_announced_conflict(self):
        cand, source = client.choose_candidate(
            make_reply([reg(H1)], [ann(H2)]))
        self.assertIsNone(cand)
        self.assertIsNone(source)

    def test_no_auto_with_two_registered(self):
        cand, source = client.choose_candidate(
            make_reply([reg(H1), reg(H2)]))
        self.assertIsNone(cand)

    def test_no_auto_announced_only(self):
        cand, source = client.choose_candidate(make_reply([], [ann(H2)]))
        self.assertIsNone(cand)

    def test_pin_index_combined_order(self):
        reply = make_reply([reg(H1)], [ann(H2)])
        cand, _ = client.choose_candidate(reply, pin_index=0)
        self.assertEqual(client.candidate_hash(cand), H1)
        cand, _ = client.choose_candidate(reply, pin_index=1)
        self.assertEqual(client.candidate_hash(cand), H2)

    def test_pin_index_out_of_range(self):
        with self.assertRaises(IndexError):
            client.choose_candidate(make_reply([reg(H1)]), pin_index=5)


class TofuTests(unittest.TestCase):
    def test_auto_pin_when_unpinned(self):
        pet = FakePetnames()
        result = client.apply_tofu(pet, "mynode", make_reply([reg(H1)]))
        self.assertTrue(result["pinned"])
        self.assertFalse(result["changed"])
        self.assertEqual(result["hash"], H1)
        self.assertEqual(result["source"], "auto")
        self.assertEqual(pet.data["mynode"]["hash"], H1)

    def test_no_auto_pin_with_conflict(self):
        pet = FakePetnames()
        result = client.apply_tofu(pet, "mynode",
                                   make_reply([reg(H1)], [ann(H2)]))
        self.assertFalse(result["pinned"])
        self.assertNotIn("mynode", pet.data)

    def test_explicit_pin_index(self):
        pet = FakePetnames()
        result = client.apply_tofu(pet, "mynode",
                                   make_reply([reg(H1)], [ann(H2)]),
                                   pin_index=1)
        self.assertTrue(result["pinned"])
        self.assertEqual(pet.data["mynode"]["hash"], H2)

    def test_pinned_same_hash_no_change(self):
        pet = FakePetnames()
        pet.pin("mynode", H1, "manual")
        result = client.apply_tofu(pet, "mynode", make_reply([reg(H1)]))
        self.assertFalse(result["changed"])
        self.assertFalse(result["pinned"])
        self.assertEqual(pet.data["mynode"]["hash"], H1)

    def test_pinned_changed_without_repin_not_overwritten(self):
        pet = FakePetnames()
        pet.pin("mynode", H1, "manual")
        result = client.apply_tofu(pet, "mynode", make_reply([reg(H2)]),
                                   repin=False)
        self.assertTrue(result["changed"])
        self.assertFalse(result["pinned"])
        self.assertEqual(result["previous"], H1)
        self.assertEqual(result["hash"], H2)
        self.assertEqual(pet.data["mynode"]["hash"], H1)

    def test_pinned_changed_with_repin_overwritten(self):
        pet = FakePetnames()
        pet.pin("mynode", H1, "manual")
        result = client.apply_tofu(pet, "mynode", make_reply([reg(H2)]),
                                   repin=True)
        self.assertTrue(result["changed"])
        self.assertTrue(result["pinned"])
        self.assertEqual(pet.data["mynode"]["hash"], H2)


class ParserTests(unittest.TestCase):
    def test_defaults(self):
        args = client.build_parser().parse_args(["mynode"])
        self.assertEqual(args.query, "mynode")
        self.assertIsNone(args.resolver)
        self.assertEqual(args.app, "nomadnetwork")
        self.assertEqual(args.aspects, "node")
        self.assertEqual(args.ttl, 30 * 86400)
        self.assertEqual(args.timeout, 15.0)
        self.assertIsNone(args.pin)
        self.assertFalse(args.repin)
        self.assertFalse(args.as_json)
        self.assertIsNone(args.rns_config)
        self.assertIsNone(args.register)

    def test_all_flags(self):
        args = client.build_parser().parse_args([
            "mynode", "--resolver", RESOLVER, "--config", "/tmp/x",
            "--rns-config", "/tmp/rns", "--app", "rnsresolve",
            "--aspects", "node,page", "--ttl", "7200", "--pin", "2",
            "--repin", "--json", "--timeout", "3.5"])
        self.assertEqual(args.resolver, RESOLVER)
        self.assertEqual(args.config, "/tmp/x")
        self.assertEqual(args.rns_config, "/tmp/rns")
        self.assertEqual(args.aspects, "node,page")
        self.assertEqual(args.ttl, 7200)
        self.assertEqual(args.pin, 2)
        self.assertTrue(args.repin)
        self.assertTrue(args.as_json)
        self.assertEqual(args.timeout, 3.5)

    def test_register_without_query(self):
        args = client.build_parser().parse_args(["--register", "mynode"])
        self.assertIsNone(args.query)
        self.assertEqual(args.register, "mynode")


class MainFlowTests(unittest.TestCase):
    """CLI flows with resolve_remote stubbed. No network, no RNS."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = self.tmp.name
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(client.RESOLVER_ENV, None)

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = client.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def pre_pin(self, name, hash_hex):
        table = client.PetnameTable(
            os.path.join(self.config, "petnames.json"))
        table.pin(name, hash_hex, "manual")

    def test_hash_query_never_hits_network(self):
        remote = mock.Mock()
        with mock.patch.object(client, "resolve_remote", remote):
            rc, out, _ = self.run_main(
                [H1.upper(), "--config", self.config,
                 "--resolver", RESOLVER])
        self.assertEqual(rc, 0)
        remote.assert_not_called()
        self.assertIn(H1, out)

    def test_pinned_petname_skips_network(self):
        self.pre_pin("mynode", H2)
        remote = mock.Mock()
        with mock.patch.object(client, "resolve_remote", remote):
            rc, out, _ = self.run_main(
                ["mynode", "--config", self.config,
                 "--resolver", RESOLVER])
        self.assertEqual(rc, 0)
        remote.assert_not_called()
        self.assertIn(H2, out)

    def test_miss_without_resolver_errors(self):
        remote = mock.Mock()
        with mock.patch.object(client, "resolve_remote", remote):
            rc, _, err = self.run_main(["mynode", "--config", self.config])
        self.assertEqual(rc, 2)
        remote.assert_not_called()
        self.assertIn("resolver", err)

    def test_invalid_query_errors_before_network(self):
        remote = mock.Mock()
        with mock.patch.object(client, "resolve_remote", remote):
            rc, _, err = self.run_main(
                ["not a name!", "--config", self.config,
                 "--resolver", RESOLVER])
        self.assertEqual(rc, 2)
        remote.assert_not_called()
        self.assertIn("invalid name", err)

    def test_miss_resolves_and_auto_pins(self):
        remote = mock.Mock(return_value=make_reply([reg(H1)]))
        with mock.patch.object(client, "resolve_remote", remote):
            rc, out, _ = self.run_main(
                ["MyNode", "--config", self.config,
                 "--resolver", RESOLVER, "--json"])
        self.assertEqual(rc, 0)
        remote.assert_called_once()
        self.assertEqual(remote.call_args.args[0], RESOLVER)
        self.assertEqual(remote.call_args.args[1], "mynode")
        payload = json.loads(out)
        self.assertTrue(payload["tofu"]["pinned"])
        self.assertEqual(payload["tofu"]["source"], "auto")
        self.assertEqual(payload["tofu"]["hash"], H1)
        table = client.PetnameTable(
            os.path.join(self.config, "petnames.json"))
        self.assertEqual(table.get("mynode")["hash"], H1)

    def test_no_auto_pin_with_announced_conflict(self):
        remote = mock.Mock(return_value=make_reply([reg(H1)], [ann(H2)]))
        with mock.patch.object(client, "resolve_remote", remote):
            rc, out, _ = self.run_main(
                ["mynode", "--config", self.config,
                 "--resolver", RESOLVER, "--json"])
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertFalse(payload["tofu"]["pinned"])
        table = client.PetnameTable(
            os.path.join(self.config, "petnames.json"))
        self.assertIsNone(table.get("mynode"))

    def test_pin_index_pins_announced_candidate(self):
        remote = mock.Mock(return_value=make_reply([reg(H1)], [ann(H2)]))
        with mock.patch.object(client, "resolve_remote", remote):
            rc, _, _ = self.run_main(
                ["mynode", "--config", self.config,
                 "--resolver", RESOLVER, "--pin", "1"])
        self.assertEqual(rc, 0)
        table = client.PetnameTable(
            os.path.join(self.config, "petnames.json"))
        self.assertEqual(table.get("mynode")["hash"], H2)

    def test_repin_changed_hash_warns_and_overwrites(self):
        self.pre_pin("mynode", H1)
        remote = mock.Mock(return_value=make_reply([reg(H3)]))
        with mock.patch.object(client, "resolve_remote", remote):
            rc, _, err = self.run_main(
                ["mynode", "--config", self.config,
                 "--resolver", RESOLVER, "--repin"])
        self.assertEqual(rc, 0)
        remote.assert_called_once()
        self.assertIn("NAME/HASH CHANGED", err)
        table = client.PetnameTable(
            os.path.join(self.config, "petnames.json"))
        self.assertEqual(table.get("mynode")["hash"], H3)

    def test_resolver_from_env(self):
        os.environ[client.RESOLVER_ENV] = RESOLVER
        remote = mock.Mock(return_value=make_reply([reg(H1)]))
        with mock.patch.object(client, "resolve_remote", remote):
            rc, _, _ = self.run_main(["mynode", "--config", self.config])
        self.assertEqual(rc, 0)
        self.assertEqual(remote.call_args.args[0], RESOLVER)

    def test_bad_resolver_hash_rejected(self):
        remote = mock.Mock()
        with mock.patch.object(client, "resolve_remote", remote):
            rc, _, err = self.run_main(
                ["mynode", "--config", self.config,
                 "--resolver", "nothex"])
        self.assertEqual(rc, 2)
        remote.assert_not_called()

    def test_resolver_error_reply(self):
        remote = mock.Mock(return_value={"ok": False, "err": "bad name"})
        with mock.patch.object(client, "resolve_remote", remote):
            rc, _, err = self.run_main(
                ["mynode", "--config", self.config,
                 "--resolver", RESOLVER])
        self.assertEqual(rc, 1)
        self.assertIn("bad name", err)

    def test_register_without_resolver_errors(self):
        rc, _, err = self.run_main(
            ["--register", "mynode", "--config", self.config])
        self.assertEqual(rc, 2)
        self.assertIn("resolver", err)

    def test_no_query_no_register_errors(self):
        rc, _, err = self.run_main(["--config", self.config])
        self.assertEqual(rc, 2)

    def test_table_output_lists_candidates(self):
        remote = mock.Mock(return_value=make_reply([reg(H1)], [ann(H2)]))
        with mock.patch.object(client, "resolve_remote", remote):
            rc, out, _ = self.run_main(
                ["mynode", "--config", self.config,
                 "--resolver", RESOLVER])
        self.assertEqual(rc, 0)
        self.assertIn("registered", out)
        self.assertIn("announced", out)
        self.assertIn(H1, out)
        self.assertIn(H2, out)
        self.assertIn("evidence", out)


class RenderTests(unittest.TestCase):
    def test_format_candidates_order_and_fields(self):
        rows = client.format_candidates(make_reply([reg(H1)], [ann(H2)]))
        self.assertEqual([r["kind"] for r in rows],
                         ["registered", "announced"])
        self.assertIn("expires", rows[0]["evidence"])
        self.assertIn("trust", rows[1]["evidence"])

    def test_render_table_empty(self):
        text = client.render_table([])
        self.assertIn("idx", text)


@unittest.skipUnless(HAVE_RNS, "RNS not installed")
class RnsIdentityTests(unittest.TestCase):
    def test_client_identity_create_and_reload(self):
        with tempfile.TemporaryDirectory() as tmp:
            ident1 = client._load_client_identity(tmp)
            ident2 = client._load_client_identity(tmp)
            self.assertEqual(ident1.hash, ident2.hash)
            self.assertTrue(os.path.isfile(
                os.path.join(tmp, "client_identity")))


if __name__ == "__main__":
    unittest.main()


class ResolveNameTest(unittest.TestCase):
    """resolve_name: the NomadNet-patch embedding hook."""

    def _pets(self, tmp):
        from rns_resolve.petnames import PetnameTable
        return PetnameTable(os.path.join(tmp, "pets.json"))

    def test_pinned_short_circuits_without_network(self):
        import tempfile
        from rns_resolve import client
        with tempfile.TemporaryDirectory() as tmp:
            pets = self._pets(tmp)
            pets.pin("home", "a" * 32, "test")
            def boom(*a, **k):
                raise AssertionError("network must not be touched")
            orig = client.resolve_remote
            client.resolve_remote = boom
            try:
                self.assertEqual(
                    client.resolve_name("HOME", "b" * 32, petnames_table=pets),
                    "a" * 32)
            finally:
                client.resolve_remote = orig

    def test_registered_only_and_pins(self):
        import tempfile
        from rns_resolve import client
        with tempfile.TemporaryDirectory() as tmp:
            pets = self._pets(tmp)
            orig = client.resolve_remote
            client.resolve_remote = lambda *a, **k: {
                "ok": True,
                "registered": [{"target": "c" * 32}],
                "announced": [{"hash": "d" * 32}]}
            try:
                got = client.resolve_name("shop", "b" * 32, petnames_table=pets)
            finally:
                client.resolve_remote = orig
            self.assertEqual(got, "c" * 32)
            self.assertEqual(pets.get("shop")["hash"], "c" * 32)

    def test_announced_only_returns_none(self):
        import tempfile
        from rns_resolve import client
        with tempfile.TemporaryDirectory() as tmp:
            pets = self._pets(tmp)
            orig = client.resolve_remote
            client.resolve_remote = lambda *a, **k: {
                "ok": True, "registered": [],
                "announced": [{"hash": "d" * 32}]}
            try:
                got = client.resolve_name("shop", "b" * 32, petnames_table=pets)
            finally:
                client.resolve_remote = orig
            self.assertIsNone(got)
            self.assertIsNone(pets.get("shop"))

    def test_never_raises(self):
        from rns_resolve import client
        self.assertIsNone(client.resolve_name("...bad!!name...", "b" * 32))
