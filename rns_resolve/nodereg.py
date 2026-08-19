"""nodereg: register a node's name as part of setting the node up.

This is the intended registration flow. A NomadNet node already holds both
halves a registration needs, its identity file and its configured
node_name, so claiming the name belongs in the node's own setup (a
one-shot at deploy, or a small sidecar that renews the lease), not in a
manual page visit. Because the node's private key signs the record, setup
registrations are self-certifying and replicate between resolvers; the
register.mu page remains the manual fallback for visitors, whose records
are resolver-attested and stay local.

Usage:
  python -m rns_resolve.nodereg \
      --identity /path/to/nomadnet/storage/identity \
      --nomadnet-config /path/to/nomadnet/config \
      --resolver <32-hex resolver dest> \
      [--name override] [--rns-config DIR] [--ttl SECONDS] \
      [--interval SECONDS]   # 0 = one-shot (default); >0 = renew loop
"""

import argparse
import os
import re
import sys
import time
import unicodedata


def derive_site_name(node_name):
    """A registrable name from a NomadNet node_name.

    Announce names are decorative by convention (bold unicode letters,
    emoji): NFKC-fold to plain text, lowercase, turn whitespace runs into
    single dashes, drop everything outside the name charset, then validate
    with the contract normalizer. Raises ValueError if nothing usable
    remains."""
    from .records import normalize_name
    folded = unicodedata.normalize("NFKC", str(node_name)).casefold()
    folded = re.sub(r"\s+", "-", folded.strip())
    folded = re.sub(r"[^a-z0-9._-]", "", folded)
    folded = re.sub(r"-{2,}", "-", folded).strip("-.")
    if not folded:
        raise ValueError("node_name yields no registrable characters: "
                         + repr(node_name))
    return normalize_name(folded)


def read_node_name(config_path):
    """The node_name value from a NomadNet config file, or None."""
    try:
        with open(config_path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if stripped.startswith("node_name"):
                    _, _, value = stripped.partition("=")
                    value = value.strip()
                    if value:
                        return value
    except OSError:
        return None
    return None


def register_once(args):
    """One registration attempt. Returns the reply dict (raises on
    transport failure)."""
    import RNS
    from . import client

    identity = RNS.Identity.from_file(args.identity)
    if identity is None:
        raise RuntimeError("could not load node identity from "
                           + args.identity)

    name = args.name
    if not name:
        node_name = read_node_name(args.nomadnet_config or "")
        if not node_name:
            raise RuntimeError(
                "no --name given and no node_name found in "
                + str(args.nomadnet_config))
        name = derive_site_name(node_name)

    return client.register_remote(
        args.resolver, name, config_dir=None, rns_config=args.rns_config,
        ttl=args.ttl, timeout=args.timeout, identity=identity), name


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="rns_resolve.nodereg",
        description="Register a node's name with a resolver as part of "
                    "node setup (signed with the node's own identity).")
    parser.add_argument("--identity", required=True,
                        help="path to the node's RNS identity file")
    parser.add_argument("--nomadnet-config", default=None,
                        help="NomadNet config file to read node_name from")
    parser.add_argument("--name", default=None,
                        help="explicit name (overrides node_name derivation)")
    parser.add_argument("--resolver",
                        default=os.environ.get("RESOLVE_RESOLVER", ""),
                        help="resolver destination hash (or env "
                             "RESOLVE_RESOLVER)")
    parser.add_argument("--rns-config", default=None)
    parser.add_argument("--ttl", type=int, default=30 * 86400)
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--interval", type=int, default=0,
                        help="renew every N seconds (0 = register once "
                             "and exit)")
    args = parser.parse_args(argv)

    if not args.resolver:
        parser.error("--resolver (or env RESOLVE_RESOLVER) is required")

    while True:
        try:
            reply, name = register_once(args)
            if reply.get("ok"):
                rec = reply.get("record") or {}
                print("registered %r -> %s (expires ts %s)" % (
                    name, rec.get("target"), rec.get("expires")), flush=True)
                if args.interval <= 0:
                    return 0
                time.sleep(args.interval)
                continue
            print("registration refused: %s" % reply.get("err"), flush=True)
            if args.interval <= 0:
                return 1
        except Exception as e:
            print("registration failed: %s: %s"
                  % (e.__class__.__name__, e), flush=True)
            if args.interval <= 0:
                return 1
        # Failure in loop mode: retry sooner than the renew interval.
        time.sleep(min(args.interval, 900))


if __name__ == "__main__":
    sys.exit(main())
