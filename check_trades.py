"""
check_trades.py — Gatekeeper + Trade Diagnostics for Polymarket Multi-Asset Bot

Two modes:
  1. Import has_open_position() in main.py → returns True/False (Gatekeeper)
  2. Run standalone: python check_trades.py → full diagnostic printout

Bugs fixed vs broken "refactor" version:
  BUG 1 — 'wallet' (lowercase) used in __main__ but constants should be UPPERCASE
           → NameError at runtime; fixed by using WALLET everywhere consistently
  BUG 2 — 'CLOB' constant was removed from module level
           → NameError inside check_clob_orders(); restored
  BUG 3 — 'GAMMA' constant was removed from module level
           → NameError inside check_portfolio() / check_activity(); restored
  BUG 4 — os.getenv("POLYGON_PRIVATE_KEY_ADDRESS") does not exist in .env
           → wallet was always None → bot permanently BLOCKED every window
           Fixed: derive address from POLYGON_PRIVATE_KEY (already in .env),
           with hardcoded fallback so it works even without eth_account installed
  BUG 5 — HTTP 404 from Gamma treated as an error (blocked the bot)
           Fixed: 404 for a wallet means no positions exist → return False
"""

import os
import requests
from dotenv import load_dotenv

load_dotenv()

# ── Module-level constants (UPPERCASE — accessible by every function) ─────────
# BUG 2 + 3 fix: these were deleted in the broken version, causing NameErrors
# in all diagnostic functions that reference CLOB / GAMMA.
CLOB  = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"

# Assets the bot trades — used to filter positions in the Gatekeeper check
TRACKED_ASSETS      = ["BTC", "ETH", "SOL", "XRP"]
OPEN_SIZE_THRESHOLD = 0.0001   # anything at or below this is dust


# ── Wallet resolution ─────────────────────────────────────────────────────────
# BUG 4 fix: the broken version read os.getenv("POLYGON_PRIVATE_KEY_ADDRESS")
# which does not exist in the .env file, so wallet was always None and the
# gatekeeper always returned True (blocked).
#
# Correct approach:
#   1. Try WALLET_ADDRESS env var (optional convenience override).
#   2. Derive from POLYGON_PRIVATE_KEY (already present in .env).
#   3. Fall back to the known hardcoded address as a last resort.
#
# This means the wallet is always resolved correctly without adding a new
# env var that doesn't exist yet.

def _resolve_wallet() -> str:
    """Return the checksummed wallet address, derived from .env."""
    # Option 1: explicit override
    explicit = os.getenv("WALLET_ADDRESS", "").strip()
    if explicit:
        return explicit

    # Option 2: derive from private key (preferred — stays in sync automatically)
    pk = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
    if pk:
        try:
            from eth_account import Account
            return Account.from_key(pk).address
        except Exception as e:
            print(f"[Gatekeeper] WARN: Could not derive wallet from private key: {e}")

    # Option 3: hardcoded fallback (safe — this is a public address, not the key)
    fallback = "0xA9270F6ba064c7F2abfc04ae28196a20E0616177"
    print(f"[Gatekeeper] WARN: Using hardcoded fallback wallet: {fallback}")
    return fallback


# Resolved once at import time — cheap, and consistent across all functions.
WALLET = _resolve_wallet()


# ─────────────────────────────────────────────────────────────
#  GATEKEEPER — called by main.py before every window
# ─────────────────────────────────────────────────────────────

def has_open_position() -> bool:
    """
    Returns True if ANY tracked asset (BTC, ETH, SOL, XRP) has an open
    position with size > OPEN_SIZE_THRESHOLD on Polymarket.

    main.py calls this before starting the ptb_scanner.
    If True → skip the entire 15-minute window to protect bankroll.

    Fail-safe: any unexpected API error returns True (blocked) to prevent
    double-exposure on a bad network call.
    """
    try:
        r = requests.get(
            f"{GAMMA}/positions",
            params={"user": WALLET},
            timeout=10,
        )

        # BUG 5 fix: 404 is the normal response for a wallet with no positions.
        # The broken version treated it as an error and blocked the bot forever.
        if r.status_code == 404:
            return False   # No positions exist for this wallet — clear to trade

        if r.status_code != 200:
            print(f"[Gatekeeper] WARN: Gamma API returned {r.status_code} — defaulting to BLOCKED.")
            return True    # Fail-safe: block on unexpected API error

        data      = r.json()
        positions = data if isinstance(data, list) else data.get("data", [])

        for p in positions:
            try:
                size = float(p.get("size", 0))
            except (TypeError, ValueError):
                size = 0.0

            if size <= OPEN_SIZE_THRESHOLD:
                continue   # Dust — ignore

            # Gamma API may return the market name under different keys
            # depending on endpoint version, so check all likely candidates.
            market_name = (
                p.get("market",   "") or
                p.get("title",    "") or
                p.get("question", "") or ""
            ).upper()

            for asset in TRACKED_ASSETS:
                if asset in market_name:
                    print(
                        f"[Gatekeeper] BLOCKED — Open {asset} position detected. "
                        f"size={size}, market='{market_name[:60]}'"
                    )
                    return True   # Found a real open position → block

        return False   # No blocking positions found

    except requests.exceptions.Timeout:
        print("[Gatekeeper] WARN: Gamma API timed out — defaulting to BLOCKED.")
        return True

    except Exception as e:
        print(f"[Gatekeeper] ERROR: {e} — defaulting to BLOCKED.")
        return True


# ─────────────────────────────────────────────────────────────
#  DIAGNOSTICS — only used when running standalone
# ─────────────────────────────────────────────────────────────

def check_clob_orders():
    """Check open and filled orders on the CLOB."""
    print("\n[1] Checking CLOB orders for your wallet...")
    try:
        r = requests.get(
            f"{CLOB}/orders",
            params={"maker_address": WALLET},
            timeout=10,
        )
        if r.status_code == 200:
            orders = r.json()
            if orders:
                print(f"  Open orders: {len(orders)}")
                for o in orders[:5]:
                    print(
                        f"    - {o.get('asset_id', '?')[:20]}... "
                        f"size={o.get('original_size')} "
                        f"filled={o.get('size_matched')} "
                        f"status={o.get('status')}"
                    )
            else:
                print("  No open orders")
        else:
            print(f"  CLOB orders endpoint: {r.status_code} — {r.text[:100]}")
    except Exception as e:
        print(f"  Error: {e}")


def check_clob_trades():
    """Check filled trades."""
    print("\n[2] Checking CLOB trade history...")
    try:
        r = requests.get(
            f"{CLOB}/data/trades",
            params={"maker_address": WALLET},
            timeout=10,
        )
        if r.status_code == 200:
            data   = r.json()
            trades = data if isinstance(data, list) else data.get("data", [])
            if trades:
                print(f"  Found {len(trades)} trades:")
                for t in trades[:10]:
                    print(f"    - market={t.get('market', '?')[:30]}...")
                    print(
                        f"      side={t.get('side')} size={t.get('size')} "
                        f"price={t.get('price')} status={t.get('status')}"
                    )
                    print(
                        f"      matched_amount={t.get('matched_amount')} "
                        f"type={t.get('type')}"
                    )
            else:
                print("  No trades found for this wallet on CLOB")
        else:
            print(f"  CLOB trades endpoint: {r.status_code}")
            r2 = requests.get(
                f"{CLOB}/data/trades",
                params={"taker_address": WALLET},
                timeout=10,
            )
            print(f"  (taker_address query): {r2.status_code}")
            if r2.status_code == 200:
                data2   = r2.json()
                trades2 = data2 if isinstance(data2, list) else data2.get("data", [])
                print(f"  Found {len(trades2)} trades as taker")
                for t in trades2[:5]:
                    print(f"    - {t}")
    except Exception as e:
        print(f"  Error: {e}")


def check_portfolio():
    """Check Polymarket portfolio/positions."""
    print("\n[3] Checking Polymarket portfolio positions...")
    try:
        r = requests.get(
            f"{GAMMA}/positions",
            params={"user": WALLET},
            timeout=10,
        )
        if r.status_code == 200:
            data      = r.json()
            positions = data if isinstance(data, list) else data.get("data", [])
            if positions:
                print(f"  Found {len(positions)} positions:")
                for p in positions[:10]:
                    print(f"    - market: {p.get('market', '?')[:40]}")
                    print(
                        f"      size={p.get('size')} avgPrice={p.get('avgPrice')} "
                        f"redeemable={p.get('redeemable')}"
                    )
                    print(f"      conditionId={str(p.get('conditionId', '?'))[:30]}")
            else:
                print("  No positions found")
        else:
            print(f"  Portfolio endpoint: {r.status_code}")
    except Exception as e:
        print(f"  Error: {e}")


def check_activity():
    """Check recent activity."""
    print("\n[4] Checking recent activity/fills...")
    try:
        r = requests.get(
            f"{GAMMA}/activity",
            params={"user": WALLET, "limit": 20},
            timeout=10,
        )
        if r.status_code == 200:
            data  = r.json()
            items = data if isinstance(data, list) else data.get("data", [])
            if items:
                print(f"  Found {len(items)} activity items:")
                for item in items[:10]:
                    print(
                        f"    - type={item.get('type')} "
                        f"market={str(item.get('market', '?'))[:30]}"
                    )
                    print(
                        f"      amount={item.get('amount')} "
                        f"price={item.get('price')} outcome={item.get('outcome')}"
                    )
            else:
                print("  No activity found")
        else:
            print(f"  Activity endpoint: {r.status_code} — {r.text[:100]}")
    except Exception as e:
        print(f"  Error: {e}")


def check_polygon_usdc_txs():
    """Check recent USDC.e transactions via Polygonscan API."""
    print("\n[5] Checking recent USDC.e on-chain transactions...")
    POLYGONSCAN_KEY = os.getenv("POLYGONSCAN_API_KEY", "")
    if not POLYGONSCAN_KEY:
        print("  (No POLYGONSCAN_API_KEY in .env — skipping on-chain check)")
        print(
            f"  → Manually check: https://polygonscan.com/address/"
            f"{WALLET}#tokentxns"
        )
        return

    try:
        r = requests.get(
            "https://api.polygonscan.com/api",
            params={
                "module":          "account",
                "action":          "tokentx",
                "address":         WALLET,
                "contractaddress": "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174",
                "sort":            "desc",
                "apikey":          POLYGONSCAN_KEY,
            },
            timeout=10,
        )
        data = r.json()
        txs  = data.get("result", [])
        if txs and isinstance(txs, list):
            print(f"  Last {min(len(txs), 10)} USDC.e transactions:")
            for tx in txs[:10]:
                arrow = "IN ↓" if tx.get("to", "").lower() == WALLET.lower() else "OUT ↑"
                amt   = int(tx.get("value", 0)) / 1e6
                print(
                    f"    {arrow} {amt:.4f} USDC | "
                    f"from={tx.get('from', '?')[:20]}... | "
                    f"{tx.get('timeStamp')}"
                )
        else:
            print(f"  No USDC.e txs or error: {data.get('message')}")
    except Exception as e:
        print(f"  Error: {e}")


def run_gatekeeper_test():
    """Standalone test of the Gatekeeper function with verbose output."""
    print("\n[GATEKEEPER TEST] Checking for open positions across BTC/ETH/SOL/XRP...")
    result = has_open_position()
    if result:
        print("  ✗ Result: BLOCKED — open position detected. Bot would skip this window.")
    else:
        print("  ✓ Result: CLEAR — no open positions. Bot is free to trade.")
    return result


# ─────────────────────────────────────────────────────────────
#  STANDALONE ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("  Trade History Diagnostic + Gatekeeper Test")
    print(f"  Wallet: {WALLET}")      # BUG 1 fix: was 'wallet' (lowercase) → NameError
    print("=" * 60)

    run_gatekeeper_test()
    check_clob_orders()
    check_clob_trades()
    check_portfolio()
    check_activity()
    check_polygon_usdc_txs()

    print("\n" + "=" * 60)
    print("  SUMMARY — what to look for:")
    print("  • Gatekeeper BLOCKED → do NOT start a trade window")
    print("  • If [2] shows size=0 or matched_amount=0 → orders never filled")
    print("  • If [3] shows redeemable=True → redeem via UI")
    print("  • If [5] shows no USDC.e OUT txs → bot never sent $ to Polymarket")
    print("=" * 60)
    