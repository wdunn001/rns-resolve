---
name: peer-replication
description: How records move between resolvers. Offer/want/push, LXMF peering stamps, the withholding audit, and the deploy rules that come with them. Read before touching peers.py or any sync op.
---

# Skill: peer-replication

Replication is what makes a name registered on one resolver resolvable on
another without a central registry. It is also the largest attack surface in
the project, so every step is gated.

## When to use

- Editing `rns_resolve/peers.py` or the `sync.*` ops in `service.py`
- Adding a peer, changing peering cost, or changing the allowlist
- Diagnosing why a record did not propagate
- Anything touching the withholding audit

Also read: `.agents/conventions/reticulum-zen.md`,
`.agents/skills/trust-invariants/SKILL.md`, `docs/LXMPEER-GAPS.md`.

## The protocol

Modeled on LXMPeer, deliberately: their semantics are proven and their audit is
ours.

1. `sync.offer` sends the ids we hold that are replicable (signed only).
2. The peer answers with `want`: the subset it lacks.
3. `sync.push` sends exactly those records. The peer validates each one
   (signature, pubkey binding, target re-derivation) and reports accepted and
   rejected counts.
4. `sync.inventory` and `sync.fetch` back the audit: list what we hold, serve
   records by id.

All four require an identified link and a peering stamp. Payloads are capped at
5000 ids and 500 records.

## Peering stamps

Stamps use LXMF's own `LXStamper`, with the same key material
(`peer_identity.hash + own_identity.hash`), the same 25-round workblock, and a
default cost of 18, which is `LXMRouter.PEERING_COST`. `RESOLVE_PEERING_COST=0`
disables stamping.

Cost is self-negotiated: a keyless or underweight offer gets an error carrying
the required cost, the sender generates a key for that cost and retries once.
Keys are memory-only because regenerating one is cheap.

**Deploy rule: both resolvers must ship together.** A stamped resolver rejects
an unstamped peer, so a half-upgraded pair stops replicating and looks like a
network fault.

## Backoff and scheduling

`PeerScheduler` syncs each peer every `SYNC_INTERVAL` (15 min). A failure
doubles that peer's interval up to `MAX_BACKOFF` (4 h); a success resets it.
The scheduler syncs at container start, so `docker restart` is a legitimate
on-demand sync trigger, and the dashboard's sync button is the polite version.

`state()` reports per-peer interval, next due, last result and last success for
the operator. Keep it accurate when you touch the loop: it is how an operator
tells "peer is down" from "peer is fine, next sync in 12 minutes".

## Withholding audit (MOFU)

Signatures stop forgery. They do not stop a peer that simply never offers what
it holds. One audit round:

1. Fetch every peer's inventory.
2. **Repair first.** Pull anything a peer holds that we lack, validating it
   exactly like a push. One honest holder defeats another's omission, and this
   is what actually defeats withholding, not the flagging.
3. A record is expected when a majority of responding parties hold it.
4. A peer lacking an expected record is a candidate. Only after
   `RESOLVE_AUDIT_STRIKES` consecutive rounds, and only for records older than
   `RESOLVE_AUDIT_GRACE`, does it become a suspect.

Flagging is **disclosure only**, surfaced in `/healthz` and the dashboard.
Nothing is auto-banned, because withholding and being partitioned are
indistinguishable from inside. The operator acts through `RESOLVE_SYNC_FROM`.

**Honest limit:** with two resolvers there is no majority (2 voters, threshold
2), so the live pair repairs but cannot accuse. A third resolver changes that.

## Debugging propagation

1. Is the record signed? Attested records never replicate, by design.
2. Does the peer appear in `RESOLVE_PEERS` on both sides? Peering is mutual.
3. Do both ends stamp? Check `RESOLVE_PEERING_COST` on each.
4. Is the record older than the grace window before you judge anything missing?
5. Check the dashboard's per-peer card, or `/healthz` `peer_audit`, before
   restarting anything. Restart is a sync trigger, so it hides the symptom you
   were trying to read.

## Do not

- Do not offer, push, or serve an unsigned record.
- Do not accept a record whose target does not re-derive from its identity.
- Do not add an auto-ban. Disclosure is the ceiling.
- Do not remove the caps or the identified-link requirement to make a test
  easier; add a seam instead.
