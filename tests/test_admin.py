"""Unit tests for the operator dashboard (rns_resolve.admin).

No sockets and no RNS: the HTTP transport is injected into ResolverClient,
and the FastAPI app is exercised with fastapi.testclient against a Registry
built from fake clients. FastAPI/jinja2 are an optional extra, so the app
tests skip when they are absent while the pure-logic tests always run.
"""

import json
import time
import unittest

from rns_resolve import admin

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:  # noqa: BLE001
    HAVE_FASTAPI = False

try:
    import jinja2  # noqa: F401
    HAVE_JINJA = True
except Exception:  # noqa: BLE001
    HAVE_JINJA = False


def settings(**kw):
    base = dict(
        resolvers=admin.AdminSettings.parse_resolvers(
            "A=http://127.0.0.1:8225|http://127.0.0.1:8226,"
            "B=http://127.0.0.1:8227|http://127.0.0.1:8228"),
        trusted_proxies=["192.168.1.88"],
    )
    base.update(kw)
    return admin.AdminSettings(**base)


class FakeFetch:
    """Records calls, replies from a {(method, path): (status, body)} table."""

    def __init__(self, table=None, boom=False):
        self.table = table or {}
        self.calls = []
        self.boom = boom

    def __call__(self, method, url, body, timeout):
        self.calls.append((method, url, body))
        if self.boom:
            raise OSError("connection refused")
        path = url.split("://", 1)[1].split("/", 1)[1]
        path = "/" + path.split("?", 1)[0]
        return self.table.get((method, path), (404, {"ok": False, "err": "not found"}))


class SettingsTest(unittest.TestCase):
    def test_parse_resolvers_explicit_private_url(self):
        got = admin.AdminSettings.parse_resolvers("A=http://h:1|http://h:2")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].name, "A")
        self.assertEqual(got[0].private_url, "http://h:2")

    def test_parse_resolvers_defaults_private_to_port_plus_one(self):
        got = admin.AdminSettings.parse_resolvers("A=http://127.0.0.1:8225")
        self.assertEqual(got[0].private_url, "http://127.0.0.1:8226")

    def test_parse_resolvers_rejects_missing_name(self):
        with self.assertRaises(ValueError):
            admin.AdminSettings.parse_resolvers("http://127.0.0.1:8225")

    def test_from_env_reads_knobs(self):
        s = admin.AdminSettings.from_env({
            "RESOLVE_ADMIN_PORT": "9999",
            "RESOLVE_ADMIN_RESOLVERS": "solo=http://x:1|http://x:2",
            "RESOLVE_ADMIN_TRUSTED_PROXIES": "10.0.0.1, 10.0.0.2",
            "RESOLVE_ADMIN_NODE_HASH": "abc",
        })
        self.assertEqual(s.port, 9999)
        self.assertEqual([r.name for r in s.resolvers], ["solo"])
        self.assertEqual(s.trusted_proxies, ["10.0.0.1", "10.0.0.2"])
        self.assertEqual(s.node_hash, "abc")


class ResolverClientTest(unittest.TestCase):
    def client(self, table=None, boom=False):
        target = admin.AdminSettings.parse_resolvers(
            "A=http://127.0.0.1:8225|http://127.0.0.1:8226")[0]
        fetch = FakeFetch(table, boom=boom)
        return admin.ResolverClient(target, fetch=fetch), fetch

    def test_health_hits_health_port(self):
        c, fetch = self.client({("GET", "/healthz"): (200, {"status": "ok", "records": 3})})
        reply = c.health()
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["records"], 3)
        self.assertTrue(fetch.calls[0][1].startswith("http://127.0.0.1:8225"))

    def test_admin_calls_hit_private_port(self):
        c, fetch = self.client({("GET", "/admin/status"): (200, {"ok": True})})
        c.status()
        self.assertTrue(fetch.calls[0][1].startswith("http://127.0.0.1:8226"))

    def test_unreachable_is_reported_not_raised(self):
        c, _ = self.client(boom=True)
        reply = c.health()
        self.assertFalse(reply["ok"])
        self.assertTrue(reply["unreachable"])

    def test_delete_posts_the_id(self):
        c, fetch = self.client({("POST", "/admin/records/delete"): (200, {"ok": True})})
        c.delete("deadbeef")
        method, url, body = fetch.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(body, {"id": "deadbeef"})

    def test_records_passes_filters(self):
        c, fetch = self.client({("GET", "/admin/records"): (200, {"ok": True, "records": []})})
        c.records(q="beacon", limit=5, offset=10, include_expired=False)
        url = fetch.calls[0][1]
        self.assertIn("q=beacon", url)
        self.assertIn("limit=5", url)
        self.assertIn("offset=10", url)
        self.assertIn("expired=0", url)

    def test_non_json_reply_is_an_error_not_a_crash(self):
        c, _ = self.client({("GET", "/healthz"): (500, "<html>nope</html>")})
        reply = c.health()
        self.assertFalse(reply["ok"])
        self.assertIn("non-json", reply["err"])


class ResolverCardTest(unittest.TestCase):
    def test_card_of_a_healthy_peered_resolver(self):
        now = 1_700_000_000.0
        health = {"ok": True, "status": "ok", "rns_ready": True, "records": 20,
                  "beacon_db": True, "dest": "5f38"}
        status = {"ok": True, "identity": "aa11", "uptime_s": 7200,
                  "started_at": now - 7200, "last_announce_at": now - 120,
                  "announce_count": 4, "announce_interval_s": 1800,
                  "config": {"peers": ["ca87"], "peering_cost": 18, "sync_from": []},
                  "peer_sync": [{"peer": "ca87", "interval_s": 900, "backoff": False,
                                 "due_in_s": 300, "last_ok_at": now - 300,
                                 "last": {"at": now - 300,
                                          "result": {"ok": True, "offered": 20,
                                                     "pushed": 1, "accepted": 1,
                                                     "rejected": 0}}}],
                  "metrics": {"total": 9, "ops": {"resolve": 8, "whois": 1},
                              "recent": [{"ts": now - 5, "op": "resolve", "q": "beacon"}]}}
        card = admin.resolver_card("A", health, status, now=now)
        self.assertTrue(card["up"])
        self.assertTrue(card["rns_ready"])
        self.assertTrue(card["peering"])
        self.assertEqual(card["records"], 20)
        self.assertEqual(card["uptime"], "2h 0m")
        self.assertEqual(card["peers"][0]["short"], "ca87")
        self.assertTrue(card["peers"][0]["last_ok_flag"])
        self.assertIn("pushed 1", card["peers"][0]["last_summary"])
        self.assertEqual(card["metrics_total"], 9)
        self.assertEqual(card["recent"][0]["q"], "beacon")

    def test_card_of_a_down_resolver_keeps_the_error(self):
        card = admin.resolver_card("B", {"ok": False, "unreachable": True,
                                         "err": "connection refused"}, {"ok": False})
        self.assertFalse(card["up"])
        self.assertEqual(card["error"], "connection refused")
        self.assertEqual(card["records"], 0)

    def test_backoff_peer_is_marked(self):
        card = admin.resolver_card("A", {"ok": True}, {
            "ok": True,
            "peer_sync": [{"peer": "ca87", "interval_s": 3600, "backoff": True,
                           "due_in_s": 1200, "last": {"result": {"ok": False}}}]})
        self.assertTrue(card["peers"][0]["backoff"])
        self.assertFalse(card["peers"][0]["last_ok_flag"])


class RegistryTest(unittest.TestCase):
    def build(self, tables):
        s = settings()
        clients = []
        for target in s.resolvers:
            clients.append(admin.ResolverClient(target, fetch=FakeFetch(tables[target.name])))
        return admin.Registry(s, clients)

    def test_overview_aggregates_every_resolver(self):
        reg = self.build({
            "A": {("GET", "/healthz"): (200, {"status": "ok", "records": 20, "dest": "5f38"}),
                  ("GET", "/admin/status"): (200, {"ok": True, "records": 20})},
            "B": {("GET", "/healthz"): (200, {"status": "ok", "records": 19, "dest": "ca87"}),
                  ("GET", "/admin/status"): (200, {"ok": True, "records": 19})},
        })
        ov = reg.overview()
        self.assertEqual(ov["total"], 2)
        self.assertEqual(ov["up"], 2)
        self.assertEqual(ov["total_records"], 39)

    def test_overview_survives_one_dead_resolver(self):
        reg = self.build({
            "A": {("GET", "/healthz"): (200, {"status": "ok", "records": 20}),
                  ("GET", "/admin/status"): (200, {"ok": True})},
            "B": {},   # every call 404s
        })
        ov = reg.overview()
        self.assertEqual(ov["up"], 1)
        self.assertEqual(ov["total"], 2)

    def test_records_merges_and_sorts_newest_first(self):
        reg = self.build({
            "A": {("GET", "/admin/records"): (200, {"ok": True, "total": 1, "records": [
                {"id": "a1", "name": "beacon", "target": "7d6d", "identity": "11",
                 "ts": 100, "expires_at": 200}]})},
            "B": {("GET", "/admin/records"): (200, {"ok": True, "total": 1, "records": [
                {"id": "b1", "name": "signal-fire", "target": "ca87", "identity": "22",
                 "ts": 300, "expires_at": 400}]})},
        })
        out = reg.records(None, None, 100, 0, True)
        self.assertEqual([r["id"] for r in out["records"]], ["b1", "a1"])
        self.assertEqual([r["resolver"] for r in out["records"]], ["B", "A"])
        self.assertEqual(out["total"], 2)

    def test_records_reports_a_failing_resolver_without_losing_the_others(self):
        reg = self.build({
            "A": {("GET", "/admin/records"): (200, {"ok": True, "total": 1, "records": [
                {"id": "a1", "name": "beacon", "ts": 1}]})},
            "B": {("GET", "/admin/records"): (500, {"ok": False, "err": "boom"})},
        })
        out = reg.records(None, None, 100, 0, True)
        self.assertEqual(len(out["records"]), 1)
        self.assertEqual(out["errors"]["B"], "boom")

    def test_unknown_resolver_raises(self):
        reg = self.build({"A": {}, "B": {}})
        with self.assertRaises(KeyError):
            reg.get("nope")


class AccessControlTest(unittest.TestCase):
    def setUp(self):
        self.s = settings()

    def test_loopback_is_operator(self):
        self.assertTrue(admin.is_operator("127.0.0.1", {}, self.s))

    def test_trusted_proxy_needs_authentik_header(self):
        self.assertFalse(admin.is_operator("192.168.1.88", {}, self.s))
        self.assertTrue(admin.is_operator(
            "192.168.1.88", {"x-authentik-username": "wdunn001"}, self.s))

    def test_lan_client_is_rejected_even_with_a_forged_header(self):
        self.assertFalse(admin.is_operator(
            "192.168.1.50", {"x-authentik-username": "wdunn001"}, self.s))

    def test_unknown_host_is_rejected(self):
        self.assertFalse(admin.is_operator(None, {}, self.s))


@unittest.skipUnless(HAVE_FASTAPI and HAVE_JINJA, "fastapi/jinja2 extra not installed")
class AppTest(unittest.TestCase):
    def setUp(self):
        now = time.time()
        self.tables = {
            "A": {("GET", "/healthz"): (200, {"status": "ok", "rns_ready": True,
                                              "records": 2, "beacon_db": True,
                                              "dest": "5f382b5d"}),
                  ("GET", "/admin/status"): (200, {"ok": True, "identity": "aa11",
                                                   "uptime_s": 60, "started_at": now - 60,
                                                   "config": {"peers": ["ca87"], "peering_cost": 18},
                                                   "peer_sync": [{"peer": "ca87", "interval_s": 900,
                                                                  "due_in_s": 10, "last": {}}],
                                                   "metrics": {"total": 1, "ops": {"resolve": 1},
                                                               "recent": []}}),
                  ("GET", "/admin/records"): (200, {"ok": True, "total": 1, "records": [
                      {"id": "rec1", "name": "beacon", "target": "7d6d19a4",
                       "identity": "1122", "app": "nomadnetwork", "aspects": ["node"],
                       "ts": now - 10, "ttl": 100, "expires_at": now + 90,
                       "attested": False, "pubkey_bound": True}]}),
                  ("GET", "/resolve"): (200, {"ok": True, "results": [
                      {"name": "beacon", "target": "7d6d19a4", "source": "record"}]}),
                  ("POST", "/admin/records/delete"): (200, {"ok": True, "removed": 1}),
                  ("POST", "/admin/sync"): (200, {"ok": True, "results": {
                      "ca87": {"ok": True, "pushed": 1, "accepted": 1}}}),
                  ("POST", "/admin/announce"): (200, {"ok": True}),
                  ("POST", "/admin/audit"): (200, {"ok": True, "result": {
                      "peers_answering": 1, "pulled": 0, "flagged": {}}}),
                  },
            "B": {("GET", "/healthz"): (200, {"status": "ok", "rns_ready": True,
                                              "records": 1, "dest": "ca8751d6"}),
                  ("GET", "/admin/status"): (200, {"ok": True}),
                  ("GET", "/admin/records"): (200, {"ok": True, "total": 0, "records": []}),
                  },
        }
        self.s = settings()
        self.fetches = {}
        clients = []
        for target in self.s.resolvers:
            f = FakeFetch(self.tables[target.name])
            self.fetches[target.name] = f
            clients.append(admin.ResolverClient(target, fetch=f))
        self.app = admin.create_app(self.s, admin.Registry(self.s, clients))
        self.op = {"x-authentik-username": "wdunn001"}

    def client(self, host="192.168.1.88"):
        return TestClient(self.app, client=(host, 12345))

    def test_healthz_is_open(self):
        r = self.client("192.168.1.50").get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["resolvers"], ["A", "B"])

    def test_dashboard_requires_operator(self):
        self.assertEqual(self.client().get("/").status_code, 403)
        self.assertEqual(self.client("192.168.1.50").get("/", headers=self.op).status_code, 403)
        r = self.client().get("/", headers=self.op)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Resolver A", r.text)
        self.assertIn("5f382b5d", r.text)

    def test_dashboard_renders_from_loopback_without_a_header(self):
        r = self.client("127.0.0.1").get("/")
        self.assertEqual(r.status_code, 200)

    def test_api_overview_json(self):
        r = self.client().get("/api/overview", headers=self.op)
        body = r.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(body["total_records"], 3)

    def test_records_page_lists_and_filters(self):
        r = self.client().get("/records?q=beacon", headers=self.op)
        self.assertEqual(r.status_code, 200)
        self.assertIn("beacon", r.text)
        self.assertIn("q=beacon", self.fetches["A"].calls[-1][1])

    def test_records_unknown_resolver_404s(self):
        r = self.client().get("/records?resolver=zz", headers=self.op)
        self.assertEqual(r.status_code, 404)

    def test_lookup_queries_every_resolver(self):
        r = self.client().get("/lookup?q=beacon", headers=self.op)
        self.assertEqual(r.status_code, 200)
        self.assertIn("7d6d19a4", r.text)
        self.assertTrue(any("/resolve?q=beacon" in c[1] for c in self.fetches["A"].calls))

    def test_delete_requires_confirmation(self):
        c = self.client()
        r = c.post("/actions/delete", headers=self.op, follow_redirects=False,
                   data={"resolver": "A", "record_id": "rec1", "back": "/records"})
        self.assertEqual(r.status_code, 303)
        self.assertIn("not+confirmed", r.headers["location"].replace("%20", "+"))
        self.assertFalse(any(call[0] == "POST" and "delete" in call[1]
                             for call in self.fetches["A"].calls))

    def test_delete_with_confirmation_calls_the_resolver(self):
        c = self.client()
        r = c.post("/actions/delete", headers=self.op, follow_redirects=False,
                   data={"resolver": "A", "record_id": "rec1", "confirm": "yes",
                         "back": "/records"})
        self.assertEqual(r.status_code, 303)
        self.assertTrue(any(call[0] == "POST" and call[1].endswith("/admin/records/delete")
                            for call in self.fetches["A"].calls))

    def test_actions_require_operator(self):
        r = self.client().post("/actions/announce", data={"resolver": "A"})
        self.assertEqual(r.status_code, 403)

    def test_sync_and_announce_and_audit_actions(self):
        c = self.client()
        for path in ("/actions/sync", "/actions/announce", "/actions/audit"):
            r = c.post(path, headers=self.op, follow_redirects=False,
                       data={"resolver": "A", "back": "/"})
            self.assertEqual(r.status_code, 303, path)
        posted = [call[1] for call in self.fetches["A"].calls if call[0] == "POST"]
        self.assertTrue(any(p.endswith("/admin/sync") for p in posted))
        self.assertTrue(any(p.endswith("/admin/announce") for p in posted))
        self.assertTrue(any(p.endswith("/admin/audit") for p in posted))

    def test_action_on_unknown_resolver_404s(self):
        r = self.client().post("/actions/announce", headers=self.op,
                               data={"resolver": "zz"})
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
