# Test conventions

Applies when editing `tests/**/*.py`.

- One file per module: `tests/test_<module>.py`, plain `unittest` classes run
  under pytest. Run with `py -3 -m pytest tests -q`.
- No sockets, no RNS, no Beacon database in the suite. Every external boundary
  has a seam: `Deps` collaborators, `PeerScheduler._open_link` and friends, and
  the injected `fetch` in `admin.ResolverClient`. Use them.
- An optional dependency gets `skipUnless`, and the pure-logic tests around it
  still run without it. `tests/test_admin.py` skips only the FastAPI app tests
  when the `admin` extra is absent.
- Fakes live in the test file that uses them. Keep them honest: a fake store
  that returns attested records from `get_many` would hide a real bug, so the
  fakes mirror the contract.
- Prefer a focused file while iterating. Run the whole suite before you claim
  a change works.

## Oracle style

A test must assert an accept or a reject, not merely that nothing raised.

Refuse `except Exception: pass`, tests that only check a reply has an `ok` key
without checking its value, and mocks that always succeed under a security
oracle. Prefer:

- an independent oracle: predict accept or reject from the input, then assert
  the code agrees,
- a derivation oracle: recompute the target from the identity hash and assert
  the record matches, which is how registration ownership is checked,
- closed reason sets: the error message is one of the known machine reasons,
- round-trip invariants for canonical bytes and record ids.

## What must have a test

- Every rejection path on the wire: bad version, unknown op, malformed payload,
  rate limit, unsigned record offered for replication, a target that does not
  derive from the caller.
- Every trust invariant in `.agents/skills/trust-invariants/SKILL.md`. If you
  change one, the test that proves it should fail first.
- Operator surfaces: that they read what they claim, and that they cannot be
  reached from an untrusted peer.
