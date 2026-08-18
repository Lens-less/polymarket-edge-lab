"""Decisive analysis: with true Chainlink strikes, does the structural floor pair ever cost < 1?

cost variants for the floor pair (legs A and B):
  taker/taker  = askA + askB + fee(askA) + fee(askB)
  maker/taker  = bidA + askB + fee(askB)      (rest at the bid on A, FOK hedge B)
  maker/maker  = bidA + bidB                  (rest at the bid on both legs, zero fee)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from decimal import Decimal

path = sys.argv[1] if len(sys.argv) > 1 else "run1.jsonl"
ZERO, ONE = Decimal(0), Decimal(1)


def fee(shares: Decimal, price: Decimal) -> Decimal:
    raw = shares * Decimal("0.07") * price * (ONE - price)
    return raw.quantize(Decimal("0.00001"))


twaps: dict[int, float] = {}
books: list[dict] = []
for line in open(path, encoding="utf-8"):
    line = line.strip()
    if not line:
        continue
    rec = json.loads(line)
    if rec.get("kind") == "twap" and rec.get("ts"):
        twaps[int(rec["ts"]) // 1000] = float(rec["value"])
    elif rec.get("kind") == "book" and rec.get("top"):
        books.append(rec)


def twap_at(sec: int, tol: int = 5):
    for d in range(tol + 1):
        if sec - d in twaps:
            return twaps[sec - d]
        if sec + d in twaps:
            return twaps[sec + d]
    return None


by_close = defaultdict(list)
for rec in books:
    by_close[rec["close"]].append(rec)

print(f"twap seconds covered: {len(twaps)}   book samples: {len(books)}")
print()

for close in sorted(by_close):
    k15 = twap_at(close - 900)
    k5 = twap_at(close - 300)
    term = twap_at(close)
    rows = sorted(by_close[close], key=lambda r: -r["ttc"])
    live = [r for r in rows if r["ttc"] <= 300]
    head = f"=== close={close}  samples={len(rows)} (5m-live={len(live)})"
    if k5 is None or k15 is None:
        print(head + "  strikes: UNKNOWN (outside capture window)")
        print()
        continue
    if k5 > k15:
        ordering, a, b = "K5>K15", "5down", "15up"
    elif k5 < k15:
        ordering, a, b = "K5<K15", "5up", "15down"
    else:
        ordering, a, b = "K5==K15", None, None
    print(
        head + f"  K15={k15:.4f} K5={k5:.4f} dK={k5-k15:+.4f} {ordering}"
        + (f" T={term:.4f}" if term else "")
    )
    if term is not None:
        print(
            f"    settled: 5m={'Up' if term>=k5 else 'Down'}  "
            f"15m={'Up' if term>=k15 else 'Down'}  "
            f"split={'YES' if (term>=k5)!=(term>=k15) else 'no'}"
        )
    if a is None:
        print("    equal strikes: both cross pairs have floor 1")
        print()
        continue
    print(f"    floor pair = buy {a} + buy {b}")
    best = {"taker": None, "maker_taker": None, "maker_maker": None}
    below = {"taker": 0, "maker_taker": 0, "maker_maker": 0}
    n = 0
    for r in live:
        t = r["top"]
        try:
            aa, ab = Decimal(t[a]["ask"]), Decimal(t[b]["ask"])
            ba, bb = Decimal(t[a]["bid"]), Decimal(t[b]["bid"])
        except Exception:  # noqa: BLE001
            continue
        n += 1
        c = {
            "taker": aa + ab + fee(ONE, aa) + fee(ONE, ab),
            "maker_taker": ba + ab + fee(ONE, ab),
            "maker_maker": ba + bb,
        }
        for k, v in c.items():
            if best[k] is None or v < best[k][0]:
                best[k] = (v, r["ttc"])
            if v < ONE:
                below[k] += 1
    if not n:
        print("    no live-window samples yet")
        print()
        continue
    for k in ("taker", "maker_taker", "maker_maker"):
        v, ttc = best[k]
        floor = ONE - v
        mark = "   <<< POSITIVE FLOOR" if floor > 0 else ""
        print(
            f"    {k:<12} best_cost={v:.5f} floor={floor:+.5f} (ttc={ttc}s)  "
            f"samples_with_positive_floor={below[k]}/{n}{mark}"
        )
    print()
