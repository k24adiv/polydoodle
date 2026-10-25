"""
ptb_scanner.py — Multi-Asset PTB (Price to Beat) Scanner
Scans BTC, ETH, SOL, XRP simultaneously over a 60-second window
and returns a locked baseline mean for each asset.
"""

import time
import threading
import requests
import numpy as np

# ── Constants ──────────────────────────────────────────────────────────────────
BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price"
BINANCE_VOLUME_URL = "https://api.binance.com/api/v3/ticker/24hr"

ASSETS = ["ETH", "SOL", "XRP"]

SYMBOL_MAP = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
}

# Decimal precision per asset (for clean display)
PRECISION_MAP = {
    "BTC": 2,
    "ETH": 2,
    "SOL": 3,
    "XRP": 4,
}

# PTB confidence thresholds — scaled relative to typical price ranges
# Expressed as std-dev % of mean price (asset-agnostic normalization).
# HIGH   < 0.05% → very stable baseline, tight spreads expected
# MEDIUM < 0.40% → moderate noise, PTB still reliable
# LOW   >= 0.40% → choppy price action, treat baseline with caution
CONFIDENCE_PCT_HIGH   = 0.0005   # 0.05% of price
CONFIDENCE_PCT_MEDIUM = 0.0040   # 0.40% of price


# ── Price Fetching ─────────────────────────────────────────────────────────────

def get_asset_price(asset: str) -> float | None:
    """Fetch current spot price for a single asset from Binance."""
    symbol = SYMBOL_MAP.get(asset)
    if not symbol:
        print(f"  ⚠️  Unknown asset: {asset}")
        return None
    try:
        r = requests.get(BINANCE_PRICE_URL, params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["price"])
    except requests.exceptions.Timeout:
        print(f"  ⚠️  [{asset}] Binance timeout")
        return None
    except requests.exceptions.HTTPError as e:
        print(f"  ⚠️  [{asset}] HTTP error: {e}")
        return None
    except Exception as e:
        print(f"  ⚠️  [{asset}] Price fetch error: {e}")
        return None


def get_asset_volume(asset: str) -> float | None:
    """Fetch 24h quote volume (in USDT) for a single asset from Binance."""
    symbol = SYMBOL_MAP.get(asset)
    if not symbol:
        return None
    try:
        r = requests.get(BINANCE_VOLUME_URL, params={"symbol": symbol}, timeout=5)
        r.raise_for_status()
        return float(r.json()["quoteVolume"])  # USDT volume, more comparable across assets
    except Exception as e:
        print(f"  ⚠️  [{asset}] Volume fetch error: {e}")
        return None


# ── Confidence Scoring ─────────────────────────────────────────────────────────

def calc_confidence(prices: list[float]) -> tuple[str, float]:
    """
    Return (confidence_label, std_dev) using price-relative thresholds
    so the same logic works for BTC at $60k and XRP at $0.60.
    """
    if len(prices) < 2:
        return "LOW", 0.0

    std  = float(np.std(prices))
    mean = float(np.mean(prices))
    pct  = std / mean if mean else 0.0

    if pct < CONFIDENCE_PCT_HIGH:
        label = "HIGH"
    elif pct < CONFIDENCE_PCT_MEDIUM:
        label = "MEDIUM"
    else:
        label = "LOW"

    return label, std


# ── Threaded Per-Asset Collector ───────────────────────────────────────────────

def _collect_prices_for_asset(
    asset: str,
    duration: int,
    interval: float,
    results: dict,
    lock: threading.Lock,
):
    """
    Worker thread: polls Binance every `interval` seconds for `duration` seconds.
    Stores collected prices in shared `results` dict under `results[asset]`.
    """
    prices = []
    start  = time.time()
    prec   = PRECISION_MAP.get(asset, 4)

    while time.time() - start < duration:
        price = get_asset_price(asset)
        if price is not None:
            prices.append(price)
            elapsed = time.time() - start
            # FIX: acquire lock before printing so 4 concurrent threads
            # don't interleave their output lines
            with lock:
                print(f"  [{asset}][{elapsed:.0f}s] ${price:,.{prec}f}")
        time.sleep(interval)

    with lock:
        results[asset] = prices


# ── Main PTB Scanner ───────────────────────────────────────────────────────────

def scan_and_lock_ptb_multi(
    assets: list[str] = None,
    duration: int = 60,
    interval: float = 2.0,
) -> dict:
    """
    Scan all assets simultaneously over `duration` seconds.
    Returns a dict keyed by asset with PTB stats.

    Return structure:
    {
        "BTC": {
            "ptb":        67432.15,   # locked mean price
            "confidence": "HIGH",     # HIGH / MEDIUM / LOW
            "std":        12.34,      # std-dev in USD
            "samples":    28,         # number of price samples collected
            "prices":     [...],      # raw sample list (for candle classification)
            "ok":         True        # False if no prices collected
        },
        ...
    }
    """
    if assets is None:
        assets = ASSETS

    print(f"\n{'='*60}")
    print(f"🔍 PTB Phase — Scanning {len(assets)} assets for {duration}s")
    print(f"   Assets : {', '.join(assets)}")
    print(f"   Interval: every {interval}s  |  Threads: {len(assets)}")
    print(f"{'='*60}")

    raw_results: dict[str, list[float]] = {}
    lock    = threading.Lock()
    threads = []

    # Spin up one thread per asset
    for asset in assets:
        t = threading.Thread(
            target=_collect_prices_for_asset,
            args=(asset, duration, interval, raw_results, lock),
            daemon=True,
            name=f"PTB-{asset}",
        )
        threads.append(t)
        t.start()

    # Wait for all threads to finish
    for t in threads:
        t.join(timeout=duration + 10)

    print(f"\n{'─'*60}")
    print("🔒 PTB LOCKED RESULTS")
    print(f"{'─'*60}")

    final: dict[str, dict] = {}

    for asset in assets:
        prices = raw_results.get(asset, [])
        prec   = PRECISION_MAP.get(asset, 4)

        if not prices:
            print(f"  ❌ [{asset}] No data collected — marking as BLOCKED")
            final[asset] = {
                "ptb":        None,
                "confidence": "LOW",
                "std":        None,
                "samples":    0,
                "prices":     [],
                "ok":         False,
            }
            continue

        ptb               = round(float(np.mean(prices)), prec)
        confidence, std   = calc_confidence(prices)

        final[asset] = {
            "ptb":        ptb,
            "confidence": confidence,
            "std":        round(std, prec),
            "samples":    len(prices),
            "prices":     prices,
            "ok":         True,
        }

        bar = {"HIGH": "🟢", "MEDIUM": "🟡", "LOW": "🔴"}[confidence]
        print(
            f"  {bar} [{asset}]  PTB=${ptb:,.{prec}f}  "
            f"Conf={confidence:<6}  Std=${std:.{prec}f}  "
            f"Samples={len(prices)}"
        )

    print(f"{'='*60}\n")
    return final


# ── Convenience Helper ─────────────────────────────────────────────────────────

def ptb_is_tradeable(ptb_result: dict) -> bool:
    """
    Quick gate-check: returns True only if PTB collected data and
    confidence is not LOW (LOW = choppy baseline, unreliable).
    LOW-confidence assets are still returned but flagged here.
    Callers can decide whether to skip or proceed with caution.
    """
    return ptb_result.get("ok", False) and ptb_result.get("confidence") != "LOW"


# ── Standalone Test ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Quick smoke-test with a 12-second scan (instead of 60s) for dev speed
    TEST_DURATION = 12
    TEST_INTERVAL = 2.0

    print("🧪 Running PTB scanner smoke-test (12s scan)...\n")

    ptbs = scan_and_lock_ptb_multi(
        assets=ASSETS,
        duration=TEST_DURATION,
        interval=TEST_INTERVAL,
    )

    print("\n📊 Final PTB Summary:")
    for asset, data in ptbs.items():
        if data["ok"]:
            tradeable = "✅ tradeable" if ptb_is_tradeable(data) else "⚠️  low-conf"
            prec = PRECISION_MAP.get(asset, 4)
            print(
                f"  {asset:<4}  PTB=${data['ptb']:>12,.{prec}f}  "
                f"[{data['confidence']}]  {tradeable}"
            )
        else:
            print(f"  {asset:<4}  ❌ NO DATA — asset blocked for this window")