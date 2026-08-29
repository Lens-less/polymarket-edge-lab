# BTC 5m/15m relative-value paper v0.5 Linux deployment

> [!WARNING]
> Historical paper asset. The strategy track is closed; do not deploy or enable it.

These assets stage an isolated public-only paper run under
`/opt/poly-mm-v05` and `/var/lib/poly-mm-v05`.
They are frozen to the repository path
`research/btc_5m_15m_relative_value_paper_v05_linux_2026-08-13/`.
The deploy commit must already contain that v0.5 research directory with
`SERVICE_CONFIG.json`, `PREREGISTRATION.json`, and the referenced strategy spec.

Operational boundaries:

- v0.5 code and data stay isolated from `/opt/poly-mm` and `/var/lib/poly-mm`
- the service account is `polybotv05:polybotv05`
- the main unit validates first and writes only under `/var/lib/poly-mm-v05`
- the monitor writes `health-latest.json` atomically and keeps unique history files under `/var/lib/poly-mm-v05/monitor/history/`
- the bootstrap never stops, moves, or deletes any v0.4 unit, tree, or report root
- v0.5 is staged only; it is not enabled or started automatically

Files:

- `bootstrap_amazon_linux.sh` - Amazon Linux 2023 host bootstrap
- `polymm-btc-twap-paper-v05.service` - persistent main service
- `polymm-btc-twap-paper-v05-health.service` - read-only health snapshot
- `polymm-btc-twap-paper-v05-health.timer` - five-minute monitor timer
- `polymm-btc-twap-paper-v05-healthcheck.sh` - atomic latest snapshot writer with unique history filenames

Preferred content-addressed archive stage on the new EC2 host:

1. Build an archive from the immutable release commit and include two root
   markers: `.deployment-revision` containing the release commit and
   `.implementation-revision` containing the preregistered `repository_head`.
2. Verify the archive SHA-256 locally, transfer it through an encrypted
   temporary object, verify the same SHA-256 on the host, and extract it to a
   staging directory.
3. After both revision markers match, atomically rename the staging directory
   to `/opt/poly-mm-v05`, then run:

```bash
sudo DEPLOY_REF=<40-character-release-commit-sha> \
  bash /opt/poly-mm-v05/deploy/aws/paper_v05/bootstrap_amazon_linux.sh
```

The bootstrap also supports a detached Git checkout as a recovery path. In
that path it requires the preregistered implementation revision to be an
ancestor of the exact `DEPLOY_REF` and writes the implementation marker. The
normal AWS deployment uses the verified archive path and never copies Git
credentials to the instance.

The bootstrap installs `git`, `python3.11`, `chrony`, the v0.5 virtualenv,
the v0.5 unit files, and validates the v0.5 config. It does not enable or
start the v0.5 service or timer.

Start only after the stage step completes cleanly:

```bash
sudo systemctl enable polymm-btc-twap-paper-v05.service
sudo systemctl start polymm-btc-twap-paper-v05.service
sudo systemctl enable polymm-btc-twap-paper-v05-health.timer
sudo systemctl start polymm-btc-twap-paper-v05-health.timer
```

Verify:

```bash
sudo systemctl status polymm-btc-twap-paper-v05.service --no-pager
sudo systemctl status polymm-btc-twap-paper-v05-health.timer --no-pager
sudo /opt/poly-mm-v05/.venv/bin/python /opt/poly-mm-v05/scripts/run_btc_twap_relative_value_service.py --config /opt/poly-mm-v05/research/btc_5m_15m_relative_value_paper_v05_linux_2026-08-13/SERVICE_CONFIG.json --validate-only
sudo cat /var/lib/poly-mm-v05/monitor/health-latest.json
sudo chronyc -n tracking
```

Cutover while preserving v0.4 evidence:

```bash
sha256sum /opt/poly-mm/research/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13/DEPLOYMENT_REPORT.md
```

The expected v0.4 report hash is:

```text
321f1e797038f4445f1bcc96529119c676f472d3bdda27b3a2cb9b75e7874b98
```

Preservation rules during cutover:

- do not move, rewrite, or delete `/opt/poly-mm`
- do not move, rewrite, or delete `/var/lib/poly-mm`
- do not edit `/opt/poly-mm/research/btc_5m_15m_relative_value_paper_v04_linux_2026-08-13/`
- keep v0.4 artifacts in place even after v0.5 starts
- if v0.4 remains on a separate host, leave that host unchanged and switch operators to the new v0.5 host only after the verify step passes
