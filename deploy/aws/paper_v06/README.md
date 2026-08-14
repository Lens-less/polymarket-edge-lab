# BTC 5m/15m relative-value paper v0.6 Linux deployment

These assets stage the prospective-only 60s/60s transfer track under
`/opt/poly-mm-v06` and `/var/lib/poly-mm-v06`. The bootstrap validates the
strategy hash, settlement registry hash, implementation revision, paper-only
guards, and clock source. It never starts the service automatically and never
writes to the v0.5 tree.

Use a content-addressed source archive with `.deployment-revision` and
`.implementation-revision` markers, then run:

```bash
sudo DEPLOY_REF=<40-character-release-commit-sha> \
  bash /opt/poly-mm-v06/deploy/aws/paper_v06/bootstrap_amazon_linux.sh
```

After validation and the frozen prospective cutoff:

```bash
sudo systemctl enable --now polymm-btc-twap-paper-v06.service
sudo systemctl enable --now polymm-btc-twap-paper-v06-health.timer
```

Verify with `--validate-only`, both unit statuses, and
`/var/lib/poly-mm-v06/monitor/health-latest.json`. Do not copy v0.6 data into
v0.5. Live orders, credentials, authenticated endpoints, and proxies remain
disabled.
