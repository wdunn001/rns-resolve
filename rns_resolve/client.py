"""Client-side resolution for rns-resolve.

Flow (see CONTRACTS.md, trust model):
1. Hash-shaped input is returned as-is; a valid 32-hex string is NEVER
   sent to a resolver (trust invariant).
2. Pinned petnames answer locally with zero network, except under --repin.
3. Only a miss consults a resolver, over an RNS Link with a msgpack
   request to path "q".
4. Answers are ranked candidates; the client pins locally (TOFU).

RNS is imported lazily so classify/TOFU logic works without it installed.
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone

from . import APP_NAME, ASPECT, REQUEST_PATH
from .petnames import PetnameTable
from .records import HASH_RE, normalize_name

DEFAULT_CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".rns_resolve")
DEFAULT_APP = "nomadnetwork"
DEFAULT_ASPECTS = ["node"]
DEFAULT_TTL = 30 * 86400
DEFAULT_TIMEOUT = 15.0
RESOLVER_ENV = "RESOLVE_RESOLVER"


def _msgpack():
    """Family convention: umsgpack, falling back to the RNS vendored copy."""
    try:
        import umsgpack
        return umsgpack
    except ImportError:
        from RNS.vendor import umsgpack
        return umsgpack


# ---------------------------------------------------------------------------
# Pure logic (no network, fully unit-testable)
# ---------------------------------------------------------------------------

def classify(query, petnames):
    """Classify user input. Returns (kind, value):

    ("hash", hex32)       input already is a destination hash (lowercased)
    ("petname", hex32)    pinned locally; value is the pinned hash
    ("miss", name_norm)   needs a resolver; value is the normalized name

    Raises ValueError for input that is neither a hash nor a valid name.
    """
    if HASH_RE.match(query):
        return ("hash", query.lower())
    name_norm = normalize_name(query)
    entry = petnames.get(name_norm)
    if entry is not None:
        return ("petname", entry["hash"])
    return ("miss", name_norm)


def candidate_hash(cand):
    """Registered records carry 'target'; announced candidates carry 'hash'."""
    return cand.get("target") or cand.get("hash")


def choose_candidate(reply, pin_index=None):
    """Pick the candidate to pin from a resolver reply.

    pin_index indexes the combined list (registered first, then announced),
    matching the printed table. Raises IndexError if out of range.
    Without pin_index, the auto-pin rule applies: exactly one registered
    candidate and zero announced conflicts. Returns (candidate|None, source).
    """
    registered = reply.get("registered") or []
    announced = reply.get("announced") or []
    combined = list(registered) + list(announced)
    if pin_index is not None:
        if 0 <= pin_index < len(combined):
            kind = "manual" if pin_index < len(registered) else "manual-announced"
            return (combined[pin_index], kind)
        raise IndexError("--pin index %d out of range (0..%d)"
                         % (pin_index, len(combined) - 1))
    if len(registered) == 1 and len(announced) == 0:
        return (registered[0], "auto")
    return (None, None)


def apply_tofu(petnames, name_norm, reply, pin_index=None, repin=False):
    """Apply the TOFU rules to a resolver reply. Pure w.r.t. the network.

    Returns {"pinned": bool, "changed": bool, "hash": hex|None,
             "previous": hex|None, "source": str|None}.

    - Not pinned yet: pin on explicit --pin N, or automatically when the
      auto-pin rule holds (exactly one registered, zero announced).
    - Already pinned, same hash: overwrite only under repin (refresh).
    - Already pinned, different hash: changed=True always; the pin is
      overwritten ONLY under repin.
    """
    cand, source = choose_candidate(reply, pin_index)
    existing = petnames.get(name_norm)
    result = {
        "pinned": False,
        "changed": False,
        "hash": None,
        "previous": existing["hash"] if existing else None,
        "source": source,
    }
    if cand is None:
        return result
    new_hash = candidate_hash(cand)
    result["hash"] = new_hash
    if existing is not None:
        if existing["hash"] != new_hash:
            result["changed"] = True
            if repin:
                petnames.pin(name_norm, new_hash, source or "repin")
                result["pinned"] = True
        elif repin:
            petnames.pin(name_norm, new_hash, source or "repin")
            result["pinned"] = True
    else:
        if pin_index is not None or source == "auto":
            petnames.pin(name_norm, new_hash, source or "manual")
            result["pinned"] = True
    return result


# ---------------------------------------------------------------------------
# Remote (RNS Link round trip; lazy RNS import)
# ---------------------------------------------------------------------------

def _wait(predicate, timeout, interval=0.1):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


def _remote_request(resolver_hash, payload, rns_config, timeout, identity=None):
    """Open an RNS Link to the resolver destination and request path "q"
    with the msgpack-packed payload. Returns the unpacked reply dict."""
    import RNS
    umsgpack = _msgpack()

    try:
        RNS.Reticulum(configdir=rns_config)
    except Exception:
        # Already initialised in this process (loop callers like nodereg);
        # a genuine init failure surfaces loudly at the path request below.
        pass
    dest_bytes = bytes.fromhex(resolver_hash)

    if not RNS.Transport.has_path(dest_bytes):
        RNS.Transport.request_path(dest_bytes)
        if not _wait(lambda: RNS.Transport.has_path(dest_bytes), timeout):
            raise TimeoutError("no path to resolver " + resolver_hash)

    server_identity = RNS.Identity.recall(dest_bytes)
    if server_identity is None:
        raise RuntimeError("could not recall resolver identity for "
                           + resolver_hash)

    destination = RNS.Destination(
        server_identity,
        RNS.Destination.OUT,
        RNS.Destination.SINGLE,
        APP_NAME,
        ASPECT,
    )
    link = RNS.Link(destination)
    try:
        if not _wait(lambda: link.status == RNS.Link.ACTIVE, timeout):
            raise TimeoutError("link to resolver did not establish")
        if identity is not None:
            link.identify(identity)

        done = threading.Event()
        box = {}

        def _on_response(receipt):
            box["data"] = receipt.response
            done.set()

        def _on_failed(receipt):
            box["err"] = "request failed"
            done.set()

        link.request(
            REQUEST_PATH,
            umsgpack.packb(payload),
            response_callback=_on_response,
            failed_callback=_on_failed,
            timeout=timeout,
        )
        if not done.wait(timeout + 5):
            raise TimeoutError("no response from resolver")
        if "err" in box:
            raise RuntimeError(box["err"])
        data = box.get("data")
        if isinstance(data, (bytes, bytearray)):
            return umsgpack.unpackb(bytes(data))
        return data
    finally:
        try:
            link.teardown()
        except Exception:
            pass


def resolve_remote(resolver_hash, query, rns_config=None,
                   timeout=DEFAULT_TIMEOUT):
    """Send op "resolve" for query (already normalized) to the resolver."""
    payload = {"v": 1, "op": "resolve", "q": query}
    return _remote_request(resolver_hash, payload, rns_config, timeout)


def resolve_name(name, resolver_hash, rns_config=None,
                 timeout=DEFAULT_TIMEOUT, petnames_table=None):
    """One-shot embedding API: name in, single hash out (or None).

    This is the hook the NomadNet browser patch calls. Deliberately
    conservative: it returns ONLY a registered (ownership-derived) record's
    target, never an announced candidate, because the caller has no UI to
    present ranked candidates and an announce name is an unverified
    self-claim. On success the answer is pinned into the local petname
    table (TOFU), so subsequent lookups never touch the network. Never
    raises; returns None on any failure so callers can fall back to their
    stock behavior.
    """
    try:
        from .records import normalize_name
        from .petnames import PetnameTable
        name_norm = normalize_name(name)
        pets = petnames_table if petnames_table is not None else PetnameTable()
        pinned = pets.get(name_norm)
        if pinned and pinned.get("hash"):
            return pinned["hash"]
        reply = resolve_remote(resolver_hash, name_norm,
                               rns_config=rns_config, timeout=timeout)
        if not isinstance(reply, dict) or not reply.get("ok"):
            return None
        registered = reply.get("registered") or []
        if not registered:
            return None
        target = registered[0].get("target")
        if not target:
            return None
        try:
            pets.pin(name_norm, target, "resolver:" + str(resolver_hash))
        except Exception:
            pass
        return target
    except Exception:
        return None


def _load_client_identity(config_dir):
    """Load or create the client identity at <config_dir>/client_identity."""
    import RNS
    os.makedirs(config_dir, exist_ok=True)
    path = os.path.join(config_dir, "client_identity")
    if os.path.isfile(path):
        identity = RNS.Identity.from_file(path)
        if identity is None:
            raise RuntimeError("could not load client identity from " + path)
        return identity
    identity = RNS.Identity()
    identity.to_file(path)
    return identity


def register_remote(resolver_hash, name, config_dir, rns_config=None,
                    app=DEFAULT_APP, aspects=None, ttl=DEFAULT_TTL,
                    timeout=DEFAULT_TIMEOUT, identity=None):
    """Register a name: identify on the link, sign the canonical record
    with the registrant identity, send op "register". By default the
    identity is the client identity at <config_dir>/client_identity; pass
    identity= to register with another key you hold (e.g. a NomadNet
    node's own identity, see rns_resolve.nodereg)."""
    from .records import sign_record
    aspects = list(aspects) if aspects else list(DEFAULT_ASPECTS)
    if identity is None:
        identity = _load_client_identity(config_dir)
    name_norm = normalize_name(name)
    ts = time.time()
    rec = {
        "v": 1,
        "name": name_norm,
        "identity": identity.hash.hex(),
        "app": app,
        "aspects": aspects,
        "target": "",
        "ts": ts,
        "ttl": int(ttl),
        "sig": None,
    }
    sig = sign_record(rec, identity)
    payload = {
        "v": 1,
        "op": "register",
        "name": name_norm,
        "app": app,
        "aspects": aspects,
        "ts": ts,
        "ttl": int(ttl),
        "sig": sig,
    }
    return _remote_request(resolver_hash, payload, rns_config, timeout,
                           identity=identity)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _json_default(obj):
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).hex()
    return str(obj)


def _iso(ts):
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc) \
            .strftime("%Y-%m-%d %H:%M UTC")
    except (TypeError, ValueError, OSError, OverflowError):
        return "?"


def format_candidates(reply):
    """Flatten a resolver reply into printable rows, registered first."""
    rows = []
    for rec in reply.get("registered") or []:
        rows.append({
            "kind": "registered",
            "name": str(rec.get("name", "")),
            "hash": str(rec.get("target", "")),
            "evidence": "expires " + _iso(rec.get("expires")),
        })
    for cand in reply.get("announced") or []:
        try:
            evidence = "trust %.2f" % float(cand.get("trust", 0.0))
        except (TypeError, ValueError):
            evidence = "trust ?"
        if cand.get("last_seen"):
            evidence += ", last_seen " + str(cand["last_seen"])
        rows.append({
            "kind": "announced",
            "name": str(cand.get("name", "")),
            "hash": str(cand.get("hash", "")),
            "evidence": evidence,
        })
    return rows


def render_table(rows):
    headers = ["idx", "kind", "name", "hash", "evidence"]
    table = [[str(i), r["kind"], r["name"], r["hash"], r["evidence"]]
             for i, r in enumerate(rows)]
    widths = []
    for i, header in enumerate(headers):
        cells = [len(row[i]) for row in table]
        widths.append(max([len(header)] + cells))
    lines = ["  ".join(headers[i].ljust(widths[i])
                       for i in range(len(headers)))]
    for row in table:
        lines.append("  ".join(row[i].ljust(widths[i])
                               for i in range(len(headers))))
    return "\n".join(lines)


def _print_changed_warning(name_norm, tofu, repin, out=None):
    out = out or sys.stderr
    bar = "*" * 68
    print(bar, file=out)
    print("WARNING: NAME/HASH CHANGED for '%s'" % name_norm, file=out)
    print("  pinned : %s" % tofu.get("previous"), file=out)
    print("  offered: %s" % tofu.get("hash"), file=out)
    if repin:
        print("  pin OVERWRITTEN (--repin given).", file=out)
    else:
        print("  refusing to overwrite; rerun with --repin to accept.",
              file=out)
    print(bar, file=out)


def _emit_simple(as_json, kind, query, hash_hex):
    if as_json:
        print(json.dumps({"kind": kind, "query": query, "hash": hash_hex},
                         indent=2))
    elif kind == "hash":
        print("%s  (destination hash, resolver not consulted)" % hash_hex)
    else:
        print("%s  (pinned petname '%s')" % (hash_hex, query))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="rns-resolve",
        description="Human-readable name resolution for Reticulum "
                    "(petnames + resolver-on-miss, TOFU pinning).",
    )
    parser.add_argument("query", nargs="?",
                        help="name to resolve, or a 32-hex destination hash")
    parser.add_argument("--resolver", default=None, metavar="HEX32",
                        help="resolver destination hash "
                             "(or env %s)" % RESOLVER_ENV)
    parser.add_argument("--config", default=DEFAULT_CONFIG_DIR, metavar="DIR",
                        help="client config dir (petnames, client identity)")
    parser.add_argument("--rns-config", dest="rns_config", default=None,
                        metavar="DIR", help="RNS config dir")
    parser.add_argument("--register", default=None, metavar="NAME",
                        help="register NAME for your own identity")
    parser.add_argument("--app", default=DEFAULT_APP,
                        help="app name for registration")
    parser.add_argument("--aspects", default=",".join(DEFAULT_ASPECTS),
                        help="comma-separated aspects for registration")
    parser.add_argument("--ttl", type=int, default=DEFAULT_TTL,
                        metavar="SECONDS", help="registration TTL")
    parser.add_argument("--pin", type=int, default=None, metavar="N",
                        help="pin candidate N from the result table")
    parser.add_argument("--repin", action="store_true",
                        help="force a fresh resolve for a pinned name and "
                             "allow overwriting the pin")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="machine-readable JSON output")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        metavar="SECS", help="network timeout")
    return parser


def _resolver_from(args):
    resolver = args.resolver or os.environ.get(RESOLVER_ENV)
    if not resolver:
        print("error: no resolver known; pass --resolver HEX32 or set "
              + RESOLVER_ENV, file=sys.stderr)
        return None
    if not HASH_RE.match(resolver):
        print("error: resolver must be 32 hex chars", file=sys.stderr)
        return None
    return resolver.lower()


def main(argv=None):
    args = build_parser().parse_args(argv)
    aspects = [a for a in args.aspects.split(",") if a]
    petnames = PetnameTable(os.path.join(args.config, "petnames.json"))

    if args.register:
        resolver = _resolver_from(args)
        if resolver is None:
            return 2
        try:
            reply = register_remote(resolver, args.register, args.config,
                                    args.rns_config, args.app, aspects,
                                    args.ttl, args.timeout)
        except Exception as exc:
            print("register failed: %s" % exc, file=sys.stderr)
            return 1
        if not isinstance(reply, dict):
            print("register failed: malformed reply", file=sys.stderr)
            return 1
        if args.as_json:
            print(json.dumps(reply, default=_json_default, indent=2))
            return 0 if reply.get("ok") else 1
        if reply.get("ok"):
            rec = reply.get("record") or {}
            print("registered '%s' -> %s"
                  % (rec.get("name"), rec.get("target")))
            print(json.dumps(rec, default=_json_default, indent=2))
            return 0
        print("register rejected: %s" % reply.get("err"), file=sys.stderr)
        return 1

    if not args.query:
        print("error: a query or --register NAME is required",
              file=sys.stderr)
        return 2

    try:
        kind, value = classify(args.query, petnames)
    except ValueError as exc:
        print("invalid name: %s" % exc, file=sys.stderr)
        return 2

    if kind == "hash":
        # Trust invariant: never consult a resolver for hash-shaped input.
        _emit_simple(args.as_json, "hash", args.query, value)
        return 0

    name_norm = normalize_name(args.query)

    if kind == "petname" and not args.repin:
        # Pinned names never hit the network except under --repin.
        _emit_simple(args.as_json, "petname", name_norm, value)
        return 0

    resolver = _resolver_from(args)
    if resolver is None:
        return 2

    try:
        reply = resolve_remote(resolver, name_norm, args.rns_config,
                               args.timeout)
    except Exception as exc:
        print("resolve failed: %s" % exc, file=sys.stderr)
        return 1
    if not isinstance(reply, dict) or not reply.get("ok"):
        err = reply.get("err") if isinstance(reply, dict) else "malformed reply"
        print("resolver error: %s" % err, file=sys.stderr)
        return 1

    try:
        tofu = apply_tofu(petnames, name_norm, reply,
                          pin_index=args.pin, repin=args.repin)
    except IndexError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if tofu["changed"]:
        _print_changed_warning(name_norm, tofu, args.repin)

    if args.as_json:
        print(json.dumps({
            "kind": "resolve",
            "query": args.query,
            "q": reply.get("q", name_norm),
            "registered": reply.get("registered") or [],
            "announced": reply.get("announced") or [],
            "tofu": tofu,
        }, default=_json_default, indent=2))
    else:
        rows = format_candidates(reply)
        if rows:
            print(render_table(rows))
        else:
            print("no candidates for '%s'" % name_norm)
        if tofu["pinned"]:
            print("pinned '%s' -> %s (%s)"
                  % (name_norm, tofu["hash"], tofu["source"]))
    return 0
