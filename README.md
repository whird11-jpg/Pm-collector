# Forward data collection for Polymarket up/down markets

Polymarket's historical price API doesn't retain enough granular data to test
pricing hypotheses out-of-sample. The previous attempt produced 161 observations
where roughly 8,700 were needed. This collects forward instead.

It also records something the historical API never exposed: **the real order
book**. Every backtest so far assumed a 1¢ half-spread. These rows measure it.

## What runs

| Script | When | Does |
|---|---|---|
| `collect.py` | every 5 min | snapshots open markets + top of book |
| `resolve.py` | daily | fills in what actually happened |
| `analyze.py` | whenever | status, measured costs, the test |

`collect.py` loops internally for 4.5 minutes at 60-second intervals. A scheduler
that only fires every 5 minutes cannot otherwise sample a 5-minute contract
anywhere except its opening tick — which silently drops the markets that supply
most of the sample.

## Deploy: GitHub Actions (free, no server)

1. Create a **public** repo. This matters: public repos get unlimited Actions
   minutes, private ones get 2,000/month and this burns ~8,600. The data is
   public market data, so there's nothing to hide.
2. Push these files.
3. Actions tab → enable workflows. It starts on the next 5-minute boundary.
4. Settings → Actions → General → Workflow permissions → **Read and write**.
   Without this the commit step fails silently-ish and you collect nothing.

Check progress from your phone: the repo's commit history should show a new
`data:` commit every few minutes.

**Honest caveats.** GitHub throttles `*/5` schedules under load — expect gaps and
occasional 15-minute delays. Runs are dropped, not queued. This is fine for
statistical sampling (gaps are unrelated to price) but it is not a trading-grade
feed.

## Deploy: VPS (~$6/month, more reliable)

```bash
git clone <your-repo> && cd <your-repo>
pip install requests pandas numpy
crontab -e
```

```cron
*/5 * * * * cd /path/to/repo && python3 collect.py --duration 4.5 --every 60 >> log 2>&1
17  3 * * * cd /path/to/repo && python3 resolve.py >> log 2>&1
```

No throttling, no dropped runs. If you're going to run this for three weeks,
this is the version I'd pick.

## Reading progress

```bash
python analyze.py                 # status + measured spreads
python analyze.py --test flb      # the test, once powered
```

Expect roughly **408 usable observations/day** and **~18 days** to reach power
for a 0.015 effect. The progress bar tells you where you are. Section 1 prints
`UNDERPOWERED` until the confidence interval is narrower than the effect being
looked for — that's there so a meaningless null can't be mistaken for an answer,
which is exactly what happened last time.

## Pre-registration

`SAMPLE_FRACTION_LEFT`, `BUCKETS`, and `TARGET_EFFECT` in `analyze.py` are fixed
**before** the data exists. That's the entire point of collecting forward.

Running `--test flb` repeatedly while data accumulates and stopping when it looks
good is called peeking, and it manufactures significance from nothing. Run it
when the bar is full. Once.

If you want to test a second hypothesis later, write it down before you look, and
treat it as a separate test with its own threshold.

## What's deliberately not modelled

Queue position, latency, market impact, and the fact that consecutive BTC
contracts are one correlated bet rather than many independent ones. All of these
make live results worse than anything measured here.
