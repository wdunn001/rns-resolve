# Deploying a resolver

The service is one container riding an existing local RNS instance as an
ordinary client. It needs:

1. A reachable RNS hub (default config dials `127.0.0.1:4343`; edit
   `deploy/rns/config` for your topology). The config file is copied into
   the `rns_resolve_config` volume on first start if absent.
2. Optional announce-candidate backing: set `BEACON_DB_*` in a `.env` next
   to the compose file (read-only credentials to a Beacon crawler
   database). Without it the resolver serves registered records only.
3. Ports: `8225` (healthz, LAN) and `8226` (private page API, bound to
   127.0.0.1 only). Change via `RESOLVE_HEALTH_PORT` / `RESOLVE_PRIVATE_PORT`.

The service identity (and therefore the resolver's destination hash) lives
in the `rns_resolve_config` volume as `resolve_identity`. Preserve the
volume; the hash is what clients trust.

For the human-facing pages (`pages/index.mu`, `pages/register.mu`), run a
NomadNet node on the same host with the pages copied into its pages
directory with the executable bit set. The pages talk to the service on
`127.0.0.1:8226` and never need their own RNS access.

Safety notes for shared hosts: this stack must remain additive. Own
volumes, own ports, no host rnsd changes, no other stack's files.
