# Legacy baseline and old Edge Lab evidence audit

Date: 2026-07-24  
Mode: offline-only, read-only source audit  
Machine-readable result: [`BASELINE_EVIDENCE_PACK.json`](BASELINE_EVIDENCE_PACK.json)

## Conclusion

The audited repository contains no fill-backed, settlement-aware,
out-of-sample profitability evidence. Its highest old evidence level is L1:
public snapshots, unsupported public history, public-flow proxies, and shadow
touches. The authenticated strategy fill count is **0**.

Synthetic, `sim_*`, public-flow, touch, and shadow records are counted
separately and are never promoted to fills. Financial metrics are `null` with
an explicit reason unless the necessary capital, fill, settlement, payout, and
grouping evidence exists.

## Evidence inventory

| Evidence set | Level | Markets/events | Execution records | Financial conclusion |
|---|---:|---|---|---|
| Legacy mock crossing backtest | L0 | 1 synthetic token / 0 events | 499 synthetic fills; 0 authenticated fills | Rejected |
| Legacy autoresearch search | L0 | 1 synthetic token / 0 events | Five retained parameter runs; 0 authenticated fills | Rejected |
| Legacy dry-run logs | L0 | 5 token IDs / event count unavailable | 330 `sim_*` records; 0 authenticated fills | Rejected |
| Public replay corpus | L1 | 7 conditions / event count unavailable | 153 canonical public-flow records; 12 canonical shadow touches; 0 authenticated fills | Insufficient |
| Independent observation JSONL | L1 | 1 condition / event count unavailable | 21 snapshots; 0 authenticated fills | Insufficient |
| Complete-set scans | L1 | 1,821 evaluated rows reported in the largest scan | Snapshot calculations only; 0 authenticated fills | Insufficient overall; retained rows rejected |
| Reward scans | L1 | 19 retained conditions / event count unavailable | 0 preflight-eligible; 0 authenticated fills; 0 payout records | Insufficient |
| Current quote snapshots | L1 | 2 conditions / event count unavailable | Scenario calculations only | Insufficient |

Parent Gamma event IDs are absent from the replay, observation, and current
snapshot artifacts. Event counts are therefore `null`, not inferred from
condition IDs.

## Corrected legacy mock accounting

The legacy command remains reproducible:

```bash
./.venv/bin/python scripts/run_backtest.py \
  --mock --snapshots 500 --capital 1000
```

The original engine reports final capital `952.2423350160120378`, return about
`-4.78%`, and maximum drawdown about `21.02%`. That equity curve is not
reconciled: after changing cash for buys and sells, it adds only unrealized
profit rather than signed inventory market value.

An independent Decimal ledger over the same 499 synthetic fills gives:

| Metric | Recomputed value |
|---|---:|
| Initial equity | `1000` |
| Final cash | `952.44195645408953970` |
| Final inventory | `30` |
| Last midpoint | `0.49961516150908212` |
| Final equity | `967.43041129936200330` |
| PnL | `-32.56958870063799670` |
| Return | `-0.0325695887006379967` |
| Maximum drawdown | `0.0472630935224204686` |
| Minimum / maximum inventory | `-330 / 70` |
| Modeled fees / rewards | `0 / 0` |

This correction does not rescue the strategy. The path is synthetic, the
example deliberately crosses the spread, and it reaches a naked short of 330
shares. The result remains L0 and rejected. Sources:
[`run_backtest.py`](../../scripts/run_backtest.py),
[`engine.py`](../../src/backtest/engine.py), and
[`fee_model.py`](../../src/backtest/fee_model.py).

## Autoresearch parameter search

The five retained rows in
[`autoresearch-results.tsv`](../../autoresearch-results.tsv) all have negative
Sharpe (`-1.8098` to `-1.5875`). An offline current-code diagnostic reproduces
all five recorded values to their four-decimal precision. This is not a
historical-data validation: the so-called real-data module seeds a
deterministic trigonometric path from one current snapshot, and the same
legacy equity-accounting flaw applies. The TSV does not persist capital, PnL,
fees, rewards, drawdown, confidence intervals, or concentration, so those
fields are `null`.

Sources:
[`autoresearch_verify.py`](../../scripts/autoresearch_verify.py),
[`real_data_backtest.py`](../../src/strategy/real_data_backtest.py), and
[`backtest_wrapper.py`](../../src/strategy/backtest_wrapper.py).

## Replay and scanner recomputation

The 18 replay/robustness JSON files contain 32,859 derived sample rows but only
10,403 unique `(condition_id, timestamp_ms)` pairs across seven conditions.
The canonical seven-file selection has 10,403 samples, 153 public-flow
records, and 12 shadow-touch events. None is a strategy fill.

Retained arithmetic was independently recomputed:

- Replay distributions: 100 blocks; 99 exact and 100 within `1e-12`; maximum
  absolute difference `5E-16`.
- Integrated static-reward scenarios: four artifacts; all within `1e-12`;
  maximum difference `3.33333333333E-16`.
- Complete-set retained top-50 rows: 800 market-size rows and 1,600 directional
  net-edge identities; all exact.
- Reward scans: 57 paired quote directions, 57 paired-margin checks, 57 static
  reward checks, 57 hedge-summary checks, and 152 complete-set edge checks;
  all within `1e-12`.

These checks prove only that the retained derived arithmetic is internally
consistent. The replay artifacts do not retain complete immutable raw L2
responses, and the scan files retain only the top 50 results. Consequently,
the full 1,821-market “zero positive” claim and the original depth/fee inputs
cannot be independently reconstructed from these artifacts alone.

Sources:
[`replay.py`](../../src/edge_lab/replay.py),
[`economics.py`](../../src/edge_lab/economics.py), and the
[`profit_redesign_2026-07-24`](../profit_redesign_2026-07-24/) artifacts.

## Reproduction

```bash
./.venv/bin/python scripts/build_baseline_evidence_pack.py
./.venv/bin/pytest -q tests/test_baseline_evidence_pack.py
```

The pack includes SHA-256 and byte size for each source artifact and method
source. It performs no network calls, reads no credentials, and has no order
path.
