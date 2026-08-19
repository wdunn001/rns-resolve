"""Local petname table for rns-resolve (TOFU pin store).

A petname table maps a normalized name to a pinned destination hash.
It is a plain JSON file on disk, written atomically (tmp + os.replace).
A corrupt or missing file never crashes: the table just starts empty.

Entry shape (per CONTRACTS.md):

    {
      "hash": str,           # 32 lowercase hex chars
      "source": str,         # where the pin came from (e.g. "registered")
      "first_seen": float,   # unix time the name was first pinned
      "last_verified": float # unix time of the most recent pin/verify
    }
"""

import json
import os
import tempfile
import time

DEFAULT_PATH = os.path.join("~", ".rns_resolve", "petnames.json")


class PetnameTable:
    def __init__(self, path: str | None = None):
        # expanduser at call time (not import time) per contract
        self._path = os.path.expanduser(path if path is not None else DEFAULT_PATH)
        self._table = self._load()

    # -- internal helpers -------------------------------------------------

    def _load(self) -> dict:
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            # missing, unreadable, or corrupt file: start empty, never crash
            return {}
        if not isinstance(data, dict):
            return {}
        # keep only well-formed entries
        table = {}
        for name, entry in data.items():
            if isinstance(name, str) and isinstance(entry, dict) and "hash" in entry:
                table[name] = entry
        return table

    def _save(self) -> None:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".petnames-", suffix=".tmp", dir=directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._table, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- public API (per CONTRACTS.md) ------------------------------------

    def get(self, name_norm: str) -> dict | None:
        entry = self._table.get(name_norm)
        return dict(entry) if entry is not None else None

    def pin(self, name_norm: str, hash_hex: str, source: str) -> None:
        now = time.time()
        existing = self._table.get(name_norm)
        if existing is not None and existing.get("hash") == hash_hex.lower():
            first_seen = existing.get("first_seen", now)
        else:
            first_seen = now
        self._table[name_norm] = {
            "hash": hash_hex.lower(),
            "source": source,
            "first_seen": first_seen,
            "last_verified": now,
        }
        self._save()

    def unpin(self, name_norm: str) -> bool:
        if name_norm in self._table:
            del self._table[name_norm]
            self._save()
            return True
        return False

    def changed(self, name_norm: str, hash_hex: str) -> bool:
        entry = self._table.get(name_norm)
        if entry is None:
            return False
        return entry.get("hash") != hash_hex.lower()

    def all(self) -> dict:
        return {name: dict(entry) for name, entry in self._table.items()}
