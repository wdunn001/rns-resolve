# CLAUDE.md

Guidance for Claude Code and other agents working in this repository.

The durable guidance lives in [AGENTS.md](AGENTS.md) and under
[.agents/](.agents/README.md), shared by every assistant and editor rather
than duplicated per tool. Read those. This file exists so Claude Code finds
the entry point without being told.

Read in this order:

1. [.agents/overview.md](.agents/overview.md) for what the service is, how it
   is wired, and the invariants that must survive your change.
2. [.agents/conventions/reticulum-zen.md](.agents/conventions/reticulum-zen.md)
   before any mesh-facing design or code.
3. [.agents/conventions/core.md](.agents/conventions/core.md) for the
   always-on standards (style, scope, what not to invent).
4. The convention for the surface you are editing:
   [service.md](.agents/conventions/service.md),
   [admin.md](.agents/conventions/admin.md),
   [tests.md](.agents/conventions/tests.md).
5. A [skill](.agents/README.md#skills) when the task matches one.
6. [.agents/skills/no-ai-slop/SKILL.md](.agents/skills/no-ai-slop/SKILL.md)
   before writing prose of any kind.

Contribution rules, including the AI disclosure policy, are in
[CONTRIBUTING.md](CONTRIBUTING.md). They apply to agent-authored changes
exactly as they apply to human ones.
