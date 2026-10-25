"""
live_executor.py — Multi-Asset Live Execution Engine
----------------------------------------------------
MERGED + FIXED (v7):

  v7 changes (BUG FIX — invalid amounts on every live order):
    - execute_live_trade: replaced the order sizing algorithm entirely.
      ROOT CAUSE: py_clob_client_v2 internally truncates `size` to 2dp via
      round_down(size, 2) BEFORE computing maker_amount = size_2dp * price.
      The old algorithm produced shares like 2.3438 which passed our own
      check, but the library truncated to 2.34 and computed maker=1.4976
      (4dp), which the API rejected with "invalid amounts". Our gross_stake
      of $1.50 was also wrong — it never matched what the library sent.
      FIX: iterate candidate sizes in 0.01 steps using Python Decimal
      arithmetic. For each candidate, check if (size * price) % 0.01 == 0
      (i.e. exactly representable in ≤2dp). Pick the largest passing value.
      This guarantees the library's internal maker_amount will be exactly
      2dp regardless of price, since we're already working in the same
      0.01-step space the library uses. gross_stake is now set directly
      from the Decimal maker value, not re-derived from floats.

MERGED + FIXED (v6):

  From v5 (all retained):
    - _has_open_positions: non-200 now returns True (safe/blocked)
    - _get_token_id: exceptions now logged; tokens[1] guarded vs IndexError
    - execute_sequential_multi_asset: all branches carry a "reason" key
    - _check_vpn + BLOCKED_COUNTRIES
    - _get_market_price (live orderbook ask)
    - _calc_stake_usd (env-loaded, hard-capped)
    - _log_live (IST-stamped flat log)
    - Full execute_live_trade: dry-run, VPN, too-late guard, IOC, retry
    - Power trade path fully removed
    - test_connection: slug updated from BTC to ETH
    - _verify_slug_exists: inspects market fields (closed/resolved/active)
    - _get_market_price: resolved-market circuit breaker (ask >= 0.97 → None)
    - get_active_15m_slug: DEBUG log showing computed UTC window range

  v6 changes (latency + stale-guard fixes + crash hardening):
    - execute_live_trade: new `cached_token_id` parameter. If the pre-cached
      token ID from main.py's window-open fetch is provided, _get_token_id is
      skipped entirely (saves 1 Gamma API round-trip, ~0.5–2s of latency).
      If cached_token_id is None (slug hadn't propagated yet), falls back to
      a fresh _get_token_id call — this is the lazy retry path.
    - execute_sequential_multi_asset: reads `_cached_token_id` from the opp
      dict. If present, _verify_slug_exists is also skipped (pre-caching
      already ran that check via get_yes_token_id → _verify_slug_exists).
      This eliminates two redundant Gamma calls per asset at signal-fire time.
    - execute_live_trade: too-late guard now recomputes actual seconds
      remaining at guard time (900 − time.time() % 900) rather than using
      the stale `secs_left` value captured at signal-fire.
    - execute_live_trade: orderbook-not-ready 15s retry guard also switched
      from stale `secs_left` to `actual_secs_left` for the same reason.
    - _get_client: wrapped in threading.Lock() to fix TOCTOU race between
      the main trade thread and background redemption threads that can both
      call _get_client() before it is initialised. threading is now imported.
    - _log_live: wrapped in try/except so a disk error (full disk, bad
      permissions) cannot crash the executor in the middle of a live order.
    - get_active_15m_slug: replaced deprecated datetime.utcfromtimestamp()
      with datetime.fromtimestamp(ts, tz=timezone.utc) for Python 3.12+.
    - _print_and_log: was defined but never called — live fills were silently
      never written to ptb_log.json. Now called after both the first IOC fill
      path and the 15s-retry fill path.

  v8 (BUG 23, 24, VPN — resolved-market chain + window-level VPN):
    - fetch_yes_price_raw: new public helper that returns the actual best-ask
      price WITHOUT the _get_market_price circuit breaker. fetch_yes_price was
      calling _get_market_price which deliberately returns None for asks >= 0.97
      (Bug 20 circuit breaker). This meant the Bug 21 guard in main.py
      (yes_price_now >= 0.97) could never fire — None >= 0.97 is always False.
      fetch_yes_price_raw bypasses the circuit breaker and returns the real price
      (including 0.99 for resolved markets). Used ONLY by the Bug 21 pre-flight
      guard in monitor_and_decide(). Order sizing still uses _get_market_price.
    - execute_live_trade: when _get_market_price returns None (resolved market or
      API failure), result now includes \"reason\": \"resolved_market\" in addition to
      the error string. This lets execute_sequential_multi_asset distinguish between
      \"book missed but tradeable\" (no_successful_fill → retry) and \"book is dead\"
      (resolved_market → skip window immediately). Previously both returned
      no_successful_fill, causing a 633s retry loop on a corpse.
    - execute_sequential_multi_asset: when execute_live_trade returns
      reason=\"resolved_market\", propagates it immediately to the caller instead of
      continuing to the next asset or returning no_successful_fill.
    - check_vpn_once: new public wrapper around _check_vpn() so main.py can call
      VPN check at window entry instead of inside execute_live_trade. After the
      first successful check _vpn_checked=True, so all subsequent calls (including
      the guard inside execute_live_trade) are instant no-ops — no network I/O.
      The guard inside execute_live_trade is retained as a standalone safety net.

  v6.1 (BUG 14 — slug hard-skip killed all live trades):
    - execute_sequential_multi_asset: previous code called _verify_slug_exists
      once (~4s) then hard-skipped with `continue` if slug wasn't live yet.
      Polymarket publishes 15m slugs 30–90s after the window boundary, so
      every early-window signal (e.g. 789s remaining) was guaranteed to skip
      before the slug appeared. This explains 0 live trades vs 15 dry-run
      trades per day — dry-run bypasses the slug check entirely.
      FIX: when cached_token_id is None and secs_left > 750 (first ~2.5 min
      of the window only), executor now retries _verify_slug_exists every 10s
      for up to 90s before giving up. Mid-window signals (≤750s remaining)
      skip the wait entirely — at that point a verify failure is VPN lag, not
      slug propagation. Resolved/closed markets still hard-skip immediately.
"""

import os
import json
import logging
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LIVE_HOST           = "https://clob.polymarket.com"
GAMMA_HOST          = "https://gamma-api.polymarket.com"
MAX_LIVE_STAKE_USD  = 10.0
# ── FEE-ADJUSTED PROFITABILITY THRESHOLD ──────────────────────────────────────
# V2 charges 7% taker fee on ALL FOK/IOC orders (taker-only markets).
# Break-even win rate at a given limit price L (with 7% fee):
#   gross_cost = L * shares
#   total_cost = gross_cost * 1.07
#   payout     = 1.00 * shares  (win pays $1/share)
#   break_even = total_cost / shares = L * 1.07
#
# Examples:
#   L=0.55 → break-even win rate = 58.85%   (vs 55% without fee)
#   L=0.60 → break-even win rate = 64.20%   (vs 60% without fee)
#   L=0.65 → break-even win rate = 69.55%   (vs 65% without fee)
#
# MIN_EXPECTED_PROFIT is the minimum post-fee net profit per trade to accept.
# At $1.50 stake and L=0.60: shares ≈ 2.5, payout=$2.50, cost=$1.61 → profit=$0.89
# Set to $0.10 as a safety floor — signal_engine's limit_price logic is the
# real filter; this is a last-resort sanity check.
MIN_EXPECTED_PROFIT = 0.10   # post-fee; was 0.85 (pre-fee, overly strict)
LOG_FILE            = "ptb_log.json"
IST                 = timezone(timedelta(hours=5, minutes=30))

BLOCKED_COUNTRIES = {
    "United States", "United Kingdom of Great Britain and Northern Ireland",
    "Cuba", "Iran", "North Korea", "Syria", "Russia", "Belarus",
    "Myanmar", "Ukraine",
}

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------
live_log = logging.getLogger("live_executor")
live_log.setLevel(logging.DEBUG)
_lh = logging.FileHandler("live_trades.log", encoding="utf-8")
_lh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
live_log.addHandler(_lh)

_client      = None
_vpn_checked = False
_client_lock = threading.Lock()   # guards _get_client() against TOCTOU race
                                   # between the main trade thread and background
                                   # redemption threads that both call _get_client()
_log_file_lock = threading.Lock() # guards ptb_log.json writes so _print_and_log
                                   # cannot race with reconcile_trade (auto_redeem)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _to_tick(price: float) -> float:
    return float(f"{price:.2f}")


def _log_live(msg: str) -> None:
    """Appends an IST-stamped line to the flat trade log. Never raises — a
    disk error during a live trade must not crash the executor mid-order."""
    ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    try:
        with open("live_trades.log", "a", encoding="utf-8") as f:
            f.write(f"[{ist}] {msg}\n")
    except Exception as e:
        live_log.error(f"_log_live: could not write to live_trades.log — {e}")


def get_active_15m_slug(asset_name: str) -> str:
    """
    Returns the Polymarket slug for the current 15-minute window.

    FIX (v5): Added a DEBUG log line showing the computed UTC window range
    so that any stale-timestamp bugs (bot clock drift, OS clock skew) are
    immediately visible in the trace without any logic overhead.
    """
    now            = int(time.time())
    current_window = now - (now % 900)
    slug           = f"{asset_name.lower()}-updown-15m-{current_window}"
    live_log.debug(
        f"[{asset_name}] slug window={current_window} "
        f"({datetime.fromtimestamp(current_window, tz=timezone.utc).strftime('%H:%M')}–"
        f"{datetime.fromtimestamp(current_window + 900, tz=timezone.utc).strftime('%H:%M')} UTC)"
    )
    return slug


# ---------------------------------------------------------------------------
# Slug liveness check
# ---------------------------------------------------------------------------

def _verify_slug_exists(slug: str) -> bool:
    """
    Returns True ONLY if the slug refers to a market that is currently open
    for trading (active=True, closed=False, resolved=False).

    FIX (v5): The previous implementation returned True for any non-empty
    API response, which meant resolved markets (price frozen at ~0.99) also
    passed the check. Polymarket never removes resolved markets from the
    /events endpoint — they remain permanently with closed/resolved flags
    set. This caused every trade in an affected window to hit a dead
    orderbook and read a stale 0.9900 ask price.

    A resolved market is not retried on the second attempt (it will not
    become open again), so _verify_slug_exists returns False immediately
    after detecting a non-tradeable market rather than sleeping and looping.
    The 2-second retry is still performed for genuinely absent slugs (e.g.
    clock drift where the new window hasn't propagated to the API yet).
    """
    import requests
    for attempt in range(2):
        try:
            r = requests.get(
                f"{GAMMA_HOST}/events",
                params={"slug": slug},
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                if data and len(data) > 0:
                    # Pull the first real market entry (filter string-only stubs)
                    markets = [
                        m for m in data[0].get("markets", [])
                        if not isinstance(m, str)
                    ]
                    if markets:
                        m        = markets[0]
                        closed   = m.get("closed",   True)   # default True = safe
                        resolved = m.get("resolved", False)   # default False = safe
                        active   = m.get("active",   False)  # default False = safe

                        if not closed and not resolved and active:
                            return True

                        # Market exists but is not tradeable — do NOT retry.
                        # Resolved markets stay resolved; sleeping won't help.
                        live_log.warning(
                            f"_verify_slug_exists: slug '{slug}' found but NOT "
                            f"tradeable (closed={closed}, resolved={resolved}, "
                            f"active={active}) — skipping window."
                        )
                        return False

        except Exception as e:
            live_log.warning(f"Slug check failed (attempt {attempt + 1}): {e}")

        if attempt == 0:
            time.sleep(2)   # only sleep between attempts, not after a resolved-market hit

    return False


# ---------------------------------------------------------------------------
# VPN check
# ---------------------------------------------------------------------------

def _check_vpn() -> None:
    global _vpn_checked
    if _vpn_checked:
        return
    import requests
    try:
        r       = requests.get("https://ipapi.co/json/", timeout=6)
        data    = r.json()
        country = data.get("country_name", "Unknown")
        ip      = data.get("ip", "Unknown")
        if country == "India" or country in BLOCKED_COUNTRIES:
            raise RuntimeError(
                f"VPN not active or connected to blocked country: {country} ({ip}). "
                f"Connect ProtonVPN to Romania before running the bot."
            )
        print(f"  VPN check passed — country: {country} ({ip})")
        live_log.info(f"VPN check passed: {country} ({ip})")
        _vpn_checked = True
    except RuntimeError:
        raise
    except Exception as e:
        print(f"  VPN check skipped (could not reach ipapi.co): {e}")
        _vpn_checked = True


def check_vpn_once() -> None:
    """
    Public wrapper around _check_vpn() for use by main.py at window entry.

    VPN ARCHITECTURE:
    _check_vpn() guards on the module-level _vpn_checked flag. After the
    first successful check the flag is True and ALL subsequent calls
    (including the guard inside execute_live_trade) return instantly —
    zero network I/O, zero latency. Calling this at window entry means:
      • VPN failure is caught before monitoring, not milliseconds before FOK
      • The hot path (signal → token lookup → FOK) has no network overhead
      • execute_live_trade's _check_vpn() call is a guaranteed no-op

    Raises RuntimeError if VPN is not active or connected to a blocked country.
    Any other exception (ipapi.co unreachable) is swallowed by _check_vpn()
    and _vpn_checked is set True (fail-open for network errors).
    """
    _check_vpn()


# ---------------------------------------------------------------------------
# CLOB client singleton  (thread-safe via _client_lock)
# ---------------------------------------------------------------------------

def _get_client():
    global _client
    with _client_lock:
        if _client is not None:
            return _client

        from py_clob_client_v2 import ClobClient, ApiCreds
        from py_clob_client_v2.constants import POLYGON

        key            = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
        api_key        = os.getenv("CLOB_API_KEY", "").strip()
        api_secret     = os.getenv("CLOB_API_SECRET", "").strip()
        api_passphrase = os.getenv("CLOB_API_PASSPHRASE", "").strip()

        if not key:
            raise EnvironmentError("POLYGON_PRIVATE_KEY not set in .env")
        if not all([api_key, api_secret, api_passphrase]):
            raise EnvironmentError(
                "CLOB_API_KEY / CLOB_API_SECRET / CLOB_API_PASSPHRASE not all set in .env"
            )

        creds   = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )
        _client = ClobClient(
            host=LIVE_HOST, chain_id=POLYGON, key=key, creds=creds
        )
        print("  Client created (py-clob-client-v2, EOA mode)")
        live_log.info("Client created with py-clob-client-v2 (EOA)")
        return _client


# ---------------------------------------------------------------------------
# Position guard  (non-200 returns True = blocked)
# ---------------------------------------------------------------------------

def _has_open_positions() -> bool:
    """
    True  -> positions exist OR the check itself failed (safe default: block).
    False -> confirmed no open positions.
    """
    import requests
    from eth_account import Account

    key = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
    if not key:
        live_log.warning("_has_open_positions: No private key set — blocking (safe default).")
        return True

    wallet = Account.from_key(key).address
    try:
        r = requests.get(
            f"{GAMMA_HOST}/positions",
            params={"user": wallet},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            # data.get("data", data) can return None if the API returns {"data": null}
            positions = data.get("data", data)
            if positions is None:
                live_log.warning(
                    "_has_open_positions: API returned {\"data\": null} — "
                    "blocking as precaution (WHY: ambiguous null response)."
                )
                return True
            if not isinstance(positions, list):
                live_log.warning(
                    f"_has_open_positions: unexpected positions type={type(positions).__name__} "
                    f"— blocking as precaution (WHY: expected list, got something else)."
                )
                return True
            open_positions = [p for p in positions if float(p.get("size", 0)) > 0.0001]
            if open_positions:
                slugs = [p.get("market", p.get("conditionId", "?")) for p in open_positions]
                live_log.warning(
                    f"_has_open_positions: BLOCKED — {len(open_positions)} open position(s) found. "
                    f"WHY: {slugs}"
                )
            return len(open_positions) > 0
        else:
            live_log.warning(
                f"_has_open_positions: Positions API returned HTTP {r.status_code} — "
                f"blocking as precaution (WHY: non-200 status, cannot confirm clear)."
            )
            return True
    except Exception as e:
        live_log.warning(
            f"_has_open_positions: Exception during check — blocking as precaution. "
            f"WHY: {e}"
        )
        return True


# ---------------------------------------------------------------------------
# Token ID resolution
# ---------------------------------------------------------------------------

def _get_token_id(market_slug: str, direction: str) -> Optional[str]:
    """
    Resolves the CLOB token ID for a slug + direction.
    Prefers outcome-name matching ('up'/'down') with index fallback.
    """
    import requests
    try:
        r = requests.get(
            f"{GAMMA_HOST}/events",
            params={"slug": market_slug},
            timeout=10
        )
        r.raise_for_status()
        events = r.json()

        if not events:
            live_log.warning(f"No events found for slug: {market_slug}")
            return None

        # Filter out string-only market entries
        markets = [m for m in events[0].get("markets", []) if not isinstance(m, str)]
        if not markets:
            live_log.warning(f"No valid markets in event for slug: {market_slug}")
            return None

        market     = markets[0]
        tokens_raw = market.get("clobTokenIds") or market.get("clob_token_ids")
        outcomes   = market.get("outcomes")

        if not tokens_raw:
            live_log.error(f"No clobTokenIds found for {market_slug}")
            return None

        tokens   = json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw
        outcomes = json.loads(outcomes)   if isinstance(outcomes, str)   else (outcomes or [])

        print(f"  Token lookup: outcomes={outcomes} | direction={direction}")

        # Primary: match by outcome name ("up" / "down")
        want = "up" if direction == "YES" else "down"
        for i, name in enumerate(outcomes):
            if name.strip().lower() == want and i < len(tokens):
                token_id = str(tokens[i])
                print(f"  Matched outcome '{name}' -> token {token_id[:12]}...")
                live_log.info(f"Token ID for {direction} on {market_slug}: {token_id}")
                return token_id

        # Fallback: positional index (guard against single-token markets)
        idx = 0 if direction == "YES" else 1
        if idx >= len(tokens):
            live_log.warning(
                f"Fallback token[{idx}] requested but only {len(tokens)} token(s) "
                f"found for slug: {market_slug}"
            )
            return None

        token_id = str(tokens[idx])
        print(f"  Fallback token[{idx}]: {token_id[:12]}...")
        live_log.warning(f"Used fallback token index {idx} for {direction} on {market_slug}")
        return token_id

    except Exception as e:
        live_log.error(f"Token ID fetch failed for {market_slug} ({direction}): {e}")
        return None


# ---------------------------------------------------------------------------
# Live orderbook price
# ---------------------------------------------------------------------------

def _get_market_price(token_id: str) -> Optional[float]:
    """
    Returns the best ask from the live CLOB orderbook, falling back to the
    midpoint endpoint if the book is unavailable.

    FIX (v5): Added a resolved-market circuit breaker. If the best ask is
    >= 0.97 the market is almost certainly resolved (YES token frozen near
    1.0, NO token near 0.0). Rather than propagating the stale 0.99 price
    into limit-price logic, we return None and log a warning. This acts as
    a second line of defence behind _verify_slug_exists for markets that
    resolve mid-window after the initial liveness check passed.
    """
    import requests

    # Threshold above which we treat the market as resolved
    RESOLVED_PRICE_THRESHOLD = 0.97

    try:
        r = requests.get(
            f"{LIVE_HOST}/book", params={"token_id": token_id}, timeout=8
        )
        if r.status_code == 200:
            asks = r.json().get("asks", [])
            if asks:
                price = _to_tick(float(asks[0].get("price", 0.5)))
                # FIX: circuit breaker — resolved markets freeze near 0.99/1.00
                if price >= RESOLVED_PRICE_THRESHOLD:
                    live_log.warning(
                        f"_get_market_price: ask={price} on token {token_id[:12]}... "
                        f">= {RESOLVED_PRICE_THRESHOLD} — likely resolved market. "
                        f"Returning None to abort this window."
                    )
                    return None
                return price

        r2 = requests.get(
            f"{LIVE_HOST}/midpoint", params={"token_id": token_id}, timeout=8
        )
        if r2.status_code == 200:
            mid = r2.json().get("mid")
            if mid:
                price = _to_tick(float(mid))
                if price >= RESOLVED_PRICE_THRESHOLD:
                    live_log.warning(
                        f"_get_market_price: midpoint={price} on token {token_id[:12]}... "
                        f">= {RESOLVED_PRICE_THRESHOLD} — likely resolved market. "
                        f"Returning None to abort this window."
                    )
                    return None
                return price

        return None
    except Exception as e:
        live_log.warning(f"Could not fetch market price: {e}")
        return None


# ---------------------------------------------------------------------------
# Public YES-price helpers (used by main.py at signal-fire time)
# ---------------------------------------------------------------------------

def get_yes_token_id(
    asset: str,
    max_attempts: int = 4,
    retry_delay:  float = 8.0,
) -> Optional[str]:
    """
    Pre-fetch the YES token ID for the current 15m market for `asset`.
    Call this once at window start so the Gamma API round-trips are paid
    upfront, not at signal-fire time.

    Retry logic (slug propagation lag):
        Polymarket Gamma API typically publishes the new 15m slug within
        20-30s of the window boundary.  If the first verify fails (slug
        not yet live), this retries up to max_attempts times.

        Default: 4 attempts x 8s = up to 32s total wait.
        With 3 assets fetched in parallel threads (see main.py) the
        wall-clock overhead is just one retry cycle (~8s), not 3 x 8s.

    Returns None only after all attempts are exhausted — the executor
    lazy-retry path (_get_token_id fresh at signal time) handles that.
    """
    slug = get_active_15m_slug(asset)
    live_log.info(f"[{asset}] YES token slug: {slug}")

    for attempt in range(1, max_attempts + 1):
        if _verify_slug_exists(slug):
            token_id = _get_token_id(slug, "YES")
            if token_id:
                if attempt > 1:
                    live_log.info(
                        f"[{asset}] YES token resolved on attempt {attempt}/{max_attempts}: "
                        f"{token_id[:12]}..."
                    )
                return token_id
            # _verify_slug_exists passed but _get_token_id returned None --
            # market exists but token list is malformed; retrying wont help.
            live_log.warning(
                f"[{asset}] get_yes_token_id: slug verified but token lookup failed -- {slug}"
            )
            return None

        if attempt < max_attempts:
            live_log.info(
                f"[{asset}] Slug not ready yet (attempt {attempt}/{max_attempts}) "
                f"-- retrying in {retry_delay:.0f}s"
            )
            time.sleep(retry_delay)

    live_log.warning(
        f"[{asset}] get_yes_token_id: slug not live after {max_attempts} attempts -- {slug}"
    )
    return None


def fetch_yes_price(token_id: str) -> Optional[float]:
    """
    Fast YES-price snapshot using a pre-fetched token ID.
    Single HTTP call (no Gamma lookup). Call this at signal-fire time.
    Returns None on any failure — callers should treat None as missing data,
    not as a reason to skip the trade.

    NOTE: calls _get_market_price which returns None for asks >= 0.97
    (Bug 20 circuit breaker). Do NOT use this for the Bug 21 resolved-market
    pre-flight guard — use fetch_yes_price_raw instead.
    """
    if not token_id:
        return None
    return _get_market_price(token_id)


def fetch_yes_price_raw(token_id: str) -> Optional[float]:
    """
    Returns the actual best-ask YES price WITHOUT the resolved-market circuit
    breaker. Unlike fetch_yes_price (which calls _get_market_price and returns
    None for asks >= 0.97), this returns the real price even for frozen/resolved
    markets (e.g. 0.99).

    BUG 23 FIX: fetch_yes_price → _get_market_price returned None for any ask
    >= 0.97. The Bug 21 guard in monitor_and_decide() checked
    `yes_price_now >= 0.97` — but since None >= 0.97 is always False, the guard
    never fired and resolved markets slipped through to the executor. This
    function bypasses the circuit breaker so the guard sees the actual price.

    USAGE: ONLY for the Bug 21 pre-flight guard in main.py. The circuit breaker
    in _get_market_price is the correct path for order sizing — do NOT use this
    inside execute_live_trade or for any limit-price logic.

    Returns None only on genuine network/API failure (not for high prices).
    """
    if not token_id:
        return None
    import requests
    try:
        r = requests.get(
            f"{LIVE_HOST}/book", params={"token_id": token_id}, timeout=8
        )
        if r.status_code == 200:
            asks = r.json().get("asks", [])
            if asks:
                return _to_tick(float(asks[0].get("price", 0.5)))
        # Fallback: midpoint endpoint
        r2 = requests.get(
            f"{LIVE_HOST}/midpoint", params={"token_id": token_id}, timeout=8
        )
        if r2.status_code == 200:
            mid = r2.json().get("mid")
            if mid:
                return _to_tick(float(mid))
        return None
    except Exception as e:
        live_log.warning(
            f"fetch_yes_price_raw: price fetch failed for token "
            f"{str(token_id)[:12]}...: {e}"
        )
        return None


# ---------------------------------------------------------------------------
# Stake calculation
# ---------------------------------------------------------------------------

def _calc_stake_usd(stake_override: Optional[float] = None) -> float:
    """
    Returns the stake to use, capped at MAX_LIVE_STAKE_USD.
    If stake_override is set (e.g. from execute_sequential_multi_asset), use it.
    Otherwise reads LIVE_STAKE_USD from .env.
    """
    if stake_override is not None:
        stake = min(stake_override, MAX_LIVE_STAKE_USD)
        print(f"  [EXECUTOR] Stake (override): ${stake:.2f}")
        live_log.info(f"Stake from override: ${stake:.2f}")
        return round(stake, 2)

    load_dotenv(override=True)
    try:
        stake = float(os.getenv("LIVE_STAKE_USD", "3.00"))
    except ValueError:
        stake = 3.00
    stake  = min(stake, MAX_LIVE_STAKE_USD)
    loaded = round(stake, 2)
    print(f"  [EXECUTOR] Stake loaded from .env: ${loaded:.2f}")
    live_log.info(f"Stake loaded from .env: ${loaded:.2f}")
    return loaded


# ---------------------------------------------------------------------------
# Order status helper — extracted so it is not duplicated / one-lined
# ---------------------------------------------------------------------------

def _parse_order_status(order_status) -> tuple:
    """Returns (status: str, size_matched: str | int | float).

    V1 (get_order response): used 'size_matched' field.
    V2 (create_and_post_order response): uses 'takingAmount' (shares received)
    and 'makingAmount' (USDC spent). Falls back to takingAmount when
    size_matched is absent so fill detection works correctly on V2.
    """
    if isinstance(order_status, dict):
        size = (
            order_status.get("size_matched")
            or order_status.get("takingAmount")
            or 0
        )
        return (order_status.get("status", ""), size)
    return (
        getattr(order_status, "status", ""),
        getattr(order_status, "size_matched",
                getattr(order_status, "takingAmount", 0)),
    )


# ---------------------------------------------------------------------------
# Core trade execution
# ---------------------------------------------------------------------------

def execute_live_trade(
    market_slug:      str,
    direction:        str,
    market_price:     Optional[float],
    candle_type:      str = "",
    distance:         float = 0.0,
    secs_left:        float = 900.0,
    strike:           float = 0.0,
    verified_price:   Optional[float] = None,
    asset:            str = "",
    stake_override:   Optional[float] = None,
    cached_token_id:  Optional[str] = None,   # FIX: skip redundant Gamma lookup
) -> dict:
    """
    Places a live FOK/IOC order on Polymarket.
    Returns a result dict with at minimum {"success": bool, "error": str | None}.
    """
    from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

    result = {
        "success":   False,
        "order_id":  None,
        "stake_usd": 0.0,
        "error":     None,
        "dry_run":   True,
    }

    live_enabled = os.getenv("LIVE_ENABLED", "false").strip().lower() == "true"
    stake_usd    = _calc_stake_usd(stake_override)
    arrow        = "UP" if direction == "YES" else "DOWN"
    label        = asset or market_slug

    # ── DRY RUN ───────────────────────────────────────────────────────────────
    if not live_enabled:
        cents = f"{(market_price or 0.5) * 100:.1f}c"
        msg   = (f"DRY RUN | {label} {arrow} @ {cents} | ${stake_usd:.2f} | "
                 f"{candle_type} | dist=${distance:+.0f} | {secs_left:.0f}s | "
                 f"slug={market_slug}")
        print(f"\n  DRY RUN -> {arrow} @ {cents} | ${stake_usd:.2f} | "
              f"Set LIVE_ENABLED=true to go live")
        live_log.info(msg)
        _log_live(msg)
        result.update(dry_run=True, success=True, stake_usd=stake_usd)
        return result

    # ── VPN CHECK ─────────────────────────────────────────────────────────────
    try:
        _check_vpn()
    except RuntimeError as e:
        result["error"] = str(e)
        print(f"\n  VPN CHECK FAILED: {e}")
        live_log.error(str(e))
        _log_live(str(e))
        return result

    # ── TOO LATE GUARD ────────────────────────────────────────────────────────
    # FIX: recompute secs_left here rather than using the value captured at
    # signal-fire time in monitor_and_decide. By the time execution reaches
    # this point (Gamma API calls, CLOB price fetch, etc.) 3–7 seconds have
    # elapsed. A signal fired at secs_left=63 could be down to 56s by now.
    actual_secs_left = 900.0 - (time.time() % 900.0)
    if actual_secs_left < 60:
        msg = f"Too late to place order — only {actual_secs_left:.0f}s left in window"
        result["error"] = msg
        print(f"\n  SKIP — {msg}")
        live_log.info(msg)
        return result

    # ── TOKEN ID ──────────────────────────────────────────────────────────────
    # FIX: use the pre-cached token ID if provided (saves 1–2 Gamma API calls
    # and 0.5–2s of latency). If cached_token_id is None (slug hadn't propagated
    # at window open), fall back to a fresh lookup — this is the lazy retry path.
    #
    # CRITICAL BUG FIX: cached_token_id is ALWAYS the YES token (fetched by
    # get_yes_token_id at window open). Using it unconditionally means every
    # NO/DOWN trade was buying YES tokens — completely wrong side. Only use the
    # cache when direction is YES; always do a fresh lookup for NO/DOWN so
    # _get_token_id can match the "down" outcome and return the correct token ID.
    if cached_token_id and direction in ("YES", "UP"):
        token_id = cached_token_id
        live_log.info(f"Using pre-cached YES token ID: {token_id[:12]}...")
    else:
        token_id = _get_token_id(market_slug, direction)
        if token_id:
            live_log.info(f"Fresh token lookup for direction={direction}: {token_id[:12]}...")
    if not token_id:
        result["error"] = f"Could not resolve token_id for {market_slug} {direction}"
        live_log.error(result["error"])
        return result

    # ── LIVE ORDERBOOK PRICE ──────────────────────────────────────────────────
    # _get_market_price returns None in two cases:
    #   (a) ask >= 0.97 → resolved/frozen market
    #   (b) book unreachable → API/network failure
    # In BOTH cases we must abort — there is no safe price to use.
    #
    # BUG FIX (v8): the previous code only aborted when BOTH live_price AND
    # market_price were None.  market_price is always populated (it comes from
    # signal_engine's Binance price at signal time), so the resolved-market
    # circuit breaker in _get_market_price was silently bypassed every time.
    # The bot then used the Binance price as the limit and fired the FOK order
    # into a resolved book — which rejected with "couldn't be fully filled".
    # Confirmed in live_trades.log: ask=0.99, circuit breaker fires, order sent anyway.
    # FIX: any None from _get_market_price → abort immediately, no fallback.
    live_price = _get_market_price(token_id)
    if live_price is None:
        msg = (f"Orderbook price unavailable for {market_slug} "
               f"(resolved market or API failure) — aborting order")
        result["error"]  = msg
        # BUG 24 FIX: tag result so execute_sequential_multi_asset can propagate
        # "resolved_market" to main.py and break the retry loop immediately,
        # rather than returning "no_successful_fill" which triggers a 633s spin.
        result["reason"] = "resolved_market"
        print(f"\n  SKIP — {msg}")
        live_log.warning(msg)
        _log_live(msg)
        return result

    print(f"  Current orderbook ask: {live_price:.2f}")
    live_log.info(f"Current orderbook ask: {live_price:.2f}")

    TARGET_LIMIT_PRICE = _to_tick(float(os.getenv("TARGET_LIMIT_PRICE", "0.55")))
    MAX_LIMIT_PRICE    = _to_tick(float(os.getenv("MAX_LIMIT_PRICE",    "0.75")))

    if market_price is not None:
        limit_price = _to_tick(float(market_price))
        print(f"  Using dynamic limit price: {limit_price:.2f}")
        live_log.info(f"Dynamic limit price: {limit_price:.2f}")
    else:
        if live_price > MAX_LIMIT_PRICE:
            msg = (f"Market at {live_price:.2f} > MAX_LIMIT {MAX_LIMIT_PRICE:.2f} "
                   f"— too late for value, skipping")
            result["error"] = msg
            print(f"\n  SKIP — {msg}")
            live_log.info(msg)
            _log_live(msg)
            return result
        limit_price = TARGET_LIMIT_PRICE

    limit_price = _to_tick(limit_price)

    # ── API-SAFE ORDER SIZING — Library-aware (BUG FIX v7) ───────────────────
    # ROOT CAUSE OF PREVIOUS FAILURES:
    #   py_clob_client_v2 internally calls round_down(size, 2) on the size we
    #   pass, THEN computes maker_amount = size_2dp * price. Our old algorithm
    #   produced shares like 2.3438 that passed our own check but were silently
    #   truncated to 2.34 inside the library, giving maker = 2.34 * 0.64 =
    #   1.4976 — which the API rejects because maker must be ≤2dp. Our own
    #   gross_stake of $1.50 was also wrong (it never matched what was sent).
    #
    # FIX: since the library always truncates size to 2dp, WE iterate in 0.01
    # steps using Decimal arithmetic, and find the largest size where
    # (size * price) is exactly representable in ≤2dp. That value is what
    # the library will compute as maker_amount, so the API accepts it.
    #
    # V2 validation rules enforced by the API:
    #   maker amount (USDC spent)  : max 2 decimal places
    #   taker amount (shares)      : max 2 decimal places (library truncates to 2dp)
    #
    # FEE NOTE: V2 charges a 7% taker fee on FOK/IOC orders. The fee is
    # applied to the maker amount (USDC spent), so effective cost per share
    # is limit_price / (1 - 0.07) ≈ limit_price * 1.0753.
    # expected_profit reflects the post-fee net: shares won - gross stake paid.
    from decimal import Decimal, ROUND_DOWN as _DEC_DOWN

    TAKER_FEE_RATE = 0.07
    _price_d = Decimal(str(limit_price))
    _stake_d = Decimal(str(stake_usd))
    _step    = Decimal("0.01")
    _cent    = Decimal("0.01")

    # Largest size (in 0.01 steps) we can afford — this is also what the
    # library will use after its own round_down(size, 2) call.
    _max_size = (_stake_d / _price_d).quantize(_step, rounding=_DEC_DOWN)

    _shares_d = Decimal("0")
    _maker_d  = Decimal("0")

    _candidate = _max_size
    while _candidate > 0:
        _raw_maker = _candidate * _price_d
        # Accept only if raw_maker is exactly divisible by 0.01 (i.e. ≤2dp)
        if _raw_maker % _cent == Decimal("0"):
            _shares_d = _candidate
            _maker_d  = _raw_maker.quantize(_cent)
            break
        _candidate -= _step

    if _shares_d == Decimal("0"):
        # Absolute fallback: 1 integer share (0dp is always ≤2dp)
        _shares_d = Decimal("1")
        _maker_d  = _price_d.quantize(_cent, rounding=_DEC_DOWN)

    shares      = float(_shares_d)
    gross_stake = float(_maker_d)      # actual USDC the library will send as maker
    fee_paid    = round(gross_stake * TAKER_FEE_RATE, 4)
    net_cost    = round(gross_stake + fee_paid, 4)
    expected_profit = round(shares - net_cost, 4)

    live_log.info(
        f"Order sizing | limit={limit_price:.2f} | shares={shares} "
        f"| gross=${gross_stake:.2f} | fee=${fee_paid:.4f} "
        f"| net_cost=${net_cost:.4f} | profit_if_win=${expected_profit:.4f}"
    )

    # ── POST-FEE PROFITABILITY GATE ───────────────────────────────────────────
    if expected_profit < MIN_EXPECTED_PROFIT:
        msg = (f"Post-fee profit ${expected_profit:.4f} < threshold ${MIN_EXPECTED_PROFIT} "
               f"— skipping (limit={limit_price:.2f}, fee=7%)")
        result["error"] = msg
        print(f"\n  SKIP — {msg}")
        live_log.warning(msg)
        _log_live(msg)
        return result

    # Init client before the try block so the retry clause doesn't NameError
    try:
        client = _get_client()
    except Exception as e:
        result["error"] = f"Client init failed: {e}"
        live_log.error(result["error"])
        return result

    order_args = OrderArgs(
        token_id=token_id,
        price=limit_price,
        size=shares,
        side=Side.BUY,
    )

    # ── FOK ORDER ─────────────────────────────────────────────────────────────
    try:
        print(f"\n  🚀 FIRING FOK ORDER | {arrow} @ {limit_price:.2f} | ${stake_usd:.2f}...")
        # V2: single-call API — create_and_post_order signs + submits atomically.
        # FOK fills immediately or cancels. Response has status inline; no poll needed.
        response = client.create_and_post_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )

        if isinstance(response, dict):
            order_id = response.get("orderID") or response.get("order_id") or str(response)
        elif hasattr(response, "orderID"):
            order_id = response.orderID
        else:
            order_id = str(response)

        status, size_matched = _parse_order_status(response)

        if (isinstance(response, dict) and response.get("success")) or \
                status.upper() in ("MATCHED", "FILLED") or float(size_matched or 0) > 0:
            msg = (f"FOK FILLED | {arrow} @ {limit_price:.2f} | "
                   f"Matched {size_matched} shares | "
                   f"Fee: ${fee_paid:.4f} (7%) | "
                   f"Profit if win: ${expected_profit:.4f} (post-fee)")
            print(f"\n  ✅ {msg}")
            live_log.info(f"FOK FILLED | order_id={order_id} | shares={size_matched}")
            _log_live(
                f"FOK FILLED | {arrow} @ {limit_price:.2f} | "
                f"profit_if_win=${expected_profit:.2f} | order_id={order_id}"
            )
            matched_shares = float(size_matched) if size_matched else shares
            result.update(
                success=True,
                order_id=order_id,
                stake_usd=stake_usd,
                dry_run=False,
                matched_shares=matched_shares,
                fill_price=limit_price,
            )
            # FIX: _print_and_log was never called — live fills were silently
            # never written to ptb_log.json. Wire it up here.
            _print_and_log(
                asset=label, slug=market_slug, direction=direction,
                price=limit_price, size=matched_shares, stake=stake_usd,
                response={"order_id": order_id, "status": status},
            )
        else:
            msg = f"FOK UNMATCHED (no liquidity) | {arrow} @ {limit_price:.2f}"
            print(f"\n  ⏭️  {msg}")
            live_log.info(f"FOK failed/cancelled: {order_id}")
            _log_live(msg)
            result["error"] = "FOK unmatched — no liquidity"

    except Exception as e:
        err_str = str(e)

        if "does not exist" in err_str:
            # Orderbook not ready — retry once after 15 s if time allows.
            # FIX Bug 4: recompute secs remaining at the moment of this guard —
            # actual_secs_left was calculated before the IOC attempt (1.5s sleep
            # + API calls) so it's 3–7s stale by the time we reach this branch.
            actual_secs_left = 900.0 - (time.time() % 900.0)
            if actual_secs_left < 120:
                msg = f"Orderbook missing, only {actual_secs_left:.0f}s left — skipping"
                print(f"  {msg}")
                result["error"] = msg
                live_log.warning(f"Orderbook missing, insufficient time: {err_str}")
                return result

            print("  Orderbook not ready — waiting 15 s and retrying...")
            live_log.warning(f"Orderbook not ready, retrying FOK in 15 s: {err_str}")
            time.sleep(15)

            try:
                response = client.create_and_post_order(
                    order_args=order_args,
                    options=PartialCreateOrderOptions(tick_size="0.01"),
                    order_type=OrderType.FOK,
                )

                if isinstance(response, dict):
                    order_id = (response.get("orderID")
                                or response.get("order_id")
                                or str(response))
                elif hasattr(response, "orderID"):
                    order_id = response.orderID
                else:
                    order_id = str(response)

                status, size_matched = _parse_order_status(response)

                if (isinstance(response, dict) and response.get("success")) or \
                        status.upper() in ("MATCHED", "FILLED") or float(size_matched or 0) > 0:
                    matched = float(size_matched) if size_matched else shares
                    result.update(
                        success=True,
                        order_id=order_id,
                        stake_usd=stake_usd,
                        dry_run=False,
                        matched_shares=matched,
                        fill_price=limit_price,
                    )
                    print(f"\n  ✅ FOK FILLED ON RETRY -> {arrow} @ {limit_price:.2f} | ID: {order_id}")
                    live_log.info(f"FOK filled on retry | {order_id}")
                    _log_live(
                        f"FOK FILLED (retry) | {arrow} @ {limit_price:.2f} | order_id={order_id}"
                    )
                    # FIX: also write retry fills to ptb_log.json
                    _print_and_log(
                        asset=label, slug=market_slug, direction=direction,
                        price=limit_price, size=matched, stake=stake_usd,
                        response={"order_id": order_id, "status": status, "attempt": "retry"},
                    )
                else:
                    result["error"] = "FOK unmatched on retry"
                    _retry_msg = f"FOK UNMATCHED ON RETRY | {arrow} @ {limit_price:.2f}"
                    print(f"\n  ⏭️  {_retry_msg}")
                    live_log.info(f"FOK unmatched on retry: {order_id}")
                    _log_live(_retry_msg)

            except Exception as retry_err:
                result["error"] = str(retry_err)
                print(f"\n  LIVE ORDER FAILED on retry: {retry_err}")
                live_log.error(f"Retry failed: {retry_err}", exc_info=True)
                _log_live(f"LIVE ORDER FAILED (retry) | {arrow} | Error: {retry_err}")
        else:
            result["error"] = err_str
            err_msg = f"LIVE FOK FAILED | {arrow} | Error: {e}"
            print(f"\n  LIVE ORDER FAILED: {e}")
            live_log.error(err_msg, exc_info=True)
            _log_live(err_msg)

    return result


# ---------------------------------------------------------------------------
# Multi-asset sequential executor
# ---------------------------------------------------------------------------

def execute_sequential_multi_asset(asset_opportunities: list) -> dict:
    """
    Iterates a list of asset opportunities and executes the first one
    that has a live slug and a successful fill.

    NOTE: The position guard (_has_open_positions) is intentionally NOT called
    here. main.py's preflight_check() already verified no open positions before
    this function is ever reached. Calling it again doubles the latency and can
    race against a just-filled order from the previous window still settling.

    Each item in asset_opportunities:
        {
            "asset":          str,    e.g. "ETH"
            "direction":      str,    "YES" or "NO"
            "limit_price":    float
            "stake":          float   (optional — falls back to LIVE_STAKE_USD env var)
            "secs_left":      float   (optional — defaults to 900)
            "candle_type":    str     (optional — for logging)
            "distance":       float   (optional — for logging)
        }
    """
    # Tracks whether any asset made it past slug checks and reached execute_live_trade.
    # If no asset reached the orderbook (all failed pre-trade infrastructure checks),
    # we return "slug_unavailable" instead of "no_successful_fill" so main.py can
    # distinguish between "orderbook miss" and "slug not found / token not resolved".
    # This prevents fired_this_window from marking assets as attempted when they
    # never actually had an order placed — allowing re-monitoring on the next pass.
    _reached_orderbook = False

    # Counts how many assets confirmed a resolved/frozen orderbook (ask >= 0.97).
    # Used to distinguish "one dead asset, others still live" from "all assets dead".
    # Only when ALL candidates resolve do we propagate resolved_market to the caller.
    # Each asset is an independent binary market — XRP resolving has no implication
    # for ETH, SOL, or BTC in the same 15m window.
    _resolved_count = 0

    for opp in asset_opportunities:
        asset            = opp.get("asset", "")
        direction        = opp.get("direction")
        price            = opp.get("limit_price")
        stake            = opp.get("stake", None)        # None = read from .env
        secs_left        = opp.get("secs_left", 900.0)
        candle_type      = opp.get("candle_type", "")
        distance         = opp.get("distance", 0.0)
        cached_token_id  = opp.get("_cached_token_id")  # FIX: pre-cached at window open
        slug             = get_active_15m_slug(asset)

        print(f"⚡ Checking {asset} | {direction} @ {price} ...")

        # FIX: if a token ID was pre-cached by main.py at window open, the slug
        # was already verified at that time (_verify_slug_exists was called inside
        # get_yes_token_id). Skip the redundant third Gamma call here.
        # Only run _verify_slug_exists when the cache miss means we never checked.
        if cached_token_id:
            live_log.info(
                f"[{asset}] Skipping _verify_slug_exists — token ID pre-cached: {cached_token_id[:12]}..."
            )
        else:
            # BUG 14 FIX: the previous code hard-skipped on the first failed
            # slug check (continue). This killed every early-window signal —
            # Polymarket publishes slugs 30–90s after the window boundary, so
            # a signal firing at 789s remaining would hard-skip even though the
            # slug was about to go live. We now retry the slug check patiently
            # when there is enough time left (>750s = first ~2.5 min of the window only),
            # to 90s. If the slug is not tradeable (resolved/closed market), we
            # still hard-skip immediately — retrying a resolved market is futile.
            actual_secs = 900.0 - (time.time() % 900.0)
            SLUG_RETRY_BUDGET = 90      # seconds to wait for slug propagation
            SLUG_RETRY_INTERVAL = 10    # seconds between checks

            slug_live = _verify_slug_exists(slug)
            if not slug_live and actual_secs > 750:
                elapsed = 0
                live_log.info(
                    f"[{asset}] Slug not live yet — retrying every {SLUG_RETRY_INTERVAL}s "
                    f"(budget={SLUG_RETRY_BUDGET}s, secs_left={actual_secs:.0f})"
                )
                print(f"  ⏳ [{asset}] Slug not live yet — waiting up to {SLUG_RETRY_BUDGET}s...")
                while elapsed < SLUG_RETRY_BUDGET:
                    time.sleep(SLUG_RETRY_INTERVAL)
                    elapsed += SLUG_RETRY_INTERVAL
                    slug_live = _verify_slug_exists(slug)
                    if slug_live:
                        live_log.info(
                            f"[{asset}] Slug went live after {elapsed}s — proceeding"
                        )
                        print(f"  ✅ [{asset}] Slug live after {elapsed}s!")
                        break
                    live_log.info(
                        f"[{asset}] Slug still not live ({elapsed}s elapsed, "
                        f"budget={SLUG_RETRY_BUDGET}s)"
                    )

            if not slug_live:
                print(f"⏭️  Slug not live after waiting, skipping: {slug}")
                live_log.warning(f"[{asset}] Slug not live after retry budget — skipping: {slug}")
                continue

        # Past all pre-trade slug/infrastructure checks — the asset is about to
        # attempt a real order. Mark this so the caller knows at least one asset
        # reached the orderbook, regardless of whether the fill succeeded.
        _reached_orderbook = True
        result = execute_live_trade(
            market_slug=slug,
            direction=direction,
            market_price=price,
            asset=asset,
            stake_override=stake,
            secs_left=secs_left,
            candle_type=candle_type,
            distance=distance,
            cached_token_id=cached_token_id,   # FIX: skip _get_token_id if already resolved
        )

        if result["success"]:
            print(f"🎯 SUCCESS: {asset} filled.")
            return {
                "executed":  True,
                "asset":     asset,
                "slug":      slug,
                "direction": direction,
                "price":     result.get("fill_price"),
                "size":      result.get("matched_shares"),
            }

        # BUG 24 FIX (updated): execute_live_trade confirmed this asset's orderbook
        # is frozen (ask >= 0.97 → _get_market_price returned None → resolved_market).
        # Previously we returned immediately, killing the entire window after the first
        # dead asset — but each asset is an independent binary market. XRP resolving
        # has no implication for ETH/SOL/BTC. We now skip only the dead asset and
        # continue to the next. Only when ALL candidates are resolved do we propagate
        # resolved_market to the caller (so main.py can break its retry loop cleanly).
        if result.get("reason") == "resolved_market":
            _resolved_count += 1
            live_log.warning(
                f"[{asset}] Resolved market confirmed by orderbook — "
                f"skipping asset, checking remaining ({_resolved_count} resolved so far)"
            )
            continue

        live_log.warning(f"Trade failed for {asset}: {result.get('error')}")

    print("[INFO] No opportunities executed.")

    # If every asset confirmed a resolved/frozen orderbook, propagate resolved_market
    # so main.py can break its retry loop immediately rather than spinning for 633s.
    # Partial resolution (some dead, some illiquid/missed) falls through to the
    # normal no_successful_fill / slug_unavailable path below.
    if _resolved_count > 0 and _resolved_count == len(asset_opportunities):
        live_log.warning(
            f"All {_resolved_count} asset(s) confirmed resolved — "
            f"propagating resolved_market to caller"
        )
        return {"executed": False, "reason": "resolved_market"}

    # Return a specific reason so main.py can distinguish:
    #   "no_successful_fill" → asset reached the orderbook but IOC/FOK didn't match
    #                          → add to fired_this_window (don't retry same illiquid book)
    #   "slug_unavailable"   → asset never reached the orderbook (slug/token failed)
    #                          → do NOT add to fired_this_window (allow re-monitoring)
    reason = "no_successful_fill" if _reached_orderbook else "slug_unavailable"
    return {"executed": False, "reason": reason}


# ---------------------------------------------------------------------------
# JSON trade log helper
# ---------------------------------------------------------------------------

def _print_and_log(
    asset: str,
    slug: str,
    direction: str,
    price: float,
    size: float,
    stake: float,
    response: dict,
) -> None:
    """Prints a trade summary and appends a JSON record to LOG_FILE.

    FIX (Bug 1): guarded by _log_file_lock so this cannot race with
    reconcile_trade in auto_redeem.py, which also reads+writes ptb_log.json.
    FIX (Bug 1): uses atomic .tmp → os.replace() write so a mid-write crash
    cannot corrupt the log.
    FIX (Bug 2): record uses "market_slug" key (not "slug") so the reconciler's
    reconcile_trade() can match on entry.get("market_slug").
    """
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    record = {
        "timestamp":    ts,
        "asset":        asset,
        "market_slug":  slug,        # FIX Bug 2: was "slug" — reconciler needs "market_slug"
        "direction":    direction,
        "price":        price,
        "size":         size,
        "stake":        stake,
        "response":     response,
    }
    print(
        f"\n{'='*55}\n"
        f"  TRADE EXECUTED — {ts}\n"
        f"  Asset    : {asset}\n"
        f"  Direction: {direction}\n"
        f"  Price    : {price}  |  Size: {size}  |  Stake: ${stake}\n"
        f"  Slug     : {slug}\n"
        f"{'='*55}"
    )
    # FIX Bug 1: acquire lock and write atomically
    with _log_file_lock:
        try:
            existing = []
            if os.path.exists(LOG_FILE):
                with open(LOG_FILE, "r") as f:
                    existing = json.load(f)
            existing.append(record)
            tmp = LOG_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(existing, f, indent=2)
            os.replace(tmp, LOG_FILE)
        except Exception as e:
            live_log.error(f"Failed to write to {LOG_FILE}: {e}")


# ---------------------------------------------------------------------------
# Connection / sanity test
# ---------------------------------------------------------------------------

def test_connection() -> None:
    import requests
    print("\n" + "=" * 55)
    print("  Polymarket Live Executor — Connection Test")
    print("=" * 55)

    load_dotenv()
    key = os.getenv("POLYGON_PRIVATE_KEY", "")
    if not key:
        print("\n  POLYGON_PRIVATE_KEY not found in .env")
        return

    print(f"  Private key loaded: {'*' * 20}...{key[-6:]}")

    _check_vpn()

    try:
        _get_client()
        r = requests.get(
            f"{GAMMA_HOST}/events",
            params={"slug": "eth-updown-15m"},
            timeout=10
        )
        print(f"  Gamma API reachable (status {r.status_code})")
    except Exception as e:
        print(f"  ERROR: {e}")
        return

    live_enabled = os.getenv("LIVE_ENABLED", "false").strip().lower()
    stake        = _calc_stake_usd()

    print(f"\n  LIVE_ENABLED        : {live_enabled}")
    print(f"  LIVE_STAKE_USD      : ${stake:.2f}")
    print(f"  MAX HARD CAP        : ${MAX_LIVE_STAKE_USD}")
    print(f"  MIN PROFIT FILTER   : ${MIN_EXPECTED_PROFIT}")

    if live_enabled != "true":
        print("\n  Running in DRY RUN mode.")
    else:
        print("\n  ⚠️  LIVE MODE IS ACTIVE — real orders will be placed!")

    print("=" * 55 + "\n")


if __name__ == "__main__":
    test_connection()