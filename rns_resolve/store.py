"""SQLite record store for rns-resolve.

Owns persistence of name records. See CONTRACTS.md ("store.py") for the
binding interface. Records are plain dicts (shape owned by records.py);
the store keys them by record_id and adds two bookkeeping columns:
attested (1 = resolver-attested, sig is NULL, never handed to peers) and
last_used (lease renewal signal, bumped by touch_use).
"""

import json
import os
import sqlite3
import threading
import time

from rns_resolve.records import record_id

DEFAULT_APP = "nomadnetwork"
DEFAULT_ASPECTS = ["node"]
DEFAULT_TTL = 30 * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id        TEXT PRIMARY KEY,
    name      TEXT,
    identity  TEXT,
    app       TEXT,
    aspects   TEXT,
    target    TEXT,
    ts        REAL,
    ttl       INTEGER,
    sig       BLOB,
    attested  INTEGER,
    last_used REAL,
    pubkey    BLOB
);
"""

# Migration for databases created before the pubkey column existed.
# ALTER ADD appends the column last, matching the CREATE order above.
_MIGRATIONS = (
    "ALTER TABLE records ADD COLUMN pubkey BLOB;",
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_records_name ON records(name);",
    "CREATE INDEX IF NOT EXISTS idx_records_target ON records(target);",
)

# sqlite has a bound-parameter ceiling (999 in older builds); chunk IN () lists
_CHUNK = 500


class Store:
    """SQLite-backed record store (WAL, autocommit, thread-safe)."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            parent = os.path.dirname(os.path.abspath(path))
            if parent:
                os.makedirs(parent, exist_ok=True)
        # isolation_level=None -> autocommit; the service touches the store
        # from several threads, so serialize access with a lock.
        self._conn = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False
        )
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA busy_timeout=5000;")
            self._conn.execute(_SCHEMA)
            for stmt in _INDEXES:
                self._conn.execute(stmt)
            for stmt in _MIGRATIONS:
                try:
                    self._conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists (fresh or migrated db)

    # -- write path ---------------------------------------------------------

    def put(self, rec: dict) -> str:
        """Upsert a record by record_id. Returns the id.

        attested is derived from the record itself: sig None means the
        record was accepted via the local page (resolver-attested).
        An upsert preserves last_used on the existing row.
        """
        rid = record_id(rec)
        sig = rec.get("sig")
        attested = 1 if sig is None else 0
        row = (
            rid,
            rec["name"],
            rec["identity"],
            rec.get("app", DEFAULT_APP),
            json.dumps(rec.get("aspects", DEFAULT_ASPECTS)),
            rec.get("target", ""),
            float(rec["ts"]),
            int(rec.get("ttl", DEFAULT_TTL)),
            sig,
            attested,
            bytes(rec["pubkey"]) if rec.get("pubkey") else None,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO records
                    (id, name, identity, app, aspects, target, ts, ttl,
                     sig, attested, last_used, pubkey)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    identity=excluded.identity,
                    app=excluded.app,
                    aspects=excluded.aspects,
                    target=excluded.target,
                    ts=excluded.ts,
                    ttl=excluded.ttl,
                    sig=excluded.sig,
                    attested=excluded.attested,
                    pubkey=excluded.pubkey
                """,
                row,
            )
        return rid

    def touch_use(self, record_id: str) -> None:
        """Bump last_used for a record (lease renewal signal)."""
        with self._lock:
            self._conn.execute(
                "UPDATE records SET last_used=? WHERE id=?",
                (time.time(), record_id),
            )

    def expire_sweep(self) -> int:
        """Delete records past ts+ttl. Returns the number deleted."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM records WHERE ts + ttl <= ?", (time.time(),)
            )
            return cur.rowcount

    # -- read path ----------------------------------------------------------

    def resolve(self, name_norm: str) -> list:
        """Non-expired records for an exact normalized name, newest ts first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records WHERE name=? AND ts + ttl > ?"
                " ORDER BY ts DESC",
                (name_norm, time.time()),
            ).fetchall()
        return [self._row_to_rec(r) for r in rows]

    def prefix(self, name_prefix: str, limit=10) -> list:
        """Non-expired records whose name starts with name_prefix."""
        escaped = (
            name_prefix.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records WHERE name LIKE ? ESCAPE '\\'"
                " AND ts + ttl > ? ORDER BY ts DESC LIMIT ?",
                (escaped + "%", time.time(), int(limit)),
            ).fetchall()
        return [self._row_to_rec(r) for r in rows]

    def whois(self, target_hex: str) -> list:
        """Non-expired records whose derived target matches, newest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM records WHERE target=? AND ts + ttl > ?"
                " ORDER BY ts DESC",
                (target_hex.lower(), time.time()),
            ).fetchall()
        return [self._row_to_rec(r) for r in rows]

    def delete(self, name: str, identity: str) -> int:
        """Delete records matching name AND registrant identity. Returns count.

        Used by the private /unregister surface; ownership was already
        verified upstream (the identity comes from a verified link).
        """
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM records WHERE name=? AND identity=?",
                (name, identity.lower()),
            )
        return cur.rowcount

    def all_ids(self) -> list:
        """Ids of replicable (self-certifying) records, for peer offers.

        Attested records are never replicated, so they are not offered.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM records WHERE attested=0"
            ).fetchall()
        return [r[0] for r in rows]

    def get_many(self, ids: list) -> list:
        """Records for the given ids, excluding attested ones (peer-safe)."""
        out = []
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            marks = ",".join("?" for _ in chunk)
            with self._lock:
                rows = self._conn.execute(
                    "SELECT * FROM records WHERE attested=0 AND id IN"
                    " (" + marks + ")",
                    chunk,
                ).fetchall()
            out.extend(self._row_to_rec(r) for r in rows)
        return out

    def missing(self, ids: list) -> list:
        """Subset of ids not present in the store, input order preserved."""
        have = set()
        for i in range(0, len(ids), _CHUNK):
            chunk = ids[i : i + _CHUNK]
            marks = ",".join("?" for _ in chunk)
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id FROM records WHERE id IN (" + marks + ")",
                    chunk,
                ).fetchall()
            have.update(r[0] for r in rows)
        return [i for i in ids if i not in have]

    def count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM records"
            ).fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _row_to_rec(row) -> dict:
        (rid, name, identity, app, aspects, target, ts, ttl, sig,
         attested, last_used, pubkey) = row
        if sig is not None:
            sig = bytes(sig)
        if pubkey is not None:
            pubkey = bytes(pubkey)
        return {
            "v": 1,
            "name": name,
            "identity": identity,
            "app": app,
            "aspects": json.loads(aspects),
            "target": target,
            "ts": ts,
            "ttl": ttl,
            "sig": sig,
            "pubkey": pubkey,
            "id": rid,
            "attested": bool(attested),
        }
