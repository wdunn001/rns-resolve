#!/usr/bin/env python3
# rns-resolve NomadNet exec page: name registration (register.mu)
# Self-contained stdlib script. No RNS imports. Talks only to the
# colocated resolver's private HTTP API on loopback.

import json
import os
import re
import time
import urllib.error
import urllib.request

try:
    from beaconrum import track
    track("rns-resolve", "register")
except Exception:
    pass

API_BASE = "http://127.0.0.1:" + os.environ.get("RESOLVE_PRIVATE_PORT", "8226")
TIMEOUT = 3.0

# Names safe to embed inside micron link vars (matches the contract's
# normalized-name charset).
SAFE_NAME_RE = re.compile(r"^[a-z0-9._-]{1,64}$")


def esc(text):
    # Escape backticks so echoed text cannot break micron markup.
    return str(text).replace("`", "\\`")


def emit(out, line=""):
    # Dynamic lines: guard leading chars micron would parse as
    # divider, comment or heading.
    if line[:1] in ("-", "#", ">"):
        line = "\\" + line
    out.append(line)


def read_input(key):
    # Client compat law: read var_x first, then field_x.
    for prefix in ("var_", "field_"):
        val = os.environ.get(prefix + key)
        if val is not None and val.strip() != "":
            return val.strip()
    return ""


def api_post(path, payload):
    # Returns (reply_dict_or_None, error_string).
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            API_BASE + path, data=data,
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        return None, "HTTP " + str(e.code)
    except Exception as e:
        return None, e.__class__.__name__


def api_get(path, params):
    try:
        from urllib.parse import urlencode
        url = API_BASE + path + "?" + urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        return None, "HTTP " + str(e.code)
    except Exception as e:
        return None, e.__class__.__name__


def fmt_ts(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(ts)))
    except Exception:
        return "unknown"


def render_record(out, rec):
    emit(out, "name    `!" + esc(rec.get("name", "?")) + "`!")
    emit(out, "target  " + esc(rec.get("target", "?")))
    emit(out, "app     " + esc(rec.get("app", "?")) + "  aspects "
         + esc(".".join(str(a) for a in (rec.get("aspects") or []))))
    emit(out, "expires " + fmt_ts(rec.get("expires")))


def do_unregister(out, ident, uname):
    out.append("-")
    out.append("")
    reply, err = api_post("/unregister", {"name": uname, "identity": ident})
    if reply is None:
        out.append("Could not reach the resolver service to unregister.")
        emit(out, "Detail: " + esc(err))
        return
    if reply.get("ok"):
        emit(out, "Unregistered `!" + esc(uname) + "`!.")
    else:
        emit(out, "Unregister failed: " + esc(reply.get("err", "unknown error")))


def do_register(out, ident, rname, app, aspects_raw):
    out.append("-")
    out.append("")
    payload = {"name": rname, "identity": ident}
    if app:
        payload["app"] = app
    aspects = [a.strip() for a in aspects_raw.split(",") if a.strip() != ""]
    if aspects:
        payload["aspects"] = aspects
    reply, err = api_post("/register", payload)
    if reply is None:
        out.append("Could not reach the resolver service to register.")
        emit(out, "Detail: " + esc(err))
        out.append("Please try again in a moment.")
        return
    if not reply.get("ok"):
        emit(out, "Registration failed: " + esc(reply.get("err", "unknown error")))
        return
    out.append("Registered. Stored record:")
    out.append("")
    render_record(out, reply.get("record") or {})
    out.append("")
    out.append("Note: this record is resolver attested. It was created")
    out.append("without a client side signature, so it lives only on this")
    out.append("resolver and is never replicated to peers. Use the")
    out.append("rns_resolve CLI client to create a self certifying record.")


def render_owned(out, ident):
    out.append("-")
    out.append("")
    out.append(">>Names owned by your identity")
    reply, err = api_get("/owned", {"identity": ident})
    if reply is None or not reply.get("ok"):
        out.append("Owned name listing is not available on this resolver")
        out.append("right now. Records you register still resolve normally")
        out.append("on the lookup page.")
        return
    records = reply.get("records") or []
    if not records:
        out.append("No names are registered to your identity yet.")
        return
    for rec in records:
        out.append("")
        render_record(out, rec)
        name = str(rec.get("name", ""))
        if SAFE_NAME_RE.match(name):
            out.append("`[Unregister " + name + "`:/page/register.mu`uname="
                       + name + "|ustep=unregister]")
    out.append("")
    out.append("If a link above does not work in your client, type the name")
    out.append("here and submit:")
    out.append("")
    out.append("name to remove `<32|uname`>")
    out.append("")
    out.append("action `<12|ustep`unregister>")
    out.append("")
    out.append("`[Remove name`:/page/register.mu`uname|ustep]")


def main():
    out = []
    ident = (os.environ.get("remote_identity")
             or os.environ.get("REMOTE_IDENTITY") or "").strip()

    out.append(">Register a name")
    out.append("")
    out.append("Ownership is proved by derivation, not by a registrar. The")
    out.append("resolver computes the target destination hash from your")
    out.append("verified identity hash plus the app and aspects you choose.")
    out.append("You can only register names for destinations your own")
    out.append("identity generates, so nobody can point your name at a")
    out.append("destination they do not control.")
    out.append("")

    if not ident:
        out.append("Your identity was not shared with this node, so there is")
        out.append("nothing to derive a destination from. Enable identify in")
        out.append("your client (in NomadNet: connect to this node with")
        out.append("identification allowed) and reload this page.")
        out.append("")
        out.append("`[Back to lookup`:/page/index.mu]")
        print("\n".join(out))
        return

    emit(out, "Your verified identity: `!" + esc(ident) + "`!")
    out.append("")
    out.append("Desired name (letters, digits, dot, dash, underscore)")
    out.append("`<32|rname`>")
    out.append("")
    out.append("app `<24|app`nomadnetwork>  aspects `<24|aspects`node>")
    out.append("")
    out.append("action `<12|step`register>")
    out.append("")
    out.append("`[Register`:/page/register.mu`rname|app|aspects|step]")
    out.append("")
    out.append("The defaults above register your NomadNet node destination.")

    # Unregister takes priority so a leftover register field on the same
    # submit cannot double-fire.
    ustep = read_input("ustep")
    uname = read_input("uname")
    if ustep == "unregister" and uname:
        do_unregister(out, ident, uname)
    else:
        step = read_input("step")
        # The form field must be rname (client compat law); the HTTP API
        # uses "name" internally, mapped in do_register.
        rname = read_input("rname")
        if step == "register" and rname:
            do_register(out, ident, rname, read_input("app"),
                        read_input("aspects"))

    render_owned(out, ident)
    out.append("")
    out.append("`[Back to lookup`:/page/index.mu]")

    print("\n".join(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("RNS Resolve page error: " + esc(e.__class__.__name__))
