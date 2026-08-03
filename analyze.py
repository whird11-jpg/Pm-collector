"""
analyze.py — read what has been collected and report honestly on it.

    python analyze.py                    # status + measured costs
    python analyze.py --test flb         # run the pre-registered test

Order of output is deliberate:
  1. How much data exists, and whether it is enough to answer anything.
  2. What the spread actually is — the number every backtest so far guessed at.
  3. Only then, the hypothesis test.

Section 1 is not throat-clearing. The previous attempt produced a clean-looking
null on 161 observations, where the confidence interval was three times wider than
the effect being looked for. Power comes first so that cannot happen quietly.
"""

import argparse
import glob
import math
import os

import numpy as np
import pandas as pd

SNAP_DIR = "data/snapshots"
OUT_PATH = "data/outcomes.csv"

# Pre-registered. Do not edit these to chase a result.
# Sample at a fixed FRACTION of each contract's life, not a fixed number of
# minutes. A 30-minute rule silently discards every 5m and 15m market, which is
# where almost all the sample size lives.
SAMPLE_FRACTION_LEFT = 0.50
FRACTION_TOLERANCE = 0.15
BUCKETS = [0.0, 0.05, 0.10, 0.15, 0.25, 0.35, 0.50,
           0.65, 0.75, 0.85, 0.90, 0.95, 1.0]
TARGET_EFFECT = 0.015      # the smallest bias worth trading


def load():
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "*.csv")))
    if not files:
        raise SystemExit("No snapshots yet. Is the collector running?")
    snaps = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
    snaps["captured_at"] = pd.to_datetime(snaps["captured_at"], utc=True,
                                          format="mixed")
    outcomes = (pd.read_csv(OUT_PATH) if os.path.exists(OUT_PATH)
                else pd.DataFrame(columns=["slug", "yes_resolved"]))
    return snaps, outcomes


def required_n(effect, p=0.5, alpha=0.05, power=0.80):
    """Observations needed to detect `effect` in a proportion, two-sided."""
    z_a, z_b = 1.959964, 0.841621
    return int(math.ceil(((z_a + z_b) ** 2) * p * (1 - p) / (effect ** 2)))


def status(snaps, outcomes):
    print("=" * 66)
    print("1. COLLECTION STATUS")
    print("=" * 66)
    span_h = (snaps.captured_at.max() - snaps.captured_at.min()).total_seconds() / 3600
    print(f"  snapshot rows      : {len(snaps):,}")
    by_int = snaps.groupby("interval_min").slug.nunique()
    print(f"  markets by interval: "
          + ", ".join(f"{int(k)}m={v}" for k, v in by_int.items()))
    print(f"  distinct markets   : {snaps.slug.nunique():,}")
    print(f"  collecting since   : {snaps.captured_at.min():%Y-%m-%d %H:%M} UTC")
    print(f"  span               : {span_h/24:.1f} days")
    print(f"  resolved outcomes  : {len(outcomes):,}")

    snaps = snaps.copy()
    snaps["frac_left"] = snaps.minutes_left / snaps.interval_min
    usable = snaps[(snaps.frac_left - SAMPLE_FRACTION_LEFT).abs() <= FRACTION_TOLERANCE]
    usable = usable.sort_values(
        by="frac_left", key=lambda s: (s - SAMPLE_FRACTION_LEFT).abs()
    ).drop_duplicates("slug")
    usable = usable[usable.slug.isin(set(outcomes.slug))] if len(outcomes) else usable.iloc[0:0]
    need = required_n(TARGET_EFFECT)

    print(f"\n  usable observations: {len(usable):,}  "
          f"(snapshot near {SAMPLE_FRACTION_LEFT:.0%} through AND resolved)")
    print(f"  needed for {TARGET_EFFECT:.3f} effect: {need:,}")

    if len(usable):
        ci = 1.96 * math.sqrt(0.25 / len(usable))
        print(f"  current 95% CI half-width: +/-{ci:.4f}  "
              f"(target effect is {TARGET_EFFECT:.4f})")
        if ci > TARGET_EFFECT:
            print(f"  >>> UNDERPOWERED. CI is {ci/TARGET_EFFECT:.1f}x the effect size.")
            print(f"  >>> A null result here would mean nothing.")

    if len(usable) < need:
        per_day = len(usable) / max(span_h / 24, 0.04) if len(usable) else 0
        if per_day > 0:
            print(f"\n  rate               : {per_day:.0f} usable obs/day")
            print(f"  est. days to target: {(need - len(usable)) / per_day:.0f}")
        pct = 100 * len(usable) / need
        bar = "#" * int(pct / 2.5) + "." * (40 - int(pct / 2.5))
        print(f"\n  [{bar}] {pct:.1f}%")
    else:
        print("\n  >>> Sufficient data. The test below is meaningful.")
    return usable


def costs(snaps):
    print("\n" + "=" * 66)
    print("2. MEASURED COSTS  (every backtest so far guessed at these)")
    print("=" * 66)
    s = snaps[snaps.spread.notna() & (snaps.spread >= 0)]
    if s.empty:
        print("  no book data yet")
        return
    print(f"  median spread      : {s.spread.median():.4f}")
    print(f"  mean spread        : {s.spread.mean():.4f}")
    print(f"  90th percentile    : {s.spread.quantile(0.90):.4f}")
    print(f"  implied half-spread: {s.spread.median()/2:.4f}   "
          f"(backtests assumed 0.0100)")

    print("\n  spread by price level — this is where longshot strategies die:")
    s = s.copy()
    s["px_bucket"] = pd.cut(s.mid, [0, .05, .15, .35, .65, .85, .95, 1.0])
    tab = s.groupby("px_bucket", observed=True).agg(
        n=("spread", "size"), med_spread=("spread", "median"),
        med_mid=("mid", "median")).round(4)
    tab["round_trip_pct"] = (tab.med_spread / tab.med_mid * 100).round(1)
    print(tab.to_string())
    print("\n  round_trip_pct = the spread as a share of the contract's price.")
    print("  Anything above ~5% needs a very large edge to overcome.")

    print("\n  spread by time remaining:")
    s["t_bucket"] = pd.cut(s.minutes_left, [0, 2, 5, 10, 20, 40, 70])
    print(s.groupby("t_bucket", observed=True).agg(
        n=("spread", "size"), med_spread=("spread", "median")).round(4).to_string())


def wilson(k, n, z=1.96):
    if n == 0:
        return (np.nan, np.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def test_flb(usable, outcomes, snaps):
    print("\n" + "=" * 66)
    print("3. FAVOURITE-LONGSHOT BIAS")
    print("=" * 66)
    if usable.empty:
        print("  no usable observations yet")
        return

    obs = usable.merge(outcomes[["slug", "yes_resolved"]], on="slug", how="inner")
    obs = obs[(obs.mid > 0.005) & (obs.mid < 0.995)]
    obs = obs.drop_duplicates("slug")
    if len(obs) < 30:
        print(f"  only {len(obs)} observations — not running the test yet")
        return

    obs = obs.rename(columns={"mid": "price"})
    obs["bucket"] = pd.cut(obs.price, BUCKETS)
    rows = []
    for b, g in obs.groupby("bucket", observed=True):
        n, k = len(g), int(g.yes_resolved.sum())
        lo, hi = wilson(k, n)
        imp = g.price.mean()
        rows.append({"bucket": str(b), "n": n, "implied": round(imp, 4),
                     "actual": round(k / n, 4), "ci_lo": round(lo, 4),
                     "ci_hi": round(hi, 4), "gap": round(k / n - imp, 4),
                     "sig": "YES" if not (lo <= imp <= hi) else "no"})
    tab = pd.DataFrame(rows)
    print(tab.to_string(index=False))

    # Symmetric effect, so the statistic must be signed by the hypothesis.
    d = (obs.yes_resolved - obs.price) * np.sign(obs.price - 0.5)
    z = d.mean() / d.std(ddof=1) * math.sqrt(len(d))
    med_half = snaps.spread.median() / 2 if snaps.spread.notna().any() else 0.010

    print(f"\n  n                  : {len(obs):,}")
    print(f"  FLB-signed z       : {z:+.2f}   (> +2 supports it)")
    print(f"  mean signed gap    : {d.mean():+.4f}")
    print(f"  measured cost floor: {med_half:.4f}  (from real books, not assumed)")
    print("\n  " + "-" * 60)
    if z <= 2:
        print("  VERDICT: no bias detected.")
        ci = 1.96 * d.std(ddof=1) / math.sqrt(len(d))
        print(f"  CI half-width {ci:.4f} vs target {TARGET_EFFECT:.4f} — "
              f"{'conclusive' if ci < TARGET_EFFECT else 'STILL UNDERPOWERED'}")
    elif d.mean() <= med_half:
        print("  VERDICT: bias is real but smaller than the measured spread.")
        print("  Not tradable.")
    else:
        print("  VERDICT: bias exceeds measured costs. Hold out a second window")
        print("  and confirm before acting on it.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", choices=["flb"], default=None)
    args = ap.parse_args()

    snaps, outcomes = load()
    usable = status(snaps, outcomes)
    costs(snaps)
    if args.test == "flb":
        test_flb(usable, outcomes, snaps)
    else:
        print("\n(run with --test flb once the progress bar is full)")


if __name__ == "__main__":
    main()
