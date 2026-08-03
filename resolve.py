"""
resolve.py — fill in outcomes for markets that have closed.

Snapshots record prices; this records what actually happened. Run once a day.

    python resolve.py

Writes data/outcomes.csv, keyed by slug. Idempotent: already-resolved slugs are
skipped, so re-running is free.
"""

import csv
import glob
import json
import os
import time

import requests

GAMMA = "https://gamma-api.polymarket.com"
SNAP_DIR = "data/snapshots"
OUT_PATH = "data/outcomes.csv"
FIELDS = ["slug", "market_id", "yes_outcome", "yes_resolved", "volume", "resolved_at"]

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


def seen_slugs():
    slugs = set()
    for p in glob.glob(os.path.join(SNAP_DIR, "*.csv")):
        with open(p) as f:
            for row in csv.DictReader(f):
                if row.get("slug"):
                    slugs.add(row["slug"])
    return slugs


def already_resolved():
    if not os.path.exists(OUT_PATH):
        return set()
    with open(OUT_PATH) as f:
        return {r["slug"] for r in csv.DictReader(f) if r.get("slug")}


def resolve(slug):
    js = get(f"{GAMMA}/events", {"slug": slug})
    if not js:
        return None
    ev = js[0] if isinstance(js, list) else js
    mkts = ev.get("markets") or []
    if not mkts:
        return None
    m = mkts[0]
    if not m.get("closed"):
        return None      # still open, try again tomorrow

    prices = m.get("outcomePrices")
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    outcomes = m.get("outcomes")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except json.JSONDecodeError:
            outcomes = None
    if not prices:
        return None

    return {
        "slug": slug,
        "market_id": m.get("id"),
        "yes_outcome": (outcomes or ["?"])[0],
        "yes_resolved": float(prices[0]),   # 1.0 == first outcome won
        "volume": float(m.get("volume") or 0),
        "resolved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def main():
    os.makedirs("data", exist_ok=True)
    todo = sorted(seen_slugs() - already_resolved())
    print(f"{len(todo)} unresolved slugs")

    is_new = not os.path.exists(OUT_PATH)
    done = 0
    with open(OUT_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if is_new:
            w.writeheader()
        for i, slug in enumerate(todo, 1):
            r = resolve(slug)
            if r:
                w.writerow(r)
                done += 1
            if i % 50 == 0:
                print(f"  {i}/{len(todo)} resolved {done}")
            time.sleep(0.12)

    print(f"resolved {done} new markets -> {OUT_PATH}")


if __name__ == "__main__":
    main()
