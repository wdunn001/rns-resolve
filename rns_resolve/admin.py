"""Operator dashboard + management for rns-resolve.

A small FastAPI app (one process, ``python -m rns_resolve.admin``) that talks
ONLY to resolvers' loopback private APIs (``/admin/*``, ``/resolve``) and
their ``/healthz``. It holds no state of its own and never touches the
record database directly, so one dashboard can front any number of
resolvers (the live pair A and B on lab-40, and a third when it joins).

Pattern, by request: dependency injection + pydantic.
  * ``AdminSettings``  pydantic model built from the ``RESOLVE_ADMIN_*`` env.
  * ``ResolverClient`` one per resolver; the HTTP transport is an injected
    callable (``fetch``) so tests run with no sockets.
  * ``Registry``       the set of clients + the overview aggregation.
  * ``create_app()``   wires them in via FastAPI ``Depends`` and lets tests
                       swap the registry with ``app.dependency_overrides``.

Access control: the app sits behind the .88 internal-proxy Caddy with
Authentik forward-auth (``resolve.quasarke.net``). It accepts a request only
when it arrives from a trusted proxy AND carries ``X-Authentik-Username``,
or from loopback (operator on the box). Anything else is 403, including a
LAN client hitting :8229 directly. All write actions go to the resolvers'
127.0.0.1 private ports, which are unreachable off-box by construction.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

from pydantic import BaseModel, Field

try:  # FastAPI is an optional extra (pyproject: rns-resolve[admin]).
    from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
except ImportError:  # pragma: no cover - exercised only without the extra
    Depends = FastAPI = Form = HTTPException = Query = Request = None  # type: ignore
    HTMLResponse = JSONResponse = RedirectResponse = None  # type: ignore

from rns_resolve import admin_templates

DEFAULT_PORT = 8229
DEFAULT_RESOLVERS = (
    "A=http://127.0.0.1:8225|http://127.0.0.1:8226,"
    "B=http://127.0.0.1:8227|http://127.0.0.1:8228"
)
FetchFn = Callable[[str, str, dict | None, float], tuple[int, Any]]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class ResolverTarget(BaseModel):
    name: str
    health_url: str
    private_url: str


class AdminSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    title: str = "rns-resolve"
    resolvers: list[ResolverTarget] = Field(default_factory=list)
    trusted_proxies: list[str] = Field(default_factory=lambda: ["192.168.1.88"])
    node_hash: str | None = None
    node_name: str | None = None
    request_timeout_s: float = Field(default=8.0, gt=0)
    records_page_size: int = Field(default=200, ge=1, le=5000)

    @staticmethod
    def parse_resolvers(spec: str) -> list[ResolverTarget]:
        """``NAME=health_url|private_url`` entries separated by commas.
        Missing private url defaults to health host with port+1 (the stack
        convention: 8225/8226, 8227/8228)."""
        out: list[ResolverTarget] = []
        for chunk in (spec or "").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            name, _, urls = chunk.partition("=")
            if not _:
                raise ValueError(f"resolver spec needs NAME=url: {chunk!r}")
            health, _, private = urls.partition("|")
            health = health.strip().rstrip("/")
            private = private.strip().rstrip("/")
            if not health:
                raise ValueError(f"resolver {name!r} has no health url")
            if not private:
                u = urllib.parse.urlsplit(health)
                port = (u.port or 80) + 1
                private = f"{u.scheme}://{u.hostname}:{port}"
            out.append(ResolverTarget(name=name.strip(), health_url=health,
                                      private_url=private))
        return out

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AdminSettings":
        e = os.environ if env is None else env
        proxies = [p.strip() for p in
                   e.get("RESOLVE_ADMIN_TRUSTED_PROXIES", "192.168.1.88").split(",")
                   if p.strip()]
        return cls(
            host=e.get("RESOLVE_ADMIN_HOST", "0.0.0.0"),
            port=int(e.get("RESOLVE_ADMIN_PORT", str(DEFAULT_PORT))),
            title=e.get("RESOLVE_ADMIN_TITLE", "rns-resolve"),
            resolvers=cls.parse_resolvers(
                e.get("RESOLVE_ADMIN_RESOLVERS", DEFAULT_RESOLVERS)),
            trusted_proxies=proxies,
            node_hash=e.get("RESOLVE_ADMIN_NODE_HASH") or None,
            node_name=e.get("RESOLVE_ADMIN_NODE_NAME") or None,
            request_timeout_s=float(e.get("RESOLVE_ADMIN_TIMEOUT_S", "8")),
            records_page_size=int(e.get("RESOLVE_ADMIN_PAGE_SIZE", "200")),
        )


# ---------------------------------------------------------------------------
# Resolver client (transport injected)
# ---------------------------------------------------------------------------

def urllib_fetch(method: str, url: str, body: dict | None,
                 timeout: float) -> tuple[int, Any]:
    """Default transport: stdlib urllib. Returns (status, parsed json|text)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return status, raw.decode("utf-8", "replace")


class ResolverClient:
    def __init__(self, target: ResolverTarget, fetch: FetchFn = urllib_fetch,
                 timeout: float = 8.0) -> None:
        self.target = target
        self._fetch = fetch
        self._timeout = timeout

    @property
    def name(self) -> str:
        return self.target.name

    def _call(self, method: str, base: str, path: str,
              params: dict | None = None, body: dict | None = None) -> dict:
        url = base + path
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v not in (None, "")})
        try:
            status, payload = self._fetch(method, url, body, self._timeout)
        except Exception as exc:  # noqa: BLE001 - surfaced, not raised
            return {"ok": False, "unreachable": True,
                    "err": str(exc) or exc.__class__.__name__}
        if not isinstance(payload, dict):
            return {"ok": False, "err": f"non-json reply ({status})",
                    "status": status}
        payload.setdefault("ok", status < 400)
        payload.setdefault("status", status)
        return payload

    # reads
    def health(self) -> dict:
        return self._call("GET", self.target.health_url, "/healthz")

    def status(self) -> dict:
        return self._call("GET", self.target.private_url, "/admin/status")

    def records(self, q: str | None = None, limit: int = 200, offset: int = 0,
                include_expired: bool = True) -> dict:
        return self._call("GET", self.target.private_url, "/admin/records",
                          params={"q": q, "limit": limit, "offset": offset,
                                  "expired": "1" if include_expired else "0"})

    def resolve(self, q: str, limit: int = 10) -> dict:
        return self._call("GET", self.target.private_url, "/resolve",
                          params={"q": q, "limit": limit})

    # writes (loopback private API only)
    def delete(self, record_id: str) -> dict:
        return self._call("POST", self.target.private_url,
                          "/admin/records/delete", body={"id": record_id})

    def sync(self, peer: str | None = None) -> dict:
        return self._call("POST", self.target.private_url, "/admin/sync",
                          body={"peer": peer} if peer else {})

    def announce(self) -> dict:
        return self._call("POST", self.target.private_url, "/admin/announce",
                          body={})

    def audit(self) -> dict:
        return self._call("POST", self.target.private_url, "/admin/audit",
                          body={})


# ---------------------------------------------------------------------------
# Registry + view models
# ---------------------------------------------------------------------------

def _age(ts: float | None, now: float | None = None) -> str:
    if not ts:
        return "never"
    now = time.time() if now is None else now
    d = max(0, int(now - ts))
    if d < 60:
        return f"{d}s ago"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h {(d % 3600) // 60}m ago"
    return f"{d // 86400}d {(d % 86400) // 3600}h ago"


def _dur(seconds: float | int | None) -> str:
    if seconds is None:
        return "n/a"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    if s < 86400:
        return f"{s // 3600}h {(s % 3600) // 60}m"
    return f"{s // 86400}d {(s % 86400) // 3600}h"


def _stamp(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + "Z"


def resolver_card(name: str, health: dict, status: dict,
                  now: float | None = None) -> dict:
    """Shape one resolver for the dashboard from its /healthz + /admin/status."""
    now = time.time() if now is None else now
    up = bool(health.get("ok")) and not health.get("unreachable")
    st = status if status.get("ok") else {}
    peers = []
    for p in st.get("peer_sync") or []:
        last = p.get("last") or {}
        res = last.get("result") or {}
        peers.append({
            "peer": p.get("peer"),
            "short": (p.get("peer") or "")[:12],
            "backoff": bool(p.get("backoff")),
            "interval": _dur(p.get("interval_s")),
            "due_in": _dur(p.get("due_in_s")),
            "last_ok": _age(p.get("last_ok_at"), now),
            "last_at": _age(last.get("at"), now),
            "last_ok_flag": bool(res.get("ok")),
            "last_summary": (
                f"offered {res.get('offered', 0)}, pushed {res.get('pushed', 0)}, "
                f"accepted {res.get('accepted', 0)}, rejected {res.get('rejected', 0)}"
                if res else "no sync yet"),
            "last_error": res.get("error") or "",
        })
    audit = st.get("peer_audit") or None
    suspects = []
    if isinstance(audit, dict):
        for peer, info in (audit.get("suspects") or {}).items():
            suspects.append({"peer": peer, "info": info})
    metrics = st.get("metrics") or {}
    cfg = st.get("config") or {}
    return {
        "name": name,
        "up": up,
        "rns_ready": bool(health.get("rns_ready") or st.get("rns_ready")),
        "dest": health.get("dest") or st.get("dest") or "",
        "identity": st.get("identity") or "",
        "records": health.get("records", st.get("records", 0)) or 0,
        "beacon_db": bool(health.get("beacon_db") or st.get("beacon_db")),
        "uptime": _dur(st.get("uptime_s")),
        "started_at": _stamp(st.get("started_at")),
        "last_announce": _age(st.get("last_announce_at"), now),
        "announce_count": st.get("announce_count", 0),
        "announce_interval": _dur(st.get("announce_interval_s")),
        "last_sweep_expired": st.get("last_sweep_expired", 0),
        "config": cfg,
        "peers": peers,
        "peering": bool(cfg.get("peers")),
        "audit": audit,
        "suspects": suspects,
        "metrics_total": metrics.get("total", 0),
        "metrics_ops": sorted((metrics.get("ops") or {}).items()),
        "recent": [
            {"age": _age(r.get("ts"), now), "op": r.get("op"), "q": r.get("q") or ""}
            for r in (metrics.get("recent") or [])[:15]
        ],
        "error": "" if up else (health.get("err") or "unreachable"),
        "status_error": "" if status.get("ok") else (status.get("err") or "status unavailable"),
    }


class Registry:
    """All configured resolvers + aggregate views."""

    def __init__(self, settings: AdminSettings,
                 clients: Iterable[ResolverClient] | None = None) -> None:
        self.settings = settings
        self.clients: dict[str, ResolverClient] = {
            c.name: c for c in (clients if clients is not None else
                                (ResolverClient(t, timeout=settings.request_timeout_s)
                                 for t in settings.resolvers))
        }

    def get(self, name: str) -> ResolverClient:
        try:
            return self.clients[name]
        except KeyError:
            raise KeyError(f"unknown resolver {name!r}") from None

    def overview(self) -> dict:
        now = time.time()
        names = list(self.clients)

        def one(name: str) -> dict:
            c = self.clients[name]
            return resolver_card(name, c.health(), c.status(), now)

        with ThreadPoolExecutor(max_workers=max(1, len(names))) as ex:
            cards = list(ex.map(one, names)) if names else []
        total_records = sum(int(c["records"] or 0) for c in cards)
        return {
            "generated_at": _stamp(now),
            "resolvers": cards,
            "up": sum(1 for c in cards if c["up"]),
            "total": len(cards),
            "total_records": total_records,
            "node_hash": self.settings.node_hash,
            "node_name": self.settings.node_name,
        }

    def records(self, resolver: str | None, q: str | None, limit: int,
                offset: int, include_expired: bool) -> dict:
        names = [resolver] if resolver else list(self.clients)
        rows: list[dict] = []
        errors: dict[str, str] = {}
        total = 0
        for name in names:
            c = self.get(name)
            reply = c.records(q=q, limit=limit, offset=offset,
                              include_expired=include_expired)
            if not reply.get("ok"):
                errors[name] = reply.get("err") or "unavailable"
                continue
            total += int(reply.get("total", 0))
            for r in reply.get("records") or []:
                rows.append({**r, "resolver": name,
                             "registered": _stamp(r.get("ts")),
                             "expires": _stamp(r.get("expires_at")),
                             "last_used": _stamp(r.get("last_used")),
                             "short_id": (r.get("id") or "")[:12],
                             "short_target": (r.get("target") or "")[:16],
                             "short_identity": (r.get("identity") or "")[:16]})
        rows.sort(key=lambda r: r.get("ts") or 0, reverse=True)
        return {"records": rows, "total": total, "errors": errors}


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

def is_operator(client_host: str | None, headers: dict, settings: AdminSettings) -> bool:
    """Trusted proxy + Authentik identity, or loopback. Nothing else."""
    host = (client_host or "").strip()
    if host in ("127.0.0.1", "::1", "localhost"):
        return True
    if host in set(settings.trusted_proxies):
        user = headers.get("x-authentik-username") or headers.get("X-Authentik-Username")
        return bool(user)
    return False


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

def create_app(settings: AdminSettings | None = None,
               registry: Registry | None = None):
    if FastAPI is None:  # pragma: no cover
        raise RuntimeError("fastapi is not installed; pip install 'rns-resolve[admin]'")
    settings = settings or AdminSettings.from_env()
    registry = registry or Registry(settings)
    app = FastAPI(title=f"{settings.title} admin", docs_url=None, redoc_url=None)
    render = admin_templates.renderer()

    def get_settings() -> AdminSettings:
        return settings

    def get_registry() -> Registry:
        return registry

    def operator(request: Request, s: AdminSettings = Depends(get_settings)) -> str:
        host = request.client.host if request.client else ""
        if not is_operator(host, dict(request.headers), s):
            raise HTTPException(status_code=403, detail="operator access only")
        return request.headers.get("x-authentik-username") or "local"

    def page(name: str, **ctx: Any) -> HTMLResponse:
        base = {"title": settings.title, "node_hash": settings.node_hash,
                "node_name": settings.node_name, "resolver_names": list(registry.clients)}
        base.update(ctx)
        return HTMLResponse(render(name, **base))

    @app.get("/healthz")
    def healthz(reg: Registry = Depends(get_registry)):
        return {"ok": True, "resolvers": list(reg.clients)}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, user: str = Depends(operator),
                  reg: Registry = Depends(get_registry), msg: str = ""):
        return page("dashboard", overview=reg.overview(), user=user, msg=msg)

    @app.get("/api/overview")
    def api_overview(user: str = Depends(operator), reg: Registry = Depends(get_registry)):
        return JSONResponse(reg.overview())

    @app.get("/records", response_class=HTMLResponse)
    def records(request: Request, user: str = Depends(operator),
                reg: Registry = Depends(get_registry),
                resolver: str = "", q: str = "", expired: str = "1",
                limit: int = Query(default=0, ge=0, le=5000), offset: int = Query(default=0, ge=0),
                msg: str = ""):
        limit = limit or settings.records_page_size
        if resolver and resolver not in reg.clients:
            raise HTTPException(status_code=404, detail="unknown resolver")
        data = reg.records(resolver or None, q or None, limit, offset, expired != "0")
        return page("records", user=user, msg=msg, q=q, resolver=resolver,
                    expired=expired, limit=limit, offset=offset, **data)

    @app.get("/lookup", response_class=HTMLResponse)
    def lookup(request: Request, user: str = Depends(operator),
               reg: Registry = Depends(get_registry), q: str = "", resolver: str = ""):
        results = []
        if q:
            names = [resolver] if resolver else list(reg.clients)
            for name in names:
                if name not in reg.clients:
                    raise HTTPException(status_code=404, detail="unknown resolver")
                results.append({"resolver": name, "reply": reg.get(name).resolve(q)})
        return page("lookup", user=user, q=q, resolver=resolver, results=results)

    def _back(to: str, msg: str) -> RedirectResponse:
        sep = "&" if "?" in to else "?"
        return RedirectResponse(url=f"{to}{sep}msg={urllib.parse.quote(msg)}", status_code=303)

    @app.post("/actions/delete")
    def action_delete(user: str = Depends(operator), reg: Registry = Depends(get_registry),
                      resolver: str = Form(...), record_id: str = Form(...),
                      confirm: str = Form(""), back: str = Form("/records")):
        if confirm != "yes":
            return _back(back, "delete not confirmed")
        if resolver not in reg.clients:
            raise HTTPException(status_code=404, detail="unknown resolver")
        reply = reg.get(resolver).delete(record_id)
        msg = (f"deleted {record_id[:12]} on {resolver}" if reply.get("ok")
               else f"delete failed on {resolver}: {reply.get('err')}")
        return _back(back, msg)

    @app.post("/actions/sync")
    def action_sync(user: str = Depends(operator), reg: Registry = Depends(get_registry),
                    resolver: str = Form(...), peer: str = Form(""), back: str = Form("/")):
        if resolver not in reg.clients:
            raise HTTPException(status_code=404, detail="unknown resolver")
        reply = reg.get(resolver).sync(peer or None)
        if reply.get("ok"):
            parts = []
            for p, r in (reply.get("results") or {}).items():
                parts.append(f"{p[:8]}: {'ok' if r.get('ok') else 'failed'}"
                             f" (pushed {r.get('pushed', 0)}, accepted {r.get('accepted', 0)})")
            msg = f"sync on {resolver}: " + ("; ".join(parts) or "nothing to do")
        else:
            msg = f"sync failed on {resolver}: {reply.get('err')}"
        return _back(back, msg)

    @app.post("/actions/announce")
    def action_announce(user: str = Depends(operator), reg: Registry = Depends(get_registry),
                        resolver: str = Form(...), back: str = Form("/")):
        if resolver not in reg.clients:
            raise HTTPException(status_code=404, detail="unknown resolver")
        reply = reg.get(resolver).announce()
        msg = (f"announced {resolver}" if reply.get("ok")
               else f"announce failed on {resolver}: {reply.get('err')}")
        return _back(back, msg)

    @app.post("/actions/audit")
    def action_audit(user: str = Depends(operator), reg: Registry = Depends(get_registry),
                     resolver: str = Form(...), back: str = Form("/")):
        if resolver not in reg.clients:
            raise HTTPException(status_code=404, detail="unknown resolver")
        reply = reg.get(resolver).audit()
        if reply.get("ok"):
            r = reply.get("result") or {}
            msg = (f"audit on {resolver}: {r.get('peers_answering', 0)} peers answered, "
                   f"pulled {r.get('pulled', 0)}, flagged {len(r.get('flagged') or {})}")
        else:
            msg = f"audit on {resolver}: {reply.get('err')}"
        return _back(back, msg)

    return app


def main() -> None:  # pragma: no cover - process entry point
    import uvicorn
    settings = AdminSettings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                log_level="info", proxy_headers=False)


if __name__ == "__main__":  # pragma: no cover
    main()
