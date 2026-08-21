# Agent guidance for rns-resolve

Notes for automated agents and for people who work like agents.
This tree is not user documentation. User and operator docs are `README.md`,
`docs/INTEGRATION.md`, `docs/ADMIN.md`, and `docs/LXMPEER-GAPS.md`.

## Start here

1. [overview.md](overview.md) for architecture, storage, ports, env vars, and invariants.
2. [conventions/reticulum-zen.md](conventions/reticulum-zen.md) before any mesh-facing design or code.
3. [conventions/core.md](conventions/core.md) for always-on standards.
4. [conventions/](conventions/) for the surface you are editing.
5. A [skills/](skills/) guide when the task matches that workflow.
6. [skills/no-ai-slop/SKILL.md](skills/no-ai-slop/SKILL.md) before writing or editing prose (docs, page copy, commit messages longer than a sentence).

Root [AGENTS.md](../AGENTS.md) and [CLAUDE.md](../CLAUDE.md) point here.
Editor rules mirroring this tree live under [.cursor/rules/](../.cursor/rules/):
always-on core standards and Reticulum Zen gates, plus globbed rules for the
service package, the dashboard, and tests.

## Layout

| Path | Purpose |
| --- | --- |
| [overview.md](overview.md) | Project brief, wiring, invariants |
| [conventions/reticulum-zen.md](conventions/reticulum-zen.md) | Zen of Reticulum hard gates |
| [conventions/core.md](conventions/core.md) | Always-on standards |
| [conventions/service.md](conventions/service.md) | Resolver service, store, records, peers |
| [conventions/admin.md](conventions/admin.md) | Operator dashboard and private HTTP surface |
| [conventions/tests.md](conventions/tests.md) | Test placement, oracles, verification |

## Skills

Each skill is `.agents/skills/<name>/SKILL.md` with YAML frontmatter whose
`name` matches the directory.

### Naming and trust

| Skill | Use when |
| --- | --- |
| [trust-invariants](skills/trust-invariants/SKILL.md) | Any change to what a resolver answers, what a client accepts, or how a record is proven. Read before touching resolve, register, or petnames. |
| [peer-replication](skills/peer-replication/SKILL.md) | Peer sync, stamps, the withholding audit, or anything that moves records between resolvers. |
| [nomadnet-pages](skills/nomadnet-pages/SKILL.md) | Micron exec pages under `pages/`, the node container, or the private loopback API they call. |
| [operator-dashboard](skills/operator-dashboard/SKILL.md) | The admin app: adding a panel, an action, or a resolver-side `/admin/*` endpoint. |

### Writing

Copied from [realrossmanngroup/no_ai_slop_writing_rules](https://github.com/realrossmanngroup/no_ai_slop_writing_rules), the same vendoring MeshChatX uses. Load it for every prose pass.

| Skill | Use when |
| --- | --- |
| [no-ai-slop](skills/no-ai-slop/SKILL.md) | Writing or editing prose. Banned patterns and WRONG/RIGHT fixes. Rules: [references/rules.md](skills/no-ai-slop/references/rules.md). |

## Related projects

This repo shares its agent-guidance shape with MeshChatX (`AGENTS.md` entry,
`.agents/` tree, skills with frontmatter). MeshChatX is a client that can
consume this resolver; keep the two consistent where the mesh semantics
overlap, and do not copy its patch-based contribution flow, which does not
apply here. See [CONTRIBUTING.md](../CONTRIBUTING.md).
