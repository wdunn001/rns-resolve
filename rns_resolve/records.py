"""Record shape, name normalization, canonical encoding, and signatures.

This module owns the rns-resolve record dict shape (CONTRACTS.md). It must
import cleanly with only the Python standard library available: RNS and
umsgpack are imported lazily inside the functions that need them.

Record shape (plain dict):

    {
      "v": 1,
      "name": str,        # normalized, see normalize_name()
      "identity": str,    # 32 hex chars, registrant identity hash
      "app": str,         # e.g. "nomadnetwork" (default)
      "aspects": list,    # e.g. ["node"] (default)
      "target": str,      # 32 hex chars, derived destination hash
      "ts": float,        # registration unix time
      "ttl": int,         # seconds, clamped TTL_MIN..TTL_MAX
      "sig": bytes|None,  # detached signature over canonical_bytes(), or None
    }
"""

import hashlib
import re
import time
import unicodedata

RECORD_VERSION = 1

# A destination hash is 16 bytes, rendered as 32 lowercase hex chars.
HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")

DEFAULT_APP = "nomadnetwork"
DEFAULT_ASPECTS = ["node"]

TTL_MIN = 3600
TTL_MAX = 365 * 86400
TTL_DEFAULT = 30 * 86400

MAX_LABELS = 3
MAX_LABEL_LEN = 32
MAX_NAME_LEN = 64

_LABEL_RE = re.compile(r"^[a-z0-9_-]+$")


def _msgpack():
    """Lazy msgpack import: standalone umsgpack, else the RNS vendored copy."""
    try:
        import umsgpack
        return umsgpack
    except ImportError:
        from RNS.vendor import umsgpack
        return umsgpack


def normalize_name(s):
    """Normalize a human-readable name. Raises ValueError on any violation.

    Rules (CONTRACTS.md): lowercase, NFC unicode normalization, allowed
    chars [a-z0-9._-], labels split on ".", max 3 labels, each label
    1..32 chars, total length <= 64 chars, no leading or trailing "-"
    or "." per label.
    """
    if not isinstance(s, str):
        raise ValueError("name must be a string")
    name = unicodedata.normalize("NFC", s).strip().lower()
    if not name:
        raise ValueError("name is empty")
    if len(name) > MAX_NAME_LEN:
        raise ValueError("name longer than %d chars" % MAX_NAME_LEN)
    labels = name.split(".")
    if len(labels) > MAX_LABELS:
        raise ValueError("name has more than %d labels" % MAX_LABELS)
    for label in labels:
        if not label:
            raise ValueError("empty label in name")
        if len(label) > MAX_LABEL_LEN:
            raise ValueError("label longer than %d chars" % MAX_LABEL_LEN)
        if not _LABEL_RE.match(label):
            raise ValueError("label contains characters outside [a-z0-9_-]")
        if label[0] == "-" or label[-1] == "-":
            raise ValueError("label must not start or end with '-'")
    return name


def clamp_ttl(ttl):
    """Clamp a TTL to TTL_MIN..TTL_MAX. None means TTL_DEFAULT."""
    if ttl is None:
        return TTL_DEFAULT
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        raise ValueError("ttl must be an integer number of seconds")
    if ttl < TTL_MIN:
        return TTL_MIN
    if ttl > TTL_MAX:
        return TTL_MAX
    return ttl


def make_record(name, identity_hash_hex, app=DEFAULT_APP, aspects=None,
                ts=None, ttl=None, target="", sig=None):
    """Convenience constructor. Normalizes the name, validates the identity
    hash, applies defaults, and clamps the TTL. Does NOT derive the target
    (the service does that with derive_target)."""
    identity_hash_hex = str(identity_hash_hex)
    if not HASH_RE.match(identity_hash_hex):
        raise ValueError("identity must be 32 hex chars")
    if aspects is None:
        aspects = list(DEFAULT_ASPECTS)
    return {
        "v": RECORD_VERSION,
        "name": normalize_name(name),
        "identity": identity_hash_hex.lower(),
        "app": str(app),
        "aspects": list(aspects),
        "target": target,
        "ts": float(ts) if ts is not None else time.time(),
        "ttl": clamp_ttl(ttl),
        "sig": sig,
    }


def canonical_bytes(rec):
    """msgpack of [v, name, identity, app, aspects, ts, ttl].

    Excludes target (derived, never trusted from the wire) and sig
    (the signature covers these bytes)."""
    umsgpack = _msgpack()
    return umsgpack.packb([
        int(rec.get("v", RECORD_VERSION)),
        rec["name"],
        rec["identity"],
        rec["app"],
        list(rec["aspects"]),
        float(rec["ts"]),
        int(rec["ttl"]),
    ])


def record_id(rec):
    """sha256(canonical_bytes)[:16].hex(): fixed-width diff key (32 chars)."""
    return hashlib.sha256(canonical_bytes(rec)).digest()[:16].hex()


def derive_target(identity_hash_hex, app, aspects):
    """Derive the destination hash for identity_hash_hex + app + aspects.

    Uses RNS.Destination.hash(bytes.fromhex(identity_hash_hex), app,
    *aspects) and returns 32 lowercase hex chars. RNS is imported lazily
    so this module works without RNS installed."""
    identity_hash_hex = str(identity_hash_hex)
    if not HASH_RE.match(identity_hash_hex):
        raise ValueError("identity hash must be 32 hex chars")
    import RNS
    return RNS.Destination.hash(
        bytes.fromhex(identity_hash_hex), str(app), *[str(a) for a in aspects]
    ).hex()


def sign_record(rec, identity):
    """Detached signature over canonical_bytes(rec) with an RNS.Identity
    that holds a private key. Returns the signature bytes (caller stores
    them in rec["sig"])."""
    return identity.sign(canonical_bytes(rec))


def verify_record(rec, identity):
    """Verify rec["sig"] over canonical_bytes(rec) against an RNS.Identity
    with a public key. Returns False on missing sig or any exception."""
    try:
        sig = rec.get("sig")
        if not sig:
            return False
        return bool(identity.validate(sig, canonical_bytes(rec)))
    except Exception:
        return False


def identity_from_pubkey(pubkey_bytes):
    """RNS.Identity loaded from public-key bytes, or None."""
    try:
        import RNS
        ident = RNS.Identity(create_keys=False)
        # load_public_key returns None on success in current RNS; judge
        # success by the identity hash materializing instead.
        ident.load_public_key(bytes(pubkey_bytes))
        if not getattr(ident, "hash", None):
            return None
        return ident
    except Exception:
        return None


def verify_record_standalone(rec):
    """Verify a record using its own embedded ``pubkey`` field.

    This is what makes a record self-certifying for replication: any peer
    can verify it with no prior knowledge of the registrant. The pubkey is
    bound to the record by the identity-hash check (an RNS identity hash IS
    the hash of the public key), so the pubkey needs no signature coverage
    of its own; a swapped key changes the hash and fails the check.
    """
    try:
        pub = rec.get("pubkey")
        if not pub:
            return False
        ident = identity_from_pubkey(pub)
        if ident is None:
            return False
        if ident.hash.hex() != str(rec.get("identity", "")).lower():
            return False
        return verify_record(rec, ident)
    except Exception:
        return False
