"""Beacon announce-candidate source for rns-resolve (module A3).

Read-only Postgres source over the Beacon crawler's ``nodes`` table.
Supplies "announced" candidates for resolve answers. This module must
never take the service down: any failure (missing config, missing
psycopg2, unreachable DB, bad query) degrades to an empty candidate
list and flips ``available()`` to False until a later call succeeds.

psycopg2 is imported lazily inside the connect path so this module
imports cleanly on hosts without psycopg2 installed.

psycopg2 GOTCHA honored here: the SQL text contains no literal ``%``
other than the ``%s`` parameter placeholders. LIKE patterns are built
in Python and passed as bound parameters, never embedded in the SQL
string.
"""

import math
import os
import re
import time
import unicodedata
from datetime import datetime, timezone

# Trust-ranking constants (see CONTRACTS.md, beacon_source section).
HALFLIFE_SECONDS = 7 * 86400
REACHABLE_FACTOR = 1.15
UNREACHABLE_FACTOR = 0.85
WHOLE_WORD_BONUS = 1.25

# How many rows to pull from the DB before ranking in Python.
_FETCH_CAP = 200
# Mesh announce names are frequently decorative unicode (Mathematical
# Sans-Serif Bold and the like), which SQL ILIKE can never match against an
# ASCII query. So in addition to the ILIKE pre-filter we always pull the
# top announced nodes and do the real matching in Python on NFKC-casefolded
# text (folds bold/fullwidth variants down to plain letters).
_TOP_CAP = 500

# The only percent signs in these strings are %s placeholders.
_SQL_CANDIDATES = (
    "SELECT dest_hash, name, last_seen, announce_count, reachable "
    "FROM nodes WHERE name ILIKE %s "
    "ORDER BY announce_count DESC LIMIT %s"
)
_SQL_TOP = (
    "SELECT dest_hash, name, last_seen, announce_count, reachable "
    "FROM nodes WHERE name IS NOT NULL "
    "ORDER BY announce_count DESC LIMIT %s"
)


def fold(s):
    """NFKC-normalize and casefold text for matching (bold unicode -> ascii)."""
    return unicodedata.normalize("NFKC", str(s)).casefold()


def recency_decay(last_seen, now=None, halflife=HALFLIFE_SECONDS):
    """Exponential decay by age: 1.0 at now, 0.5 at one halflife.

    ``last_seen`` may be a datetime (aware or naive, naive = UTC), an
    ISO-8601 string, a unix timestamp, or None. Unknown or unparseable
    values decay to 0.0.
    """
    ts = _to_unix(last_seen)
    if ts is None:
        return 0.0
    if now is None:
        now = time.time()
    age = now - ts
    if age < 0:
        age = 0.0
    return 0.5 ** (age / float(halflife))


def _to_unix(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


def _iso(value):
    if value is None:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    return str(value)


def _like_pattern(q):
    """Build a bound-parameter ILIKE pattern; escape LIKE wildcards."""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + esc + "%"


def _whole_word(q_lower, name):
    try:
        return re.search(
            r"\b" + re.escape(q_lower) + r"\b", name.lower()
        ) is not None
    except re.error:
        return False


class BeaconSource:
    """Read-only announce-candidate source backed by the Beacon DB."""

    def __init__(self, env=None):
        e = os.environ if env is None else env
        self._host = e.get("BEACON_DB_HOST") or ""
        self._port = e.get("BEACON_DB_PORT") or "5432"
        self._dbname = e.get("BEACON_DB_NAME") or ""
        self._user = e.get("BEACON_DB_USER") or ""
        self._password = e.get("BEACON_DB_PASSWORD") or ""
        self._configured = bool(self._host and self._dbname)
        self._conn = None
        # None = never attempted; True/False = outcome of last attempt.
        self._state = None

    def available(self):
        """True when the last DB interaction succeeded.

        Unconfigured sources are always unavailable. If nothing has
        been attempted yet, a connection attempt is made so health
        checks report reality rather than a stale default.
        """
        if not self._configured:
            return False
        if self._state is None:
            try:
                self._ensure_conn()
                self._state = True
            except Exception:
                self._close()
                self._state = False
        return bool(self._state)

    def candidates(self, q, limit=10):
        """Ranked announce candidates matching ``q``. [] on any failure."""
        if not self._configured:
            return []
        q = (q or "").strip()
        if not q:
            return []
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10
        if limit <= 0:
            return []
        fetch_limit = min(max(limit * 5, 50), _FETCH_CAP)
        params = (_like_pattern(q), fetch_limit)

        rows = None
        ok = False
        # Lazy connect with exactly one retry on failure.
        for _attempt in (0, 1):
            try:
                rows = self._run_query(_SQL_CANDIDATES, params)
                # Unicode-name coverage: also pull the top announced nodes
                # and let the folded Python match decide.
                seen = {str(r[0]).lower() for r in rows if r and r[0]}
                for row in self._run_query(_SQL_TOP, (_TOP_CAP,)):
                    if row and row[0] and str(row[0]).lower() not in seen:
                        rows.append(row)
                ok = True
                break
            except Exception:
                self._close()
        if not ok:
            self._state = False
            return []
        self._state = True
        return self._rank(q, rows, limit)

    # -- internals ---------------------------------------------------

    def _ensure_conn(self):
        if self._conn is not None:
            return self._conn
        import psycopg2  # lazy: module must import without psycopg2

        kwargs = {
            "host": self._host,
            "port": int(self._port),
            "dbname": self._dbname,
            "connect_timeout": 5,
        }
        if self._user:
            kwargs["user"] = self._user
        if self._password:
            kwargs["password"] = self._password
        self._conn = psycopg2.connect(**kwargs)
        try:
            # Read-only source: never hold write intent.
            self._conn.autocommit = True
        except Exception:
            pass
        return self._conn

    def _run_query(self, sql, params):
        conn = self._ensure_conn()
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            try:
                cur.close()
            except Exception:
                pass

    def _close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def _rank(self, q, rows, limit):
        now = time.time()
        q_fold = fold(q)
        out = []
        seen_hashes = set()
        for row in rows or []:
            try:
                dest_hash, name, last_seen, announce_count, reachable = row[:5]
            except (TypeError, ValueError):
                continue
            if not dest_hash or not name:
                continue
            name = str(name)
            key = str(dest_hash).lower()
            if key in seen_hashes:
                continue
            # The real match: folded substring, so decorative unicode names
            # (bold letters, emoji padding) answer plain-ascii queries.
            if q_fold not in fold(name):
                continue
            seen_hashes.add(key)
            count = int(announce_count or 0)
            reachable = bool(reachable)
            trust = (
                math.log1p(count)
                * recency_decay(last_seen, now=now)
                * (REACHABLE_FACTOR if reachable else UNREACHABLE_FACTOR)
            )
            if _whole_word(q_fold, fold(name)):
                trust *= WHOLE_WORD_BONUS
            out.append(
                {
                    "hash": str(dest_hash).lower(),
                    "name": name,
                    "trust": trust,
                    "last_seen": _iso(last_seen),
                    "announce_count": count,
                    "reachable": reachable,
                }
            )
        out.sort(key=lambda c: (-c["trust"], c["name"]))
        return out[:limit]
