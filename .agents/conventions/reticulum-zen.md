# Reticulum Zen conventions

Source philosophy: [Zen of Reticulum](https://reticulum.network/manual/zen.html).
This file turns that philosophy into hard gates for rns-resolve work.

Naming is the feature most likely to smuggle IP-era assumptions into a mesh,
because everyone reaches for DNS as the mental model. DNS has a root, a
registry, and an authority that can lie about a name and be believed. None of
those may exist here.

## Mental model

- There is no cloud center. A resolver is a peer that offers an opinion, not an
  authority that owns a namespace.
- Destination hashes are identity, not location. A name is a local label over a
  hash; the hash stays the address.
- Assume every resolver is hostile. A record must be verifiable without
  trusting the resolver that served it.
- Bandwidth and airtime are scarce. Replication offers ids first and pushes
  only what a peer asks for.
- Announces are presence. Discovery of resolvers and nodes comes from announces,
  not from a directory this project ships.

## Hard no

1. Do not build a root zone, a registry of registrars, or any single naming
   authority. Multiple independent resolvers disagreeing is the design.
2. Do not let a resolver answer for input that was already a valid destination
   hash. See `skills/trust-invariants/SKILL.md`.
3. Do not encode a name into `Transport.request_path()`. It takes fixed-length
   bytes, so a constructed value is an eternal timeout, and trying is a sign
   the trust boundary is being crossed.
4. Do not accept a caller-supplied target on `register`. Derive it from the
   caller's identity hash.
5. Do not replicate attested records. A record nobody signed is a local opinion.
6. Do not auto-select an announce candidate for a client. Return evidence and
   let the human or the client's own policy choose.
7. Do not require clearnet HTTP or a SaaS for resolution. The Beacon database
   is an optional local enrichment, not a dependency.
8. Do not add a plaintext or unauthenticated replication path for convenience.
9. Do not surveil. Query metrics are counters and a short recent-query ring for
   the operator, not per-identity query logs.

## Hard yes

1. Address peers by destination hash. Names are labels on a keyring the user
   owns, which is what petnames are.
2. Use the aspect correctly (`rnsresolve.query`). Do not overload another
   application's aspect.
3. Design for intermittent links. Peer sync backs off and retries; nothing
   blocks on a peer being reachable.
4. Keep payloads small and capped (5000 ids, 500 records per sync message).
5. Prefer existing RNS and LXMF primitives. Peering stamps use LXMF's own
   `LXStamper`, so LXMF's audit of that code is ours.
6. Make trust legible. Every candidate carries why it might be right: last
   seen, announce count, reachability, whether a signature verified.
7. Let a client work with no resolver at all. Petnames plus hash passthrough is
   a complete, useful mode.

## Before adding a feature

Ask, in order:

1. Does this work when the network is partitioned and half the peers are gone?
2. Can a hostile resolver use it to make a client accept an address the client
   did not choose?
3. Does it introduce a party everyone must trust?
4. Would it still be honest if two resolvers gave different answers?

If any answer is uncomfortable, the design is wrong, not the question.
