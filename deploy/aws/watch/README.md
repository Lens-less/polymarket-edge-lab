# Paper track watcher deployment

> [!WARNING]
> Historical watcher asset for retired paper tracks. Do not enable it as a current service.

This directory contains the staged AWS assets for the read-only progress watcher under `/opt/poly-mm-watch` and `/var/lib/poly-mm-watch`.

The watcher reads the EC2 `CPUCreditBalance` metric directly from CloudWatch.
Attach `cloudwatch-read-policy.json` to the instance role as an inline policy
before enabling the timer. The policy grants only
`cloudwatch:GetMetricStatistics`; CloudWatch does not support resource-level
scoping for this read action.

Paging is mandatory for a new installation. Before bootstrap, create the SNS
topic and at least one confirmed subscription, grant the instance role
`sns:Publish` on that exact topic, and grant the bootstrap identity
`sns:ListSubscriptionsByTopic`. Then run bootstrap with
`POLYMM_SNS_TOPIC_ARN=<exact-topic-arn>`. Bootstrap aborts when the variable is
missing or every subscription is still `PendingConfirmation`; it writes only
the topic ARN to `/etc/polymm-watch.env`.

The staged lifecycle configuration treats v0.5 as retired, v0.6 as active, and
rawcap as maintenance-only. Retired and maintenance tracks remain visible in
state snapshots but do not emit runtime or telemetry paging.
