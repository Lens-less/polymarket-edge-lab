# Edge readiness v0.8 deployment assets

These units install the public official-RTDS recorder, the compact paired
four-token capture, and their read-only health checks. They do not install an
execution-probe service, venue adapter, credentials, or order permissions.

Do not enable them on the CPU-credit-starved t3.small alongside the old
recorders. First resize the instance or replace the existing RTDS ingest, then
verify disk latency and capacity through a full K15-open-to-close cohort.

Create `polybotv08`, install the checkout at `/opt/poly-mm-v08`, create
`/var/lib/poly-mm-v08/{data,monitor,reports}`, install the healthcheck shell
script as `/usr/local/bin/polymm-btc-twap-edge-readiness-v08-healthcheck`, and
install the units under `/etc/systemd/system`. Start continuous RTDS first,
then the paired capture and health timers. A cohort
is clean only when its own RTDS interval has no disconnect, invalid message, or
source gap over two seconds; a later healthy status never repairs an earlier
gap.

The health service deliberately exits nonzero for stale/tampered status,
non-capturing phase, missing observations, any connection error, or a safety
guard mismatch. Probe and strategy live remain NO-GO regardless of this unit's
state until every gate in the v0.8 preregistration passes.
