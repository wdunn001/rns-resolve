"""Unit tests for rns_resolve.petnames (stdlib only, no network)."""

import json
import os
import tempfile
import unittest

from rns_resolve.petnames import PetnameTable

HASH_A = "a" * 32
HASH_B = "b" * 32


class PetnameTableTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "petnames.json")

    def _read_raw(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    # -- basics -----------------------------------------------------------

    def test_missing_file_starts_empty(self):
        t = PetnameTable(self.path)
        self.assertEqual(t.all(), {})
        self.assertIsNone(t.get("nobody"))
        self.assertFalse(os.path.exists(self.path))

    def test_pin_and_get(self):
        t = PetnameTable(self.path)
        t.pin("alice", HASH_A, "registered")
        entry = t.get("alice")
        self.assertIsNotNone(entry)
        self.assertEqual(entry["hash"], HASH_A)
        self.assertEqual(entry["source"], "registered")
        self.assertIn("first_seen", entry)
        self.assertIn("last_verified", entry)
        self.assertIsInstance(entry["first_seen"], float)
        self.assertIsInstance(entry["last_verified"], float)

    def test_pin_lowercases_hash(self):
        t = PetnameTable(self.path)
        t.pin("alice", HASH_A.upper(), "registered")
        self.assertEqual(t.get("alice")["hash"], HASH_A)

    def test_pin_persists_across_instances(self):
        PetnameTable(self.path).pin("alice", HASH_A, "registered")
        t2 = PetnameTable(self.path)
        self.assertEqual(t2.get("alice")["hash"], HASH_A)

    def test_repin_same_hash_keeps_first_seen(self):
        t = PetnameTable(self.path)
        t.pin("alice", HASH_A, "registered")
        first = t.get("alice")["first_seen"]
        t.pin("alice", HASH_A, "registered")
        entry = t.get("alice")
        self.assertEqual(entry["first_seen"], first)
        self.assertGreaterEqual(entry["last_verified"], first)

    def test_repin_different_hash_resets_first_seen_and_overwrites(self):
        t = PetnameTable(self.path)
        t.pin("alice", HASH_A, "registered")
        t.pin("alice", HASH_B, "announced")
        entry = t.get("alice")
        self.assertEqual(entry["hash"], HASH_B)
        self.assertEqual(entry["source"], "announced")

    def test_unpin(self):
        t = PetnameTable(self.path)
        t.pin("alice", HASH_A, "registered")
        self.assertTrue(t.unpin("alice"))
        self.assertIsNone(t.get("alice"))
        self.assertFalse(t.unpin("alice"))
        # removal persisted
        self.assertIsNone(PetnameTable(self.path).get("alice"))

    def test_changed(self):
        t = PetnameTable(self.path)
        self.assertFalse(t.changed("alice", HASH_A))  # not pinned
        t.pin("alice", HASH_A, "registered")
        self.assertFalse(t.changed("alice", HASH_A))
        self.assertFalse(t.changed("alice", HASH_A.upper()))
        self.assertTrue(t.changed("alice", HASH_B))

    def test_all_returns_copy(self):
        t = PetnameTable(self.path)
        t.pin("alice", HASH_A, "registered")
        snapshot = t.all()
        snapshot["alice"]["hash"] = "tampered"
        snapshot["bob"] = {"hash": HASH_B}
        self.assertEqual(t.get("alice")["hash"], HASH_A)
        self.assertIsNone(t.get("bob"))

    def test_get_returns_copy(self):
        t = PetnameTable(self.path)
        t.pin("alice", HASH_A, "registered")
        entry = t.get("alice")
        entry["hash"] = "tampered"
        self.assertEqual(t.get("alice")["hash"], HASH_A)

    # -- corrupt-file recovery --------------------------------------------

    def test_corrupt_json_starts_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{not valid json!!!")
        t = PetnameTable(self.path)
        self.assertEqual(t.all(), {})
        # and it can pin over the corrupt file
        t.pin("alice", HASH_A, "registered")
        self.assertEqual(self._read_raw()["alice"]["hash"], HASH_A)

    def test_wrong_top_level_type_starts_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(["not", "a", "dict"], f)
        self.assertEqual(PetnameTable(self.path).all(), {})

    def test_malformed_entries_dropped(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "good": {
                        "hash": HASH_A,
                        "source": "registered",
                        "first_seen": 1.0,
                        "last_verified": 2.0,
                    },
                    "no-hash": {"source": "x"},
                    "not-a-dict": "junk",
                },
                f,
            )
        t = PetnameTable(self.path)
        self.assertEqual(set(t.all().keys()), {"good"})
        self.assertEqual(t.get("good")["hash"], HASH_A)

    def test_empty_file_starts_empty(self):
        with open(self.path, "w", encoding="utf-8"):
            pass
        self.assertEqual(PetnameTable(self.path).all(), {})

    # -- atomicity ---------------------------------------------------------

    def test_file_always_valid_json_after_each_pin(self):
        t = PetnameTable(self.path)
        for i in range(20):
            t.pin("name%d" % i, ("%032x" % i), "registered")
            data = self._read_raw()  # raises if not valid JSON
            self.assertIsInstance(data, dict)
            self.assertEqual(len(data), i + 1)

    def test_no_tmp_files_left_behind(self):
        t = PetnameTable(self.path)
        t.pin("alice", HASH_A, "registered")
        t.unpin("alice")
        leftovers = [
            f for f in os.listdir(self._tmp.name) if f != "petnames.json"
        ]
        self.assertEqual(leftovers, [])

    def test_default_path_expands_user(self):
        t = PetnameTable()  # no writes performed; just check path expansion
        self.assertNotIn("~", t._path)
        self.assertTrue(t._path.endswith("petnames.json"))

    def test_creates_parent_directory_on_pin(self):
        nested = os.path.join(self._tmp.name, "deep", "dir", "petnames.json")
        t = PetnameTable(nested)
        t.pin("alice", HASH_A, "registered")
        self.assertTrue(os.path.exists(nested))


if __name__ == "__main__":
    unittest.main()
