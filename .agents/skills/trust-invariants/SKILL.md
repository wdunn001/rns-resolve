---
name: trust-invariants
description: The trust boundary of Reticulum name resolution. Read before changing what a resolver answers, what a client accepts, or how a record is proven. Every rule here has a reason a client can verify.
---

# Skill: trust-invariants

DNS habits are the main hazard in this codebase. Each rule below exists because
breaking it hands a resolver authority it cannot be given.

## When to use

- Changing `resolve`, `register`, `whois`, or the client's classify step
- Adding an answer source, a ranking, or an auto-selection
- Anything that makes a client accept an address it did not type
- Reviewing a patch that "just adds a fallback"

## Gate 0: hash input never reaches a resolver

A resolver is consulted **only** for input that never matched the 32-hex
pattern. A syntactically valid hash that times out is a plain timeout.

Why: if a dead hash could fall back to a resolver, a resolver could override a
literal address the user supplied. That is exactly the central trust this
project avoids. "Is there an alias for this dead hash?" is allowed only as a
separate, explicit user action.

This is structural, not only policy. `Transport.request_path()` takes
fixed-length bytes, so a non-hash string cannot be carried as a path request at
all. Resolvers answer only what RNS itself cannot express.

**Anti-pattern:** encoding or translating a name into `request_path()`. Path
requests can only confirm exact announced hashes, so a constructed value is an
eternal timeout, and the attempt means the boundary is being crossed.

## Gate 1: the target is derived, never supplied

`register` runs over an identified link and computes:

```
RNS.Destination.hash(bytes.fromhex(caller_identity_hex), app, *aspects)
```

A caller can therefore only register names for destinations it already owns. No
code path may accept a target from the request body. A record whose target does
not re-derive from its identity field is invalid, and peers re-derive on
receipt rather than trusting the sender.

## Gate 2: records prove themselves

A replicable record carries the registrant's signature and their public key,
hash-bound to the identity field. A peer verifies standalone first, then falls
back to `Identity.recall`.

Why the embedded key: `recall` only knows identities that have announced. A
registrant who never announced was unverifiable by a peer, so replication
silently dropped honest records. Trap: `Identity.load_public_key` returns
`None` on success. Judge by `ident.hash`.

Attested records (`sig` NULL, accepted through the local node page) are local
opinions. They never replicate, never appear in `all_ids`, and never go to a
peer.

## Gate 3: candidates carry evidence and are never chosen for the user

Announce-heard candidates from the Beacon index are a convenience, not an
answer. Each one is returned with what is known about it: last seen, announce
count, reachability, whether anything corroborates it. The client, or the
person, picks. Nothing auto-resolves a name to an unregistered candidate, and
`client.resolve_name` (the hook a browser patch calls) returns registered
records only, by design.

## Gate 4: the user's table wins

Petnames are local and pinned trust-on-first-use. A pinned name resolves with
no network. If a resolver later returns a different hash for a pinned name, the
client warns and requires `--repin`; it does not follow silently.

## Gate 5: no naming authority

Multiple independent resolvers that can disagree is the design, not a defect to
fix with a registry, a root, or a consensus protocol. When two resolvers
disagree, surface both. Never add a mechanism whose purpose is to make one
resolver's answer final for everyone.

## Reviewer checklist

1. Could a hostile resolver make a client accept an address the user did not
   choose? If yes, reject the change.
2. Does any new field let a caller state a target, a name owner, or a trust
   level instead of proving it?
3. Does anything unsigned now cross a peer boundary?
4. Does a new answer path bypass classify or petnames?
5. Is the failure mode "fewer answers", or is it "a wrong answer that looks
   right"? Only the first is acceptable.

Tests for these gates live in `tests/test_service.py`, `tests/test_records.py`,
`tests/test_peers.py` and `tests/test_client.py`. Changing a gate should break
a named test first.
