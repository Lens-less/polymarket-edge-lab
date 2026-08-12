#!/usr/bin/env python3
"""Compile completed session reports into strategy hypotheses and validation plan."""

from __future__ import annotations

import csv
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List


ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def short_addr(addr: str) -> str:
    if not addr:
        return ""
    return addr[:10] + "..." + addr[-6:]


def md_table(rows: Iterable[Dict[str, Any]], fields: List[str]) -> str:
    rows = list(rows)
    if not rows:
        return "_No rows._"
    header = "| " + " | ".join(fields) + " |"
    sep = "| " + " | ".join(["---"] * len(fields)) + " |"
    body = []
    for row in rows:
        vals = []
        for field in fields:
            value = str(row.get(field, ""))
            if len(value) > 120:
                value = value[:117] + "..."
            vals.append(value.replace("|", "\\|").replace("\n", " "))
        body.append("| " + " | ".join(vals) + " |")
    return "\n".join([header, sep] + body)


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})


def account_index(rows: Iterable[Dict[str, str]], address_key: str = "address") -> Dict[str, Dict[str, str]]:
    out: Dict[str, Dict[str, str]] = {}
    for row in rows:
        address = (row.get(address_key) or row.get("account") or "").lower()
        if address:
            out[address] = row
    return out


def collect_inputs() -> Dict[str, List[Dict[str, str]]]:
    return {
        "s1_combined": read_csv(ROOT / "session_1_classification" / "combined_account_classification.csv"),
        "s2_large": read_csv(ROOT / "session_2_pm_large_event" / "pm_large_event_accounts.csv"),
        "s3_hf": read_csv(ROOT / "session_3_pm_hf_mm_arb" / "pm_hf_mm_arb_accounts.csv"),
        "s4_mid": read_csv(ROOT / "session_4_pm_midfreq" / "pm_midfreq_accounts.csv"),
        "s5_dir": read_csv(ROOT / "session_5_hl_directional" / "hl_directional_accounts.csv"),
        "s6_turnover": read_csv(ROOT / "session_6_hl_high_turnover" / "hl_high_turnover_accounts.csv"),
    }


def build_cross_validation(data: Dict[str, List[Dict[str, str]]]) -> List[Dict[str, Any]]:
    s1 = account_index(data["s1_combined"], "account")
    checks = []
    session_specs = [
        ("Session 2", "A_pm_large_event_bet", data["s2_large"], "address", "candidate_pool_decision"),
        ("Session 3", "B_pm_hf_mm_arb", data["s3_hf"], "address", "candidate_pool_decision"),
        ("Session 4", "C_pm_midfreq_selective", data["s4_mid"], "address", "candidate_pool_decision"),
        ("Session 5", "D_hl_concentrated_directional", data["s5_dir"], "address", "candidate_pool_decision"),
        ("Session 6", "E_hl_high_turnover_multi_asset", data["s6_turnover"], "address", "candidate_pool_decision"),
    ]
    for session, expected, rows, address_key, decision_key in session_specs:
        for row in rows:
            address = (row.get(address_key) or "").lower()
            if not address:
                continue
            s1_row = s1.get(address, {})
            s1_label = s1_row.get("classification", "missing_from_s1")
            decision = row.get(decision_key, "")
            if s1_label == expected:
                status = "match"
            elif s1_label.startswith("F_detail_pending"):
                status = "deep_dive_overrides_detail_pending"
            elif "watchlist" in decision.lower() or "not_primary" in decision.lower():
                status = "downgrade_or_watchlist"
            else:
                status = "label_divergence_review"
            checks.append(
                {
                    "session": session,
                    "address": address,
                    "name": row.get("username") or row.get("name") or "",
                    "expected_type": expected,
                    "session1_label": s1_label,
                    "status": status,
                    "decision": decision,
                }
            )
    return checks


def representative_accounts(data: Dict[str, List[Dict[str, str]]]) -> Dict[str, str]:
    reps = {
        "A_large_event": [],
        "B_hf_mm_arb": [],
        "C_midfreq": [],
        "D_hl_directional": [],
        "E_hl_turnover": [],
    }

    for row in data["s2_large"][:4]:
        reps["A_large_event"].append((row.get("username", ""), row.get("address", "")))

    for row in data["s3_hf"]:
        if row.get("candidate_pool_decision", "").startswith("retain_primary"):
            reps["B_hf_mm_arb"].append((row.get("username", ""), row.get("address", "")))

    for row in data["s4_mid"]:
        if "primary" in row.get("candidate_pool_decision", "") or "secondary" in row.get("candidate_pool_decision", ""):
            reps["C_midfreq"].append((row.get("username", ""), row.get("address", "")))

    for row in data["s5_dir"][:3]:
        if "include" in row.get("candidate_pool_decision", ""):
            reps["D_hl_directional"].append((row.get("name", ""), row.get("address", "")))

    for row in data["s6_turnover"]:
        if "include_primary" in row.get("candidate_pool_decision", ""):
            reps["E_hl_turnover"].append((row.get("name", ""), row.get("address", "")))

    return {
        key: "; ".join(f"{name or 'anon'} {short_addr(addr)}" for name, addr in value) or "none"
        for key, value in reps.items()
    }


def build_strategy_candidates(reps: Dict[str, str]) -> List[Dict[str, Any]]:
    return [
        {
            "strategy_id": "PM_B_NEG_RISK_SPREAD_INVENTORY",
            "type": "B_pm_hf_mm_arb",
            "pool_status": "primary_offline_validation",
            "representative_accounts": reps["B_hf_mm_arb"],
            "evidence_summary": "High trade counts, high monthly volume, broad current-position coverage, lower PnL/volume, negative-risk and multi-position event evidence in Session 3.",
            "reproducibility_score": 2,
            "risk_score": 5,
            "applicable_market": "Polymarket liquid sports/event families with multiple related outcomes or negative-risk structures.",
            "entry_conditions": "Only when orderbook-derived bid/ask or complement baskets imply positive expected spread/arb after fees, slippage, settlement latency, and inventory haircuts; require minimum depth and multiple markets in the same event family.",
            "exit_conditions": "Close inventory when theoretical edge disappears, event correlation breaks, depth vanishes, or unresolved exposure exceeds cap; redeem/merge after settlement where applicable.",
            "position_rule": "Market-neutral or tightly hedged inventory; cap per event family, per outcome, and per unresolved settlement day; no averaging into unhedged event direction.",
            "max_loss_rule": "Hard stop by event-family mark-to-market drawdown, stale inventory age, and worst-case settlement loss; disable strategy if realized adverse selection exceeds validation budget.",
            "data_requirements": "Historical CLOB orderbooks, trades, maker/taker flags if available, market metadata, negative-risk grouping, resolution data, fees/rebates, and latency snapshots.",
            "backtestability": "medium; requires reconstructed orderbook and fill model, not just position endpoints.",
            "failure_conditions": "Edge disappears after doubled fees/slippage, best 5% trades removed, or profits concentrate in one market family/month; maker queue/fill assumptions cannot be validated.",
            "not_trade_recommended": True,
        },
        {
            "strategy_id": "PM_C_SPORTS_SELECTIVE_PRICE_BAND",
            "type": "C_pm_midfreq_selective",
            "pool_status": "secondary_offline_validation",
            "representative_accounts": reps["C_midfreq"],
            "evidence_summary": "Session 4 found distributed profitable markets, 131-488 trades in selected accounts, largest closed-win concentration 3.2%-13.3%, and descriptive winner avg-price bands around 0.42-0.56 for stronger cases.",
            "reproducibility_score": 3,
            "risk_score": 4,
            "applicable_market": "Polymarket sports moneyline/totals/spread markets with repeated fixtures and enough historical resolution data.",
            "entry_conditions": "Model-implied probability exceeds market price by a validated margin inside a liquidity/price band; avoid single-news markets and low-depth tails.",
            "exit_conditions": "Exit when model edge closes, injury/lineup/news regime invalidates signal, or price moves against position beyond pre-set loss; otherwise settle only when validation shows settlement holding is robust.",
            "position_rule": "Small fixed-fraction sizing by edge and liquidity; cap by league/event/day; no more than a small fraction of bankroll in unresolved correlated fixtures.",
            "max_loss_rule": "Per-market max loss, per-day drawdown stop, and per-category drawdown stop; remove strategy if open-risk inventory accumulates like high-drawdown watchlist accounts.",
            "data_requirements": "Resolved market history, trades, prices at entry/exit, event category, external sports odds/injury/lineup feeds, liquidity, and settlement timestamps.",
            "backtestability": "medium-high if historical prices and external sports priors are available.",
            "failure_conditions": "No edge after out-of-sample validation, category slices fail, or profitability is erased after removing best 5% trades or best single month/market.",
            "not_trade_recommended": True,
        },
        {
            "strategy_id": "HL_E_MULTI_ASSET_TURNOVER_ROTATION",
            "type": "E_hl_high_turnover_multi_asset",
            "pool_status": "secondary_offline_validation",
            "representative_accounts": reps["E_hl_turnover"],
            "evidence_summary": "Session 6 identified high fill count, broad coin breadth, high notional turnover, and lower top-coin concentration in primary accounts; BobbyBigSize was downgraded to watchlist.",
            "reproducibility_score": 3,
            "risk_score": 5,
            "applicable_market": "Hyperliquid perpetuals across liquid crypto and listed synthetic assets.",
            "entry_conditions": "Asset enters validated rotation basket by momentum/mean-reversion/funding-volatility signal; require volume, spread, and funding filters; avoid assets where fees consume expected edge.",
            "exit_conditions": "Exit on signal decay, volatility shock, funding reversal, liquidity drop, or portfolio risk cap breach; rebalance on fixed schedule only if turnover survives fee stress.",
            "position_rule": "Diversified gross and net exposure caps; per-asset notional cap; volatility-scaled sizing; exposure concentration cap based on top coin share.",
            "max_loss_rule": "Portfolio drawdown stop, per-asset stop, liquidation-distance floor, and fee-burn stop; no live use if fill model cannot reproduce fee/slippage.",
            "data_requirements": "HL trades/fills, L2 or best bid/ask, funding, mark prices, liquidation/margin state proxy, open interest, fees, and asset metadata.",
            "backtestability": "medium; feasible with public market data but exact account-style execution needs fills/orderbook assumptions.",
            "failure_conditions": "Net PnL becomes negative under doubled fees/slippage, turnover drops after parameter perturbation, or profits come from one asset/month.",
            "not_trade_recommended": True,
        },
        {
            "strategy_id": "HL_D_CONCENTRATED_TREND_RISK",
            "type": "D_hl_concentrated_directional",
            "pool_status": "research_only_high_risk",
            "representative_accounts": reps["D_hl_directional"],
            "evidence_summary": "Session 5 found single-name ETH/BTC/HYPE shorts with 100% or near-100% current notional concentration, high exposure/account-value ratios, capped 2,000-fill samples, and large day-PnL swings.",
            "reproducibility_score": 2,
            "risk_score": 5,
            "applicable_market": "Hyperliquid major and high-liquidity alt perpetuals.",
            "entry_conditions": "Only if independent trend/funding/volatility regime model identifies directional edge and liquidation distance remains wide; never copy wallet positions directly.",
            "exit_conditions": "Exit on trend invalidation, funding reversal, volatility expansion against position, liquidation gap compression, or max drawdown hit.",
            "position_rule": "Volatility-scaled directional exposure with strict leverage cap; no single asset above concentration cap; no account-value-sized copy trades.",
            "max_loss_rule": "Hard liquidation-distance buffer, daily loss limit, and per-position stop; disable after any stress test that shows tail loss exceeds budget.",
            "data_requirements": "OHLCV, funding, orderbook liquidity, volatility, open interest, liquidation estimates, and full fills for representative wallets if available.",
            "backtestability": "medium for signal proxy; low for copying observed wallet behavior because intent, hedges, and full history are not public.",
            "failure_conditions": "Edge vanishes in validation, best month/asset removal kills PnL, drawdown/liquidation risk exceeds budget, or funding costs dominate.",
            "not_trade_recommended": True,
        },
        {
            "strategy_id": "PM_A_EVENT_CONCENTRATION_FILTER",
            "type": "A_pm_large_event_bet",
            "pool_status": "excluded_as_primary_strategy_use_as_risk_filter",
            "representative_accounts": reps["A_large_event"],
            "evidence_summary": "Session 2 found sports-event PnL dominated by few settled markets, largest-win/month-PnL ratios up to multiple times monthly PnL, and reproducibility score 1 across selected accounts.",
            "reproducibility_score": 1,
            "risk_score": 5,
            "applicable_market": "Polymarket event outcomes, mainly sports samples in this run.",
            "entry_conditions": "No direct entry rule accepted from leaderboard evidence; use only as a filter to identify and exclude one-off large-win samples from strategy training.",
            "exit_conditions": "Not applicable as a primary strategy; for research, require settlement and post-event attribution before sample inclusion.",
            "position_rule": "Do not size from copied wallet outcomes; any future event-specific hypothesis must be separately modeled and capped.",
            "max_loss_rule": "Exclude from candidate pool unless edge is independently explained and survives removal of the best market/trade.",
            "data_requirements": "Resolved markets, price path, external pre-event odds/news, entry timestamps, and full trade history.",
            "backtestability": "low as observed wallet behavior; useful as anti-overfit filter.",
            "failure_conditions": "If PnL relies on one match/event or unverifiable information advantage, reject from replicable strategy pool.",
            "not_trade_recommended": True,
        },
    ]


def build_validation_plan() -> str:
    return """# Offline Validation Plan

All validation is offline and public-data-only. No real account connection, key reading, or live order submission is allowed.

## Data Splits

- Use rolling train / validation / test, not one static split.
- Polymarket: train 6 months, validation 1 month, test 1 month where historical data coverage permits; roll forward by 1 month.
- Hyperliquid: train 90 days, validation 30 days, test 30 days; roll forward by 14-30 days depending on data density.
- Keep final untouched test windows by market category and by asset.

## Required Cost And Execution Stress

- Base fees and realistic slippage from historical orderbook depth.
- 2x fee test.
- 2x slippage test.
- 2x fee plus 2x slippage combined test.
- Queue-position / partial-fill haircut for Polymarket maker or spread hypotheses.
- Funding-cost and liquidation-distance stress for Hyperliquid directional and turnover hypotheses.

## Robustness Tests

- Parameter perturbation: entry threshold, holding time, stop, position cap, liquidity filter, and rebalance frequency.
- Remove best 5% trades by realized PnL.
- Remove best single market.
- Remove best single month.
- Market-category slices: soccer, basketball, baseball, tennis, politics/news/other where available.
- Hyperliquid asset slices: BTC/ETH majors, HYPE, top alts, long-tail alts, synthetic equities/commodities where available.
- Regime slices: high/low volatility, high/low funding, trend/mean-reversion regimes, high/low liquidity.
- Start-date and end-date shifts to detect luck from a narrow window.

## Minimum Pass Gates

- Positive validation and test net PnL after base costs.
- Positive or acceptable risk-adjusted return after 2x fee/slippage.
- No single trade, market, asset, or month accounts for most of net PnL.
- Max drawdown and tail loss stay inside pre-declared limits.
- Trade count and turnover are high enough for statistical interpretation.
- Strategy remains valid across at least two independent time windows or categories.

## Failure Rule

If any required robustness gate fails, the conclusion is: **不建议交易**.
"""


def build_strategy_report(
    data: Dict[str, List[Dict[str, str]]],
    candidates: List[Dict[str, Any]],
    checks: List[Dict[str, Any]],
) -> str:
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    s1_counts = Counter(r.get("classification", "") for r in data["s1_combined"])
    status_counts = Counter(c["status"] for c in checks)

    excluded_notes = [
        {
            "bucket": "A large-event",
            "decision": "Do not use as primary strategy sample.",
            "reason": "Session 2 reproducibility score is 1; PnL frequently dominated by few resolved sports events and may reflect information advantage or luck.",
        },
        {
            "bucket": "F/detail_pending",
            "decision": "Exclude until detail is fetched and reconciled.",
            "reason": "Session 1 intentionally marked non-enriched top50/top100 rows as detail_pending to avoid overclaiming from leaderboard-only data.",
        },
        {
            "bucket": "HL concentrated directional",
            "decision": "Research-only until tail risk is modeled.",
            "reason": "Session 5 shows high exposure/account-value ratios, capped fill history, liquidation/funding risk, and large day-PnL swings.",
        },
        {
            "bucket": "BobbyBigSize",
            "decision": "Downgraded from primary E despite Session 1 E label.",
            "reason": "Session 6 found recent fills more dominated by BTC/ETH/HYPE than cleaner breadth candidates.",
        },
    ]

    report = [
        "# Session 7: Strategy Hypothesis Pool And Offline Validation Plan",
        "",
        f"Generated: {generated_at}",
        "",
        "## Final Status",
        "",
        "- Sessions 1-6 completed and wrote structured outputs.",
        "- This report compiles hypotheses only; it is not a trading recommendation.",
        "- Current portfolio-level conclusion: **不建议交易** until offline validation passes all robustness gates.",
        "",
        "## Inputs",
        "",
        md_table(
            [
                {"input": "Session 1 classification", "rows": len(data["s1_combined"])},
                {"input": "Session 2 PM large event", "rows": len(data["s2_large"])},
                {"input": "Session 3 PM HF/MM/arb", "rows": len(data["s3_hf"])},
                {"input": "Session 4 PM mid-frequency", "rows": len(data["s4_mid"])},
                {"input": "Session 5 HL directional", "rows": len(data["s5_dir"])},
                {"input": "Session 6 HL high turnover", "rows": len(data["s6_turnover"])},
            ],
            ["input", "rows"],
        ),
        "",
        "## Session 1 Classification Snapshot",
        "",
        md_table(
            [{"classification": k, "count": v} for k, v in sorted(s1_counts.items())],
            ["classification", "count"],
        ),
        "",
        "## Cross-Validation Summary",
        "",
        md_table(
            [{"status": k, "count": v} for k, v in sorted(status_counts.items())],
            ["status", "count"],
        ),
        "",
        "Key interpretation:",
        "",
        "- Matches between Session 1 and deep dives are strongest for PM B, HL D top cases, and HL E broad-turnover cases.",
        "- Session 4 adds mid-frequency candidates that Session 1 under-labeled or left unclear because Session 1 used first-pass thresholds.",
        "- Session 5/6 deep dives override some `detail_pending` rows because they fetched additional public details independently.",
        "- Watchlist/downgrade rows are not primary validation candidates.",
        "",
        "## Exclusions And Downgrades",
        "",
        md_table(excluded_notes, ["bucket", "decision", "reason"]),
        "",
        "## Strategy Candidate Pool",
        "",
        md_table(
            candidates,
            [
                "strategy_id",
                "type",
                "pool_status",
                "reproducibility_score",
                "risk_score",
                "representative_accounts",
                "backtestability",
            ],
        ),
        "",
        "## Candidate Details",
        "",
    ]

    for candidate in candidates:
        report.extend(
            [
                f"### {candidate['strategy_id']}",
                "",
                f"- Type: `{candidate['type']}`",
                f"- Status: `{candidate['pool_status']}`",
                f"- Evidence: {candidate['evidence_summary']}",
                f"- Representative accounts: {candidate['representative_accounts']}",
                f"- Applicable market: {candidate['applicable_market']}",
                f"- Entry conditions: {candidate['entry_conditions']}",
                f"- Exit conditions: {candidate['exit_conditions']}",
                f"- Position rule: {candidate['position_rule']}",
                f"- Max loss rule: {candidate['max_loss_rule']}",
                f"- Data requirements: {candidate['data_requirements']}",
                f"- Backtestability: {candidate['backtestability']}",
                f"- Failure conditions: {candidate['failure_conditions']}",
                f"- Current conclusion: **不建议交易**.",
                "",
            ]
        )

    report.extend(
        [
            "## Next Execution Plan",
            "",
            "1. Build a local immutable dataset cache for Polymarket historical trades/orderbooks/settlements and Hyperliquid OHLCV/funding/orderbook data.",
            "2. Implement backtest harnesses per strategy ID with no live account connectivity.",
            "3. Run rolling train/validation/test and all robustness tests listed in `offline_validation_plan.md`.",
            "4. Promote only strategies that pass all gates; otherwise keep conclusion as **不建议交易**.",
            "",
        ]
    )

    return "\n".join(report)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    data = collect_inputs()
    checks = build_cross_validation(data)
    reps = representative_accounts(data)
    candidates = build_strategy_candidates(reps)

    fields = [
        "strategy_id",
        "type",
        "pool_status",
        "representative_accounts",
        "evidence_summary",
        "reproducibility_score",
        "risk_score",
        "applicable_market",
        "entry_conditions",
        "exit_conditions",
        "position_rule",
        "max_loss_rule",
        "data_requirements",
        "backtestability",
        "failure_conditions",
        "not_trade_recommended",
    ]
    write_csv(OUT / "strategy_candidates.csv", candidates, fields)
    write_csv(
        OUT / "cross_validation_checks.csv",
        checks,
        ["session", "address", "name", "expected_type", "session1_label", "status", "decision"],
    )
    (OUT / "offline_validation_plan.md").write_text(build_validation_plan(), encoding="utf-8")
    (OUT / "SESSION_7_STRATEGY_POOL.md").write_text(
        build_strategy_report(data, candidates, checks),
        encoding="utf-8",
    )
    print(
        {
            "strategies": len(candidates),
            "cross_validation_checks": len(checks),
            "outputs": [
                str(OUT / "SESSION_7_STRATEGY_POOL.md"),
                str(OUT / "strategy_candidates.csv"),
                str(OUT / "offline_validation_plan.md"),
                str(OUT / "cross_validation_checks.csv"),
            ],
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
