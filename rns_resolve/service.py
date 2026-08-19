"""resolved: the rns-resolve daemon.

Op dispatch is a pure function, handle_request(payload_bytes, link_identity,
deps) -> reply_bytes, testable without RNS. All RNS wiring (identity file,
destination, request handler registration, announce loop, link identify
support) lives in ResolveService/main() and is intentionally not unit tested.

Run with: python -m rns_resolve.service
"""

import collections
import importlib
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    import umsgpack
except ImportError:
    try:
        from RNS.vendor import umsgpack
    except ImportError:
        umsgpack = None

APP_NAME = "rnsresolve"
ASPECT = "query"
REQUEST_PATH = "q"

DEFAULT_DB = "/data/resolve.db"
DEFAULT_HEALTH_PORT = 8225
DEFAULT_PRIVATE_PORT = 8226
DEFAULT_RNS_CONFIG = "/config"

DEFAULT_APP = "nomadnetwork"
DEFAULT_ASPECTS = ["node"]

TTL_DEFAULT = 30 * 86400
TTL_MIN = 3600
TTL_MAX = 365 * 86400

RATE_LIMIT_MAX = 30
RATE_LIMIT_WINDOW = 60.0

ANNOUNCE_INTERVAL = 1800
SWEEP_INTERVAL = 3600

DEFAULT_RESOLVE_LIMIT = 10
MAX_RESOLVE_LIMIT = 50


# ---------------------------------------------------------------------------
# Manifest (MeshAPI 0.1 shape)
# ---------------------------------------------------------------------------

def build_manifest(dest_hex=""):
    return {
        "meshapi": "0.1",
        "service": {
            "name": "rns-resolve",
            "summary": "Human-readable name resolution for Reticulum. "
                       "Returns ranked candidates with evidence; clients "
                       "pin locally (TOFU).",
            "app": APP_NAME,
            "aspect": ASPECT,
            "path": REQUEST_PATH,
            "dest": dest_hex,
            "encoding": "umsgpack",
            "source": "https://github.com/wdunn001/rns-resolve",
        },
        "ops": [
            {
                "op": "__manifest__",
                "summary": "Return this manifest (version tolerant).",
                "request": {"op": "str!"},
            },
            {
                "op": "resolve",
                "summary": "Resolve a name to ranked candidate destinations.",
                "request": {"v": "int!", "op": "str!", "q": "str!",
                            "limit": "int?"},
            },
            {
                "op": "register",
                "summary": "Register a name for a destination derived from "
                           "your identified link identity. Requires "
                           "link.identify() and a detached signature.",
                "request": {"v": "int!", "op": "str!", "name": "str!",
                            "app": "str?", "aspects": "list?", "ts": "float!",
                            "ttl": "int?", "sig": "bytes!"},
            },
            {
                "op": "whois",
                "summary": "List registered names for a destination hash.",
                "request": {"v": "int!", "op": "str!", "hash": "str!"},
            },
            {
                "op": "sync.offer",
                "summary": "Peer replication: offer record ids.",
                "request": {"v": "int!", "op": "str!", "ids": "list!"},
            },
            {
                "op": "sync.push",
                "summary": "Peer replication: push self-certifying records.",
                "request": {"v": "int!", "op": "str!", "records": "list!"},
            },
        ],
    }


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window per-key rate limiter."""

    def __init__(self, max_requests=RATE_LIMIT_MAX, window=RATE_LIMIT_WINDOW,
                 clock=time.monotonic):
        self.max_requests = max_requests
        self.window = window
        self.clock = clock
        self._hits = {}
        self._lock = threading.Lock()

    def allow(self, key):
        now = self.clock()
        with self._lock:
            dq = self._hits.get(key)
            if dq is None:
                dq = collections.deque()
                self._hits[key] = dq
            while dq and now - dq[0] >= self.window:
                dq.popleft()
            if len(dq) >= self.max_requests:
                return False
            dq.append(now)
            return True


# ---------------------------------------------------------------------------
# Dependency container
# ---------------------------------------------------------------------------

_UNSET = object()


def load_sync_handler():
    """Lazily import peers.handle_sync; None if peers is unavailable."""
    try:
        peers = importlib.import_module("rns_resolve.peers")
        return getattr(peers, "handle_sync", None)
    except Exception:
        return None


class Deps:
    """Carries everything handle_request needs: store, beacon, manifest,
    rate limiter and the peers.handle_sync callable (or None)."""

    def __init__(self, store=None, beacon=None, manifest=None,
                 rate_limiter=None, sync_handler=_UNSET):
        self.store = store
        self.beacon = beacon
        self.manifest = manifest if manifest is not None else build_manifest()
        self.rate_limiter = (rate_limiter if rate_limiter is not None
                             else RateLimiter())
        if sync_handler is _UNSET:
            sync_handler = load_sync_handler()
        self.sync_handler = sync_handler


def _records():
    """Lazy import so this module loads (and is testable) without records.py
    or RNS present; tests may install a fake module in sys.modules."""
    return importlib.import_module("rns_resolve.records")


# ---------------------------------------------------------------------------
# Pure op implementations (dict in, dict out)
# ---------------------------------------------------------------------------

def _err(msg):
    return {"ok": False, "err": msg}


def _identity_hash_hex(link_identity):
    h = getattr(link_identity, "hash", None)
    if isinstance(h, (bytes, bytearray)):
        return bytes(h).hex()
    if isinstance(h, str):
        return h.lower()
    return None


def _rate_key(link_identity):
    if link_identity is None:
        return "anon"
    hex_hash = _identity_hash_hex(link_identity)
    if hex_hash:
        return hex_hash
    return "link:" + str(id(link_identity))


def _clean_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RESOLVE_LIMIT
    return max(1, min(limit, MAX_RESOLVE_LIMIT))


def record_public(rec, records_mod=None):
    """Record minus sig, plus id and expires."""
    records_mod = records_mod or _records()
    pub = {k: v for k, v in rec.items() if k not in ("sig", "pubkey")}
    pub["id"] = records_mod.record_id(rec)
    pub["expires"] = rec["ts"] + rec["ttl"]
    return pub


def _beacon_available(deps):
    try:
        return bool(deps.beacon) and bool(deps.beacon.available())
    except Exception:
        return False


def op_resolve(payload, deps):
    records = _records()
    try:
        q_norm = records.normalize_name(payload.get("q"))
    except (ValueError, TypeError) as e:
        return _err(str(e) or "invalid name")
    limit = _clean_limit(payload.get("limit", DEFAULT_RESOLVE_LIMIT))

    registered = []
    for rec in deps.store.resolve(q_norm)[:limit]:
        pub = record_public(rec, records)
        try:
            deps.store.touch_use(pub["id"])
        except Exception:
            pass
        registered.append(pub)

    announced = []
    if _beacon_available(deps):
        try:
            announced = deps.beacon.candidates(q_norm, limit=limit)
        except Exception:
            announced = []

    return {"ok": True, "q": q_norm, "registered": registered,
            "announced": announced}


def op_register(payload, link_identity, deps):
    if link_identity is None:
        return _err("identify required")
    records = _records()
    try:
        name_norm = records.normalize_name(payload.get("name"))
    except (ValueError, TypeError) as e:
        return _err(str(e) or "invalid name")

    identity_hex = _identity_hash_hex(link_identity)
    if not identity_hex:
        return _err("identify required")

    app = payload.get("app") or DEFAULT_APP
    aspects = payload.get("aspects") or list(DEFAULT_ASPECTS)
    if not isinstance(app, str) or not isinstance(aspects, list):
        return _err("bad request")

    ts = payload.get("ts")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return _err("bad request")

    ttl = payload.get("ttl", TTL_DEFAULT)
    try:
        ttl = int(ttl)
    except (TypeError, ValueError):
        return _err("bad request")
    ttl = max(TTL_MIN, min(ttl, TTL_MAX))

    sig = payload.get("sig")
    if not isinstance(sig, (bytes, bytearray)):
        return _err("signature required")

    rec = {
        "v": 1,
        "name": name_norm,
        "identity": identity_hex,
        "app": app,
        "aspects": aspects,
        "target": "",
        "ts": float(ts),
        "ttl": ttl,
        "sig": bytes(sig),
    }
    try:
        rec["target"] = records.derive_target(identity_hex, app, aspects)
    except Exception:
        return _err("target derivation failed")
    if not records.verify_record(rec, link_identity):
        return _err("bad signature")

    # Embed the registrant's public key so the record is self-certifying
    # when replicated to peers that have never seen this identity announce.
    try:
        pub = link_identity.get_public_key()
        if pub:
            rec["pubkey"] = bytes(pub)
    except Exception:
        pass

    deps.store.put(rec)
    return {"ok": True, "record": record_public(rec, records)}


def op_whois(payload, deps):
    records = _records()
    target = payload.get("hash")
    if not isinstance(target, str) or not records.HASH_RE.match(target):
        return _err("invalid hash")
    target = target.lower()

    registered = [record_public(r, records)
                  for r in deps.store.whois(target)]

    announced = []
    if _beacon_available(deps):
        # BeaconSource's contract API is name-substring search only; a
        # by-hash lookup is used when the beacon object provides one.
        lookup = getattr(deps.beacon, "whois", None)
        if callable(lookup):
            try:
                announced = lookup(target) or []
            except Exception:
                announced = []

    return {"ok": True, "hash": target, "registered": registered,
            "announced": announced}


def op_sync(op, payload, deps):
    if deps.sync_handler is None:
        return _err("sync disabled")
    reply = deps.sync_handler(op, payload, deps.store)
    if reply is None:
        return _err("unknown op")
    return reply


# ---------------------------------------------------------------------------
# Pure request dispatch (wire bytes in, wire bytes out)
# ---------------------------------------------------------------------------

def handle_request(payload_bytes, link_identity, deps):
    """Pure dispatch: msgpack request bytes -> msgpack reply bytes."""
    if umsgpack is None:
        raise RuntimeError("umsgpack is not available")
    return umsgpack.packb(_handle_request_dict(payload_bytes, link_identity,
                                               deps))


def _handle_request_dict(payload_bytes, link_identity, deps):
    if not deps.rate_limiter.allow(_rate_key(link_identity)):
        return _err("rate limited")

    try:
        payload = umsgpack.unpackb(payload_bytes)
    except Exception:
        return _err("bad payload")
    if not isinstance(payload, dict):
        return _err("bad payload")

    op = payload.get("op")

    # __manifest__ is version tolerant and checked first.
    if op == "__manifest__":
        return {"ok": True, "manifest": deps.manifest}

    if payload.get("v") != 1:
        return _err("unsupported version")
    if not isinstance(op, str):
        return _err("bad request")

    if op == "resolve":
        return op_resolve(payload, deps)
    if op == "register":
        return op_register(payload, link_identity, deps)
    if op == "whois":
        return op_whois(payload, deps)
    if op.startswith("sync."):
        return op_sync(op, payload, deps)
    return _err("unknown op")


# ---------------------------------------------------------------------------
# Private HTTP surface helpers (127.0.0.1 only; NomadNet exec pages)
# ---------------------------------------------------------------------------

def private_register(body, deps):
    """POST /register: resolver-attested record (sig None, never
    replicated). body: {"name","identity","app"?,"aspects"?,"ttl"?}."""
    records = _records()
    try:
        name_norm = records.normalize_name(body.get("name"))
    except (ValueError, TypeError) as e:
        return _err(str(e) or "invalid name")
    identity = body.get("identity")
    if not isinstance(identity, str) or not records.HASH_RE.match(identity):
        return _err("invalid identity")
    identity = identity.lower()
    app = body.get("app") or DEFAULT_APP
    aspects = body.get("aspects") or list(DEFAULT_ASPECTS)
    ttl = body.get("ttl", TTL_DEFAULT)
    try:
        ttl = max(TTL_MIN, min(int(ttl), TTL_MAX))
    except (TypeError, ValueError):
        return _err("bad request")

    rec = {
        "v": 1,
        "name": name_norm,
        "identity": identity,
        "app": app,
        "aspects": aspects,
        "target": "",
        "ts": time.time(),
        "ttl": ttl,
        "sig": None,
    }
    try:
        rec["target"] = records.derive_target(identity, app, aspects)
    except Exception:
        return _err("target derivation failed")
    deps.store.put(rec)
    return {"ok": True, "record": record_public(rec, records)}


def private_unregister(body, deps):
    """POST /unregister: delete records matching name+identity."""
    records = _records()
    try:
        name_norm = records.normalize_name(body.get("name"))
    except (ValueError, TypeError) as e:
        return _err(str(e) or "invalid name")
    identity = body.get("identity")
    if not isinstance(identity, str) or not records.HASH_RE.match(identity):
        return _err("invalid identity")
    identity = identity.lower()

    deleter = getattr(deps.store, "delete", None) or \
        getattr(deps.store, "unregister", None)
    if not callable(deleter):
        return _err("unregister unsupported")
    try:
        removed = deleter(name_norm, identity)
    except Exception:
        return _err("unregister failed")
    return {"ok": True, "name": name_norm, "identity": identity,
            "removed": int(removed or 0)}


# ---------------------------------------------------------------------------
# HTTP servers (not unit tested)
# ---------------------------------------------------------------------------

def _make_health_handler(svc):
    class HealthHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_GET(self):
            if urlparse(self.path).path != "/healthz":
                self._send(404, {"ok": False, "err": "not found"})
                return
            try:
                count = svc.deps.store.count()
            except Exception:
                count = 0
            body = {
                "status": "ok",
                "rns_ready": bool(svc.rns_ready),
                "records": count,
                "beacon_db": _beacon_available(svc.deps),
                "dest": svc.dest_hex,
            }
            self._send(200 if svc.rns_ready else 503, body)

        def _send(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return HealthHandler


def _make_private_handler(svc):
    class PrivateHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send(self, code, obj):
            data = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                if not isinstance(body, dict):
                    return None
                return body
            except Exception:
                return None

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != "/resolve":
                self._send(404, {"ok": False, "err": "not found"})
                return
            qs = parse_qs(parsed.query)
            payload = {
                "q": (qs.get("q") or [""])[0],
                "limit": (qs.get("limit") or [DEFAULT_RESOLVE_LIMIT])[0],
            }
            try:
                reply = op_resolve(payload, svc.deps)
            except Exception:
                reply = {"ok": False, "err": "internal error"}
            self._send(200 if reply.get("ok") else 400, reply)

        def do_POST(self):
            path = urlparse(self.path).path
            body = self._read_json()
            if body is None:
                self._send(400, {"ok": False, "err": "bad request"})
                return
            try:
                if path == "/register":
                    reply = private_register(body, svc.deps)
                elif path == "/unregister":
                    reply = private_unregister(body, svc.deps)
                else:
                    self._send(404, {"ok": False, "err": "not found"})
                    return
            except Exception:
                reply = {"ok": False, "err": "internal error"}
            self._send(200 if reply.get("ok") else 400, reply)

    return PrivateHandler


# ---------------------------------------------------------------------------
# RNS wiring (not unit tested)
# ---------------------------------------------------------------------------

class ResolveService:
    def __init__(self, env=None):
        env = dict(os.environ) if env is None else dict(env)
        self.db_path = env.get("RESOLVE_DB", DEFAULT_DB)
        self.health_port = int(env.get("RESOLVE_HEALTH_PORT",
                                       str(DEFAULT_HEALTH_PORT)))
        self.private_port = int(env.get("RESOLVE_PRIVATE_PORT",
                                        str(DEFAULT_PRIVATE_PORT)))
        self.rns_configdir = env.get("RESOLVE_RNS_CONFIG", DEFAULT_RNS_CONFIG)
        self.peer_hashes = [p.strip() for p in
                            env.get("RESOLVE_PEERS", "").split(",")
                            if p.strip()]
        self.env = env

        self.rns_ready = False
        self.dest_hex = ""
        self.deps = None
        self.reticulum = None
        self.identity = None
        self.destination = None
        self.peer_scheduler = None
        self._stop = threading.Event()
        self._servers = []

    # -- setup ------------------------------------------------------------

    def _build_deps(self):
        from rns_resolve.store import Store
        store = Store(self.db_path)
        try:
            from rns_resolve.beacon_source import BeaconSource
            beacon = BeaconSource(self.env)
        except Exception:
            beacon = None
        self.deps = Deps(store=store, beacon=beacon,
                         manifest=build_manifest(),
                         rate_limiter=RateLimiter())

    def _load_identity(self):
        import RNS
        path = os.path.join(self.rns_configdir, "resolve_identity")
        if os.path.isfile(path):
            identity = RNS.Identity.from_file(path)
            if identity is None:
                raise RuntimeError("could not load identity file " + path)
        else:
            identity = RNS.Identity()
            identity.to_file(path)
        return identity

    def _start_rns(self):
        import RNS
        self.reticulum = RNS.Reticulum(configdir=self.rns_configdir)
        self.identity = self._load_identity()
        self.destination = RNS.Destination(
            self.identity, RNS.Destination.IN, RNS.Destination.SINGLE,
            APP_NAME, ASPECT)
        self.destination.register_request_handler(
            REQUEST_PATH, response_generator=self._rns_request_handler,
            allow=RNS.Destination.ALLOW_ALL)
        self.destination.set_link_established_callback(self._link_established)
        self.dest_hex = self.destination.hash.hex()
        self.deps.manifest["service"]["dest"] = self.dest_hex
        self.rns_ready = True

    def _link_established(self, link):
        # Nothing to do beyond accepting the link; clients may identify()
        # on it and the identity arrives with each request.
        pass

    def _rns_request_handler(self, path, data, request_id, link_id,
                             remote_identity, requested_at):
        try:
            return handle_request(data, remote_identity, self.deps)
        except Exception:
            if umsgpack is None:
                return None
            return umsgpack.packb(_err("internal error"))

    def _start_http(self):
        health = ThreadingHTTPServer(("0.0.0.0", self.health_port),
                                     _make_health_handler(self))
        private = ThreadingHTTPServer(("127.0.0.1", self.private_port),
                                      _make_private_handler(self))
        for server in (health, private):
            self._servers.append(server)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()

    def _start_peers(self):
        if not self.peer_hashes:
            return
        try:
            from rns_resolve import peers
            self.peer_scheduler = peers.PeerScheduler(
                self.deps.store, self.peer_hashes, self)
            self.peer_scheduler.start()
        except Exception:
            self.peer_scheduler = None

    def _announce_loop(self):
        last_announce = 0.0
        last_sweep = time.monotonic()
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_announce >= ANNOUNCE_INTERVAL:
                try:
                    self.destination.announce()
                except Exception:
                    pass
                last_announce = now
            if now - last_sweep >= SWEEP_INTERVAL:
                try:
                    self.deps.store.expire_sweep()
                except Exception:
                    pass
                last_sweep = now
            self._stop.wait(30)

    # -- lifecycle --------------------------------------------------------

    def start(self):
        self._build_deps()
        self._start_http()
        self._start_rns()
        self._start_peers()
        threading.Thread(target=self._announce_loop, daemon=True).start()

    def stop(self):
        self._stop.set()
        if self.peer_scheduler is not None:
            try:
                self.peer_scheduler.stop()
            except Exception:
                pass
        for server in self._servers:
            try:
                server.shutdown()
            except Exception:
                pass


def main():
    service = ResolveService()
    service.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        service.stop()


if __name__ == "__main__":
    main()
