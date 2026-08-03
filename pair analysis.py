"""
pair_analysis.py — can you actually assemble Up + Down for less than $1?

Four of the five bots in that article reduce to one claim: buy Up at one moment
and Down at another so the pair costs under $1, then collect $1 at settlement.

The arithmetic that makes or breaks it:

    Buying NO is selling YES, so   ask_no = 1 - bid_yes
    Both legs at the same instant: ask_yes + (1 - bid_yes) = 1 + spread

So a simultaneous pair always loses exactly the spread. It only works if the
price moves THROUGH the spread between your two entries:

    pair cost = min(ask) + 1 - max(bid) = 1 + (min_ask - max_bid)

Order doesn't matter — each leg just needs to happen sometime while the market
is open. So the whole strategy reduces to: did the price range exceed the
spread? That is a long-volatility bet, not an arbitrage.

    python pair_analysis.py

Reads the same data/ the collector writes.
"""

import argparse
import glob
import os

import numpy as np
import pandas as pd

SNAP_DIR = "data/snapshots"
OUT_PATH = "data/outcomes.csv"

TAKER_FEE = 0.0        # set from docs.polymarket.us/fees
MIN_TICKS = 4          # markets with fewer snapshots tell us nothing


def load():
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "*.csv")))
    if not files:
        raise SystemExit("No snapshots found. Is the collector running?")
    s = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    s["captured_at"] = pd.to_datetime(s["captured_at"], utc=True, format="mixed")
    s = s.dropna(subset=["best_bid", "best_ask"]).sort_values("captured_at")
    o = (pd.read_csv(OUT_PATH) if os.path.exists(OUT_PATH)
         else pd.DataFrame(columns=["slug", "yes_resolved"]))
    return s, o


# ---------------------------------------------------------------------------
# 1. Hindsight bound
# ---------------------------------------------------------------------------

def hindsight(snaps):
    """
    Best pair cost achievable with perfect foresight, per market. Nobody can
    actually trade this — it buys each leg at the single best print of the whole
    interval. It is a ceiling: if THIS loses money, nothing causal wins.
    """
    g = snaps.groupby("slug").agg(
        ticks=("best_bid", "size"),
        min_ask=("best_ask", "min"),
        max_bid=("best_bid", "max"),
        interval=("interval_min", "first"),
        med_spread=("spread", "median"),
    )
    g = g[g.ticks >= MIN_TICKS]
    if g.empty:
        return g
    g["pair_cost"] = 1 + g.min_ask - g.max_bid
    g["profit"] = 1 - g.pair_cost - TAKER_FEE
    g["range"] = g.max_bid - g.min_ask
    return g


# ---------------------------------------------------------------------------
# 2. Causal rule — what you could actually have traded
# ---------------------------------------------------------------------------

def causal(snaps, outcomes, target=0.005):
    """
    A rule that only ever looks backwards:
      leg 1: buy YES at the first tick where ask is the lowest seen so far
      leg 2: buy NO once bid >= entry_ask + target  (completing the pair under $1)
      if leg 2 never fires, the position settles directional at expiry

    That unfinished-leg case is the failure mode the article names for four of
    its five bots, so it has to be priced, not ignored.
    """
    res = dict(zip(outcomes.slug, outcomes.yes_resolved)) if len(outcomes) else {}
    rows = []

    for slug, g in snaps.groupby("slug"):
        g = g.sort_values("captured_at")
        if len(g) < MIN_TICKS:
            continue
        asks = g.best_ask.to_numpy()
        bids = g.best_bid.to_numpy()

        # leg 1: first strictly-improving ask (cannot peek forward)
        entry_i, entry_ask = 0, asks[0]
        for i in range(1, len(asks)):
            if asks[i] < entry_ask:
                entry_i, entry_ask = i, asks[i]
                break

        # leg 2: first later bid high enough to complete under $1
        exit_i = None
        for j in range(entry_i + 1, len(bids)):
            if bids[j] >= entry_ask + target:
                exit_i = j
                break

        if exit_i is not None:
            cost = entry_ask + (1 - bids[exit_i])
            pnl = 1 - cost - TAKER_FEE
            completed = True
        else:
            # stuck long YES at entry_ask; settles at 1 or 0
            outcome = res.get(slug)
            if outcome is None:
                continue                      # unresolved, cannot score it
            pnl = (1.0 if outcome == 1.0 else 0.0) - entry_ask - TAKER_FEE
            cost, completed = entry_ask, False

        rows.append({"slug": slug, "completed": completed,
                     "cost": cost, "pnl": pnl,
                     "interval": g.interval_min.iloc[0]})

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------

def report(snaps, outcomes, target):
    print("=" * 68)
    print("PAIR ARITHMETIC — can Up + Down be assembled below $1?")
    print("=" * 68)
    print(f"  snapshots        : {len(snaps):,}")
    print(f"  markets          : {snaps.slug.nunique():,}")
    print(f"  resolved         : {len(outcomes):,}")
    print(f"  median spread    : {snaps.spread.median():.4f}  "
          f"<- a simultaneous pair costs 1 + this")

    h = hindsight(snaps)
    if h.empty:
        print(f"\n  No market has {MIN_TICKS}+ snapshots yet. Let it collect longer.")
        return

    print("\n" + "-" * 68)
    print("1. PERFECT-HINDSIGHT CEILING")
    print("-" * 68)
    print(f"  markets with enough ticks : {len(h):,}")
    print(f"  median pair cost          : ${h.pair_cost.median():.4f}")
    print(f"  markets pairing below $1  : {(h.pair_cost < 1).mean():.1%}")
    print(f"  median profit per pair    : ${h.profit.median():+.4f}")
    print(f"  mean profit per pair      : ${h.profit.mean():+.4f}")

    print("\n  by contract length:")
    by = h.groupby("interval").agg(
        n=("profit", "size"), med_cost=("pair_cost", "median"),
        pct_under_1=("pair_cost", lambda x: (x < 1).mean()),
        med_profit=("profit", "median")).round(4)
    print(by.to_string())

    if h.profit.median() <= 0:
        print("\n  >>> Even with perfect foresight the median pair loses money.")
        print("  >>> No causal strategy can beat a ceiling that is already negative.")
        print("  >>> The price simply does not range further than the spread.")

    print("\n" + "-" * 68)
    print("2. CAUSAL RULE (backwards-looking only)")
    print("-" * 68)
    c = causal(snaps, outcomes, target=target)
    if c.empty:
        print("  not enough resolved markets to score the rule yet")
    else:
        done = c[c.completed]
        stuck = c[~c.completed]
        print(f"  markets traded       : {len(c):,}")
        print(f"  pairs completed      : {len(done):,} ({len(done)/len(c):.1%})")
        print(f"  left directional     : {len(stuck):,} ({len(stuck)/len(c):.1%})")
        if len(done):
            print(f"  avg cost when paired : ${done.cost.mean():.4f}")
            print(f"  avg profit when paired: ${done.pnl.mean():+.4f}")
        if len(stuck):
            print(f"  avg P&L when stuck   : ${stuck.pnl.mean():+.4f}  "
                  f"(this is the risk the article calls the main challenge)")
        print(f"\n  OVERALL per market   : ${c.pnl.mean():+.4f}")
        se = c.pnl.std(ddof=1) / np.sqrt(len(c)) if len(c) > 1 else np.nan
        if len(c) > 1:
            print(f"  95% CI               : "
                  f"${c.pnl.mean() - 1.96*se:+.4f} to ${c.pnl.mean() + 1.96*se:+.4f}")
            if c.pnl.mean() - 1.96 * se > 0:
                print("  VERDICT: positive and significant. Worth a second window.")
            elif c.pnl.mean() + 1.96 * se < 0:
                print("  VERDICT: reliably negative. The strategy loses.")
            else:
                print("  VERDICT: indistinguishable from zero. More data needed.")

    print("\n" + "=" * 68)
    print("LIMITS OF THIS MEASUREMENT — read before concluding anything")
    print("=" * 68)
    print("  Snapshots are ~1/minute. Real bots see every tick, so oscillation")
    print("  between our samples is invisible here. That means this UNDERSTATES")
    print("  the opportunity, and a null is weaker evidence than the")
    print("  longshot test was.")
    print()
    print("  It also assumes taking liquidity. Posting passive limit orders and")
    print("  getting filled would beat these numbers — at the cost of not")
    print("  controlling when, or whether, you get filled at all.")
    print()
    print("  The hindsight ceiling has neither caveat: it is an upper bound on")
    print("  what any strategy could extract from the prices we observed.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=float, default=0.005,
                    help="required edge before completing the second leg")
    args = ap.parse_args()
    snaps, outcomes = load()
    report(snaps, outcomes, args.target)


if __name__ == "__main__":
    main()
