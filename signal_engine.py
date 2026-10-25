"""
signal_engine.py — Multi-Asset Signal Engine  (v6)
---------------------------------------------------
Changes vs v5
~~~~~~~~~~~~~
1. seconds_remaining=None / 0 bug fixed — `or 0.0` replaced with explicit
   `is not None` guard.  None → 9999.0 (no time constraint assumed); 0.0
   (legitimately zero) is now distinct from None and routes to <120s skip
   correctly.  Previously, any None call silently SKIPped every signal.

2. get_dynamic_limit_price return type corrected: float -> float | None.

3. PERCENTAGE_TRIGGER comments corrected — were quoting wrong percentages
   (ETH said 0.20% but value is 0.15%, etc.).  Values unchanged; only
   comments fixed.  Do NOT change values — they underpin the live 82.6% WR.

4. BTC added explicitly to STRONG_THRESH at 0.001 (0.10%), matching ETH.
   Previously BTC fell through to DEFAULT_STRONG_THRESH = 0.002 (0.20%),
   contradicting the "BTC/ETH → 0.10%" docstring.

5. classify_candle length guard tightened: < 2 → != 4.  Defends against
   callers passing a malformed list without changing correct-path behaviour.

Unchanged from v5
~~~~~~~~~~~~~~~~~
- AVOID_HOURS       : {0,1,2,3}
- Limit schedule    : 0.62 / 0.64 / 0.65 / None (<120s)
- BULL_MEDIUM hard block, candle-direction alignment
- PERCENTAGE_TRIGGER values: ETH=0.0015, SOL=0.0020, XRP=0.0015
- FADE_THRESHOLD_PCT per-asset: ETH=0.0005, SOL/XRP=0.0008

INTERFACE CONTRACT (unchanged — main.py unpacks 4 values):
  classify_candle(ptb_prices, asset="BTC")
      -> str  e.g. "BULL_STRONG", "BEAR_MEDIUM", "DOJI"

  get_signal(current_price, ptb, prev_dist_pct, seconds_remaining,
             candle_type, asset)
      -> (direction: str, dist_pct: float, reason: str, limit_price: float | None)
         direction in {"YES", "NO", "SKIP"}
         limit_price is None only when direction == "SKIP" (late-entry guard)
"""

from datetime import datetime, timezone

# ── CONFIGURATION ──────────────────────────────────────────────────────────────

# Per-asset minimum distance trigger (% of price).
# ETH        : 0.15% — stable, high-price asset
# SOL        : 0.20% — volatile, low-price asset
# XRP        : 0.15% — noisier than ETH but tighter range than SOL
PERCENTAGE_TRIGGER = {
    "ETH": 0.0015,
    "SOL": 0.0020,
    "XRP": 0.0015,
}
DEFAULT_TRIGGER = 0.0015  # safe fallback for any unlisted asset

# Per-asset fade threshold — minimum dist_pct shrinkage that counts as fading.
# ETH  : 0.05% — high-price, stable asset; small shrinkage is real signal
# SOL  : 0.08% — noisier tick-to-tick; need bigger move before calling fade
# XRP  : 0.08% — similar noise profile to SOL at its price range
# If an asset isn't listed, DEFAULT_FADE_THRESHOLD applies.
FADE_THRESHOLD_PCT = {
    "ETH": 0.0005,   # 0.05%
    "SOL": 0.0008,   # 0.08%
    "XRP": 0.0008,   # 0.08%
}
DEFAULT_FADE_THRESHOLD = 0.0005  # safe fallback for any unlisted asset

# Hours (UTC) to avoid — thin markets / wide spreads
AVOID_HOURS = {0, 1, 2, 3}

# ── CANDLE STRENGTH THRESHOLDS (asset-volatility-aware) ───────────────────────
STRONG_THRESH = {
    "BTC": 0.001, "ETH": 0.001,   # 0.10% body for STRONG (stable, high-price)
    "SOL": 0.002, "XRP": 0.002,   # 0.20% body for STRONG (noisier assets)
}
DEFAULT_STRONG_THRESH = 0.002


# ── DYNAMIC LIMIT PRICE ────────────────────────────────────────────────────────

def get_dynamic_limit_price(seconds_remaining: float) -> float | None:
    """
    Returns the limit price for the current time bucket.

    Capped at 0.65 (was 0.75).  At 0.75 you need a 75% WR to break even;
    our demonstrated edge is ~67%.  Late entries (<120s) return None —
    caller should skip rather than enter at an uneconomic price.

    seconds_remaining   price    break-even WR
    -----------------   -----    -------------
    > 360s              0.62     62%
    240 - 360s          0.64     64%
    120 - 240s          0.65     65%
    < 120s              None     skip — margin gone
    """
    if seconds_remaining > 360:   return 0.62
    elif seconds_remaining > 240: return 0.64
    elif seconds_remaining > 120: return 0.65
    else:                         return None   # signal: do not enter


# ── CANDLE CLASSIFICATION ──────────────────────────────────────────────────────

def classify_candle(ptb_prices: list, asset: str = "BTC") -> str:
    """
    Classifies the 15m candle for the given asset.

    IMPORTANT — expected list layout: [open, high, low, close]
      ptb_prices[0]  → open   (used as open_price)
      ptb_prices[-1] → close  (last element; MUST be close, not high or low)
      max(ptb_prices)→ high   (works correctly only with [o,h,l,c] order)
      min(ptb_prices)→ low    (works correctly only with [o,h,l,c] order)
    Passing a different order (e.g. [o,c,h,l]) will silently misclassify candles.
    Callers: get_15m_candle_type() always passes [o, h, l, c] from the Binance kline.

    Returns one of:
        BULL_STRONG / BEAR_STRONG  — high-conviction directional body
        BULL_MEDIUM / BEAR_MEDIUM  — moderate body
        DOJI                       — indecisive / no clear move

    Asset-sensitive thresholds:
        BTC / ETH -> Strong body > 0.10% of price
        SOL / XRP -> Strong body > 0.20% of price
    """
    if not ptb_prices or len(ptb_prices) != 4:
        return "DOJI"

    open_price  = ptb_prices[0]
    close_price = ptb_prices[-1]
    high_price  = max(ptb_prices)
    low_price   = min(ptb_prices)

    if open_price == 0:
        return "DOJI"

    body        = abs(close_price - open_price)
    total_range = high_price - low_price

    if total_range == 0:
        return "DOJI"

    ratio         = body / total_range
    body_pct      = body / open_price

    strong_thresh = STRONG_THRESH.get(asset, DEFAULT_STRONG_THRESH)
    medium_thresh = strong_thresh / 2.0

    direction = "BULL" if close_price >= open_price else "BEAR"

    if   ratio > 0.7 and body_pct > strong_thresh: return f"{direction}_STRONG"
    elif ratio > 0.4 and body_pct > medium_thresh:  return f"{direction}_MEDIUM"
    else:                                            return "DOJI"


# ── TIME BUCKET (logging / display only) ──────────────────────────────────────

def get_time_bucket(seconds_remaining: float) -> str:
    if   seconds_remaining > 600: return "WAITING >10m"
    elif seconds_remaining > 480: return "WAITING 8-10m"
    elif seconds_remaining > 300: return "ENTRY 5-8m"
    elif seconds_remaining > 180: return "ENTRY 3-5m"
    elif seconds_remaining > 60:  return "ENTRY 1-3m"
    else:                         return "LATE <1m"


# ── MAIN SIGNAL FUNCTION ───────────────────────────────────────────────────────

def get_signal(
    current_price:     float,
    ptb:               float,
    prev_dist_pct:     float | None = None,
    seconds_remaining: float | None = None,
    candle_type:       str          = "BULL_STRONG",
    asset:             str          = "BTC",
) -> tuple:
    """
    Evaluates whether conditions justify a trade entry.

    Parameters
    ----------
    current_price     : latest asset price
    ptb               : price-to-beat (window baseline mean)
    prev_dist_pct     : dist_pct from the previous tick (fade detection)
    seconds_remaining : seconds left in the 15m window
    candle_type       : output of classify_candle() e.g. "BULL_STRONG"
    asset             : BTC / ETH / SOL / XRP

    Returns
    -------
    (direction, dist_pct, reason, limit_price)
        direction   : "YES" | "NO" | "SKIP"
        dist_pct    : % distance from PTB (0.0 on SKIP)
        reason      : human-readable explanation
        limit_price : float or None (None = skip late entry on caller side)
    """
    # seconds_remaining=None means no time data available — treat as no constraint
    # (9999.0 routes to the >360s bucket, limit=0.62).
    # Do NOT use `or 0.0`: that collapses both None and a legitimate 0.0 to zero,
    # which returns limit_price=None and unconditionally skips every signal.
    secs        = seconds_remaining if seconds_remaining is not None else 9999.0
    limit_price = get_dynamic_limit_price(secs)

    def skip(reason: str) -> tuple:
        return "SKIP", 0.0, reason, limit_price

    # ── 0. Sanity ──────────────────────────────────────────────────────────────
    if current_price == 0 or ptb == 0:
        return skip("Invalid price (0)")

    distance = current_price - ptb
    dist_pct = abs(distance) / ptb

    # Resolve per-asset trigger
    asset_trigger = PERCENTAGE_TRIGGER.get(asset, DEFAULT_TRIGGER)

    # ── 1. AVOID_HOURS (UTC) ──────────────────────────────────────────────────
    current_hour_utc = datetime.now(timezone.utc).hour
    if current_hour_utc in AVOID_HOURS:
        return skip(f"Avoid-hour block: {current_hour_utc:02d}:xx UTC")

    # ── 2. Late-entry limit-price guard ───────────────────────────────────────
    # If limit_price is None, window is too late for economic entry.
    if limit_price is None:
        return skip(f"Late-entry skip — <120s left, limit price uneconomic")

    # ── 3. DOJI ────────────────────────────────────────────────────────────────
    if candle_type == "DOJI":
        return skip("DOJI candle — no clear direction")

    # ── 3b. BULL_MEDIUM hard block ────────────────────────────────────────────
    # Historical WR ~18% — well below break-even at any limit price we use.
    # BEAR_MEDIUM is NOT blocked here; it clears via the normal threshold (step 6).
    if candle_type == "BULL_MEDIUM":
        return skip("BULL_MEDIUM blocked — ~18% WR, below break-even")

    # ── 4. Parse candle direction & strength ──────────────────────────────────
    if "_" in candle_type:
        candle_dir, candle_strength = candle_type.split("_", 1)
    else:
        candle_dir, candle_strength = "NEUTRAL", candle_type

    # ── 5. Per-asset distance threshold ───────────────────────────────────────
    if dist_pct <= asset_trigger:
        return skip(
            f"{asset} dist {dist_pct*100:.3f}% <= "
            f"{asset_trigger*100:.3f}% threshold"
        )

    # ── 6. Trade direction from price ─────────────────────────────────────────
    trade_direction = "YES" if distance > 0 else "NO"
    expected_candle = "BULL" if trade_direction == "YES" else "BEAR"

    # ── 7. Candle-direction alignment ────────────────────────────────────────
    if candle_dir != "NEUTRAL" and candle_dir != expected_candle:
        return skip(
            f"Candle mismatch: {candle_type} conflicts with "
            f"{trade_direction} signal on {asset}"
        )

    # ── 8. Momentum / fade filter ────────────────────────────────────────────
    if prev_dist_pct is not None:
        fade_thresh = FADE_THRESHOLD_PCT.get(asset, DEFAULT_FADE_THRESHOLD)
        shrinkage   = prev_dist_pct - dist_pct
        if shrinkage >= fade_thresh:
            return skip(
                f"Momentum fading on {asset} — "
                f"shrank {shrinkage*100:.3f}% "
                f"(>= {fade_thresh*100:.3f}%)"
            )

    # ── ENTRY PASSED ──────────────────────────────────────────────────────────
    bucket = get_time_bucket(secs)
    reason = (
        f"Valid Entry | {asset} {trade_direction} | "
        f"dist={dist_pct*100:.3f}% > {asset_trigger*100:.3f}% | "
        f"candle={candle_type} | {bucket} | limit={limit_price:.2f}"
    )
    return trade_direction, dist_pct, reason, limit_price