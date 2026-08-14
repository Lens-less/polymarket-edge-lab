# BTC regime-agnostic raw capture deployment

The raw collector records public CLOB data plus `crypto_prices`, 30-second
TWAP, and 60-second TWAP into `/var/lib/poly-mm-rawcap`. Settlement-regime
classification is metadata only: unknown regimes are quarantined but do not
block capture. The service cannot access EC2 credentials and never writes into
the v0.5 or v0.6 evidence trees.

Stage a content-addressed release under `/opt/poly-mm-rawcap`, then run:

```bash
sudo DEPLOY_REF=<40-character-release-commit-sha> \
  bash /opt/poly-mm-rawcap/deploy/aws/rawcap/bootstrap_amazon_linux.sh
```

The bootstrap validates only. Start manually after v0.4 has been archived:

```bash
sudo systemctl enable --now polymm-btc-rawcap.service
sudo systemctl enable --now polymm-btc-rawcap-health.timer
sudo systemctl enable --now polymm-btc-rawcap-maintenance.timer
```

The hourly maintenance unit compresses only capture attempts that already have
`capture-summary.json` and have been inactive for at least 30 minutes. It never
touches an in-progress attempt. Completed attempts older than 30 days are
deleted from the explicitly bounded rawcap runs tree. No S3 archival is enabled
in this release; the first 24-hour write rate remains a required capacity check.
