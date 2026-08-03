"""
collect.py — snapshot open BTC up/down markets, including the real order book.

Run this on a schedule (every 5 minutes). Each run appends one row per open
market to data/snapshots/YYYY-MM-DD.csv.

The order book is the point. Every cost assumption in the backtests was a guess;
these rows measure the spread you would actually have paid.

    python collect.py
"""

import argparse
import csv
import json
import os
import time
from datetime import datetime, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

ASSET = "btc"
INTERVALS = {"5m": 5, "15m": 15, "1h": 60}   # 5m dominates the sample count
DATA_DIR = "data/snapshots"

FIELDS = [
    "captured_at", "slug", "interval_min", "market_id", "yes_token",
    "window_start", "window_end", "minutes_left",
    "best_bid", "best_ask", "mid", "spread",
    "bid_size", "ask_size", "book_levels",
]

S = requests.Session()
S.headers.update({"User-Agent": "research-collector/1.0"})


def get(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = S.get(url, params=params, timeout=20)
            if r.status_code == 429:
                time.sleep(2 ** i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == retries - 1:
                return None
            time.sleep(1.5 ** i)
    return None


def current_windows(now_ts):
    """Slugs for the in-progress window of each interval, plus the next one."""
    out = []
    for label, mins in INTERVALS.items():
        step = mins * 60
        base = now_ts - (now_ts % step)
        for start in (base, base + step):
            out.append((f"{ASSET}-updown-{label}-{start}", label, mins, start))
    return out


def fetch_market(slug):
    js = get(f"{GAMMA}/events", {"slug": slug})
    if not js:
        return None
    ev = js[0] if isinstance(js, list) else js
    mkts = ev.get("markets") or []
    if not mkts:
        return None
    m = mkts[0]
    tokens = m.get("clobTokenIds")
    if isinstance(tokens, str):
        try:
            tokens = json.loads(tokens)
        except json.JSONDecodeError:
            return None
    if not tokens or len(tokens) < 2:
        return None
    return {"market_id": m.get("id"), "yes_token": tokens[0],
            "closed": bool(m.get("closed"))}


def fetch_book(token_id):
    """Top of book plus depth. This is the data the historical API never gave us."""
    js = get(f"{CLOB}/book", {"token_id": token_id})
    if not js:
        return None

    def top(side, reverse):
        levels = js.get(side) or []
        rows = []
        for lv in levels:
            try:
                rows.append((float(lv["price"]), float(lv["size"])))
            except (KeyError, TypeError, ValueError):
                continue
        if not rows:
            return None, None, 0
        rows.sort(key=lambda x: x[0], reverse=reverse)
        return rows[0][0], rows[0][1], len(rows)

    bid, bid_sz, n_bids = top("bids", True)
    ask, ask_sz, n_asks = top("asks", False)
    if bid is None or ask is None:
        return None

    return {"best_bid": bid, "best_ask": ask,
            "mid": round((bid + ask) / 2, 6),
            "spread": round(ask - bid, 6),
            "bid_size": bid_sz, "ask_size": ask_sz,
            "book_levels": n_bids + n_asks}


def sweep():
    """One pass over every open market."""
    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    os.makedirs(DATA_DIR, exist_ok=True)
    path = os.path.join(DATA_DIR, f"{now.strftime('%Y-%m-%d')}.csv")
    is_new = not os.path.exists(path)

    rows = []
    for slug, label, mins, start in current_windows(now_ts):
        m = fetch_market(slug)
        if not m or m["closed"]:
            continue
        book = fetch_book(m["yes_token"])
        if not book:
            continue

        end = start + mins * 60
        rows.append({
            "captured_at": now.isoformat(),
            "slug": slug,
            "interval_min": mins,
            "market_id": m["market_id"],
            "yes_token": m["yes_token"],
            "window_start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
            "window_end": datetime.fromtimestamp(end, timezone.utc).isoformat(),
            "minutes_left": round((end - now_ts) / 60.0, 2),
            **book,
        })
        time.sleep(0.1)

    if not rows:
        print(f"{now:%H:%M:%S}  no open markets captured")
        return 0

    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            w.writeheader()
        w.writerows(rows)

    print(f"{now:%H:%M:%S}  wrote {len(rows)} rows")
    for r in rows:
        print(f"    {r['slug']:<30} {r['minutes_left']:>6.1f}m left  "
              f"bid={r['best_bid']:.3f} ask={r['best_ask']:.3f} "
              f"spread={r['spread']:.3f}")
    return len(rows)


def main():
    """
    A scheduler that only fires every 5 minutes cannot sample a 5-minute market
    anywhere except its opening tick. So one invocation loops internally at
    minute granularity, giving 1-minute resolution from a 5-minute cron.
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=4.5,
                    help="minutes to keep sweeping (stay under the cron period)")
    ap.add_argument("--every", type=float, default=60.0,
                    help="seconds between sweeps")
    args = ap.parse_args()

    deadline = time.time() + args.duration * 60
    total = 0
    while True:
        try:
            total += sweep() or 0
        except Exception as e:            # never let one bad sweep kill the run
            print(f"  sweep failed: {e}")
        if time.time() + args.every >= deadline:
            break
        time.sleep(args.every)

    print(f"done: {total} rows this run")


if __name__ == "__main__":
    main()
