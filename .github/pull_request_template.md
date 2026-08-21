## What this changes

<!-- What changed and why. If it alters the wire format, the record canonical
form, or replication behaviour, say so here: those affect every deployed
resolver. -->

## Trust impact

<!-- Delete if clearly none. Otherwise: could a hostile resolver use this to
make a client accept an address the user did not choose? Does anything
unsigned now cross a peer boundary? Does a new answer path bypass classify or
petnames? See .agents/skills/trust-invariants/SKILL.md -->

## Verification

- [ ] `py -3 -m pytest tests -q` passes
- [ ] New behaviour has a test, including its rejection paths
- [ ] Deploy-coupled changes are called out (peering changes ship in pairs)

## AI disclosure

<!-- Required by CONTRIBUTING.md. Name the tools or services used in a
material way, the model or product, and whether it ran locally or in the
cloud. If the change was written without meaningful AI assistance, say so in
one line. -->
