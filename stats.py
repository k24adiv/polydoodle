"""
stats.py — Per-Asset Win Rate Tracker
Reads your ptb_log CSV and outputs win rate breakdowns by:
  - Asset
  - Asset × Candle Type
  - Asset × Hour (UTC)
  - Asset × Distance bucket

Usage:
  python stats.py                        # reads ptb_log.csv by default
  python stats.py --log my_log.csv       # custom path
  python stats.py --min-samples 10       # lower threshold for small logs

Columns expected in CSV:
  timestamp, asset, dist_pct, volume, confidence, candle_type,
  hour, direction, frozen_ptb, target_outcome, final_price
"""

import argparse
import csv
import sys
from collections import defaultdict
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────────
DEFAULT_LOG     = "ptb_log.csv"
MIN_SAMPLES     = 20          # warn if fewer than this for a bucket
DIST_BUCKETS    = [           # (label, min_pct, max_pct)
    ("0.10-0.15%", 0.0010, 0.0015),
    ("0.15-0.20%", 0.0015, 0.0020),
    ("0.20-0.30%", 0.0020, 0.0030),
    ("0.30-0.50%", 0.0030, 0.0050),
    ("0.50%+",     0.0050, 9999.0),
]

# ── Helpers ────────────────────────────────────────────────────────────────────

def load_log(path: str) -> list[dict]:
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("target_outcome") not in ("WIN", "LOSS"):
                continue  # skip unresolved / paper pending
            rows.append(row)
    return rows


def wr(wins: int, total: int) -> str:
    if total == 0:
        return "  —  "
    pct = wins / total * 100
    flag = "" if total >= MIN_SAMPLES else f" ⚠️ <{MIN_SAMPLES}"
    return f"{pct:5.1f}%  ({wins}/{total}){flag}"


def dist_bucket(dist_pct: float) -> str:
    for label, lo, hi in DIST_BUCKETS:
        if lo <= dist_pct < hi:
            return label
    return "unknown"


def section(title: str):
    print(f"\n{'═'*62}")
    print(f"  {title}")
    print(f"{'═'*62}")


def table_row(label: str, wins: int, total: int, width: int = 28):
    print(f"  {label:<{width}} {wr(wins, total)}")


# ── Analysis ───────────────────────────────────────────────────────────────────

def analyse(rows: list[dict]):
    # Accumulators  {key: [wins, total]}
    by_asset        = defaultdict(lambda: [0, 0])
    by_asset_candle = defaultdict(lambda: [0, 0])
    by_asset_hour   = defaultdict(lambda: [0, 0])
    by_asset_dist   = defaultdict(lambda: [0, 0])
    by_asset_conf   = defaultdict(lambda: [0, 0])

    assets = set()

    for row in rows:
        asset   = row["asset"].strip()
        outcome = row["target_outcome"].strip()
        candle  = row.get("candle_type", "").strip()
        hour    = row.get("hour", "?").strip()
        conf    = row.get("confidence", "").strip()
        win     = 1 if outcome == "WIN" else 0

        try:
            dp = float(row["dist_pct"])
        except (ValueError, KeyError):
            dp = 0.0

        assets.add(asset)
        bucket = dist_bucket(dp)

        by_asset[asset][0]                    += win
        by_asset[asset][1]                    += 1
        by_asset_candle[(asset, candle)][0]   += win
        by_asset_candle[(asset, candle)][1]   += 1
        by_asset_hour[(asset, hour)][0]       += win
        by_asset_hour[(asset, hour)][1]       += 1
        by_asset_dist[(asset, bucket)][0]     += win
        by_asset_dist[(asset, bucket)][1]     += 1
        by_asset_conf[(asset, conf)][0]       += win
        by_asset_conf[(asset, conf)][1]       += 1

    total_trades = len(rows)
    total_wins   = sum(1 for r in rows if r["target_outcome"] == "WIN")

    # ── Overall ────────────────────────────────────────────────────────────────
    section("OVERALL")
    print(f"  Total resolved trades : {total_trades}")
    print(f"  Overall win rate      : {wr(total_wins, total_trades)}")

    # ── Per Asset ─────────────────────────────────────────────────────────────
    section("WIN RATE BY ASSET")
    for asset in sorted(assets):
        w, t = by_asset[asset]
        table_row(asset, w, t)

    # ── Asset × Candle Type ───────────────────────────────────────────────────
    section("WIN RATE BY ASSET × CANDLE TYPE")
    candle_types = sorted({k[1] for k in by_asset_candle})
    for asset in sorted(assets):
        print(f"\n  [{asset}]")
        for candle in candle_types:
            w, t = by_asset_candle[(asset, candle)]
            if t > 0:
                table_row(f"  {candle}", w, t)

    # ── Asset × Distance Bucket ───────────────────────────────────────────────
    section("WIN RATE BY ASSET × DISTANCE BUCKET")
    for asset in sorted(assets):
        print(f"\n  [{asset}]")
        for label, lo, hi in DIST_BUCKETS:
            w, t = by_asset_dist[(asset, label)]
            if t > 0:
                table_row(f"  {label}", w, t)

    # ── Asset × Hour ──────────────────────────────────────────────────────────
    section("WIN RATE BY ASSET × HOUR  (local hour from timestamp)")
    for asset in sorted(assets):
        print(f"\n  [{asset}]")
        hours = sorted(
            {k[1] for k in by_asset_hour if k[0] == asset},
            key=lambda h: int(h) if h.isdigit() else 99
        )
        for h in hours:
            w, t = by_asset_hour[(asset, h)]
            table_row(f"  hr{h:>2}", w, t)

    # ── Asset × Confidence ────────────────────────────────────────────────────
    section("WIN RATE BY ASSET × CONFIDENCE")
    for asset in sorted(assets):
        print(f"\n  [{asset}]")
        for conf in ("HIGH", "MEDIUM", "LOW"):
            w, t = by_asset_conf[(asset, conf)]
            if t > 0:
                table_row(f"  {conf}", w, t)

    # ── Actionable flags ──────────────────────────────────────────────────────
    section("ACTIONABLE FLAGS  (once ≥20 samples per bucket)")
    print(f"  ⚠️  = fewer than {MIN_SAMPLES} samples — don't tune yet\n")

    flags = []
    for (asset, candle), (w, t) in by_asset_candle.items():
        if t >= MIN_SAMPLES:
            pct = w / t * 100
            if pct < 55:
                flags.append(f"  ❌ {asset} {candle:<14}  WR={pct:.1f}%  — consider blocking")
            elif pct > 75:
                flags.append(f"  ✅ {asset} {candle:<14}  WR={pct:.1f}%  — strong edge")

    for (asset, hour), (w, t) in by_asset_hour.items():
        if t >= MIN_SAMPLES:
            pct = w / t * 100
            if pct < 50:
                flags.append(f"  ❌ {asset} hr{hour:<3}           WR={pct:.1f}%  — consider avoiding")

    if flags:
        for f in sorted(flags):
            print(f)
    else:
        print("  No buckets with ≥20 samples yet — keep accumulating trades.")

    print(f"\n{'═'*62}\n")


# ── Entry Point ────────────────────────────────────────────────────────────────

def main():
    global MIN_SAMPLES
    parser = argparse.ArgumentParser(description="POLYBOT win rate stats")
    parser.add_argument("--log",         default=DEFAULT_LOG, help="Path to CSV log")
    parser.add_argument("--min-samples", type=int, default=MIN_SAMPLES,
                        help="Min samples before a bucket is flagged")
    args = parser.parse_args()
    MIN_SAMPLES = args.min_samples

    path = Path(args.log)
    if not path.exists():
        print(f"❌ Log file not found: {path}")
        sys.exit(1)

    rows = load_log(str(path))
    if not rows:
        print("⚠️  No resolved trades (WIN/LOSS) found in log.")
        sys.exit(0)

    print(f"\n📊 POLYBOT Stats  |  Source: {path}  |  Resolved trades: {len(rows)}")
    analyse(rows)


if __name__ == "__main__":
    main()