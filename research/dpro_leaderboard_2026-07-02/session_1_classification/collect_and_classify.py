#!/usr/bin/env python3
"""Collect public d.Pro/Polymarket/Hyperliquid data and classify accounts.

This script intentionally uses public endpoints only. It does not read local
environment files, credentials, wallets, or API keys.
"""

from __future__ import annotations

import csv
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


OUT_DIR = Path(__file__).resolve().parent
ROOT_DIR = OUT_DIR.parent
RAW_DIR = OUT_DIR / "raw"

PM_LEADERBOARD_URL = (
    "https://data-api.polymarket.com/v1/leaderboard"
    "?timePeriod=month&orderBy=PNL&category=overall&limit=50&offset=0"
)
DPRO_HL_LEADERBOARD_URL = (
    "https://api.d.pro/api/v1/leaderboard"
    "?page=1&limit=100&sort=pnl_month&order=desc"
)
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

LOCAL_PM_SUMMARY = Path("/tmp/dpro_pm_top20_summary.csv")
LOCAL_HL_SUMMARY = Path("/tmp/dpro_hl_top10_summary.csv")
LOCAL_DPRO_SAMPLE = Path("/tmp/dpro_leaderboard_sample.csv")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}

HTTP_TIMEOUT_S = float(os.environ.get("DPRO_RESEARCH_HTTP_TIMEOUT_S", "8"))
PM_DETAIL_LIMIT = int(os.environ.get("DPRO_RESEARCH_PM_DETAIL_LIMIT", "50"))
HL_DETAIL_LIMIT = int(os.environ.get("DPRO_RESEARCH_HL_DETAIL_LIMIT", "35"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_float(value: Any, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(parsed) or math.isinf(parsed):
        return default
    return parsed


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Optional[Dict[str, Any]] = None,
    retries: int = 2,
    sleep_s: float = 0.25,
    timeout_s: float = HTTP_TIMEOUT_S,
) -> Tuple[Optional[Any], Optional[str]]:
    body = None
    headers = dict(HEADERS)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8")), None
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries:
                time.sleep(sleep_s * (attempt + 1))
    return None, last_error


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: List[Dict[str, Any]], fields: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})


def pm_detail_url(endpoint: str, wallet: str, extra: Dict[str, Any]) -> str:
    query = urllib.parse.urlencode(extra)
    return f"https://data-api.polymarket.com/{endpoint}?user={wallet}&{query}"


def fetch_pm_accounts(errors: List[Dict[str, str]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    local_rows = read_csv_rows(LOCAL_PM_SUMMARY)
    local_by_addr = {r.get("addr", "").lower(): r for r in local_rows if r.get("addr")}

    data, err = request_json(PM_LEADERBOARD_URL)
    source = "fresh_api"
    if err:
        errors.append({"scope": "pm_leaderboard", "address": "", "error": err})
        source = "local_sample_fallback"
        leaderboard = []
        for r in local_rows:
            leaderboard.append(
                {
                    "rank": r.get("rank"),
                    "proxyWallet": r.get("addr"),
                    "userName": r.get("user"),
                    "pnl": to_float(r.get("month_pnl")),
                    "vol": to_float(r.get("month_vol")),
                }
            )
    else:
        leaderboard = data if isinstance(data, list) else data.get("data", [])
        write_json(RAW_DIR / "pm_leaderboard_top50.json", data)

    rows: List[Dict[str, Any]] = []
    for idx, item in enumerate(leaderboard[:50], start=1):
        if idx % 10 == 1:
            print(f"PM classification progress: {idx}/{min(len(leaderboard), 50)}", flush=True)
        wallet = (item.get("proxyWallet") or item.get("addr") or "").lower()
        if not wallet:
            continue
        local = local_by_addr.get(wallet, {})
        rank = to_int(item.get("rank"), idx)
        pnl = to_float(item.get("pnl"), to_float(local.get("month_pnl")))
        volume = to_float(item.get("vol"), to_float(local.get("month_vol")))
        user = item.get("userName") or local.get("user") or ""

        if local:
            detail_mode = "local_sample_detail"
            stats = {
                "trades": local.get("stats_trades"),
                "largestWin": local.get("stats_largest_win"),
                "joinDate": local.get("joinDate"),
            }
            closed = []
            current = []
        elif idx <= PM_DETAIL_LIMIT:
            detail_mode = "fresh_public_detail"
            stats, stats_err = request_json(
                f"https://data-api.polymarket.com/v1/user-stats?proxyAddress={wallet}",
                retries=0,
            )
            if stats_err:
                errors.append({"scope": "pm_user_stats", "address": wallet, "error": stats_err})
                stats = {}

            closed, closed_err = request_json(
                pm_detail_url(
                    "closed-positions",
                    wallet,
                    {
                        "limit": 500,
                        "offset": 0,
                        "sortBy": "realizedpnl",
                        "sortDirection": "DESC",
                    },
                ),
                retries=0,
            )
            if closed_err:
                errors.append({"scope": "pm_closed_positions", "address": wallet, "error": closed_err})
                closed = []
            if not isinstance(closed, list):
                closed = []

            current, current_err = request_json(
                pm_detail_url(
                    "positions",
                    wallet,
                    {
                        "limit": 500,
                        "offset": 0,
                        "sortBy": "CURRENT",
                        "sortDirection": "DESC",
                        "sizeThreshold": 0.1,
                    },
                ),
                retries=0,
            )
            if current_err:
                errors.append({"scope": "pm_current_positions", "address": wallet, "error": current_err})
                current = []
            if not isinstance(current, list):
                current = []
        else:
            detail_mode = "leaderboard_only_detail_pending"
            stats = {}
            closed = []
            current = []

        trades = to_int((stats or {}).get("trades"), to_int(local.get("stats_trades")))
        largest_win = to_float((stats or {}).get("largestWin"), to_float(local.get("stats_largest_win")))
        join_date = (stats or {}).get("joinDate") or local.get("joinDate", "")
        pnl_per_vol = pnl / volume if volume > 0 else 0.0
        largest_win_to_pnl = largest_win / pnl if pnl > 0 else 0.0

        if detail_mode == "local_sample_detail":
            top_closed = {
                "title": local.get("top_closed_title", ""),
                "outcome": local.get("top_closed_outcome", ""),
                "realizedPnl": local.get("top_closed_realized", ""),
            }
            closed_count = to_int(local.get("closed_count"))
            closed_realized_sum = to_float(local.get("sum_top_closed_realized"))
            top_closed_realized = to_float(local.get("top_closed_realized"))
            top_closed_share = top_closed_realized / closed_realized_sum if closed_realized_sum > 0 else 0.0
            current_count = to_int(local.get("current_pos_count"))
            current_value = to_float(local.get("current_pos_value"))
            current_cost = to_float(local.get("current_pos_cost"))
            top5_titles = [t for t in local.get("top5_titles", "").split(" | ") if t]
            unique_events = max(len(top5_titles), closed_count, current_count)
            negative_risk_count = 0
        else:
            top_closed = closed[0] if closed else {}
            closed_positive = [to_float(p.get("realizedPnl")) for p in closed if to_float(p.get("realizedPnl")) > 0]
            closed_realized_sum = sum(to_float(p.get("realizedPnl")) for p in closed)
            closed_positive_sum = sum(closed_positive)
            top_closed_realized = to_float(top_closed.get("realizedPnl"))
            top_closed_share = top_closed_realized / closed_positive_sum if closed_positive_sum > 0 else 0.0

            current_value = sum(to_float(p.get("currentValue")) for p in current)
            current_cost = sum(to_float(p.get("initialValue")) for p in current)
            current_count = len(current)
            closed_count = len(closed)
            negative_risk_count = sum(1 for p in closed + current if p.get("negativeRisk"))
            unique_events = len(
                {
                    str(p.get("eventSlug") or p.get("slug") or p.get("title") or "")
                    for p in closed + current
                    if p.get("eventSlug") or p.get("slug") or p.get("title")
                }
            )

        if detail_mode == "leaderboard_only_detail_pending":
            label = "F_detail_pending_excluded"
            reasons = ["top50 leaderboard row read; per-account public detail deferred to deep-dive sessions"]
            excluded = True
        else:
            label, reasons, excluded = classify_pm(
                pnl=pnl,
                volume=volume,
                trades=trades,
                largest_win_to_pnl=largest_win_to_pnl,
                pnl_per_vol=pnl_per_vol,
                current_count=current_count,
                closed_count=closed_count,
                unique_events=unique_events,
            )

        rows.append(
            {
                "market": "polymarket",
                "rank": rank,
                "account": wallet,
                "name": user,
                "pnl_month": round(pnl, 6),
                "volume_month": round(volume, 6),
                "pnl_per_volume": round(pnl_per_vol, 8),
                "trade_count": trades,
                "largest_win": round(largest_win, 6),
                "largest_win_to_pnl": round(largest_win_to_pnl, 6),
                "closed_count": closed_count,
                "closed_realized_sum": round(closed_realized_sum, 6),
                "top_closed_realized": round(top_closed_realized, 6),
                "top_closed_share_of_positive_closed": round(top_closed_share, 6),
                "top_closed_title": top_closed.get("title", ""),
                "top_closed_outcome": top_closed.get("outcome", ""),
                "current_position_count": current_count,
                "current_position_value": round(current_value, 6),
                "current_position_cost": round(current_cost, 6),
                "unique_event_count_sample": unique_events,
                "negative_risk_count_sample": negative_risk_count,
                "join_date": join_date,
                "classification": label,
                "excluded": excluded,
                "classification_reasons": "; ".join(reasons),
                "data_source": f"{source}:{detail_mode}",
                "not_trade_recommended": True,
            }
        )
        time.sleep(0.05)

    return rows, {"source": source, "local_rows": len(local_rows), "fresh_rows": len(leaderboard)}


def classify_pm(
    *,
    pnl: float,
    volume: float,
    trades: int,
    largest_win_to_pnl: float,
    pnl_per_vol: float,
    current_count: int,
    closed_count: int,
    unique_events: int,
) -> Tuple[str, List[str], bool]:
    reasons: List[str] = []
    if volume <= 0 and pnl > 0:
        return "F_anomaly_excluded", ["positive PnL with zero/invalid volume"], True
    if trades <= 1 and pnl > 10000:
        return "F_anomaly_excluded", ["PnL high with <=1 reported trade"], True
    if pnl_per_vol > 2.0 and volume < 10000:
        return "F_anomaly_excluded", ["extreme PnL/volume on low base"], True

    if trades >= 5000 or (trades >= 1000 and current_count >= 30 and volume >= 1_000_000):
        reasons.extend(["very high trade count", "broad/current position footprint"])
        if pnl_per_vol < 0.12:
            reasons.append("low-to-moderate PnL/volume consistent with spread or microstructure edge")
        if largest_win_to_pnl >= 0.8:
            reasons.append("largest win still large; verify not a single-event artifact")
        return "B_pm_hf_mm_arb", reasons, False

    if (
        trades <= 50
        and largest_win_to_pnl >= 0.45
        and current_count <= 10
        and pnl_per_vol >= 0.08
    ) or (largest_win_to_pnl >= 0.75 and closed_count <= 20):
        reasons.extend(["low trade count or concentrated closed wins", "high largestWin/PnL"])
        if pnl_per_vol >= 0.10:
            reasons.append("high PnL/volume")
        return "A_pm_large_event_bet", reasons, False

    if 20 <= trades <= 5000 and unique_events >= 3 and largest_win_to_pnl < 0.9:
        reasons.extend(["mid-range trade count", "multiple sampled markets", "not entirely single-win dominated"])
        return "C_pm_midfreq_selective", reasons, False

    reasons.append("does not cleanly meet A/B/C thresholds")
    return "F_anomaly_or_unclear_excluded", reasons, True


def parse_window_performance(item: Dict[str, Any], key: str) -> Dict[str, float]:
    for window_name, perf in item.get("windowPerformances", []):
        if window_name == key:
            return {
                "pnl": to_float(perf.get("pnl")),
                "roi": to_float(perf.get("roi")),
                "vlm": to_float(perf.get("vlm")),
            }
    return {"pnl": 0.0, "roi": 0.0, "vlm": 0.0}


def fetch_hl_accounts(errors: List[Dict[str, str]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    local_rows = read_csv_rows(LOCAL_HL_SUMMARY)
    local_by_addr = {r.get("addr", "").lower(): r for r in local_rows if r.get("addr")}

    data, err = request_json(DPRO_HL_LEADERBOARD_URL)
    source = "fresh_api"
    if err:
        errors.append({"scope": "hl_dpro_leaderboard", "address": "", "error": err})
        source = "local_sample_fallback"
        leaderboard = []
        for r in local_rows:
            leaderboard.append(
                {
                    "ethAddress": r.get("addr"),
                    "displayName": r.get("name"),
                    "accountValue": r.get("accountValue_rank"),
                    "windowPerformances": [
                        ["month", {"pnl": r.get("pnl_month"), "roi": r.get("roi_month"), "vlm": r.get("volume_month")}],
                        ["allTime", {"pnl": r.get("pnl_all"), "roi": r.get("roi_all"), "vlm": ""}],
                    ],
                    "tags": [],
                }
            )
    else:
        leaderboard = data.get("data", {}).get("data", []) if isinstance(data, dict) else []
        write_json(RAW_DIR / "hl_leaderboard_top100.json", data)

    rows: List[Dict[str, Any]] = []
    for idx, item in enumerate(leaderboard[:100], start=1):
        if idx % 10 == 1:
            print(f"HL classification progress: {idx}/{min(len(leaderboard), 100)}", flush=True)
        address = (item.get("ethAddress") or item.get("address") or "").lower()
        if not address:
            continue
        local = local_by_addr.get(address, {})
        month = parse_window_performance(item, "month")
        all_time = parse_window_performance(item, "allTime")
        pnl = month["pnl"] or to_float(local.get("pnl_month"))
        volume = month["vlm"] or to_float(local.get("volume_month"))
        roi = month["roi"] or to_float(local.get("roi_month"))
        pnl_per_vol = pnl / volume if volume > 0 else 0.0
        name = item.get("displayName") or local.get("name") or ""
        account_value = to_float(item.get("accountValue"), to_float(local.get("accountValue_rank")))

        detail_mode = "fresh_public_detail"
        if idx <= HL_DETAIL_LIMIT:
            state, state_err = request_json(
                HL_INFO_URL,
                method="POST",
                payload={"type": "clearinghouseState", "user": address},
                retries=0,
            )
            if state_err:
                errors.append({"scope": "hl_clearinghouse_state", "address": address, "error": state_err})
                state = {}

            fills, fills_err = request_json(
                HL_INFO_URL,
                method="POST",
                payload={"type": "userFills", "user": address},
                retries=0,
            )
            if fills_err:
                errors.append({"scope": "hl_user_fills", "address": address, "error": fills_err})
                fills = []
            if not isinstance(fills, list):
                fills = []
        else:
            detail_mode = "leaderboard_only_detail_pending"
            state = {}
            fills = []

        asset_positions = (state or {}).get("assetPositions", []) if isinstance(state, dict) else []
        positions = []
        for p in asset_positions:
            pos = p.get("position", {})
            value = abs(to_float(pos.get("positionValue")))
            positions.append(
                {
                    "coin": pos.get("coin", ""),
                    "szi": to_float(pos.get("szi")),
                    "entry": to_float(pos.get("entryPx")),
                    "value": value,
                    "upnl": to_float(pos.get("unrealizedPnl")),
                    "margin": to_float(pos.get("marginUsed")),
                    "leverage": to_float((pos.get("leverage") or {}).get("value")),
                }
            )
        total_pos_value = sum(p["value"] for p in positions)
        top_pos = max(positions, key=lambda p: p["value"], default={})
        top_pos_share = top_pos.get("value", 0.0) / total_pos_value if total_pos_value > 0 else 0.0
        margin_summary = (state or {}).get("marginSummary", {}) if isinstance(state, dict) else {}
        total_margin = to_float(margin_summary.get("totalMarginUsed"))
        total_upnl = sum(p["upnl"] for p in positions)

        fill_counter = Counter()
        dir_counter = Counter()
        fill_notional = 0.0
        fees = 0.0
        fill_times: List[int] = []
        for fill in fills:
            coin = str(fill.get("coin", ""))
            if coin:
                fill_counter[coin] += 1
            direction = str(fill.get("dir", ""))
            if direction:
                dir_counter[direction] += 1
            px = to_float(fill.get("px"))
            sz = abs(to_float(fill.get("sz")))
            fill_notional += px * sz
            fees += abs(to_float(fill.get("fee")))
            fill_time = to_int(fill.get("time"))
            if fill_time:
                fill_times.append(fill_time)

        dominant_fill_coin, dominant_fill_count = ("", 0)
        if fill_counter:
            dominant_fill_coin, dominant_fill_count = fill_counter.most_common(1)[0]
        dominant_fill_share = dominant_fill_count / len(fills) if fills else 0.0
        fill_start = datetime.fromtimestamp(min(fill_times) / 1000, tz=timezone.utc).isoformat() if fill_times else ""
        fill_end = datetime.fromtimestamp(max(fill_times) / 1000, tz=timezone.utc).isoformat() if fill_times else ""

        if detail_mode == "leaderboard_only_detail_pending":
            label = "F_detail_pending_excluded"
            reasons = ["top100 leaderboard row read; public position/fill detail deferred to deep-dive sessions"]
            excluded = True
        else:
            label, reasons, excluded = classify_hl(
                pnl=pnl,
                volume=volume,
                position_count=len(positions),
                top_pos_share=top_pos_share,
                fill_count=len(fills),
                fill_coin_count=len(fill_counter),
                dominant_fill_share=dominant_fill_share,
                total_pos_value=total_pos_value,
            )

        rows.append(
            {
                "market": "hyperliquid",
                "rank": idx,
                "account": address,
                "name": name,
                "account_value": round(account_value, 6),
                "pnl_month": round(pnl, 6),
                "roi_month": round(roi, 8),
                "volume_month": round(volume, 6),
                "pnl_per_volume": round(pnl_per_vol, 8),
                "pnl_all_time": round(all_time["pnl"], 6),
                "roi_all_time": round(all_time["roi"], 8),
                "position_count": len(positions),
                "position_notional": round(total_pos_value, 6),
                "position_upnl": round(total_upnl, 6),
                "margin_used": round(total_margin, 6),
                "top_position_coin": top_pos.get("coin", ""),
                "top_position_value": round(top_pos.get("value", 0.0), 6),
                "top_position_share": round(top_pos_share, 6),
                "top_position_leverage": round(top_pos.get("leverage", 0.0), 6),
                "fill_count_recent": len(fills),
                "fill_count_capped_at_2000": len(fills) >= 2000,
                "fill_coin_count": len(fill_counter),
                "dominant_fill_coin": dominant_fill_coin,
                "dominant_fill_share": round(dominant_fill_share, 6),
                "fill_notional_recent": round(fill_notional, 6),
                "fees_recent": round(fees, 6),
                "fee_bps_recent": round((fees / fill_notional) * 10000, 6) if fill_notional > 0 else 0.0,
                "fill_start": fill_start,
                "fill_end": fill_end,
                "fill_dirs": json.dumps(dict(dir_counter), sort_keys=True),
                "classification": label,
                "excluded": excluded,
                "classification_reasons": "; ".join(reasons),
                "data_source": f"{source}:{detail_mode}",
                "not_trade_recommended": True,
            }
        )
        time.sleep(0.05)

    return rows, {"source": source, "local_rows": len(local_rows), "fresh_rows": len(leaderboard)}


def classify_hl(
    *,
    pnl: float,
    volume: float,
    position_count: int,
    top_pos_share: float,
    fill_count: int,
    fill_coin_count: int,
    dominant_fill_share: float,
    total_pos_value: float,
) -> Tuple[str, List[str], bool]:
    reasons: List[str] = []
    if volume <= 0 and pnl > 0:
        return "F_anomaly_excluded", ["positive PnL with zero/invalid monthly volume"], True
    if pnl > 0 and total_pos_value == 0 and fill_count == 0:
        return "F_anomaly_or_incomplete_excluded", ["PnL present but no public positions or fills fetched"], True

    if position_count <= 3 and total_pos_value > 0 and top_pos_share >= 0.70:
        reasons.extend(["few active positions", "top position dominates current notional"])
        if dominant_fill_share >= 0.70:
            reasons.append("recent fills concentrated in one coin")
        return "D_hl_concentrated_directional", reasons, False

    if fill_count >= 500 and fill_coin_count >= 5:
        reasons.extend(["many recent fills", "multi-asset recent activity"])
        if position_count >= 5:
            reasons.append("multiple active positions")
        if dominant_fill_share < 0.70:
            reasons.append("fills not fully dominated by a single coin")
        return "E_hl_high_turnover_multi_asset", reasons, False

    if position_count <= 5 and top_pos_share >= 0.55:
        reasons.extend(["moderately concentrated active positions", "directional risk likely material"])
        return "D_hl_concentrated_directional", reasons, False

    if fill_count >= 100 and fill_coin_count >= 3:
        reasons.extend(["active recent trading", "several traded assets"])
        return "E_hl_high_turnover_multi_asset", reasons, False

    reasons.append("does not cleanly meet D/E thresholds or public detail incomplete")
    return "F_anomaly_or_unclear_excluded", reasons, True


def candidate_rows(rows: Iterable[Dict[str, Any]], label: str, limit: int = 8) -> List[Dict[str, Any]]:
    selected = [r for r in rows if r.get("classification") == label and not str(r.get("excluded")).lower() == "true"]
    return sorted(selected, key=lambda r: to_float(r.get("pnl_month")), reverse=True)[:limit]


def build_report(pm_rows: List[Dict[str, Any]], hl_rows: List[Dict[str, Any]], meta: Dict[str, Any], errors: List[Dict[str, str]]) -> str:
    all_rows = pm_rows + hl_rows
    counts = Counter(r["classification"] for r in all_rows)

    def md_table(rows: List[Dict[str, Any]], fields: List[str]) -> str:
        if not rows:
            return "_No rows._"
        header = "| " + " | ".join(fields) + " |"
        sep = "| " + " | ".join(["---"] * len(fields)) + " |"
        body = []
        for row in rows:
            vals = []
            for f in fields:
                val = str(row.get(f, ""))
                if len(val) > 90:
                    val = val[:87] + "..."
                vals.append(val.replace("|", "\\|"))
            body.append("| " + " | ".join(vals) + " |")
        return "\n".join([header, sep] + body)

    pm_a = candidate_rows(pm_rows, "A_pm_large_event_bet", 5)
    pm_b = candidate_rows(pm_rows, "B_pm_hf_mm_arb", 5)
    pm_c = candidate_rows(pm_rows, "C_pm_midfreq_selective", 5)
    hl_d = candidate_rows(hl_rows, "D_hl_concentrated_directional", 5)
    hl_e = candidate_rows(hl_rows, "E_hl_high_turnover_multi_asset", 5)
    excluded = [r for r in all_rows if str(r.get("excluded")).lower() == "true"][:10]

    lines = [
        "# Session 1 Report: Data Collection And Account Classification",
        "",
        f"Generated: {meta['generated_at']}",
        "",
        "## Scope And Safety",
        "",
        "- Public leaderboard and public account detail APIs only.",
        "- No `.env`, keys, wallet files, credentials, private account sessions, or order endpoints were read or used.",
        "- These labels are research triage labels, not trading signals.",
        "- Current recommendation for all candidates: **不建议交易** until the Session 7 robustness plan is implemented and passed.",
        "",
        "## Data Sources",
        "",
        f"- Polymarket source: `{meta['pm']['source']}`; rows classified: {len(pm_rows)}.",
        f"- Hyperliquid source: `{meta['hl']['source']}`; rows classified: {len(hl_rows)}.",
        f"- Local PM sample rows available: {meta['pm']['local_rows']}; local HL sample rows available: {meta['hl']['local_rows']}.",
        f"- API/detail errors captured: {len(errors)}. See `fetch_errors.json`.",
        "",
        "## Classification Counts",
        "",
        md_table(
            [{"classification": k, "count": v} for k, v in sorted(counts.items())],
            ["classification", "count"],
        ),
        "",
        "## Top Candidates By Type",
        "",
        "### A. Polymarket 大额事件押注型",
        "",
        md_table(
            pm_a,
            [
                "rank",
                "name",
                "account",
                "pnl_month",
                "volume_month",
                "trade_count",
                "largest_win_to_pnl",
                "closed_count",
                "top_closed_title",
                "classification_reasons",
            ],
        ),
        "",
        "### B. Polymarket 高频 / 做市 / 套利型",
        "",
        md_table(
            pm_b,
            [
                "rank",
                "name",
                "account",
                "pnl_month",
                "volume_month",
                "trade_count",
                "pnl_per_volume",
                "current_position_count",
                "classification_reasons",
            ],
        ),
        "",
        "### C. Polymarket 中频精选型",
        "",
        md_table(
            pm_c,
            [
                "rank",
                "name",
                "account",
                "pnl_month",
                "volume_month",
                "trade_count",
                "largest_win_to_pnl",
                "unique_event_count_sample",
                "classification_reasons",
            ],
        ),
        "",
        "### D. Hyperliquid 集中方向仓型",
        "",
        md_table(
            hl_d,
            [
                "rank",
                "name",
                "account",
                "pnl_month",
                "volume_month",
                "position_count",
                "top_position_coin",
                "top_position_share",
                "fill_count_recent",
                "classification_reasons",
            ],
        ),
        "",
        "### E. Hyperliquid 高周转多资产型",
        "",
        md_table(
            hl_e,
            [
                "rank",
                "name",
                "account",
                "pnl_month",
                "volume_month",
                "position_count",
                "fill_count_recent",
                "fill_coin_count",
                "dominant_fill_share",
                "classification_reasons",
            ],
        ),
        "",
        "### F. 异常 / 剔除样本",
        "",
        md_table(
            excluded,
            [
                "market",
                "rank",
                "name",
                "account",
                "pnl_month",
                "volume_month",
                "classification",
                "classification_reasons",
            ],
        ),
        "",
        "## Evidence Notes",
        "",
        "- Polymarket `largest_win_to_pnl > 1` means the account had a single closed win larger than monthly PnL, implying losses or open-position drag offset the win. It is concentration risk, not proof of a stable edge.",
        "- Hyperliquid `fill_count_recent` is from public `userFills` and can be capped at 2000 by the API; capped rows should be interpreted as lower bounds.",
        "- Current-position concentration on Hyperliquid is a snapshot and may not reflect the whole month. Deep-dive sessions must cross-check fills and position changes.",
        "- Classification is a triage result for downstream research. No account enters a live-trading sample pool before robustness validation.",
        "",
        "## Next Round Plan",
        "",
        "- Session 2 validates A candidates for single-event concentration, event type, negative-risk exposure, and whether the result looks like information advantage or luck.",
        "- Session 3 validates B candidates for market breadth, current inventory, spread capture, negative-risk or cross-market arbitrage evidence.",
        "- Session 4 validates C candidates for distributed wins and whether event-selection / entry-band / exit rules can be stated cleanly.",
        "- Session 5 validates D candidates for directional exposure, leverage, current notional concentration, and drawdown/liquidation risk.",
        "- Session 6 validates E candidates for fill breadth, turnover, fees, and whether returns are driven by few assets.",
        "- Session 7 should compile only high-evidence, non-anomalous hypotheses into offline validation designs. Until those pass: **不建议交易**.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    errors: List[Dict[str, str]] = []
    pm_rows, pm_meta = fetch_pm_accounts(errors)
    hl_rows, hl_meta = fetch_hl_accounts(errors)
    combined = pm_rows + hl_rows

    pm_fields = [
        "market",
        "rank",
        "account",
        "name",
        "pnl_month",
        "volume_month",
        "pnl_per_volume",
        "trade_count",
        "largest_win",
        "largest_win_to_pnl",
        "closed_count",
        "closed_realized_sum",
        "top_closed_realized",
        "top_closed_share_of_positive_closed",
        "top_closed_title",
        "top_closed_outcome",
        "current_position_count",
        "current_position_value",
        "current_position_cost",
        "unique_event_count_sample",
        "negative_risk_count_sample",
        "join_date",
        "classification",
        "excluded",
        "classification_reasons",
        "data_source",
        "not_trade_recommended",
    ]
    hl_fields = [
        "market",
        "rank",
        "account",
        "name",
        "account_value",
        "pnl_month",
        "roi_month",
        "volume_month",
        "pnl_per_volume",
        "pnl_all_time",
        "roi_all_time",
        "position_count",
        "position_notional",
        "position_upnl",
        "margin_used",
        "top_position_coin",
        "top_position_value",
        "top_position_share",
        "top_position_leverage",
        "fill_count_recent",
        "fill_count_capped_at_2000",
        "fill_coin_count",
        "dominant_fill_coin",
        "dominant_fill_share",
        "fill_notional_recent",
        "fees_recent",
        "fee_bps_recent",
        "fill_start",
        "fill_end",
        "fill_dirs",
        "classification",
        "excluded",
        "classification_reasons",
        "data_source",
        "not_trade_recommended",
    ]
    combined_fields = sorted({*pm_fields, *hl_fields})

    write_csv(OUT_DIR / "pm_accounts_classified.csv", pm_rows, pm_fields)
    write_csv(OUT_DIR / "hl_accounts_classified.csv", hl_rows, hl_fields)
    write_csv(OUT_DIR / "combined_account_classification.csv", combined, combined_fields)

    candidates = {
        "A_pm_large_event_bet": candidate_rows(pm_rows, "A_pm_large_event_bet", 10),
        "B_pm_hf_mm_arb": candidate_rows(pm_rows, "B_pm_hf_mm_arb", 10),
        "C_pm_midfreq_selective": candidate_rows(pm_rows, "C_pm_midfreq_selective", 10),
        "D_hl_concentrated_directional": candidate_rows(hl_rows, "D_hl_concentrated_directional", 10),
        "E_hl_high_turnover_multi_asset": candidate_rows(hl_rows, "E_hl_high_turnover_multi_asset", 10),
        "F_excluded": [r for r in combined if str(r.get("excluded")).lower() == "true"][:50],
    }
    write_json(OUT_DIR / "top_candidates_by_type.json", candidates)
    write_json(OUT_DIR / "fetch_errors.json", errors)

    meta = {"generated_at": now_iso(), "pm": pm_meta, "hl": hl_meta}
    write_json(OUT_DIR / "run_meta.json", meta)
    report = build_report(pm_rows, hl_rows, meta, errors)
    (OUT_DIR / "SESSION_1_REPORT.md").write_text(report, encoding="utf-8")

    print(json.dumps({"pm_rows": len(pm_rows), "hl_rows": len(hl_rows), "errors": len(errors)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
