# BTC 5m/15m relative-value paper v0.4 Linux deployment

> [!WARNING]
> Historical paper asset. The strategy track is closed; do not deploy or enable it.

These assets deploy the public-only paper run frozen in
`research/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13/PREREGISTRATION.json`.

Operational boundaries:

- strategy execution remains paper-only and never receives credentials
- v0.4 evidence is isolated under
  `/var/lib/poly-mm/data/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13`
- generated reports are isolated under
  `/var/lib/poly-mm/research/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13`
- the frozen preregistration stays read-only under `/opt/poly-mm/research/...`
- chrony is locked to Amazon Time Sync Service `169.254.169.123`
- a read-only five-minute monitor atomically writes
  `/var/lib/poly-mm/monitor/health-latest.json` and retains timestamped copies
  under `/var/lib/poly-mm/monitor/history/`
- an unhealthy snapshot remains durable JSON and does not stop future timer runs;
  consumers must check its top-level `healthy` field

Files:

- `bootstrap_amazon_linux.sh` — Amazon Linux 2023 host bootstrap
- `polymm-btc-twap-paper-v04.service` — persistent main service
- `polymm-btc-twap-paper-v04-health.service` — read-only health/PnL snapshot
- `polymm-btc-twap-paper-v04-health.timer` — five-minute monitor timer
- `polymm-btc-twap-paper-v04-healthcheck.sh` — atomic JSON snapshot writer

The bootstrap requires an immutable Git commit:

```bash
DEPLOY_REF=<40-character-commit-sha> bash deploy/aws/paper_v04/bootstrap_amazon_linux.sh
```

For a private repository, a source archive generated from that immutable commit
may be extracted into `/opt/poly-mm` with the same 40-character SHA stored in
`/opt/poly-mm/.deployment-revision`; the bootstrap verifies that marker instead
of persisting Git credentials on the instance.

Acceptance checks:

1. `systemctl status polymm-btc-twap-paper-v04.service --no-pager`
2. `systemctl status polymm-btc-twap-paper-v04-health.timer --no-pager`
3. `chronyc -n tracking`
4. `/opt/poly-mm/.venv/bin/python /opt/poly-mm/scripts/run_btc_twap_relative_value_service.py --config /opt/poly-mm/research/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13/SERVICE_CONFIG.json --validate-only`
5. `cat /var/lib/poly-mm/monitor/health-latest.json`

These assets do not open inbound ports, configure a proxy, load trading
credentials, sign orders, or call authenticated trading endpoints.
