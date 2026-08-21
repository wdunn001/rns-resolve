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

Pull requests are the path for now. Patch files sent over the mesh, the way
some Reticulum projects accept contributions, are not wired up here yet.

## Deploy-coupled changes

Two rules exist because of how this is deployed, and both have bitten already:

- **Peering changes ship in pairs.** A stamped resolver rejects an unstamped
  peer, so upgrading one side of a peered pair stops replication and looks like
  a network fault. Say in the pull request when a change requires both ends.
- **The resolver identity is a trust anchor.** The identity file in the config
  volume is the destination hash clients have pinned. Nothing in a patch should
  regenerate, move, or default it away.

## Generative AI policy

You may use generative AI here. The maintainer does, and says so in `AUTHORS`.
What follows is what we expect of anyone who does, contributor or maintainer.

### The failure mode we actually care about

In most projects the risk from an underinformed model is wrong or generic code
that a reviewer spots quickly. In this one the risk is narrower and worse: a
model that has not read the trust boundary will write something that looks
helpful and quietly removes a guarantee. The two we see attempted most often:

- adding a resolver lookup as a fallback when a destination hash times out,
  which hands a resolver the power to override an address the user typed,
- accepting a target from the request body on `register` instead of deriving it
  from the caller's identity, which lets anyone register a name pointing
  anywhere.

Both read as reasonable improvements in isolation. Both destroy the design.
That is why context is a requirement here and not a suggestion.

### Give the model the context this repo already ships

Point your tool at the guidance in the repository before it writes anything:
`AGENTS.md` is the entry, `.agents/overview.md` carries the architecture and
the invariants, `.agents/conventions/` carries the standards per surface, and
`.agents/skills/` carries the task guides. `CLAUDE.md` and `.cursor/rules/`
exist so specific tools find that same tree instead of inventing their own
rules. A model working from the diff alone does not have enough to be safe in
this codebase.

### Route work by complexity, not by habit

Not every task deserves a frontier model, and using one for everything is both
wasteful and slower than it looks.

- **Mechanical, high-volume work** belongs on a local or open-weight model:
  summarizing a long log, skimming a document for one fact, classifying many
  short items, extracting a snippet by signature, computing embeddings. The
  answer does not depend on judgment, so a small model close to your machine
  is the right tool.
- **Judgment work** stays with the strongest model you have, loaded with the
  context above: anything touching the trust boundary, replication, the record
  format, or the shape of a public API. So does prose a reader will see.
- **Work that needs the conversation's own context** cannot be delegated at
  all, because the delegate does not have it.

The maintainer runs this as a small registry of delegates that records what
each local resource has actually succeeded at, per capability, with a success
rate. Routing is a lookup rather than a guess, and a delegate with no track
record on a capability does not get the job. You do not need that machinery to
contribute, but the principle holds when you decide what to hand to which
model: match the tool to the complexity, and prefer local or open-weight models
where they are good enough.

### Disclosure

Say in the pull request body or a commit trailer which tools or services you
used in a material way, naming the model or product and whether it ran locally
or in the cloud. If a change was written without meaningful AI assistance, one
line saying so is enough. This is so reviewers can judge scope and provenance.
It is not a substitute for reading, testing, and being able to defend the
change yourself.

Prefer providers that do not train on your code. Prefer local or open-weight
models when they are practical for the task.

### What we will send back

Bulk-generated changes the author has not read, understood, and tested. Style
churn from a tool run without engineering judgment. A change that removes or
weakens a trust gate, no matter how well argued the summary is.

## Reporting a vulnerability

See `SECURITY.md`. Do not open a public issue for an unfixed vulnerability in
name resolution, record verification, or replication.

## Licensing of contributions

By submitting a change you agree it is licensed under the repository `LICENSE`
(MIT), and you confirm you have the right to submit it under those terms: it is
your own work or you have permission from the copyright holder, and you are not
knowingly introducing code under an incompatible license.
