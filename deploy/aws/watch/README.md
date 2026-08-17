# Paper track watcher deployment

This directory contains the staged AWS assets for the read-only progress watcher under `/opt/poly-mm-watch` and `/var/lib/poly-mm-watch`.

The watcher reads the EC2 `CPUCreditBalance` metric directly from CloudWatch.
Attach `cloudwatch-read-policy.json` to the instance role as an inline policy
before enabling the timer. The policy grants only
`cloudwatch:GetMetricStatistics`; CloudWatch does not support resource-level
scoping for this read action.

The staged lifecycle configuration treats v0.5 as retired, v0.6 as active, and
rawcap as maintenance-only. Retired and maintenance tracks remain visible in
state snapshots but do not emit runtime or telemetry paging.
