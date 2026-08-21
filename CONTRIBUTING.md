# Contributing to rns-resolve

Changes come in as branches and pull requests. The canonical remote is Forgejo
(`devops.quasarke.net/wdunn001/rns-resolve`) with a public GitHub mirror
(`github.com/wdunn001/rns-resolve`); either is fine to open against.

This project sits on Reticulum, so read
[Reticulum Zen](https://reticulum.network/manual/zen.html) and
`.agents/conventions/reticulum-zen.md` before proposing anything mesh-facing.
Naming is the feature most likely to smuggle DNS assumptions into a mesh, and
`.agents/skills/trust-invariants/SKILL.md` lists the boundaries that are not
negotiable.

## Before you open a pull request

1. `py -3 -m pytest tests -q` passes. The suite needs no network, no RNS and no
   database, so there is no reason to skip it.
2. New behaviour has a test, including its rejection paths. If your change
   touches a trust invariant, a named test should fail before your fix.
3. Keep the diff focused on one change. Style-only churn across unrelated files
   will be asked to come back smaller.
4. Follow `.agents/conventions/core.md`: minimal diffs, stdlib-first Python, no
   emojis, no emdashes or semicolons in comments and docs you write, no TODO
   noise.
5. Describe what changed and why in the pull request body. If the change alters
   the wire format, the record canonical form, or replication behaviour, say so
   explicitly, because those affect every deployed resolver.

## Deploy-coupled changes

Two rules exist because of how this is deployed, and both have bitten already:

- **Peering changes ship in pairs.** A stamped resolver rejects an unstamped
  peer, so upgrading one side of a peered pair stops replication and looks like
  a network fault. Say in the pull request when a change requires both ends.
- **The resolver identity is a trust anchor.** The identity file in the config
  volume is the destination hash clients have pinned. Nothing in a patch should
  regenerate, move, or default it away.

## Generative AI policy

You may use generative AI tools when contributing, on the condition that your
setup actually supplies the model with enough context to produce sound work and
your provider does not train on the code. Read
[Reticulum Zen](https://reticulum.network/manual/zen.html) and the
[Reticulum License](https://reticulum.network/manual/license.html). Vague
prompts and thin context produce wrong or generic changes, and that burden is
on the contributor, not the reviewers.

You must disclose AI usage in the pull request body or the commit message:
state which tools or services you used in a material way for that change (model
or product name, and whether it was local or cloud). If a change was written
without meaningful AI assistance, say so briefly. This lets reviewers judge
scope and provenance. It does not replace your own review and testing.

We prefer models that run locally or offline when that is practical for you.

Contributions must still be yours to justify and maintain. Do not submit
bulk-generated changes you have not read, understood, and tested. We are not
looking for unreviewed AI output or style-only churn from tools used without
engineering judgment.

Agents working in this repository start at [AGENTS.md](AGENTS.md), which points
at `.agents/`. `CLAUDE.md` and `.cursor/rules/` exist so tools find that same
guidance instead of inventing their own.

## Licensing of contributions

By submitting a change you agree it is licensed under the repository `LICENSE`
(MIT), and you confirm you have the right to submit it under those terms: it is
your own work or you have permission from the copyright holder, and you are not
knowingly introducing code under an incompatible license.
