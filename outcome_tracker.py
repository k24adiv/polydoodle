"""
outcome_tracker.py
------------------
Deferred win/loss auditor for POLYBOT dry-run trades.

Usage in main.py / monitor_and_decide():
    from outcome_tracker import schedule_outcome_check

    # After calling log_dry_trade(...)
    schedule_outcome_check(
        asset      = asset,          # "BTC" | "ETH" | "SOL" | "XRP"
        frozen_ptb = frozen_ptb,     # PTB captured at signal time
        signal     = direction,      # "YES" | "NO"
        signal_ts  = time.time(),    # unix timestamp at signal fire
        record_outcome_fn = _record_outcome,  # your circuit-breaker hook
    )
"""

import csv
import os
import threading
import time
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── config ────────────────────────────────────────────────────────────────────
DRY_RUN_CSV      = "dry_run_data.csv"   # must match log_dry_trade() in main.py
BINANCE_KLINE_URL = "https://api.binance.com/api/v3/klines"
SETTLE_DELAY_S   = 8    # wait this long after expiry before fetching (API lag)
FETCH_RETRIES    = 3
FETCH_RETRY_WAIT = 5    # seconds between retries

# ── internal state ─────────────────────────────────────────────────────────────
_csv_lock = threading.Lock()


# ── helpers ────────────────────────────────────────────────────────────────────

def _next_15m_boundary(ts: float) -> float:
    """Return the next 15-minute UTC boundary strictly after ts."""
    interval = 15 * 60
    return (int(ts // interval) + 1) * interval


def _fetch_15m_close(asset: str, expiry_ts: float) -> float:
    """
    Fetch the closing price of the 15m candle that ended at expiry_ts.
    The candle's startTime = expiry_ts - 900s.
    Retries up to FETCH_RETRIES times on failure.
    """
    symbol    = asset.upper() + "USDT"
    start_ms  = int((expiry_ts - 15 * 60) * 1000)
    last_exc  = None

    for attempt in range(1, FETCH_RETRIES + 1):
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
                raise ValueError(f"Empty kline response for {symbol} @ {start_ms}")
            return float(rows[0][4])   # index 4 = close price
        except Exception as exc:
            last_exc = exc
            if attempt < FETCH_RETRIES:
                time.sleep(FETCH_RETRY_WAIT)

    raise RuntimeError(f"Binance fetch failed after {FETCH_RETRIES} attempts: {last_exc}")


def _update_csv(asset: str, signal_ts_prefix: str, outcome_label: str,
                final_price: float, csv_path: str = DRY_RUN_CSV):
    """
    Find the PENDING row matching (asset, timestamp prefix) and update it.
    Thread-safe via _csv_lock. Adds final_price column if not present.
    """
    with _csv_lock:
        if not Path(csv_path).exists():
            print(f"[AUTO-AUDIT] WARNING: {csv_path} not found — outcome not saved.")
            return

        with open(csv_path, newline="") as f:
            reader  = csv.DictReader(f)
            rows    = list(reader)
            fields  = list(reader.fieldnames or [])

        # add final_price column if this is the first time
        if "final_price" not in fields:
            fields.append("final_price")
            for row in rows:
                row.setdefault("final_price", "")

        updated = False
        for row in rows:
            ts_match     = row.get("timestamp", "").startswith(signal_ts_prefix)
            asset_match  = row.get("asset", "") == asset
            pending      = row.get("target_outcome", "") == "PENDING"
            if ts_match and asset_match and pending:
                row["target_outcome"] = outcome_label
                row["final_price"]    = f"{final_price:.6f}"
                updated = True
                break   # update only the first matching PENDING row

        if not updated:
            print(f"[AUTO-AUDIT] WARNING: No PENDING row found for {asset} @ {signal_ts_prefix}")
            return

        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


# ── core audit thread ──────────────────────────────────────────────────────────

def _audit_worker(
    asset: str,
    frozen_ptb: float,
    signal: str,
    signal_ts: float,
    record_outcome_fn,
    csv_path: str,
):
    """Runs in a daemon thread. Sleeps until expiry, then fetches and records."""
    expiry_ts   = _next_15m_boundary(signal_ts)
    wait_secs   = (expiry_ts + SETTLE_DELAY_S) - time.time()
    expiry_str  = datetime.fromtimestamp(expiry_ts, tz=timezone.utc).strftime("%H:%M:%S UTC")

    if wait_secs > 0:
        time.sleep(wait_secs)

    try:
        final_price = _fetch_15m_close(asset, expiry_ts)

        if signal == "YES":
            is_win = final_price > frozen_ptb
        else:                                  # "NO"
            is_win = final_price < frozen_ptb

        outcome_label = "WIN" if is_win else "LOSS"
        direction     = "▲" if final_price > frozen_ptb else "▼"

        # timestamp prefix must match how log_dry_trade() writes it:
        # datetime.now(IST).isoformat() → "2026-04-29 12:28:51.XXXXXX+05:30"
        # We match on the first 19 chars: "2026-04-29 12:28:51"
        IST_OFFSET = timezone(timedelta(hours=5, minutes=30))
        ts_prefix = datetime.fromtimestamp(signal_ts, tz=IST_OFFSET) \
                             .strftime("%Y-%m-%d %H:%M:%S")

        _update_csv(asset, ts_prefix, outcome_label, final_price, csv_path)

        if record_outcome_fn:
            record_outcome_fn(outcome_label)

        print(
            f"[AUTO-AUDIT] {asset:>3} {signal:<3} | "
            f"PTB={frozen_ptb:.4f}  Final={final_price:.4f} {direction} | "
            f"Expiry={expiry_str} | {outcome_label}"
        )

    except Exception as exc:
        print(f"[AUTO-AUDIT] {asset} outcome check FAILED: {exc}")


# ── public API ─────────────────────────────────────────────────────────────────

def schedule_outcome_check(
    asset: str,
    frozen_ptb: float,
    signal: str,
    signal_ts: float,
    record_outcome_fn=None,
    csv_path: str = DRY_RUN_CSV,
):
    """
    Fire-and-forget: spawns a daemon thread that waits until the 15m window
    closes, then fetches the Binance settlement price and updates the CSV.

    Args:
        asset              : "BTC" | "ETH" | "SOL" | "XRP"
        frozen_ptb         : PTB value captured at signal time
        signal             : "YES" or "NO"
        signal_ts          : unix timestamp when the signal fired (time.time())
        record_outcome_fn  : optional callable(label) — your circuit-breaker hook
                             Pass None for live (CB handled by fill flow).
        csv_path           : path to the CSV file (default: dry_run_data.csv)
    """
    t = threading.Thread(
        target=_audit_worker,
        args=(asset, frozen_ptb, signal, signal_ts, record_outcome_fn, csv_path),
        daemon=True,
        name=f"audit-{asset}-{int(signal_ts)}",
    )
    t.start()
