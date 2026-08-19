#!/usr/bin/env python3
# rns-resolve NomadNet exec page: name lookup (index.mu)
# Self-contained stdlib script. No RNS imports. Talks only to the
# colocated resolver's private HTTP API on loopback.

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from beaconrum import track
    track("rns-resolve", "index")
except Exception:
    pass

API_BASE = "http://127.0.0.1:" + os.environ.get("RESOLVE_PRIVATE_PORT", "8226")
TIMEOUT = 3.0


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


def api_get(path, params):
    # Returns (reply_dict_or_None, error_string).
    url = API_BASE + path + "?" + urllib.parse.urlencode(params)
    try:
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


def render_registered(out, regs):
    out.append(">>Registered")
    if not regs:
        out.append("No registered records for this name.")
        return
    for rec in regs:
        out.append("")
        name = esc(rec.get("name", "?"))
        app = esc(rec.get("app", "?"))
        aspects = esc(".".join(str(a) for a in (rec.get("aspects") or [])))
        emit(out, "`!" + name + "`!  app " + app + "  aspects " + aspects)
        emit(out, esc(rec.get("target", "?")))
        emit(out, "expires " + fmt_ts(rec.get("expires")))
        emit(out, "registrant identity " + esc(rec.get("identity", "?")))


def render_announced(out, anns):
    out.append(">>Announced candidates")
    if not anns:
        out.append("No announce candidates matched.")
        return
    for cand in anns:
        out.append("")
        emit(out, "`!" + esc(cand.get("name", "?")) + "`!")
        emit(out, esc(cand.get("hash", "?")))
        try:
            trust = "%.2f" % float(cand.get("trust", 0.0))
        except Exception:
            trust = "?"
        reachable = "yes" if cand.get("reachable") else "no"
        emit(out, "trust " + trust + "  last seen " + esc(cand.get("last_seen", "?"))
             + "  reachable " + reachable)


def render_results(out, q):
    out.append("-")
    out.append("")
    reply, err = api_get("/resolve", {"q": q, "limit": 10})
    if reply is None:
        out.append("The resolver service is not reachable right now.")
        emit(out, "Detail: " + esc(err))
        out.append("Please try again in a moment.")
        return
    if not reply.get("ok"):
        emit(out, "Lookup failed: " + esc(reply.get("err", "unknown error")))
        return
    emit(out, "Results for `!" + esc(reply.get("q", q)) + "`!")
    out.append("")
    render_registered(out, reply.get("registered") or [])
    out.append("")
    render_announced(out, reply.get("announced") or [])
    out.append("")
    out.append("Answers are ranked candidates with evidence, never one")
    out.append("authoritative truth. Copy the 32 character hash you trust")
    out.append("into your client and pin it there, so future lookups need")
    out.append("no network and no resolver.")


def main():
    out = []
    q = read_input("q")

    out.append(">RNS Resolve")
    out.append("")
    out.append("Look up a human readable name and get ranked destination")
    out.append("candidates: records registered here by their owners, plus")
    out.append("names heard in network announces.")
    out.append("")
    out.append("Name to look up")
    out.append("`<32|q`" + esc(q) + ">")
    out.append("")
    out.append("action `<10|step`lookup>")
    out.append("")
    out.append("`[Look up`:/page/index.mu`q|step]")
    out.append("")
    out.append("To register a name of your own, open /page/register.mu on")
    out.append("this node with identification enabled.")

    if q:
        out.append("")
        render_results(out, q)

    print("\n".join(out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("RNS Resolve page error: " + esc(e.__class__.__name__))
