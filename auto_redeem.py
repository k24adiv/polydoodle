"""
auto_redeem.py — Multi-Asset Automatic Redemption for Polymarket Bot
---------------------------------------------------------------------
Redeems winning positions back to pUSD across BTC, ETH, SOL, XRP
15-minute markets.

Standalone usage:
  python auto_redeem.py                          # sweep last 2 windows, all assets
  python auto_redeem.py <slug> <YES|NO>          # redeem specific market + direction

Fixes applied over draft:
  - Restored 270s resolution-wait retry loop (Polymarket takes 2-5 min to resolve)
  - Fixed broken-link artifacts: datetime.now(), w3.to_checksum_address()
  - Fixed idx_sets loop: unknown direction now tries [1] and [2] as separate TXs
  - Added direction passthrough in auto-sweep mode
  - Hardened condition_id extraction against empty/malformed markets list
  - Re-added logging module alongside print statements

V2 MIGRATION (addresses updated):
  - CTF_CONTRACT updated to Neg Risk CTF Exchange V2
  - COLLATERAL updated from USDC.e to pUSD (0x6...the wrapped collateral token)
  - V2 Neg Risk CTF Exchange: 0xC5d563A36AE78145C45a50134d48A1215220f80a is V1.
    V2 address is 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E (standard CTF exchange)
    and the Neg Risk Adapter is 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296.
    Redemptions on V2 15m markets go through the NegRiskAdapter, not directly
    to the CTF exchange.

FIX [CRITICAL] — Sentinel 2**128 reverts on NegRiskAdapter
  Old code used _MAX_AMOUNT = 2**128 as a "redeem all" sentinel, assuming the
  contract would cap at your held balance. NegRiskAdapter does NOT do this —
  it tries to move exactly 2**128 units, your wallet has ~4.7M raw units, TX
  reverts. Fix: query actual ERC-1155 balance from the Polymarket CTF contract
  using clobTokenIds fetched from Gamma API, pass the real amount.

FIX [MEDIUM] — Zero-balance guard
  If balance comes back 0 (e.g. RPC hiccup, wrong token ID), skip that side
  instead of sending a 0-amount TX that would revert or do nothing.
"""

import os
import json
import time
import logging
import threading
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger("auto_redeem")

# ── ASSETS ────────────────────────────────────────────────────────────────────
ASSETS = ["BTC", "ETH", "SOL", "XRP"]

# ── CONTRACTS ─────────────────────────────────────────────────────────────────
# V2 15-minute up/down markets are binary neg-risk markets.
# Redemption path for V2:
#
#   CTF Exchange V2   : 0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E
#     → standard binary markets — redeemPositions(collateral, parent, conditionId, indexSets)
#   NegRiskAdapter    : 0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296
#     → neg-risk markets  — different 2-arg signature:
#       redeemPositions(bytes32 conditionId, uint256[] amounts)
#       amounts[0] = YES position size (raw units), amounts[1] = NO position size
#   pUSD (collateral) : 0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB (V2)
#
# 15m up/down markets ARE neg-risk (binary sub-markets inside a larger neg-risk
# market), so we call redeemPositions on the NegRiskAdapter with the 2-arg form.
# The amounts array slots match position size, not indexSets bitmasks.
#
# NOTE: The V1 4-arg form (collateral, parent, conditionId, indexSets) is used on
# the plain CTF contract for standard binary markets — NOT for neg-risk 15m markets.
CTF_EXCHANGE_V2    = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"   # standard binary (unused here)
NEG_RISK_ADAPTER   = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"   # 15m up/down markets
PUSD               = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"   # V2 collateral

# Convenience alias — the actual redemption target for all 15m markets
CTF_CONTRACT = NEG_RISK_ADAPTER

# ── POLYMARKET CTF (ERC-1155 position token contract) ─────────────────────────
# Used to query actual position token balances before sending the redeem TX.
# This is NOT the same as the NegRiskAdapter — it is the underlying conditional
# token framework contract that holds all position tokens.
POLYMARKET_CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

CTF_ERC1155_ABI = [
    {
        "inputs": [
            {"name": "account", "type": "address"},
            {"name": "id",      "type": "uint256"},
        ],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

# Global lock so concurrent redeem threads never race on nonce.
_nonce_lock = threading.Lock()

# ── CONTRACT ABIs ──────────────────────────────────────────────────────────────
# The NegRiskAdapter exposes a 2-argument redeemPositions, different from CTF's 4-arg form.
# Signature: redeemPositions(bytes32 conditionId, uint256[] amounts)
#   conditionId : the market condition identifier from Gamma API
#   amounts     : [YES_amount, NO_amount] in raw token units (position size × 10^6 for 6-dp)
#                 The winning side's amount should be your actual position size;
#                 the losing side's amount should be 0.
#                 FIX: we now pass the REAL balance, not a sentinel value.
NEG_RISK_ABI = [
    {
        "inputs": [
            {"name": "_conditionId", "type": "bytes32"},
            {"name": "_amounts",     "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

# Legacy 4-arg ABI kept for reference / standard binary markets (not used for 15m)
CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

GAMMA_URL = "https://gamma-api.polymarket.com/events"

# ── RESOLUTION WAIT CONFIG ────────────────────────────────────────────────────
# Polymarket resolves 2-5 mins after window close.
# Bot has ~240s free during next window's pre-entry + PTB scan.
# We wait up to 270s (9 × 30s checks) before attempting anyway.
RESOLVE_CHECK_INTERVAL = 30    # seconds between resolution checks
RESOLVE_MAX_WAIT       = 270   # max seconds to wait


# ── HELPERS ───────────────────────────────────────────────────────────────────

def get_active_15m_slug(asset_name: str, offset_seconds: int = 0) -> str:
    """Return the market slug for a given asset at now + offset_seconds."""
    now = int(time.time()) + offset_seconds
    window_start = now - (now % 900)
    return f"{asset_name.lower()}-updown-15m-{window_start}"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _print_and_log(asset: str, action: str, status: str, details: str = "") -> None:
    msg = f"[{_ts()}] ASSET: {asset} | ACTION: {action} | STATUS: {status}"
    if details:
        msg += f" | {details}"
    print(f"> {msg}")
    log.info(msg)


def _get_rpc() -> str:
    ankr_key = os.getenv("ANKR_KEY", "").strip()
    if ankr_key:
        return f"https://rpc.ankr.com/polygon/{ankr_key}"
    return "https://polygon-bor-rpc.publicnode.com"


def _get_web3():
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    w3 = Web3(Web3.HTTPProvider(_get_rpc()))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to Polygon RPC: {_get_rpc()}")
    return w3


def _get_account():
    from eth_account import Account
    key = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
    if not key:
        raise EnvironmentError("POLYGON_PRIVATE_KEY not set in .env")
    return Account.from_key(key), key


# ── GAMMA API QUERIES ─────────────────────────────────────────────────────────

def _fetch_event(market_slug: str) -> dict | None:
    """Fetch the raw event object from Gamma for a given slug."""
    try:
        r = requests.get(GAMMA_URL, params={"slug": market_slug}, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data[0] if data else None
    except Exception as e:
        log.warning(f"Gamma fetch failed for {market_slug}: {e}")
        return None


def _get_condition_id(market_slug: str) -> str | None:
    """Extract conditionId from the Gamma event, safely handling empty markets."""
    event = _fetch_event(market_slug)
    if not event:
        return None
    for m in event.get("markets", []):
        if isinstance(m, dict):
            cid = m.get("conditionId") or m.get("condition_id")
            if cid:
                return cid
    return None


def _get_clob_token_ids(market_slug: str) -> tuple[int | None, int | None]:
    """
    Fetch YES and NO CLOB token IDs from Gamma API for a given slug.

    Gamma returns clobTokenIds as a list of two string token IDs:
      [YES_token_id, NO_token_id]

    These are the ERC-1155 token IDs on the Polymarket CTF contract.
    We use them to query the wallet's actual position token balance before
    calling redeemPositions — avoids the sentinel-value revert.

    Returns (yes_token_id, no_token_id) as ints, or (None, None) on failure.
    """
    event = _fetch_event(market_slug)
    if not event:
        return None, None
    for m in event.get("markets", []):
        if not isinstance(m, dict):
            continue
        ids = m.get("clobTokenIds")
        if ids is None:
            continue
        # Gamma sometimes returns clobTokenIds as a JSON-encoded string
        # e.g. "[123456789, 987654321]" instead of a list — parse it first.
        if isinstance(ids, str):
            try:
                ids = json.loads(ids)
            except (json.JSONDecodeError, ValueError):
                log.warning(
                    f"_get_clob_token_ids: clobTokenIds is unparseable string for "
                    f"{market_slug}: {ids!r}"
                )
                continue
        if ids and len(ids) >= 2:
            try:
                return int(ids[0]), int(ids[1])
            except (ValueError, TypeError) as e:
                log.warning(f"_get_clob_token_ids: could not parse token IDs for {market_slug}: {e}")
    return None, None


def _get_position_balance(w3, wallet: str, token_id: int) -> int:
    """
    Query the actual ERC-1155 position token balance from the Polymarket CTF
    contract for the given wallet and token ID.

    Returns raw balance (integer, 6-decimal fixed point for Polymarket).
    Returns 0 on any error so callers can safely skip a zero-balance side.

    FIX: replaces the _MAX_AMOUNT = 2**128 sentinel which caused NegRiskAdapter
    to revert — the contract does not cap at held balance.
    """
    try:
        ctf = w3.eth.contract(
            address=w3.to_checksum_address(POLYMARKET_CTF),
            abi=CTF_ERC1155_ABI,
        )
        balance = ctf.functions.balanceOf(
            w3.to_checksum_address(wallet),
            token_id,
        ).call()
        log.debug(
            f"_get_position_balance: token_id={token_id} wallet={wallet[:10]}… "
            f"balance={balance} (contract={POLYMARKET_CTF[:10]}…)"
        )
        return balance
    except Exception as e:
        # Log as ERROR not WARNING — a 0 from here causes a silent skip,
        # so we need to distinguish RPC failures from true zero balances.
        log.error(
            f"_get_position_balance: RPC CALL FAILED for token_id={token_id} "
            f"wallet={wallet[:10]}… contract={POLYMARKET_CTF[:10]}… — {e}"
        )
        return 0


def _is_market_resolved(market_slug: str) -> bool:
    """Return True if the market is closed/resolved on Gamma."""
    event = _fetch_event(market_slug)
    if not event:
        return False
    if event.get("closed") is True:
        return True
    for m in event.get("markets", []):
        if isinstance(m, dict) and (m.get("resolved") or m.get("closed")):
            return True
    return False


# ── WINNING SIDE DETECTOR ─────────────────────────────────────────────────────

def _get_winning_side(market_slug: str) -> str | None:
    """
    After resolution, Polymarket sets outcomePrices to >0.95 for the winner
    and <0.05 for the loser.  outcomes[0] is always Up/YES, outcomes[1] is Down/NO.

    Returns "YES", "NO", or None if the market isn't resolved yet / data unavailable.
    Threshold is 0.90 (slightly loose) to handle the brief price-settling window.
    """
    event = _fetch_event(market_slug)
    if not event:
        return None
    for m in event.get("markets", []):
        if not isinstance(m, dict):
            continue
        prices_raw = m.get("outcomePrices")
        if not prices_raw:
            continue
        try:
            # outcomePrices is a JSON string like '["0.97", "0.03"]' or a list
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
            else:
                prices = prices_raw
            prices = [float(p) for p in prices]
            if len(prices) >= 2:
                if prices[0] >= 0.90:
                    return "YES"
                if prices[1] >= 0.90:
                    return "NO"
        except Exception as e:
            log.warning(f"_get_winning_side parse error for {market_slug}: {e}")
    return None


# ── LOG RECONCILER ─────────────────────────────────────────────────────────────

_PTB_LOG_LOCK = threading.Lock()   # guards ptb_log.json writes across threads


def reconcile_trade(entry: dict, log_path: str = "ptb_log.json") -> str:
    """
    Given a single ptb_log.json entry (dict), check if the market is now resolved
    and patch the entry with WIN / LOSS / VOID + settled_at_utc + actual_winning_side.

    Writes atomically via a .tmp rename so a crash never corrupts the log.

    Returns: "WIN", "LOSS", "VOID", "PENDING" (market not yet resolved), or "ERROR".
    """
    slug      = entry.get("market_slug", "")
    direction = (entry.get("direction") or "").upper()

    if not slug:
        return "ERROR"

    winning_side = _get_winning_side(slug)
    if winning_side is None:
        return "PENDING"   # market not resolved yet

    if direction not in ("YES", "NO"):
        outcome = "VOID"
    elif direction == winning_side:
        outcome = "WIN"
    else:
        outcome = "LOSS"

    with _PTB_LOG_LOCK:
        try:
            # Load current log
            if os.path.exists(log_path):
                with open(log_path, "r") as f:
                    entries = json.load(f)
            else:
                entries = []

            # Find and patch the matching entry.
            # Match on slug + direction + filled_at (if present) to avoid collisions.
            patched = False
            for e in entries:
                if e.get("market_slug") != slug:
                    continue
                if e.get("direction", "").upper() != direction:
                    continue
                # if filled_at present on both, use it as tiebreaker
                if entry.get("filled_at") and e.get("filled_at") != entry.get("filled_at"):
                    continue
                # Only patch entries that are still PENDING
                # (default "PENDING" catches entries with no settled_outcome field)
                if e.get("settled_outcome", "PENDING") not in ("PENDING", ""):
                    continue
                e["settled_outcome"]     = outcome
                e["settled_at_utc"]      = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                e["actual_winning_side"] = winning_side
                patched = True
                break

            if not patched:
                log.warning(
                    f"reconcile_trade: no PENDING entry found for {slug} / {direction} "
                    f"in {log_path} — may already be settled or entry missing"
                )

            # Atomic write via tmp file
            tmp = log_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(entries, f, indent=2)
            os.replace(tmp, log_path)

        except Exception as e:
            log.warning(f"reconcile_trade write error for {slug}: {e}")
            return "ERROR"

    log.info(f"reconcile_trade | {slug} | {direction} → {outcome} (winner={winning_side})")
    return outcome


def reconcile_pending_log(log_path: str = "ptb_log.json") -> dict:
    """
    Sweep ptb_log.json at startup (or on demand).
    Finds all entries where settled_outcome == "PENDING" (or field is absent)
    and calls reconcile_trade on each.

    Returns a summary: {"resolved": int, "still_pending": int, "errors": int}
    """
    summary = {"resolved": 0, "still_pending": 0, "errors": 0}

    if not os.path.exists(log_path):
        return summary

    try:
        with open(log_path, "r") as f:
            entries = json.load(f)
    except Exception as e:
        log.warning(f"reconcile_pending_log: could not read {log_path}: {e}")
        return summary

    # Snapshot the pending list before iterating — reconcile_trade rewrites
    # the file on each call so we must not iterate over live data.
    pending = [e for e in entries if e.get("settled_outcome", "PENDING") == "PENDING"]

    for entry in pending:
        result = reconcile_trade(entry, log_path=log_path)
        if result in ("WIN", "LOSS", "VOID"):
            summary["resolved"] += 1
        elif result == "PENDING":
            summary["still_pending"] += 1
        else:
            summary["errors"] += 1

    log.info(f"reconcile_pending_log: {summary}")
    return summary


# ── RESOLUTION WAIT ───────────────────────────────────────────────────────────

def _wait_for_resolution(market_slug: str, asset: str, silent: bool = False) -> bool:
    """
    Poll until market resolves or RESOLVE_MAX_WAIT is exhausted.
    Returns True if resolved, False if timed-out.
    """
    if _is_market_resolved(market_slug):
        if not silent:
            _print_and_log(asset, "RESOLVE_CHECK", "ALREADY_RESOLVED")
        return True

    if not silent:
        _print_and_log(
            asset, "RESOLVE_WAIT", "WAITING",
            f"up to {RESOLVE_MAX_WAIT}s (every {RESOLVE_CHECK_INTERVAL}s)"
        )

    waited = 0
    while waited < RESOLVE_MAX_WAIT:
        time.sleep(RESOLVE_CHECK_INTERVAL)
        waited += RESOLVE_CHECK_INTERVAL
        if _is_market_resolved(market_slug):
            if not silent:
                _print_and_log(asset, "RESOLVE_CHECK", "RESOLVED", f"after {waited}s")
            return True
        if not silent:
            print(f"  [{waited}s / {RESOLVE_MAX_WAIT}s] {asset} still unresolved...")

    if not silent:
        _print_and_log(
            asset, "RESOLVE_TIMEOUT", "WARNING",
            f"not resolved after {RESOLVE_MAX_WAIT}s — attempting redeem anyway"
        )
    return False


# ── TX HELPERS ────────────────────────────────────────────────────────────────

def _build_amounts_from_balances(
    effective_direction: str | None,
    yes_bal: int,
    no_bal: int,
    silent: bool = False,
) -> list[list[int]]:
    """
    Build the amounts list for NegRiskAdapter.redeemPositions() using REAL
    ERC-1155 balances instead of the sentinel value 2**128.

    FIX [CRITICAL]: The old sentinel approach (amounts = [0, 2**128] for NO)
    caused every redeem TX to revert because NegRiskAdapter does not cap the
    amount at what you hold — it tries to transfer exactly that many tokens.
    Passing the actual balance fixes this.

    amounts[0] = YES position raw units to redeem
    amounts[1] = NO  position raw units to redeem

    Returns a list of amounts arrays to submit:
      - Known direction with non-zero balance → one call
      - Unknown direction → one call per side that has a non-zero balance
      - Zero balance on a known side → empty list (caller should skip/warn)
    """
    direction_upper = (effective_direction or "").upper()

    if direction_upper in ("YES", "UP"):
        if yes_bal > 0:
            return [[yes_bal, 0]]
        if no_bal > 0:
            # clobTokenIds ordering is market-specific — ids[0] is not always the YES
            # token. If our YES slot is empty but NO slot has a balance, the token
            # ordering is reversed for this market. Redeem the non-zero side and let
            # the NegRiskAdapter decide what it pays out (it knows the winning outcome).
            log.warning(
                "_build_amounts: YES direction but yes_bal=0; no_bal is non-zero — "
                "clobTokenIds may be reversed for this market; redeeming ids[1] slot as fallback"
            )
            return [[0, no_bal]]
        log.warning("_build_amounts: YES direction but yes_bal=0 — nothing to redeem")
        return []

    if direction_upper in ("NO", "DOWN"):
        if no_bal > 0:
            return [[0, no_bal]]
        if yes_bal > 0:
            # Same reversal case — ids[0] holds the DOWN/NO position for this market.
            log.warning(
                "_build_amounts: NO direction but no_bal=0; yes_bal is non-zero — "
                "clobTokenIds may be reversed for this market; redeeming ids[0] slot as fallback"
            )
            return [[yes_bal, 0]]
        log.warning("_build_amounts: NO direction but no_bal=0 — nothing to redeem")
        return []

    # Unknown direction: try each side that actually has a balance.
    # One will be 0 (the losing side you never held or already redeemed),
    # the other will be non-zero (your winning position).
    amounts_list = []
    if yes_bal > 0:
        amounts_list.append([yes_bal, 0])
    if no_bal > 0:
        amounts_list.append([0, no_bal])

    if not amounts_list:
        log.warning("_build_amounts: both yes_bal and no_bal are 0 — nothing to redeem")

    return amounts_list


def _send_redeem_tx(w3, account, pk: str, condition_id: str,
                    amounts: list[int], silent: bool = False) -> tuple:
    """
    Build, sign, and send a NegRiskAdapter.redeemPositions call.

    NegRiskAdapter signature (2-arg, neg-risk markets):
      redeemPositions(bytes32 conditionId, uint256[] amounts)

    This is different from the legacy CTF 4-arg form used for standard binary markets.
    """
    adapter = w3.eth.contract(
        address=w3.to_checksum_address(NEG_RISK_ADAPTER),
        abi=NEG_RISK_ABI,
    )

    # Guard condition_id: Gamma returns 0x-prefixed 32-byte hex (66 chars total).
    cid = condition_id.strip()
    if not cid.startswith("0x"):
        cid = "0x" + cid
    if len(cid) != 66:   # 0x + 64 hex chars = 32 bytes
        raise ValueError(
            f"conditionId has unexpected length {len(cid)} (expected 66): {cid[:20]}…"
        )
    condition_bytes = w3.to_bytes(hexstr=cid)

    # EIP-1559 gas for Polygon
    base_fee     = w3.eth.get_block("latest")["baseFeePerGas"]
    priority_fee = w3.to_wei(30, "gwei")   # aggressive priority for redemptions
    max_fee      = base_fee + priority_fee

    # Acquire global nonce lock so concurrent threads and the two-call
    # unknown-direction path never collide on the same nonce.
    with _nonce_lock:
        nonce = w3.eth.get_transaction_count(account.address, "pending")

        tx = adapter.functions.redeemPositions(
            condition_bytes,    # bytes32 conditionId
            amounts,            # uint256[] [YES_amount, NO_amount]
        ).build_transaction({
            "chainId":              137,
            "maxFeePerGas":         max_fee,
            "maxPriorityFeePerGas": priority_fee,
            "gas":                  250000,
            "nonce":                nonce,
        })

        signed  = w3.eth.account.sign_transaction(tx, pk)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    # Lock released — other threads can now fetch nonce+1 safely

    if not silent:
        side = "YES" if amounts[0] > 0 else "NO"
        print(f"  TX sent (side={side}): {tx_hash.hex()[:20]}…")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    return tx_hash, receipt


# ── MAIN REDEMPTION FUNCTION ──────────────────────────────────────────────────

def redeem_all_positions(market_slug: str, direction: str = None,
                         silent: bool = False) -> dict:
    """
    Attempt to redeem a winning position for the given market_slug.

    Args:
        market_slug: e.g. "btc-updown-15m-1774033200"
        direction:   "YES" / "UP" or "NO" / "DOWN" (or None if unknown)
        silent:      suppress print output (log still writes)

    Returns:
        {"redeemed": int, "failed": int, "skipped": int, "tx_hashes": list,
         "winning_side": str | None}
    """
    result = {"redeemed": 0, "failed": 0, "skipped": 0, "tx_hashes": [], "winning_side": None}

    if not market_slug:
        if not silent:
            print("  auto_redeem: no market_slug provided, skipping")
        result["skipped"] = 1
        return result

    asset = market_slug.split("-")[0].upper()

    try:
        # ── Step 1: get condition ID ──────────────────────────────────────────
        if not silent:
            _print_and_log(asset, "CONDITION_FETCH", "START", market_slug)

        condition_id = _get_condition_id(market_slug)
        if not condition_id:
            if not silent:
                _print_and_log(asset, "CONDITION_FETCH", "FAILED", "no conditionId found")
            result["failed"] = 1
            return result

        if not silent:
            _print_and_log(asset, "CONDITION_FETCH", "OK", condition_id[:20] + "…")

        # ── Step 2: wait for resolution ───────────────────────────────────────
        _wait_for_resolution(market_slug, asset, silent=silent)

        # ── Step 2b: detect winning side from outcomePrices ───────────────────
        # Prefer the on-chain outcomePrices signal over the caller-supplied
        # direction — this gives the correct single TX (not the try-both path)
        # and guarantees reconciler has the authoritative outcome.
        detected_side = _get_winning_side(market_slug)
        if detected_side:
            result["winning_side"] = detected_side
            if not silent:
                _print_and_log(asset, "WINNING_SIDE", "DETECTED", detected_side)
            # Override direction with the authoritative value so the TX targets
            # only the correct index set (avoids the expected-revert on the wrong side)
            effective_direction = detected_side
        else:
            effective_direction = direction
            if not silent:
                _print_and_log(asset, "WINNING_SIDE", "UNKNOWN",
                               "outcomePrices not settled — using caller direction")

        # ── Step 2c: fetch CLOB token IDs and real ERC-1155 balances ─────────
        # FIX [CRITICAL]: Old code used sentinel 2**128 which caused NegRiskAdapter
        # to revert. We now query the actual balance and pass that.
        w3, (account, pk) = _get_web3(), _get_account()

        yes_token_id, no_token_id = _get_clob_token_ids(market_slug)
        if not silent:
            _print_and_log(asset, "TOKEN_IDS", "FETCHED",
                           f"YES={str(yes_token_id)[:12] if yes_token_id else 'None'}… "
                           f"NO={str(no_token_id)[:12] if no_token_id else 'None'}…")

        yes_bal = _get_position_balance(w3, account.address, yes_token_id) if yes_token_id else 0
        no_bal  = _get_position_balance(w3, account.address, no_token_id)  if no_token_id  else 0

        if not silent:
            _print_and_log(asset, "BALANCES", "FETCHED",
                           f"YES_raw={yes_bal} ({yes_bal / 1e6:.6f} shares)  "
                           f"NO_raw={no_bal} ({no_bal / 1e6:.6f} shares)")

        # ── Step 3: build amounts and redeem ──────────────────────────────────
        amounts_list = _build_amounts_from_balances(
            effective_direction, yes_bal, no_bal, silent=silent
        )

        if not amounts_list:
            _print_and_log(asset, "REDEEM", "SKIPPED",
                           "zero balance on target side — nothing to redeem "
                           "(position may already be redeemed or never held)")
            result["skipped"] = 1
            return result

        if not silent:
            _print_and_log(asset, "REDEEM", "START",
                           f"direction={effective_direction} | calls={len(amounts_list)}")

        for amounts in amounts_list:
            side = "YES" if amounts[0] > 0 else "NO"
            try:
                tx_hash, receipt = _send_redeem_tx(
                    w3, account, pk, condition_id, amounts, silent
                )
                if receipt["status"] == 1:
                    _print_and_log(asset, "REDEEM", "SUCCESS",
                                   f"side={side} | TX: {tx_hash.hex()[:12]}…")
                    result["redeemed"] += 1
                    result["tx_hashes"].append(tx_hash.hex())
                else:
                    _print_and_log(asset, "REDEEM", "REVERTED",
                                   f"side={side} | TX: {tx_hash.hex()[:12]}…")
                    if len(amounts_list) == 1:
                        result["failed"] += 1
                    # Unknown direction: one of the two may revert if balance was
                    # non-zero but contract-side conditions aren't met — log it.
            except Exception as e:
                log.warning(f"{asset} redeem side={side} error: {e}")
                if len(amounts_list) == 1:
                    result["failed"] += 1

    except Exception as e:
        _print_and_log(asset, "REDEEM", "ERROR", str(e))
        log.warning(f"auto_redeem error for {market_slug}: {e}")
        result["failed"] = 1

    return result


# ── ENTRY POINT ALIAS (called by main.py) ─────────────────────────────────────

def redeem_after_win(market_slug: str, direction: str = None,
                     silent: bool = False) -> dict:
    """Thin alias kept for compatibility with main.py."""
    return redeem_all_positions(market_slug=market_slug,
                                direction=direction,
                                silent=silent)


# ── STANDALONE ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s — %(message)s",
    )

    print("\n" + "=" * 60)
    print("  Auto Redeem — Multi-Asset")
    print("=" * 60)

    arg = sys.argv[1] if len(sys.argv) > 1 else "auto"

    if arg == "auto":
        # Sweep the previous 2 completed windows for all 4 assets.
        print("> Sweeping previous 2 windows for all assets...\n")
        for asset in ASSETS:
            for offset in [-900, -1800]:
                slug = get_active_15m_slug(asset, offset)
                print(f"\n  ── {asset} | {slug}")
                res = redeem_all_positions(slug, direction=None, silent=False)
                print(f"  Result: redeemed={res['redeemed']} failed={res['failed']} "
                      f"skipped={res['skipped']}")
    else:
        # Manual: python auto_redeem.py btc-updown-15m-1774033200 YES
        direction = sys.argv[2] if len(sys.argv) > 2 else None
        res = redeem_all_positions(arg, direction=direction, silent=False)
        print(f"\n  Done — redeemed={res['redeemed']} "
              f"failed={res['failed']} skipped={res['skipped']}")

    print("\n" + "=" * 60)