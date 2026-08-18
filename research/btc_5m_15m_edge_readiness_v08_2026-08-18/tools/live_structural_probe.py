"""Live mini-Gate-0: official 60s TWAP + four public books on co-terminal BTC 5m/15m pairs.

Records, per sample:
  - official Chainlink 60s TWAP (RTDS public topic crypto_prices_twap_sixty)
  - four token books (top-of-book + walked ask cost at several sizes)
Then, offline, K15 = TWAP at the 15m open, K5 = TWAP at the 5m open, which fixes
the true strike ordering and therefore the true structural (floor=1) pair.

Read-only. Public endpoints only. No credentials, no orders.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

import websockets

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
RTDS_WS = "wss://ws-live-data.polymarket.com"
TOPIC = "crypto_prices_twap_sixty"
SYMBOL = "btc/usd"
TICK = Decimal("0.01")

latest_twap: dict = {}


def http_get(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": "poly-mm-research/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def event_tokens(slug: str):
    data = http_get(f"{GAMMA}/events?slug={slug}")
    if not data:
        return None
    mkt = data[0]["markets"][0]
    outcomes = json.loads(mkt["outcomes"])
    tokens = json.loads(mkt["clobTokenIds"])
    m = {o.lower(): t for o, t in zip(outcomes, tokens)}
    return {"end": data[0]["endDate"], "up": m["up"], "down": m["down"]}


def fee(shares: Decimal, price: Decimal) -> Decimal:
    raw = shares * Decimal("0.07") * price * (Decimal(1) - price)
    return raw.quantize(Decimal("0.00001"), rounding=ROUND_HALF_UP)


def levels(bk, side):
    return sorted(
        ((Decimal(a["price"]), Decimal(a["size"])) for a in bk.get(side, [])),
        key=lambda x: x[0],
        reverse=(side == "bids"),
    )


def walk(bk, qty: Decimal):
    remaining, notional, fees = qty, Decimal(0), Decimal(0)
    for price, size in levels(bk, "asks"):
        if remaining <= 0:
            break
        take = min(remaining, size)
        notional += take * price
        fees += fee(take, price)
        remaining -= take
    return notional, fees, qty - remaining


def topbook(bk, side):
    lv = levels(bk, side)
    return lv[0] if lv else (None, None)


async def rtds_task(stop: asyncio.Event, out):
    sub = {
        "action": "subscribe",
        "subscriptions": [
            {"topic": TOPIC, "type": "update", "filters": json.dumps({"symbol": SYMBOL}, separators=(",", ":"))}
        ],
    }
    while not stop.is_set():
        try:
            async with websockets.connect(RTDS_WS, ping_interval=5, open_timeout=20) as ws:
                await ws.send(json.dumps(sub))
                while not stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    try:
                        msg = json.loads(raw)
                    except Exception:  # noqa: BLE001
                        continue
                    if not isinstance(msg, dict):
                        continue
                    payload = msg.get("payload")
                    if msg.get("topic") != TOPIC or not isinstance(payload, dict):
                        continue
                    if payload.get("symbol") != SYMBOL:
                        continue
                    rec = {
                        "kind": "twap",
                        "t": int(time.time()),
                        "ts": payload.get("timestamp"),
                        "value": payload.get("value"),
                        "full": payload.get("full_accuracy_value"),
                        "window_s": payload.get("window_s"),
                    }
                    latest_twap.clear()
                    latest_twap.update(rec)
                    out.write(json.dumps(rec) + "\n")
                    out.flush()
        except Exception as exc:  # noqa: BLE001
            out.write(json.dumps({"kind": "twap_error", "t": int(time.time()), "error": f"{type(exc).__name__}:{exc}"}) + "\n")
            out.flush()
            await asyncio.sleep(2)


async def book_task(stop: asyncio.Event, out, interval: float, qtys):
    current = None
    loop = asyncio.get_running_loop()
    while not stop.is_set():
        now = int(time.time())
        close = ((now // 900) + 1) * 900
        if current is None or current["close"] != close:
            try:
                e15 = await loop.run_in_executor(None, event_tokens, f"btc-updown-15m-{close - 900}")
                e5 = await loop.run_in_executor(None, event_tokens, f"btc-updown-5m-{close - 300}")
            except Exception as exc:  # noqa: BLE001
                out.write(json.dumps({"kind": "gamma_error", "t": now, "error": str(exc)}) + "\n")
                out.flush()
                await asyncio.sleep(interval)
                continue
            if not e5 or not e15:
                await asyncio.sleep(interval)
                continue
            current = {
                "close": close,
                "tokens": {"5up": e5["up"], "5down": e5["down"], "15up": e15["up"], "15down": e15["down"]},
            }
            out.write(json.dumps({"kind": "pair", "t": now, "close": close, "tokens": current["tokens"]}) + "\n")
            out.flush()
        row = {"kind": "book", "t": now, "close": close, "ttc": close - now, "twap": dict(latest_twap)}
        books = {}
        try:
            for key, tid in current["tokens"].items():
                books[key] = await loop.run_in_executor(None, http_get, f"{CLOB}/book?token_id={tid}")
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}:{exc}"
            out.write(json.dumps(row) + "\n")
            out.flush()
            await asyncio.sleep(interval)
            continue
        row["top"] = {}
        for k, bk in books.items():
            bp, bs = topbook(bk, "bids")
            ap, asz = topbook(bk, "asks")
            row["top"][k] = {
                "bid": str(bp) if bp is not None else None,
                "bid_sz": str(bs) if bs is not None else None,
                "ask": str(ap) if ap is not None else None,
                "ask_sz": str(asz) if asz is not None else None,
            }
        row["cost"] = {}
        for name, (a, b) in (("5up+15down", ("5up", "15down")), ("5down+15up", ("5down", "15up"))):
            entry = {}
            for q in qtys:
                q = Decimal(q)
                na, fa, qa = walk(books[a], q)
                nb, fb, qb = walk(books[b], q)
                if qa == q and qb == q:
                    entry[f"taker_{q}"] = str(((na + fa + nb + fb) / q).quantize(Decimal("0.000001")))
                else:
                    entry[f"taker_{q}"] = None
                # maker on leg a (join best bid, zero maker fee) + taker hedge on leg b
                mb, _ = topbook(books[a], "bids")
                if mb is not None and qb == q:
                    entry[f"maker_a_{q}"] = str(((mb * q + nb + fb) / q).quantize(Decimal("0.000001")))
                # maker on leg b + taker hedge on leg a
                mb2, _ = topbook(books[b], "bids")
                if mb2 is not None and qa == q:
                    entry[f"maker_b_{q}"] = str(((mb2 * q + na + fa) / q).quantize(Decimal("0.000001")))
            row["cost"][name] = entry
        out.write(json.dumps(row) + "\n")
        out.flush()
        await asyncio.sleep(interval)


async def main():
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
    interval = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    path = sys.argv[3] if len(sys.argv) > 3 else "live_gate0.jsonl"
    qtys = ("5", "20", "50")
    stop = asyncio.Event()
    with open(path, "a", encoding="utf-8") as out:
        tasks = [
            asyncio.create_task(rtds_task(stop, out)),
            asyncio.create_task(book_task(stop, out, interval, qtys)),
        ]
        await asyncio.sleep(minutes * 60)
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
