"""
repair_csv.py
-------------
One-shot script to backfill all PENDING rows in dry_run_trades.csv
using historical Binance 15m kline data.

Fixes vs original:
  1. Direction-aware: reads 'direction' column (YES/NO).
     Falls back to candle_type for old rows: BULL_* → YES, BEAR_* → NO.
  2. Old always-NO bug removed — was inverting outcomes for YES trades.
  3. Can be imported and called programmatically for auto-run on bot start.

Fixes vs v2 (this version):
  4. frozen_ptb now written back to CSV when fallback (candle open) is used.
  5. Timestamp parsing detects +05:30 suffix before stripping — UTC-stored
     timestamps no longer get a bogus 5.5h subtraction.
  6. calendar.timegm() used instead of naive.timestamp() so the result is
     always UTC-epoch regardless of the machine's local timezone.
  7. Fallback frozen_ptb fetch (candle open) now has the same 3-attempt retry
     loop as _fetch_15m_close — a single network hiccup no longer skips the row.

Run manually:
    python repair_csv.py
    python repair_csv.py --csv path/to/other.csv
"""

import argparse
import calendar
import csv
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
IST_OFFSET_S      = 5 * 3600 + 30 * 60   # 19800 s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_ts_to_utc(ts_str: str) -> float:
    """
    Parse a timestamp string and return a UTC Unix epoch (float).

    Handles:
      "2024-01-15 14:30:45+05:30"          → IST, subtract offset
      "2024-01-15 14:30:45.123456+05:30"   → IST with microseconds
      "2024-01-15 14:30:45.123456"         → naive (assumed UTC)
      "2024-01-15 14:30:45"                → naive (assumed UTC)

    FIX (v3): detect +05:30 BEFORE stripping it so UTC-stored timestamps
    don't get a spurious 5.5h subtraction.
    FIX (v3): calendar.timegm() treats the struct as UTC regardless of the
    host machine's local timezone (avoids double-offset on IST machines).
    """
    ts_str = ts_str.strip()

    # Detect IST before we modify the string
    is_ist = "+05:30" in ts_str

    # Strip microseconds, keeping the timezone suffix if present
    if "." in ts_str:
        dot_idx  = ts_str.index(".")
        plus_idx = ts_str.find("+", dot_idx)
        if plus_idx != -1:
            ts_str = ts_str[:dot_idx] + ts_str[plus_idx:]   # "…+05:30"
        else:
            ts_str = ts_str[:dot_idx]                        # no tz suffix

    # Remove timezone suffix so strptime gets a clean "%Y-%m-%d %H:%M:%S"
    clean = ts_str.replace("+05:30", "")
    naive = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S")

    # calendar.timegm: interprets naive struct as UTC, returns UTC epoch
    utc_epoch = calendar.timegm(naive.timetuple())

    # Only subtract IST offset when the original string actually said +05:30
    return float(utc_epoch - IST_OFFSET_S) if is_ist else float(utc_epoch)


def _next_15m_boundary(ts: float) -> float:
    """Return the Unix timestamp of the next 15-minute boundary after ts."""
    return (int(ts // (15 * 60)) + 1) * (15 * 60)


def _fetch_kline(asset: str, expiry_ts: float) -> list:
    """
    Fetch the single 15m Binance kline that closes at expiry_ts.
    Returns the raw kline list [open_time, open, high, low, close, ...].
    Retries up to 3 times with 3-second backoff.
    """
    symbol   = asset.upper() + "USDT"
    start_ms = int((expiry_ts - 15 * 60) * 1000)

    for attempt in range(1, 4):
        try:
            resp = requests.get(
                BINANCE_KLINE_URL,
                params={"symbol": symbol, "interval": "15m",
                        "startTime": start_ms, "limit": 1},
                timeout=10,
            )
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                raise ValueError("Empty response from Binance")
            return rows[0]
        except Exception as e:
            if attempt < 3:
                print(f"  Retry {attempt}/3 for {symbol}: {e}")
                time.sleep(3)
            else:
                raise


def _fetch_15m_close(asset: str, expiry_ts: float) -> float:
    """Return the closing price of the 15m candle ending at expiry_ts."""
    return float(_fetch_kline(asset, expiry_ts)[4])


def _fetch_15m_open(asset: str, expiry_ts: float) -> float:
    """
    Return the opening price of the 15m candle ending at expiry_ts.
    Used as a PTB proxy for old rows that pre-date the frozen_ptb column.
    FIX (v3): now shares the same 3-attempt retry loop via _fetch_kline().
    """
    return float(_fetch_kline(asset, expiry_ts)[1])


def _infer_direction(row: dict) -> str:
    """
    Determine trade direction:
      1. 'direction' column (YES/NO) — written by new main.py.
      2. Infer from candle_type for old rows: BULL_* → YES, BEAR_* → NO.
      3. Default NO with a visible warning.
    """
    direction = row.get("direction", "").strip().upper()
    if direction in ("YES", "NO"):
        return direction

    candle = row.get("candle_type", "").upper()
    if candle.startswith("BULL"):
        return "YES"
    if candle.startswith("BEAR"):
        return "NO"

    print(f"  WARNING: Cannot infer direction for {row.get('asset')} "
          f"@ {str(row.get('timestamp', '?'))[:19]} — defaulting to NO")
    return "NO"


def _is_win(direction: str, final_price: float, frozen_ptb: float) -> bool:
    """
    Matches outcome_tracker.py exactly:
      YES wins if final price settled ABOVE the PTB.
      NO  wins if final price settled BELOW the PTB.
    Uses frozen_ptb saved at signal time, NOT candle open (except old-row fallback).
    """
    if direction == "YES":
        return final_price > frozen_ptb
    return final_price < frozen_ptb


# ---------------------------------------------------------------------------
# Main repair routine
# ---------------------------------------------------------------------------

def repair(csv_path: str = "dry_run_data.csv", silent: bool = False) -> int:
    """
    Backfill PENDING rows in csv_path using Binance historical data.
    Returns the number of rows successfully resolved.
    """
    p = Path(csv_path)
    if not p.exists():
        if not silent:
            print(f"ERROR: {csv_path} not found.")
        return 0

    with open(p, newline="") as f:
        reader = csv.DictReader(f)
        rows   = list(reader)
        fields = list(reader.fieldnames or [])

    if not fields:
        if not silent:
            print("ERROR: CSV has no headers.")
        return 0

    # ------------------------------------------------------------------
    # Ensure required columns exist (graceful upgrade for old CSV files)
    # ------------------------------------------------------------------
    if "direction" not in fields:
        idx = fields.index("target_outcome")
        fields.insert(idx, "direction")
        for row in rows:
            row.setdefault("direction", "")

    if "frozen_ptb" not in fields:
        idx = fields.index("target_outcome")
        fields.insert(idx, "frozen_ptb")
        for row in rows:
            row.setdefault("frozen_ptb", "")

    if "yes_price_at_signal" not in fields:
        idx = fields.index("target_outcome")
        fields.insert(idx, "yes_price_at_signal")
        for row in rows:
            row.setdefault("yes_price_at_signal", "")

    if "final_price" not in fields:
        fields.append("final_price")
        for row in rows:
            row.setdefault("final_price", "")

    # ------------------------------------------------------------------
    # Early exit if nothing to do
    # ------------------------------------------------------------------
    pending = [r for r in rows if r.get("target_outcome") == "PENDING"]
    if not pending:
        if not silent:
            print("No PENDING rows — nothing to do.")
        return 0

    if not silent:
        print(f"\nFound {len(pending)} PENDING rows in {csv_path}\n")

    fixed = skipped = 0

    for row in rows:
        if row.get("target_outcome") != "PENDING":
            continue

        asset  = row.get("asset", "").strip()
        ts_str = row.get("timestamp", "").strip()

        if not asset or not ts_str:
            print(f"  SKIP  row with missing asset/timestamp: {row}")
            skipped += 1
            continue

        try:
            signal_utc = _parse_ts_to_utc(ts_str)
            expiry_ts  = _next_15m_boundary(signal_utc)
            expiry_str = datetime.fromtimestamp(expiry_ts, tz=timezone.utc).strftime("%H:%M UTC")

            # Don't resolve windows that haven't closed yet
            if expiry_ts > time.time():
                if not silent:
                    print(f"  SKIP  {asset} @ {ts_str[:19]} — "
                          f"window not closed yet ({expiry_str})")
                skipped += 1
                continue

            direction = _infer_direction(row)

            # Prefer the PTB saved at signal time; fall back to candle open
            raw_ptb = row.get("frozen_ptb", "").strip()
            using_fallback_ptb = not raw_ptb
            frozen_ptb: float

            if not silent:
                print(f"  {asset} {direction} @ {ts_str[:19]} → expiry {expiry_str} ...",
                      end=" ", flush=True)

            # Fetch final (close) price — always needed
            final_price = _fetch_15m_close(asset, expiry_ts)

            if using_fallback_ptb:
                # FIX (v3): retry loop now covers this fetch too
                frozen_ptb = _fetch_15m_open(asset, expiry_ts)
                if not silent:
                    print(f"(no frozen_ptb — using candle open {frozen_ptb:.4f} as proxy) ",
                          end="", flush=True)
            else:
                frozen_ptb = float(raw_ptb)

            win           = _is_win(direction, final_price, frozen_ptb)
            outcome_label = "WIN" if win else "LOSS"
            arrow         = "▲" if final_price > frozen_ptb else "▼"

            # ----------------------------------------------------------
            # Write results back to the row
            # FIX (v3): frozen_ptb now persisted when fallback was used
            # ----------------------------------------------------------
            row["direction"]      = direction
            row["frozen_ptb"]     = f"{frozen_ptb:.6f}"   # ← was missing
            row["target_outcome"] = outcome_label
            row["final_price"]    = f"{final_price:.6f}"

            if not silent:
                print(f"ptb={frozen_ptb:.4f}  final={final_price:.4f} {arrow}  "
                      f"bet={direction}  → {outcome_label}")

            fixed += 1
            time.sleep(0.25)

        except Exception as e:
            print(f"\n  ERROR {asset} @ {ts_str[:19]}: {e}")
            skipped += 1

    # ------------------------------------------------------------------
    # Write repaired CSV back to disk (atomic-ish: overwrite in place)
    # ------------------------------------------------------------------
    with open(p, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    if not silent:
        print(f"\nDone. Fixed: {fixed}  Skipped/Errors: {skipped}")
        labeled = [r for r in rows if r.get("target_outcome") in ("WIN", "LOSS")]
        wins    = sum(1 for r in labeled if r["target_outcome"] == "WIN")
        if labeled:
            print(f"Overall: {len(labeled)} trades | {wins}W | "
                  f"WR: {wins / len(labeled) * 100:.1f}%")
    elif fixed > 0:
        print(f"[repair_csv] Auto-resolved {fixed} PENDING rows.")

    return fixed


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backfill PENDING rows in dry_run_data.csv using Binance kline history."
    )
    parser.add_argument(
        "--csv",
        default="dry_run_data.csv",
        help="Path to the CSV file (default: dry_run_data.csv)",
    )
    args = parser.parse_args()
    repair(args.csv)