# rns-resolve

Human-readable name resolution for [Reticulum](https://reticulum.network/),
without a registrar, without a blockchain, and without pretending anyone owns
a name.

**Status: prototype.** This is a working exploration of the design space
discussed in
[Reticulum Discussion #965](https://github.com/markqvist/Reticulum/discussions/965),
not a finished protocol. Wire shapes, record formats, and trust heuristics
may all change. Do not build anything load-bearing on top of it yet.

## What it is

A resolver is an ordinary Reticulum destination (`rnsresolve.query`) that
anyone can run. Clients ask it to turn a name like `quasarke.wiki` into a
ranked list of candidate destination hashes, each with evidence attached.
The client picks one, pins it locally, and never asks again until it wants
to.

There is no root zone, no global namespace authority, and no fee. Multiple
resolvers can exist with different views of the world, and they can
optionally replicate self-certifying records to each other.

## Trust model

Four rules, all enforced in code:

1. **Resolvers only ever see non-hash-shaped input.** If what you typed is
   already a valid 32-character hex destination hash, the client uses it
   directly and never contacts a resolver. A resolver can therefore never
   substitute an answer for a hash you already have.
2. **You can only register names for destinations your identity generates.**
   The service derives the target destination hash from the registrant's
   own verified identity hash plus the declared app and aspects. Forging a
   record that points a name at someone else's destination is not possible,
   because the target is computed, never accepted from the caller.
3. **Records are self-certifying when possible.** Records registered over
   an identified link carry a detached signature from the registrant and can
   be replicated between resolvers, with each receiving resolver verifying
   the signature and re-deriving the target itself. Records registered
   through a resolver's local NomadNet page are merely resolver-attested and
   are never replicated.
4. **Answers are candidates, not authority.** A resolve reply is a ranked
   list: registered records first, then names observed in network announces
   (via an optional Beacon crawl database), each with evidence such as
   expiry, announce count, recency, and reachability. The client pins its
   choice locally (trust on first use) and warns loudly if a pinned name
   ever resolves to a different hash.

Names are petnames with gossip, not property.

## Try it against a live resolver

Two independent resolvers run on the public mesh and replicate signed
records to each other. To reach them, your Reticulum config needs a route
onto that mesh; a public entry point is `rns.quasarke.net` on port `4965`
(a plain `TCPClientInterface`). Any other route works too, these are
ordinary destinations.

```sh
pip install rns
git clone https://github.com/wdunn001/rns-resolve && cd rns-resolve

# Resolver A (also serves announce-heard candidates from a crawler index):
export RESOLVE_RESOLVER=5f382b5d0f73a8e35adce587ef7f05f0
# Resolver B, its independent replica, if you would rather ask a second one:
#   ca8751d6d24dcab3a7175264641954a5

# A name registered by its owner at node setup:
python -m rns_resolve rns-resolve

# A name nobody registered: you get announce-heard candidates with evidence
# (trust, last seen, reachability) and pick one yourself:
python -m rns_resolve beacon
```

The first returns a registered record whose target was derived from the
registrant's own identity, and pins it. The second shows why answers are
candidates rather than truth. Nothing here is special to these resolvers:
run your own and point `RESOLVE_RESOLVER` at it instead.

The same resolvers back a NomadNet node you can browse without any client
install, at `3435a207cc2d43cd7ea979e78e89dc16` (lookup on `/page/index.mu`,
registration on `/page/register.mu`, documentation on `/page/docs.mu`).

## Quickstart: users (client)

Requires Python 3.11+, `rns`, and a working Reticulum config of your own.
The client never dials any hardcoded host; it uses your own RNS
configuration to reach the resolver destination you choose.

```sh
# A hash-shaped query is returned as-is, zero network:
python -m rns_resolve 1735f6b1c3a4e29fa2df7a1a47f5c8d0

# Resolve a name via a resolver you trust:
python -m rns_resolve quasarke.wiki --resolver <resolver dest hash>

# Or set it once:
export RESOLVE_RESOLVER=<resolver dest hash>
python -m rns_resolve quasarke.wiki

# Pin candidate 1 into your local petname table:
python -m rns_resolve quasarke.wiki --pin 1

# Pinned names resolve locally from then on. To force a fresh network
# lookup for a pinned name (and accept a changed hash):
python -m rns_resolve quasarke.wiki --repin

# Register a name for a destination derived from YOUR identity:
python -m rns_resolve quasarke.wiki --register quasarke.wiki --resolver <hash>
```

The petname table lives at `~/.rns_resolve/petnames.json` by default. If a
name is pinned, resolution is local and instant; the network is only
consulted on a miss or an explicit `--repin`.

## Quickstart: operators (service)

The daemon attaches to a local Reticulum instance, announces a
`rnsresolve.query` destination, and serves resolve/register/whois requests
over the mesh.

```sh
export RESOLVE_DB=/data/resolve.db
export RESOLVE_RNS_CONFIG=/config
python -m rns_resolve.service
```

Key environment variables (full list in `docs/INTEGRATION.md`):

| Variable | Default | Purpose |
| --- | --- | --- |
| `RESOLVE_DB` | `/data/resolve.db` | sqlite record store |
| `RESOLVE_RNS_CONFIG` | `/config` | Reticulum config dir |
| `RESOLVE_HEALTH_PORT` | `8225` | public health endpoint `/healthz` |
| `RESOLVE_PRIVATE_PORT` | `8226` | loopback-only API for colocated NomadNet pages |
| `RESOLVE_PEERS` | empty | comma-separated peer resolver hashes; empty = replication off |
| `BEACON_DB_*` | unset | optional read-only announce-candidate source |

A `deploy/` directory with a container setup and NomadNet exec pages
(`pages/`) for lookup and attested registration is included.

## Integrating into client apps

See `docs/INTEGRATION.md` for the adoption order (classify, petnames,
resolver, TOFU), the trust invariants your integration must keep, a patch
for the NomadNet browser (`patches/nomadnet-browser-resolve.patch`), and
notes on MeshChat.

## License

MIT. See `LICENSE`.
