# rns-resolve agent entry

Start at [.agents/README.md](.agents/README.md).

Architecture and invariants: [.agents/overview.md](.agents/overview.md).
Mesh design: [.agents/conventions/reticulum-zen.md](.agents/conventions/reticulum-zen.md).
Prose: [.agents/skills/no-ai-slop/SKILL.md](.agents/skills/no-ai-slop/SKILL.md) before writing or editing docs, page copy, or commit messages longer than a sentence.

## The one rule that outranks everything

A resolver is consulted **only** for input that never matched the 32-hex
destination-hash pattern. A hash-shaped query that times out stays a plain
timeout. Never fall back to a resolver for a literal address a user supplied,
because that hands a resolver the power to override a user's own address and
reintroduces the central trust this project exists to avoid.

Full statement and the rest of the trust boundary:
[.agents/skills/trust-invariants/SKILL.md](.agents/skills/trust-invariants/SKILL.md).
