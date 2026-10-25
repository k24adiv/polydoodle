"""
main.py — Multi-Asset Polymarket Sniper Bot Orchestrator


import os
import sys
import time
import logging
import threading
import requests
import csv
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from web3 import Web3

# ── Load env FIRST so all subsequent imports see the values ───────────────────
load_dotenv()

# ── Local imports ─────────────────────────────────────────────────────────────
from ptb_scanner    import scan_and_lock_ptb_multi, ASSETS, PRECISION_MAP, ptb_is_tradeable
from signal_engine  import classify_candle, get_signal
from predictor      import Predictor
from live_executor  import (execute_sequential_multi_asset, get_yes_token_id,
                            fetch_yes_price, fetch_yes_price_raw, check_vpn_once)
from check_trades   import has_open_position
from auto_redeem    import redeem_after_win, reconcile_trade, reconcile_pending_log
from notifier       import notify, notify_error
from outcome_tracker import schedule_outcome_check
from asset_score    import score_manager, init_score_manager
from repair_csv     import repair as repair_pending_rows

# ═══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════════════════

STAKE_USD        = 1.50   # per-trade stake (USD)
SCAN_DURATION    = 60
SCAN_INTERVAL    = 2.0
MONITOR_INTERVAL      = 5
MONITOR_INTERVAL_FAST = 1   # 1s polling for the first 60s of a window
CLOCK_DRIFT_SECS = 2
WINDOW_SECONDS   = 900

# Per-asset minimum 24h volume (USDT).  Below this the market is too thin
# for reliable momentum signals.
MIN_VOLUME = {
    "ETH": 400_000_000,   # $400M
    "SOL":  80_000_000,   # $80M
    "XRP":  50_000_000,   # $50M
}

IST = timezone(timedelta(hours=5, minutes=30))

BINANCE_PRICE_URL  = "https://api.binance.com/api/v3/ticker/price"
BINANCE_VOLUME_URL = "https://api.binance.com/api/v3/ticker/24hr"
BINANCE_KLINE_URL  = "https://api.binance.com/api/v3/klines"
SYMBOL_MAP         = {"ETH": "ETHUSDT",
                      "SOL": "SOLUSDT", "XRP": "XRPUSDT"}


# ═══════════════════════════════════════════════════════════════════════════════
# ML TRAINING DATA LOGGER
# ═══════════════════════════════════════════════════════════════════════════════

def log_dry_trade(asset, dist, vol, conf, candle, hour, direction, frozen_ptb=None, yes_price=None):
    """
    Log a trade signal to dry_run_data.csv (ML training data) and, in dry-run
    mode only, to ptb_log.json as a PENDING entry for the reconciler.

    FIX (Bug 16): capture time.time() once to avoid the double-call race where
    a 900s boundary between the two calls gives the wrong window slug.

    FIX (Bug 14): in LIVE mode, skip the ptb_log.json write. live_executor
    already wrote its own entry (with order_id, size, price, response). Writing
    a second PENDING entry here creates a duplicate that reconcile_trade matches
    first, leaving the executor's authoritative entry unpatched forever.
    In DRY RUN mode, no executor entry exists, so the write still happens.
    """
    file_exists = os.path.isfile('dry_run_data.csv')
    try:
        with open('dry_run_data.csv', 'a', newline='') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(['timestamp', 'asset', 'dist_pct', 'volume',
                                 'confidence', 'candle_type', 'hour', 'direction',
                                 'frozen_ptb', 'yes_price_at_signal', 'target_outcome'])
            writer.writerow([
                datetime.now(IST), asset, dist, vol, conf, candle, hour, direction,
                f'{frozen_ptb:.6f}' if frozen_ptb is not None else '',
                f'{yes_price:.4f}' if yes_price is not None else '',
                'PENDING',
            ])
    except Exception as e:
        # A disk error (full disk, bad permissions, file lock) must not crash
        # the bot mid-window. Log and continue — the trade still executes.
        alog("error", "SYS", f"log_dry_trade: CSV write failed — {e}")

    # FIX (Bug 14): only write ptb_log.json entry in dry-run mode.
    # In live mode, live_executor's entry is already in ptb_log.json and serves
    # as the record. Writing a second PENDING entry here creates the split-entry
    # bug where reconcile_trade patches the wrong object and the other stays
    # PENDING forever.
    if _is_live():
        return   # live_executor entry is authoritative — skip duplicate write

    # DRY RUN path: write a PENDING entry so reconcile_trade can patch it later.
    import time as _time
    import json as _json

    # FIX (Bug 16): capture _now once — avoids the rare race where two calls to
    # int(_time.time()) straddle a 900s boundary and produce the wrong slug.
    _now = int(_time.time())
    window_start = _now - (_now % 900)
    market_slug  = f"{asset.lower()}-updown-15m-{window_start}"
    now_ts       = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    log_entry = {
        "market_slug":     market_slug,
        "asset":           asset,
        "direction":       direction,
        "dist_pct":        dist,
        "candle_type":     candle,
        "frozen_ptb":      frozen_ptb,
        "yes_price":       yes_price,
        "filled_at":       now_ts,
        "settled_outcome": "PENDING",
    }
    try:
        log_path = "ptb_log.json"
        existing = []
        if os.path.exists(log_path):
            with open(log_path, "r") as f:
                existing = _json.load(f)
        existing.append(log_entry)
        tmp = log_path + ".tmp"
        with open(tmp, "w") as f:
            _json.dump(existing, f, indent=2)
        os.replace(tmp, log_path)
    except Exception as e:
        alog("error", "SYS", f"log_dry_trade: ptb_log.json write failed — {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# PERSISTENT AUDIT LOGGER
# ═══════════════════════════════════════════════════════════════════════════════

def _setup_audit_log() -> logging.Logger:
    audit = logging.getLogger("bot_audit")
    audit.setLevel(logging.DEBUG)
    if audit.handlers:
        return audit

    fh = logging.FileHandler("bot_audit.log", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    ))

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S"
    ))

    audit.addHandler(fh)
    audit.addHandler(ch)
    audit.propagate = False
    return audit


audit = _setup_audit_log()


def alog(level: str, asset: str, message: str) -> None:
    ist_now = datetime.now(IST).strftime("%H:%M:%S IST")
    full    = f"[{asset:<3}] [{ist_now}] {message}"
    getattr(audit, level.lower(), audit.info)(full)


# ═══════════════════════════════════════════════════════════════════════════════
# ENVIRONMENT HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def _is_live() -> bool:
    return os.getenv("LIVE_ENABLED", "false").strip().lower() == "true"


def _mode_label() -> str:
    return "🔴 LIVE" if _is_live() else "📝 DRY RUN"


# ═══════════════════════════════════════════════════════════════════════════════
# ON-CHAIN BALANCE FETCHER
# ═══════════════════════════════════════════════════════════════════════════════

def get_real_usdc_balance() -> float:
    """Fetches the pUSD balance directly from the Polygon blockchain.
    V2 MIGRATION: collateral is now pUSD (0xC011a7E1...), not USDC.e (0x2791...).
    """
    try:
        rpc_url = os.getenv("RPC_URL", "https://polygon-bor-rpc.publicnode.com")
        w3 = Web3(Web3.HTTPProvider(rpc_url))

        private_key = os.getenv("POLYGON_PRIVATE_KEY")
        if not private_key:
            alog("warning", "SYS", "POLYGON_PRIVATE_KEY not set — cannot fetch on-chain balance")
            return 0.0

        account        = w3.eth.account.from_key(private_key)
        wallet_address = account.address

        # V2: pUSD is the collateral token for all V2 markets
        pusd_address = w3.to_checksum_address("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")
        erc20_abi = [{
            "constant": True,
            "inputs":  [{"name": "_owner", "type": "address"}],
            "name":    "balanceOf",
            "outputs": [{"name": "balance", "type": "uint256"}],
            "type":    "function",
        }]

        pusd_contract = w3.eth.contract(address=pusd_address, abi=erc20_abi)
        raw_balance   = pusd_contract.functions.balanceOf(wallet_address).call()
        balance       = raw_balance / (10 ** 6)
        alog("info", "SYS", f"On-chain pUSD balance: ${balance:.2f} | wallet={wallet_address}")
        return balance

    except Exception as e:
        print(f"\n ⚠️ WARNING: Could not fetch on-chain balance ({e}). Defaulting to $0.00.\n")
        alog("warning", "SYS", f"Balance fetch failed: {e}")
        return 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# CIRCUIT BREAKER STATE  (module-level so it survives window iterations)
# ═══════════════════════════════════════════════════════════════════════════════

CIRCUIT_BREAKER_LIMIT = 4
_consecutive_losses   = 0   # reset on WIN or SKIP; incremented on every LOSS window
_cb_lock              = threading.Lock()   # guards _consecutive_losses across threads


def _record_outcome(outcome: str, asset: str = None) -> None:
    """
    Call with 'WIN', 'LOSS', or 'SKIP' after each window.
    Thread-safe — may be called from background outcome-tracker threads.

    If `asset` is provided, also updates the adaptive asset score so the
    scorer reflects per-asset performance rather than global outcomes.
    """
    global _consecutive_losses
    with _cb_lock:
        if outcome == "LOSS":
            _consecutive_losses += 1
            alog("warning", "SYS",
                 f"Consecutive losses: {_consecutive_losses}/{CIRCUIT_BREAKER_LIMIT}")
            if asset:
                score_manager.on_loss(asset)
                score = score_manager.get_score(asset)
                alog("warning", asset,
                     f"Asset score after LOSS: {score:.3f} "
                     f"{'(SUSPENDED)' if not score_manager.is_tradeable(asset) else ''}")
        elif outcome in ("WIN", "SKIP"):
            if _consecutive_losses > 0:
                alog("info", "SYS",
                     f"Streak reset on {outcome} (was {_consecutive_losses})")
            _consecutive_losses = 0
            if asset:
                if outcome == "WIN":
                    score_manager.on_win(asset)
                else:
                    score_manager.on_skip(asset)
                score = score_manager.get_score(asset)
                alog("info", asset, f"Asset score after {outcome}: {score:.3f}")


def _circuit_breaker_tripped() -> bool:
    with _cb_lock:
        return _consecutive_losses >= CIRCUIT_BREAKER_LIMIT


# ═══════════════════════════════════════════════════════════════════════════════
# WINDOW COUNTER PERSISTENCE
# ═══════════════════════════════════════════════════════════════════════════════

_WINDOW_COUNTER_FILE = "window_counter.txt"


def _load_window_counter() -> int:
    try:
        with open(_WINDOW_COUNTER_FILE, "r") as f:
            return int(f.read().strip())
    except Exception:
        return 0


def _save_window_counter(n: int) -> None:
    try:
        with open(_WINDOW_COUNTER_FILE, "w") as f:
            f.write(str(n))
    except Exception as e:
        alog("warning", "SYS", f"Could not persist window counter: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# BINANCE DATA HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def get_binance_data(asset: str, retries: int = 3, backoff: float = 2.0) -> dict:
    """Fetch current price + 24h volume from Binance with retry."""
    symbol = SYMBOL_MAP.get(asset)
    if not symbol:
        return {"price": None, "volume": None}

    price = volume = None

    for attempt in range(1, retries + 1):
        try:
            rp = requests.get(BINANCE_PRICE_URL, params={"symbol": symbol}, timeout=5)
            rp.raise_for_status()
            price = float(rp.json()["price"])
            break
        except requests.exceptions.Timeout:
            alog("warning", asset, f"Binance price timeout (attempt {attempt}/{retries})")
        except Exception as e:
            alog("warning", asset, f"Binance price error attempt {attempt}: {e}")
        if attempt < retries:
            time.sleep(backoff * attempt)

    for attempt in range(1, retries + 1):
        try:
            rv = requests.get(BINANCE_VOLUME_URL, params={"symbol": symbol}, timeout=5)
            rv.raise_for_status()
            volume = float(rv.json()["quoteVolume"])
            break
        except requests.exceptions.Timeout:
            alog("warning", asset, f"Binance volume timeout (attempt {attempt}/{retries})")
        except Exception as e:
            alog("warning", asset, f"Binance volume error attempt {attempt}: {e}")
        if attempt < retries:
            time.sleep(backoff * attempt)

    return {"price": price, "volume": volume}


def get_binance_price(asset: str) -> float | None:
    """
    Fetch ONLY the current spot price from Binance (single HTTP call).
    Use this for per-tick monitoring — volume is pre-fetched once per window
    via get_binance_data, so calling the full two-request function on every
    poll tick wastes ~150 HTTP calls per window across 3 assets.
    """
    symbol = SYMBOL_MAP.get(asset)
    if not symbol:
        return None
    for attempt in range(1, 4):
        try:
            r = requests.get(BINANCE_PRICE_URL, params={"symbol": symbol}, timeout=5)
            r.raise_for_status()
            return float(r.json()["price"])
        except requests.exceptions.Timeout:
            alog("warning", asset, f"Binance price timeout (attempt {attempt}/3)")
        except Exception as e:
            alog("warning", asset, f"Binance price error attempt {attempt}: {e}")
        if attempt < 3:
            time.sleep(2.0 * attempt)
    return None


def get_15m_candle_type(asset: str) -> str:
    """
    FIX (Bug 1): Fetch the real completed 15m OHLC kline from Binance
    and classify it. Uses candles[0] = last fully CLOSED candle.
    candles[1] = still-forming live candle, never used here.

    Passes [open, high, low, close] to classify_candle so it sees a
    real body and wick — produces BULL/BEAR instead of always DOJI.
    """
    symbol = SYMBOL_MAP.get(asset)
    if not symbol:
        return "DOJI"
    try:
        r = requests.get(
            BINANCE_KLINE_URL,
            params={"symbol": symbol, "interval": "15m", "limit": 2},
            timeout=5,
        )
        r.raise_for_status()
        candles = r.json()
        if not candles:
            alog("warning", asset, "No kline data — defaulting to DOJI")
            return "DOJI"

        o = float(candles[0][1])   # open
        h = float(candles[0][2])   # high
        l = float(candles[0][3])   # low
        c = float(candles[0][4])   # close

        candle_type = classify_candle([o, h, l, c], asset)
        alog("info", asset, f"15m kline O={o} H={h} L={l} C={c} → {candle_type}")
        return candle_type

    except Exception as e:
        alog("warning", asset, f"15m kline fetch failed: {e} — defaulting to DOJI")
        return "DOJI"


# ═══════════════════════════════════════════════════════════════════════════════
# CANDLE TYPE NORMALIZER
# ═══════════════════════════════════════════════════════════════════════════════

def _candle_strength(candle_type: str) -> str:
    """
    signal_engine returns "BULL_STRONG" / "BEAR_MEDIUM" / "DOJI"
    predictor expects "STRONG" / "MEDIUM" / "DOJI"
    """
    if "_" in candle_type:
        return candle_type.split("_", 1)[1]
    return candle_type


# ═══════════════════════════════════════════════════════════════════════════════
# TIMING HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def seconds_remaining_in_window() -> float:
    """Returns seconds left in the current 15m boundary-aligned window."""
    return WINDOW_SECONDS - (time.time() % WINDOW_SECONDS)


def sleep_to_next_window(reason: str = "") -> None:
    secs = seconds_remaining_in_window()
    msg  = f"Sleeping {secs:.0f}s to next window. {reason}".strip()
    alog("info", "SYS", msg)
    print(f"\n  ⏳ {msg}")
    time.sleep(secs + CLOCK_DRIFT_SECS)


# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND PTB SCAN STATE  (module-level so main() and monitor share it)
# ═══════════════════════════════════════════════════════════════════════════════

_bg_ptb_store:  dict                  = {}    # filled by background thread
_bg_ptb_thread: threading.Thread | None = None  # reference for .join()


def _launch_background_ptb_scan() -> None:
    """
    Starts run_ptb_scan() in a daemon thread so it can overlap with the
    last ~65 seconds of the current window.  Results land in _bg_ptb_store
    and are consumed by main() at the next window boundary.

    Safe to call multiple times — if a scan is still running this is a
    no-op (avoids double-scanning on long FOK retry windows).
    """
    global _bg_ptb_store, _bg_ptb_thread

    if _bg_ptb_thread is not None and _bg_ptb_thread.is_alive():
        alog("debug", "SYS", "Background PTB scan already running — skipping duplicate launch")
        return

    new_store: dict = {}   # fresh dict captured by _run closure — avoids
    _bg_ptb_store = new_store  # race where main() reassigns the global mid-scan

    def _run():
        alog("info", "SYS", "Background PTB scan started (overlapping window tail)")
        try:
            result = run_ptb_scan()
            new_store.update(result)   # always writes to THIS window's dict
            alog("info", "SYS", f"Background PTB scan complete — assets: {list(result.keys())}")
        except Exception as e:
            alog("error", "SYS", f"Background PTB scan failed: {e}")

    _bg_ptb_thread = threading.Thread(target=_run, daemon=True, name="bg-ptb-scan")
    _bg_ptb_thread.start()
    alog("info", "SYS", "Background PTB scan thread launched")


# ═══════════════════════════════════════════════════════════════════════════════
# BACKGROUND AUTO-REDEEM
# ═══════════════════════════════════════════════════════════════════════════════

_PENDING_REDEEMS_FILE = "pending_redeems.json"


def _persist_pending_redeem(slug: str, direction: str) -> None:
    """Write a pending redemption to disk so it survives crashes/restarts."""
    import json
    try:
        existing = []
        if os.path.exists(_PENDING_REDEEMS_FILE):
            with open(_PENDING_REDEEMS_FILE, "r") as f:
                existing = json.load(f)
        # Avoid duplicates
        if not any(r["slug"] == slug for r in existing):
            existing.append({"slug": slug, "direction": direction,
                             "queued_at": datetime.now(IST).isoformat()})
            with open(_PENDING_REDEEMS_FILE, "w") as f:
                json.dump(existing, f, indent=2)
    except Exception as e:
        alog("warning", "SYS", f"Could not persist pending redeem: {e}")


def _clear_pending_redeem(slug: str) -> None:
    """Remove a completed redemption from the persistence file."""
    import json
    try:
        if not os.path.exists(_PENDING_REDEEMS_FILE):
            return
        with open(_PENDING_REDEEMS_FILE, "r") as f:
            existing = json.load(f)
        existing = [r for r in existing if r["slug"] != slug]
        with open(_PENDING_REDEEMS_FILE, "w") as f:
            json.dump(existing, f, indent=2)
    except Exception as e:
        alog("warning", "SYS", f"Could not clear pending redeem: {e}")


def sweep_pending_redeems() -> None:
    """
    Called at startup — retries any redemptions that were persisted but
    never completed (e.g. bot crashed during _wait_for_resolution).
    FIX (Bug 4): daemon threads are killed on exit; this ensures recovery.
    """
    import json
    if not os.path.exists(_PENDING_REDEEMS_FILE):
        return
    try:
        with open(_PENDING_REDEEMS_FILE, "r") as f:
            pending = json.load(f)
    except Exception:
        return

    if not pending:
        return

    alog("info", "SYS",
         f"Startup sweep: {len(pending)} unfinished redemption(s) found — retrying")
    print(f"\n  🔄 Sweeping {len(pending)} unfinished redemption(s) from last session...\n")
    for item in pending:
        slug      = item["slug"]
        direction = item.get("direction")
        alog("info", "SYS", f"Startup redeem: slug={slug} dir={direction}")
        _redeem_in_background(slug, direction)


def _redeem_in_background(slug: str, direction: str) -> None:
    """
    Launch auto-redeem in a background daemon thread.

    FIX (Bug 4): persist to disk BEFORE launching thread so the entry
    exists even if the process dies before the thread completes.

    FIX (Bug 17): on failure, schedule an in-process 5-minute retry thread
    instead of only relying on startup sweep. Prevents a failed redeem from
    sitting uncollected for hours when the bot runs continuously.
    """
    _persist_pending_redeem(slug, direction)

    def _run(is_retry: bool = False):
        prefix = "Auto-redeem retry" if is_retry else "Auto-redeem"
        try:
            alog("info", "SYS", f"{prefix} started | slug={slug} | dir={direction}")
            result = redeem_after_win(slug, direction, silent=False)
            alog("info", "SYS",
                 f"{prefix} done | redeemed={result.get('redeemed')} "
                 f"failed={result.get('failed')} skipped={result.get('skipped')}")

            if result.get("redeemed", 0) > 0:
                _clear_pending_redeem(slug)

                # Patch ptb_log.json with the settled outcome now that we know
                # the winning side. winning_side is authoritative (from outcomePrices);
                # falls back to caller direction when detection failed.
                winning_side = result.get("winning_side") or direction
                log_entry = {
                    "market_slug": slug,
                    "direction":   (winning_side or direction or "").upper(),
                }
                try:
                    outcome = reconcile_trade(log_entry, log_path="ptb_log.json")
                    alog("info", "SYS",
                         f"reconcile_trade | slug={slug} | outcome={outcome} | "
                         f"winning_side={winning_side}")
                except Exception as re:
                    alog("warning", "SYS", f"reconcile_trade failed for {slug}: {re}")

            elif result.get("skipped", 0) > 0:
                # Zero balance — position already redeemed or never held.
                # Clear from pending so we don't keep retrying.
                alog("info", "SYS",
                     f"{prefix} skipped (zero balance) — clearing pending | slug={slug}")
                _clear_pending_redeem(slug)

            else:
                # FIX (Bug 17): redeemed=0, failed>0 — schedule in-process retry
                # rather than only relying on the next startup sweep.
                if not is_retry:
                    retry_delay = 300   # 5 minutes
                    alog("warning", "SYS",
                         f"Redeem failed — scheduling in-process retry in {retry_delay}s | slug={slug}")

                    def _delayed_retry():
                        time.sleep(retry_delay)
                        _run(is_retry=True)

                    t = threading.Thread(target=_delayed_retry, daemon=True,
                                         name=f"redeem-retry-{slug[-8:]}")
                    t.start()
                else:
                    alog("warning", "SYS",
                         f"Redeem retry also failed — leaving in pending file for next startup | slug={slug}")

        except Exception as e:
            alog("error", "SYS", f"Auto-redeem thread error: {e}")
            # Leave in pending file — will be retried at next startup

    t = threading.Thread(target=_run, daemon=True, name=f"redeem-{slug[-8:]}")
    t.start()
    alog("debug", "SYS", f"Redemption thread launched: {t.name}")


# ═══════════════════════════════════════════════════════════════════════════════
# STARTUP HISTORY DISPLAY
# ═══════════════════════════════════════════════════════════════════════════════

def show_recent_history(csv_path: str = "dry_run_data.csv", n: int = 10) -> None:
    """
    Reads the last `n` resolved trades from the CSV and prints a compact
    summary table at startup so previous attempts are visible on restart.
    Skips PENDING rows — those haven't resolved yet.
    """
    import csv as _csv

    if not os.path.exists(csv_path):
        print("  📂 No trade history found.")
        return
    try:
        with open(csv_path, newline="") as f:
            rows = list(_csv.DictReader(f))
    except Exception as e:
        print(f"  ⚠️  Could not read history: {e}")
        return

    resolved = [r for r in rows if r.get("target_outcome", "").upper() in ("WIN", "LOSS")]
    if not resolved:
        print("  📂 No resolved trades in history yet.")
        return

    recent   = resolved[-n:]
    wins     = sum(1 for r in resolved if r["target_outcome"].upper() == "WIN")
    total    = len(resolved)
    r10_wins = sum(1 for r in recent if r["target_outcome"].upper() == "WIN")

    print(f"\n  {'─'*60}")
    print(f"  📊 HISTORY  "
          f"All-time: {wins}W/{total-wins}L {wins/total*100:.0f}%  |  "
          f"Last {len(recent)}: {r10_wins}W/{len(recent)-r10_wins}L "
          f"{r10_wins/len(recent)*100:.0f}%")
    print(f"  {'─'*60}")
    print(f"  {'#':<4} {'Time':<17} {'Asset':<5} {'Candle':<14} {'Dir':<4} {'Dist%':<8} Result")
    print(f"  {'─'*60}")
    for i, r in enumerate(recent, start=total - len(recent) + 1):
        ts      = r.get("timestamp", "")[:16]
        asset   = r.get("asset", "?")
        candle  = r.get("candle_type", "?")
        dirn    = r.get("direction", "?")[:3]
        outcome = r.get("target_outcome", "?").upper()
        icon    = "✅" if outcome == "WIN" else "❌"
        try:
            dist_str = f"{float(r.get('dist_pct', 0))*100:.3f}%"
        except Exception:
            dist_str = "?"
        print(f"  {i:<4} {ts:<17} {asset:<5} {candle:<14} {dirn:<4} {dist_str:<8} {icon} {outcome}")
    print(f"  {'─'*60}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 0 — INITIALISE
# ═══════════════════════════════════════════════════════════════════════════════

def initialise() -> Predictor:
    mode     = _mode_label()
    live_raw = os.getenv("LIVE_ENABLED", "false").strip()

    print("\n  📡 Fetching real on-chain bankroll...")
    balance = get_real_usdc_balance()

    banner = (
        f"\n{'═'*58}\n"
        f"  🤖  POLYBOT — Multi-Asset Sniper Bot\n"
        f"  Mode         : {mode}\n"
        f"  LIVE_ENABLED : {live_raw}\n"
        f"  Assets       : {', '.join(ASSETS)}\n"
        f"  Stake        : ${STAKE_USD:.2f} per trade\n"
        f"  Bankroll     : ${balance:.2f}\n"
        f"  Started      : {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}\n"
        f"{'═'*58}"
    )
    print(banner)
    alog("info", "SYS",
         f"Bot started | mode={mode} | LIVE_ENABLED={live_raw} | stake=${STAKE_USD} | bankroll=${balance:.2f}")

    if not _is_live():
        print("\n  ⚠️  DRY RUN MODE — no real orders will be placed.")
        print("      Set LIVE_ENABLED=true in .env to go live.\n")
        alog("info", "SYS", "DRY RUN confirmed — LIVE_ENABLED is false")

    predictor = Predictor()
    alog("info", "SYS", f"Predictor status: {predictor.status()}")

    # Initialise adaptive asset scorer with the live asset list
    init_score_manager(ASSETS)
    alog("info", "SYS", f"Asset scorer initialised for: {ASSETS}")

    # FIX (Bug 4): retry any redemptions that didn't complete before last shutdown
    sweep_pending_redeems()

    # Reconcile any PENDING entries in ptb_log.json from previous sessions.
    # Called before repair_pending_rows so both the JSON log and CSV are
    # consistent by the time the history display runs.
    try:
        rp_summary = reconcile_pending_log(log_path="ptb_log.json")
        if rp_summary["resolved"] or rp_summary["still_pending"]:
            alog("info", "SYS",
                 f"reconcile_pending_log: resolved={rp_summary['resolved']} "
                 f"still_pending={rp_summary['still_pending']} "
                 f"errors={rp_summary['errors']}")
    except Exception as e:
        alog("warning", "SYS", f"reconcile_pending_log startup sweep failed: {e}")

    # Auto-resolve any PENDING dry run rows from previous sessions
    # (handles crashes/restarts where outcome_tracker thread never completed)
    repair_pending_rows(csv_path="dry_run_data.csv", silent=True)

    show_recent_history()

    return predictor


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 1 — PRE-FLIGHT GATEKEEPER
# ═══════════════════════════════════════════════════════════════════════════════

def preflight_check() -> bool:
    alog("info", "SYS", "Pre-flight: checking for open positions...")
    try:
        blocked = has_open_position()
    except Exception as e:
        alog("error", "SYS", f"Pre-flight check threw exception: {e} — blocking window")
        blocked = True

    if blocked:
        msg = "Open position detected (or API error). Skipping this window."
        alog("warning", "SYS", msg)
        notify(msg, prefix="🛑")
        return False

    alog("info", "SYS", "Pre-flight: CLEAR — no open positions found")
    return True


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 2 — SYNC TO WINDOW BOUNDARY
# ═══════════════════════════════════════════════════════════════════════════════

def sync_to_window() -> None:
    elapsed = time.time() % WINDOW_SECONDS
    grace   = SCAN_DURATION + 10

    if elapsed <= grace:
        window_ts = datetime.now(IST).strftime("%H:%M:%S IST")
        alog("info", "SYS",
             f"Already at window boundary ({elapsed:.0f}s elapsed) — skipping sync wait")
        print(f"\n{'─'*58}")
        print(f"  🟢 NEW WINDOW — {window_ts} | {_mode_label()}")
        print(f"{'─'*58}")
        return

    secs = seconds_remaining_in_window()
    alog("info", "SYS",
         f"Syncing to window boundary — waiting {secs:.0f}s "
         f"(+{CLOCK_DRIFT_SECS}s drift buffer)")
    print(f"\n  🕐 Next window in {secs:.0f}s — waiting...")
    time.sleep(secs + CLOCK_DRIFT_SECS)
    window_ts = datetime.now(IST).strftime("%H:%M:%S IST")
    alog("info", "SYS", f"Window started at {window_ts}")
    print(f"\n{'─'*58}")
    print(f"  🟢 NEW WINDOW — {window_ts} | {_mode_label()}")
    print(f"{'─'*58}")


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 3 — PTB SCAN
# ═══════════════════════════════════════════════════════════════════════════════

def run_ptb_scan() -> dict:
    """Scan all assets for 60s and return locked PTB data."""
    MIN_PTB_SAMPLES = 12
    alog("info", "SYS", f"PTB scan started — {SCAN_DURATION}s across {ASSETS}")
    ptbs = scan_and_lock_ptb_multi(
        assets=ASSETS,
        duration=SCAN_DURATION,
        interval=SCAN_INTERVAL,
    )
    for asset, data in ptbs.items():
        if data.get("ok"):
            samples = data.get("samples", 0)
            if samples < MIN_PTB_SAMPLES:
                alog("warning", asset,
                     f"PTB samples too low ({samples} < {MIN_PTB_SAMPLES}) — marking asset SKIP")
                data["ok"] = False
            elif not ptb_is_tradeable(data):
                # LOW confidence: warn but allow through.
                # signal_engine's distance threshold is the real filter —
                # a noisy PTB baseline makes the threshold harder to clear naturally.
                alog("warning", asset,
                     f"PTB confidence LOW — proceeding with caution (distance filter applies)")
                # do NOT set data["ok"] = False
                alog("info", asset,
                     f"PTB locked | ptb={data['ptb']} | "
                     f"conf={data['confidence']} | samples={samples}")
            else:
                alog("info", asset,
                     f"PTB locked | ptb={data['ptb']} | "
                     f"conf={data['confidence']} | samples={samples}")
        else:
            alog("warning", asset, "PTB scan failed — asset will be skipped")
    return ptbs


# ═══════════════════════════════════════════════════════════════════════════════
# STEPS 4 + 5 — MONITOR & DECIDE
# ═══════════════════════════════════════════════════════════════════════════════

def monitor_and_decide(
    ptbs:          dict,
    predictor:     Predictor,
    candle_types:  dict | None = None,
    skip_assets:   set | None  = None,
    yes_token_ids: dict | None = None,
) -> tuple:
    """
    Polls Binance every MONITOR_INTERVAL seconds.
    Evaluates signal_engine + predictor for every asset.

    candle_types: if provided (pre-fetched at window t=0), uses those.
                  If None, falls back to fetching internally (e.g. bootstrap window).

    skip_assets:  set of asset names to exclude from every poll tick.
                  The main loop passes fired_this_window so that any asset
                  already attempted this window (successful fill OR failed FOK)
                  cannot re-signal.  This prevents the same candle from being
                  evaluated twice across consecutive FOK-retry passes.
                  FIX (Bug 6): guards against same-asset duplicate CSV rows
                  and live double-entry on an already-open position.

    Fast polling: 1s per tick when secs_left > 840 (first 60s of window)
                  to catch the early-window signal that matters most.
                  Normal 5s polling resumes after that.

    Background PTB: launches _launch_background_ptb_scan() once when
                    secs_left drops below 65s so the next window's PTB
                    is ready at the boundary without delaying monitoring.

    FIX (Bug 3): prev_dist updated from raw_dist_pct (price vs PTB)
    not from post-gate dist_pct which is 0.0 on all SKIP paths.

    Returns (opportunities, volumes) — utc_hour is now stored per-opportunity
    as opp["_utc_hour"] (captured at exact signal-fire time) so that
    hour-boundary signals (e.g. monitor starts at 23:58, signal at 00:02)
    get the correct UTC hour in the training CSV row.
    """
    if skip_assets is None:
        skip_assets = set()
    prev_dist: dict[str, float | None] = {a: None for a in ASSETS}

    # Pre-fetch volumes once (stable over 15m window)
    volumes: dict[str, float] = {}
    for asset in ASSETS:
        if not ptbs.get(asset, {}).get("ok"):
            continue
        data = get_binance_data(asset)
        vol  = data.get("volume") or 0.0
        volumes[asset] = vol
        alog("debug", asset, f"24h volume fetched: ${vol:,.0f}")

    # YES token IDs — use the window-level dict passed in from main() if available.
    # This avoids re-launching Gamma API fetch threads on every FOK retry cycle.
    #
    # BUG FIX (Bug 19): monitor_and_decide was called on every FOK retry, and each
    # call re-launched 3 background threads to fetch YES token IDs (one per asset).
    # In the logs this appears as three "[asset] YES token slug" lines immediately
    # after every FOK miss — adding 3 × Gamma round-trips per retry, and racing
    # against the signal path on the second pass. Token IDs don't change within a
    # window, so this was pure waste. Fix: caller (main loop) creates yes_token_ids
    # once at window open and passes it in. monitor_and_decide only launches threads
    # when called with yes_token_ids=None (e.g. bootstrap window, first call).
    if yes_token_ids is None:
        yes_token_ids = {}
        _tid_lock = threading.Lock()

        def _fetch_token_id(asset: str) -> None:
            if not ptbs.get(asset, {}).get("ok"):
                return
            tid = get_yes_token_id(asset)
            with _tid_lock:
                yes_token_ids[asset] = tid
            if tid:
                alog("info", asset, f"YES token ID cached: {tid[:12]}...")
            else:
                alog("warning", asset,
                     "YES token ID not available after all retries — "
                     "executor lazy-retry path will handle it at signal time")

        _tid_threads = [
            threading.Thread(target=_fetch_token_id, args=(a,), daemon=True, name=f"tid-{a}")
            for a in ASSETS
        ]
        for _t in _tid_threads:
            _t.start()
        # Do NOT join — monitoring starts immediately. Threads resolve in background.
    else:
        alog("debug", "SYS",
             f"YES token IDs passed from window scope — skipping re-fetch "
             f"(cached: {[a for a, v in yes_token_ids.items() if v]})")

    # Candle classification — use pre-fetched if provided, else fetch now
    if candle_types is None:
        candle_types = {}
        for asset in ASSETS:
            if not ptbs.get(asset, {}).get("ok"):
                continue
            candle_types[asset] = get_15m_candle_type(asset)
            alog("info", asset,
                 f"Window candle: {candle_types[asset]} (fetched in monitor)")
    else:
        for asset, ct in candle_types.items():
            alog("info", asset, f"Window candle: {ct} (pre-fetched at t=0)")

    alog("info", "SYS", "Monitoring loop started")
    print(f"\n  👁️  Monitoring {len(ASSETS)} assets ...\n")

    bg_scan_launched = False  # guard: only launch once per window

    while True:
        secs_left = seconds_remaining_in_window()

        if secs_left < 30:
            alog("info", "SYS",
                 f"Window closing in {secs_left:.0f}s — monitoring ended")
            break

        # ── BACKGROUND PTB SCAN TRIGGER ───────────────────────────────────────
        # Launch at ~65s remaining so it finishes just before the next window.
        if not bg_scan_launched and secs_left < 65:
            _launch_background_ptb_scan()
            bg_scan_launched = True

        opportunities = []

        for asset in ASSETS:
            ptb_data = ptbs.get(asset, {})
            if not ptb_data.get("ok"):
                continue

            # ── SKIP-ASSETS GATE (FIX Bug 6) ──────────────────────────────────
            # Exclude any asset already attempted this window so the same candle
            # cannot re-signal across consecutive FOK-retry monitor passes.
            if asset in skip_assets:
                alog("debug", asset,
                     f"Already fired this window — skipping (skip_assets={skip_assets})")
                continue

            # ── ASSET SCORE GATE ──────────────────────────────────────────────
            if not score_manager.is_tradeable(asset):
                score = score_manager.get_score(asset)
                alog("info", asset,
                     f"Asset scorer BLOCKED | score={score:.3f} < 0.40 "
                     f"(recovering from recent losses)")
                print(f"  [{asset}] Score={score:.3f} — suspended, skipping")
                continue

            ptb         = ptb_data["ptb"]
            conf        = ptb_data["confidence"]
            candle_type = candle_types.get(asset, "DOJI")

            # FIX: use price-only fetch (single HTTP call) — volume is already
            # pre-fetched once per window above, so get_binance_data (which
            # fires two HTTP requests) would waste ~150 calls per window.
            current_price = get_binance_price(asset)
            if current_price is None:
                alog("warning", asset, "Could not fetch live price — skipping tick")
                print(f"  [{asset}] Price fetch failed — skipping")
                continue

            # ── VOLUME GATE ───────────────────────────────────────────────────
            vol = volumes.get(asset, 0.0)
            min_vol = MIN_VOLUME.get(asset, 0)
            if vol < min_vol:
                alog("info", asset,
                     f"Volume gate BLOCKED | vol=${vol:,.0f} < min=${min_vol:,.0f}")
                print(f"  [{asset}] Vol=${vol/1e6:.0f}M below ${min_vol/1e6:.0f}M min — skipping")
                continue

            prec = PRECISION_MAP.get(asset, 4)

            # FIX (Bug 3): compute raw distance before signal gating
            raw_dist_pct = abs(current_price - ptb) / ptb if ptb else 0.0

            direction, dist_pct, sig_reason, limit_price = get_signal(
                current_price     = current_price,
                ptb               = ptb,
                prev_dist_pct     = prev_dist[asset],
                seconds_remaining = secs_left,
                candle_type       = candle_type,
                asset             = asset,
            )

            # ── LATE-ENTRY GUARD ──────────────────────────────────────────────
            if limit_price is None:
                direction  = "SKIP"
                sig_reason = "Late-entry skip: <120s left, limit price uneconomic"

            # FIX (Bug 3): always update from raw, not post-gate zero
            prev_dist[asset] = raw_dist_pct

            dist_display  = f"{raw_dist_pct * 100:.5f}%"
            price_display = f"${current_price:,.{prec}f}"
            print(
                f"  [{asset}] {price_display:<14} "
                f"Dist: {dist_display:<10} "
                f"Candle: {candle_type:<13} "
                f"Signal: {direction:<5} | {sig_reason}"
            )
            alog("debug", asset,
                 f"price={current_price:.{prec}f} | dist={dist_display} | "
                 f"candle={candle_type} | signal={direction} | {sig_reason}")

            if direction == "SKIP":
                continue

            candle_strength = _candle_strength(candle_type)
            allow, prob, pred_reason = predictor.should_trade(
                distance_pct = dist_pct,
                hour         = datetime.now(timezone.utc).hour,   # FIX: inline — no stale var
                volume       = volumes.get(asset, 0.0),
                confidence   = conf,
                candle_type  = candle_strength,
            )

            prob_str = f"{prob:.0%}" if prob is not None else "N/A"
            if not allow:
                alog("info", asset,
                     f"🛡️ ML SHADOW BLOCK | Would skip ({prob_str}) | {pred_reason}")
                print(f"  [{asset}] 🧠 Shadow Filter: ML says SKIP ({prob_str}), bypassing.")
            else:
                alog("info", asset, f"🧠 ML APPROVE | Prob: {prob_str}")
                print(f"  [{asset}] 🧠 Predictor: APPROVED ({prob_str})")

            usd_distance = abs(current_price - ptb)
            # Snapshot YES price at the exact moment the signal fires.
            # Uses pre-cached token ID — this is a single fast /book call.
            #
            # BUG 21 + BUG 23 FIX: use fetch_yes_price_raw (bypasses the
            # _get_market_price circuit breaker). The old fetch_yes_price called
            # _get_market_price which returns None for asks >= 0.97 — so
            # yes_price_now was always None on a resolved market and the guard
            # (yes_price_now >= 0.97) could never fire. fetch_yes_price_raw
            # returns the actual price including resolved-market values (0.99).
            # If the YES price is already >= 0.97 at signal time, the market has
            # resolved (YES token frozen near 1.0). Skip BEFORE building the opp
            # dict — prevents wasted Gamma round-trips and FOK retries on a dead book.
            # Threshold 0.97 matches _get_market_price's RESOLVED_PRICE_THRESHOLD.
            # Bug 20 fix in live_executor remains the second line of defence.
            yes_price_now = fetch_yes_price_raw(yes_token_ids.get(asset))
            if yes_price_now is not None:
                # Resolved-market detection using only the YES token (we only
                # pre-cache YES IDs). The YES token encodes both outcomes:
                #   Resolved YES → yes_price ≈ 0.99 (frozen near 1.0)
                #   Resolved NO  → yes_price ≈ 0.01 (frozen near 0.0)
                #
                # BUG FIX: previous guard only caught "resolved YES" (>= 0.97).
                # A market resolved NO has YES ≈ 0.01, which fails the old check
                # and lets a resolved-NO market slip through to the executor.
                # Fix: also catch the mirror case (<= 0.03, matching the 0.97
                # threshold applied to the NO side: 1 - 0.97 = 0.03).
                _resolved_yes = yes_price_now >= 0.97
                _resolved_no  = yes_price_now <= 0.03
                if _resolved_yes or _resolved_no:
                    _resolve_side = "YES" if _resolved_yes else "NO"
                    alog("warning", asset,
                         f"YES price={yes_price_now:.2f} — market resolved {_resolve_side} "
                         f"at signal time, skipping {asset} (direction={direction})")
                    print(f"  [{asset}] ⛔ YES price={yes_price_now:.2f} — resolved {_resolve_side}, skip")
                    continue   # back to `for asset in ASSETS` — do not build opp
                alog("info", asset, f"YES price at signal: {yes_price_now:.4f}")
            else:
                alog("warning", asset, "YES price fetch failed at signal time — logging blank")

            opportunities.append({
                "asset":                asset,
                "direction":            direction,
                "limit_price":          limit_price,
                "stake":                STAKE_USD,
                "secs_left":            secs_left,
                "candle_type":          candle_type,
                "distance":             usd_distance,
                # ── outcome tracker fields (captured at signal-fire time) ──────
                "_frozen_ptb":          ptb,
                "_signal_ts":           time.time(),
                "_dist_pct":            dist_pct,
                "_confidence":          conf,
                "_sig_reason":          sig_reason,
                "_pred_reason":         pred_reason,
                "_yes_price_at_signal": yes_price_now,
                # FIX: capture UTC hour at signal-fire time, not at window start.
                # If monitoring starts at 23:58 UTC and a signal fires at 00:02,
                # the old window-start capture would log hour=23 instead of 0.
                "_utc_hour":            datetime.now(timezone.utc).hour,
                # FIX: pass pre-cached YES token ID to executor so the v6
                # fast-path (_cached_token_id branch) is actually triggered.
                # Without this, execute_sequential_multi_asset always gets None
                # and falls back to the redundant Gamma API calls it was meant
                # to skip. When None (slug hadn't propagated yet), the executor
                # does a fresh _get_token_id lookup as a lazy retry.
                "_cached_token_id":     yes_token_ids.get(asset),
            })
            alog("info", asset,
                 f"✅ OPPORTUNITY | {direction} | dist={dist_pct*100:.5f}% | "
                 f"limit={limit_price:.2f} | secs={secs_left:.0f} | sig={sig_reason}")

        if opportunities:
            alog("info", "SYS",
                 f"Opportunities found: {[o['asset'] for o in opportunities]}")
            return opportunities, volumes

        print(f"  {'─'*52}")
        # Fast 1s polling for first 60s of window (secs_left > 840),
        # standard 5s polling thereafter.
        poll_interval = MONITOR_INTERVAL_FAST if secs_left > 840 else MONITOR_INTERVAL
        time.sleep(poll_interval)

    return [], volumes


# ═══════════════════════════════════════════════════════════════════════════════
# STEP 6 — EXECUTE
# ═══════════════════════════════════════════════════════════════════════════════

def execute(opportunities: list) -> dict:
    alog("info", "SYS",
         f"Sending {len(opportunities)} opportunities to executor: "
         f"{[o['asset'] for o in opportunities]}")
    if not _is_live():
        print("\n  ⚠️  DRY RUN MODE — executor will simulate only (LIVE_ENABLED=false)")
    return execute_sequential_multi_asset(opportunities)


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    global _bg_ptb_thread, _bg_ptb_store   # module-level bg-scan state shared
    predictor     = initialise()            # across all window iterations
    window_number = _load_window_counter()
    alog("info", "SYS", f"Resuming from window #{window_number}")

    # ── BOOTSTRAP PTB ─────────────────────────────────────────────────────────
    # Sync to the first window boundary, then run an initial PTB scan so
    # Window 1 has a valid baseline immediately.  After this, PTB is
    # maintained by the background scan launched at the tail of each window.
    alog("info", "SYS", "Bootstrap: syncing to first window boundary...")
    sync_to_window()
    alog("info", "SYS", "Bootstrap: running initial PTB scan...")
    ptbs = run_ptb_scan()
    alog("info", "SYS", "Bootstrap complete — entering main loop")

    while True:
        # ── CIRCUIT BREAKER CHECK ──────────────────────────────────────────────
        if _circuit_breaker_tripped():
            msg = (f"🔴 CIRCUIT BREAKER TRIGGERED — "
                   f"{_consecutive_losses} consecutive losses. Bot halted. "
                   f"Restart manually after reviewing.")
            alog("error", "SYS", msg)
            print(f"\n  {msg}\n")
            notify_error(msg)
            sys.exit(1)

        window_number += 1
        _save_window_counter(window_number)
        alog("info", "SYS", f"{'═'*20} WINDOW #{window_number} {'═'*20}")

        # Asset score: passive time-recovery + display
        for _a in ASSETS:
            score_manager.on_window_end(_a)
        _score_snap = score_manager.summary()
        _score_line = '  '.join(
            f"{a}={v['score']:.2f}{'(SUSP)' if not v['tradeable'] else ''}"
            for a, v in _score_snap.items()
        )
        alog('info', 'SYS', f'Asset scores: {_score_line}')
        print(f'\n  Scores | {_score_line}')

        if not preflight_check():
            sleep_to_next_window(reason="(Open position / gatekeeper blocked)")
            # Scan PTB synchronously for the new window and stash it in
            # _bg_ptb_store so the consumer below picks it up without a
            # second redundant scan (avoids double-scan on blocked windows).
            alog("info", "SYS", "Re-scanning PTB after preflight block...")
            _bg_ptb_store = run_ptb_scan()
            _bg_ptb_thread = None
            continue

        # ── CONSUME BACKGROUND PTB (or fall back to synchronous scan) ─────────
        # The bg scan was launched at ~t=835 of the previous window.
        # By the time we reach here (after sleep_to_next_window + clock drift)
        # it has had ≥65s to complete.

        if _bg_ptb_thread is not None and _bg_ptb_thread.is_alive():
            alog("info", "SYS",
                 "Background PTB scan still running — waiting up to 70s...")
            _bg_ptb_thread.join(timeout=70)

        if _bg_ptb_store:
            ptbs = dict(_bg_ptb_store)
            _bg_ptb_store = {}
            _bg_ptb_thread = None
            alog("info", "SYS", "Using PTB from background scan")
        else:
            # bg scan failed or was never launched (e.g. window was skipped)
            alog("warning", "SYS",
                 "No background PTB results — falling back to synchronous scan")
            ptbs = run_ptb_scan()

        ok_assets = [a for a in ASSETS if ptbs.get(a, {}).get("ok")]
        if not ok_assets:
            alog("warning", "SYS", "All PTB scans failed — skipping window.")
            notify("All PTB scans failed — no assets tradeable.", prefix="⚠️")
            _record_outcome("SKIP")
            sleep_to_next_window(reason="(No PTB data)")
            continue

        alog("info", "SYS", f"Tradeable assets after PTB: {ok_assets}")

        # ── VPN CHECK (once per window, live mode only) ───────────────────────
        # check_vpn_once() calls _check_vpn() which guards on the module-level
        # _vpn_checked flag in live_executor. After the first successful check
        # the flag is True and ALL subsequent calls (including the guard inside
        # execute_live_trade) are instant no-ops — zero network I/O.
        # Calling here means VPN failures are caught BEFORE monitoring starts,
        # not milliseconds before the FOK order fires after a full signal cycle.
        if _is_live():
            try:
                check_vpn_once()
            except RuntimeError as e:
                alog("error", "SYS", f"VPN check failed at window entry: {e}")
                print(f"\n  ⛔ VPN FAILED — {e}\n")
                notify_error(f"VPN check failed: {e}")
                sleep_to_next_window(reason="(VPN failure)")
                continue

        # ── FETCH CANDLES AT t=0 ──────────────────────────────────────────────
        # The window just opened.  Fetch the just-closed 15m kline immediately
        # so monitoring can fire at t≈5–10s instead of t≈62s.
        window_ts = datetime.now(IST).strftime("%H:%M:%S IST")
        print(f"\n{'─'*58}")
        print(f"  🟢 NEW WINDOW — {window_ts} | {_mode_label()}")
        print(f"{'─'*58}")
        alog("info", "SYS", f"Fetching candles at window open ({window_ts})")
        candle_types: dict[str, str] = {
            asset: get_15m_candle_type(asset) for asset in ok_assets
        }

        # ── YES TOKEN IDs — window-scoped, fetched once ────────────────────────
        # BUG FIX (Bug 19): previously yes_token_ids was created inside
        # monitor_and_decide, so it was re-created (and re-fetched from Gamma)
        # on every FOK retry call. Token IDs are stable within a window — there
        # is no reason to re-fetch them. Creating here and passing in means threads
        # launch once at window open and the same dict is reused across all retries.
        _window_token_ids: dict[str, str | None] = {}
        _window_tid_lock = threading.Lock()

        def _fetch_window_token_id(asset: str) -> None:
            if not ptbs.get(asset, {}).get("ok"):
                return
            tid = get_yes_token_id(asset)
            with _window_tid_lock:
                _window_token_ids[asset] = tid
            if tid:
                alog("info", asset, f"YES token ID cached: {tid[:12]}...")
            else:
                alog("warning", asset,
                     "YES token ID not available — executor lazy-retry will handle it")

        for _a in ok_assets:
            threading.Thread(
                target=_fetch_window_token_id, args=(_a,),
                daemon=True, name=f"tid-{_a}"
            ).start()
        # Non-blocking — monitoring starts immediately. Threads write into
        # _window_token_ids as they resolve. Signal path reads from it at fire time.

        # ── MONITOR + RETRY LOOP ───────────────────────────────────────────────
        # If the executor returns "no_successful_fill" (FOK missed liquidity),
        # continue monitoring instead of sleeping — another fill chance may
        # exist while time remains in the window.
        #
        # fired_this_window: tracks assets attempted this window with a timestamp.
        # Maps asset → time.time() of the FOK attempt.
        #
        # FIX (Bug 6): prevents same-asset duplicate rows in dry_run_data.csv
        # and guards against live double-entry on an already-open position.
        #
        # FIX (time-decay): previously a permanent set — once an asset missed a
        # FOK it was excluded for the entire window even with 600s remaining.
        # Now uses a 60s cooldown: after FOK_COOLDOWN_SECS the asset re-enters
        # monitoring. If price is still above threshold and candle still valid,
        # it gets another shot. If price faded, the fade filter catches it.
        # A SUCCESSFUL fill always uses permanent exclusion (executed_this_window=True
        # exits the outer loop immediately, so there is no re-entry risk).
        FOK_COOLDOWN_SECS    = 60        # seconds before a missed asset re-enters
        executed_this_window = False
        fok_retry_count      = 0
        MAX_FOK_RETRIES      = 3
        fired_this_window: dict = {}     # asset → timestamp of FOK attempt

        while not executed_this_window and fok_retry_count < MAX_FOK_RETRIES:
            try:
                # Build the active skip set: only assets whose cooldown hasn't
                # expired yet.  Assets that missed FOK more than FOK_COOLDOWN_SECS
                # ago are removed so they can re-enter the monitor loop.
                _now_ts = time.time()
                _active_skip = {
                    a for a, t in fired_this_window.items()
                    if _now_ts - t < FOK_COOLDOWN_SECS
                }
                if _active_skip != set(fired_this_window.keys()):
                    _cooled = set(fired_this_window.keys()) - _active_skip
                    alog("info", "SYS",
                         f"FOK cooldown expired — re-admitting to monitor: {_cooled}")
                opportunities, volumes = monitor_and_decide(
                    ptbs, predictor, candle_types,
                    skip_assets=_active_skip,
                    yes_token_ids=_window_token_ids,
                )
            except KeyboardInterrupt:
                raise
            except Exception as e:
                alog("error", "SYS", f"Monitor loop crashed: {e}")
                notify_error(f"Monitor loop crashed: {e}")
                sleep_to_next_window(reason="(Monitor error)")
                break

            if not opportunities:
                alog("info", "SYS", "No approved opportunities. Moving to next window.")
                print("\n  ⏭️  No trades this window. Waiting for next...\n")
                _record_outcome("SKIP")
                sleep_to_next_window()
                break

            try:
                result = execute(opportunities)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                alog("error", "SYS", f"Executor threw exception: {e}")
                notify_error(f"Executor exception: {e}")
                sleep_to_next_window(reason="(Executor error)")
                break

            # ── UPDATE FIRED SET ───────────────────────────────────────────────
            # Only mark assets as fired when they actually reached the orderbook
            # (executor reason == "no_successful_fill" or a successful fill).
            #
            # "slug_unavailable" means the asset never had an order placed —
            # the slug check failed before execute_live_trade was ever called.
            # In that case do NOT add to fired_this_window: the signal is still
            # valid, and the asset must remain eligible for re-monitoring.
            #
            # FIX (Bug 15): previously this was unconditional — any executor
            # return, including slug-not-found skips, would add the asset to the
            # fired set. Effect: in windows where the slug hadn't propagated yet,
            # the only signaling asset got silently excluded from all subsequent
            # monitor ticks, making the FOK-retry re-monitoring loop useless.
            executor_reason = result.get("reason", "")
            if executor_reason != "slug_unavailable":
                _fire_ts = time.time()
                for opp in opportunities:
                    fired_this_window[opp["asset"]] = _fire_ts
                if fired_this_window:
                    alog("debug", "SYS",
                         f"fired_this_window after execute: {list(fired_this_window.keys())} "
                         f"(cooldown={FOK_COOLDOWN_SECS}s — will re-admit after expiry)")
            else:
                alog("info", "SYS",
                     f"Executor returned slug_unavailable — NOT adding "
                     f"{[o['asset'] for o in opportunities]} to fired_this_window "
                     f"(no orderbook attempt was made; asset remains eligible)")

            if result.get("executed") or not _is_live():
                # ── FIX (Bug 5): log ONLY the executed opportunity ─────────────
                # Old code: `for opp in opportunities: log_dry_trade(...)`
                # This logged ALL assets that signaled simultaneously, e.g. all
                # three at 23:01:07, even though only the first was filled.
                # That inflated the win count and scheduled phantom outcome checks.
                # New code: identify the executed asset and log exactly one row.
                executed_asset = result.get("asset")
                executed_opp   = next(
                    (o for o in opportunities if o["asset"] == executed_asset),
                    opportunities[0] if opportunities else None,
                )
                if executed_opp:
                    log_dry_trade(
                        executed_opp["asset"],
                        executed_opp["_dist_pct"],
                        volumes.get(executed_opp["asset"], 0.0),
                        executed_opp["_confidence"],
                        executed_opp["candle_type"],
                        executed_opp["_utc_hour"],   # FIX: hour at signal-fire, not window-start
                        executed_opp["direction"],
                        frozen_ptb=executed_opp["_frozen_ptb"],
                        yes_price=executed_opp.get("_yes_price_at_signal"),
                    )
                    schedule_outcome_check(
                        asset             = executed_opp["asset"],
                        frozen_ptb        = executed_opp["_frozen_ptb"],
                        signal            = executed_opp["direction"],
                        signal_ts         = executed_opp["_signal_ts"],
                        record_outcome_fn = lambda outcome, a=executed_opp["asset"]: _record_outcome(outcome, a),
                    )
                    # Log the non-executed (shadow) opportunities at DEBUG level
                    # so they're visible in bot_audit.log for analysis without
                    # polluting the training CSV.
                    shadow_opps = [o for o in opportunities if o["asset"] != executed_asset]
                    if shadow_opps:
                        shadow_names = [o["asset"] for o in shadow_opps]
                        alog("debug", "SYS",
                             f"Shadow opportunities (not executed, not logged): {shadow_names}")

            if result.get("executed"):
                asset     = result.get("asset", "?")
                direction = result.get("direction", "?")
                slug      = result.get("slug", "")
                price     = result.get("price", "?")
                msg = (f"Trade filled | {asset} {direction} | "
                       f"limit={price} | stake=${STAKE_USD:.2f} | slug={slug}")
                alog("info", "SYS", f"✅ EXECUTED — {msg}")
                notify(msg, asset=asset, prefix="🎯")
                # Only attempt on-chain redemption in LIVE mode.
                # Dry run returns executed=True (dry_run=True flag) — do NOT
                # call _redeem_in_background as that fires real blockchain TXs.
                if slug and _is_live():
                    _redeem_in_background(slug, direction)
                elif slug and not _is_live():
                    alog("info", "SYS",
                         f"DRY RUN — skipping redemption for {slug} (no real position)")
                else:
                    alog("warning", "SYS", "No slug — cannot schedule auto-redeem")
                executed_this_window = True
                sleep_to_next_window(reason="(Trade filled — waiting for resolution)")

            else:
                reason = result.get("reason", "unknown")
                if reason == "resolved_market":
                    # BUG 24 FIX: executor confirmed the orderbook is frozen
                    # (ask >= 0.97 → _get_market_price returned None). Skip the
                    # window immediately — no FOK retry, no 633s spin loop.
                    # Bug 23's fetch_yes_price_raw guard is the first defence;
                    # this is the second (race condition: market resolves between
                    # pre-flight check and the FOK attempt).
                    alog("warning", "SYS",
                         "Executor confirmed resolved market — skipping window immediately")
                    print(f"\n  ⛔ Resolved market confirmed by executor — next window\n")
                    _record_outcome("SKIP")
                    sleep_to_next_window(reason="(Resolved market)")
                    break
                elif reason == "no_successful_fill":
                    fok_retry_count += 1
                    secs_left = seconds_remaining_in_window()
                    if fok_retry_count < MAX_FOK_RETRIES and secs_left > 60:
                        alog("info", "SYS",
                             f"FOK missed liquidity (attempt {fok_retry_count}/{MAX_FOK_RETRIES}) "
                             f"— {secs_left:.0f}s left, continuing to monitor for re-entry")
                        print(f"\n  🔄 FOK unmatched ({fok_retry_count}/{MAX_FOK_RETRIES}) "
                              f"— {secs_left:.0f}s left, re-monitoring...\n")
                    else:
                        alog("info", "SYS",
                             f"FOK retries exhausted or window closing ({secs_left:.0f}s) — moving on")
                        print(f"\n  ⏭️  No fill after {fok_retry_count} attempt(s). Next window...\n")
                        _record_outcome("SKIP")
                        sleep_to_next_window(reason="(FOK retries exhausted)")
                        break
                else:
                    alog("info", "SYS", f"No fill — reason: {reason}. Moving on.")
                    print(f"\n  ⏭️  No fill ({reason}). Moving to next window...\n")
                    _record_outcome("SKIP")
                    sleep_to_next_window()
                    break


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        alog("info", "SYS", "Bot stopped by user (KeyboardInterrupt)")
        print("\n\n  🛑 Bot stopped. Goodbye.\n")
        sys.exit(0)
    except Exception as e:
        alog("error", "SYS", f"Unhandled top-level exception: {e}")
        notify_error(f"Bot crashed: {e}")
        raise
