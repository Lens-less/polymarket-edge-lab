# BTC 5m/15m relative-value paper v0.7 shadow deployment

> [!WARNING]
> Historical shadow asset. The strategy track is closed; do not deploy or enable it.

These assets stage a future-only v0.7 counterfactual shadow track under
`/opt/poly-mm-v07` and `/var/lib/poly-mm-v07`.

This deployment is deliberately limited:

- it reads only finalized v0.6 public-paper runs under `/var/lib/poly-mm-v06`
- it never submits orders, reads credentials, or calls authenticated endpoints
- it writes only v0.7 shadow status, health, and performance snapshots under
  `/var/lib/poly-mm-v07`
- it does not prove true edge or real trading profitability
- it cannot produce qualified PnL because no prelabel lock journal is supplied

Explicit V6 `capture_error` attempts are rejected from projection and cohort
admission but remain counted in the V7 status and health warnings. This avoids
both permanent monitor deadlock and silent survivorship filtering. Schema,
integrity, safety, path, and provenance violations remain hard failures.

The bootstrap grants the independent `polybotv07` account read/traverse-only
ACL access to the exact V6 capture and service-status paths, and asserts it
cannot write either source. The runtime unit keeps `/var/lib/poly-mm-v06`
mounted read-only as a second boundary.

V6 checkpoint files are atomically replaced with mode `0600`, which resets the
effective mask of inherited named ACLs. Before every 30-minute refresh, a
hardened root oneshot repairs read-only ACLs only on single-link `*.json`
checkpoint files in attempts that already have `capture-summary.json`. It does
not follow links, alter capture content, or grant V7 write access. The helper
opens every directory and file with no-follow semantics, applies the ACL through
a stable inherited file descriptor, and rechecks device, inode, type, and link
count before retaining the grant.

Bootstrap validates the frozen release, the preregistration binding, and the
required same-filesystem V6-to-V7 XFS reflink behavior, but it does not
auto-start the timers:

```bash
sudo DEPLOY_REF=<40-character-release-commit-sha> \
  bash /opt/poly-mm-v07/deploy/aws/paper_v07/bootstrap_amazon_linux.sh
```

Manual validation:

```bash
sudo /opt/poly-mm-v07/.venv/bin/python \
  /opt/poly-mm-v07/scripts/run_btc_twap_relative_value_v07_shadow.py \
  --config /opt/poly-mm-v07/research/btc_5m_15m_relative_value_paper_v07_shadow_2026-08-16/SERVICE_CONFIG.json \
  --validate-only
sudo systemctl status polymm-btc-twap-paper-v07-performance.timer --no-pager
sudo systemctl status polymm-btc-twap-paper-v07-health.timer --no-pager
sudo cat /var/lib/poly-mm-v07/monitor/performance-latest.json
sudo cat /var/lib/poly-mm-v07/monitor/health-latest.json
```

Enable only after validation:

```bash
sudo systemctl enable polymm-btc-twap-paper-v07-performance.timer
sudo systemctl start polymm-btc-twap-paper-v07-performance.timer
sudo systemctl enable polymm-btc-twap-paper-v07-health.timer
sudo systemctl start polymm-btc-twap-paper-v07-health.timer
```

Do not rewrite, move, or delete `/opt/poly-mm-v06`, `/var/lib/poly-mm-v06`,
or `/var/lib/poly-mm-rawcap`.
