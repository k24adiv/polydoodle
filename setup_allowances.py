"""
setup_allowances.py — Approve Polymarket contracts to spend USDC.e + move CTF tokens
--------------------------------------------------------------------------------------
Two separate approval layers are required before live trading:

  Layer 1 — USDC.e ERC-20 approvals (so exchanges can take your collateral)
    • CTF Exchange      ← approve(spender, amount)
    • Neg Risk Exchange ← approve(spender, amount)

  Layer 2 — CTF ERC-1155 setApprovalForAll (so exchanges can move outcome tokens)
    • CTF Exchange      ← setApprovalForAll(operator, true)
    • Neg Risk Exchange ← setApprovalForAll(operator, true)

Both layers are checked and skipped if already set.
Handles stuck mempool TXs by re-using their nonces at a higher gas price.

Contract addresses confirmed by Polymarket support 06-04-2026.
"""

import os
import sys
import time
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from dotenv import load_dotenv
load_dotenv()

# ─── Contract addresses ────────────────────────────────────────────────────────
USDC_E            = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_CONTRACT      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"  # ERC-1155 outcome tokens
CTF_EXCHANGE      = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

MAX_APPROVAL = 2**256 - 1   # MAX_INT — approve once, never again
# To limit exposure use e.g. int(20 * 1e6)  →  $20 USDC.e

GAS_PRICE_GWEI = 170   # above typical 127 gwei — also replaces stuck TXs
GAS_LIMIT_ERC20    = 100_000
GAS_LIMIT_ERC1155  =  80_000
TIMEOUT            = 300   # 5 minutes per TX

RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://rpc-mainnet.matic.quiknode.pro",
]

# ─── ABIs (only the functions we need) ────────────────────────────────────────

ERC20_ABI = [
    {
        "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# ERC-1155 interface — only setApprovalForAll + isApprovedForAll
CTF_ABI = [
    {
        "inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
        "name": "setApprovalForAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}],
        "name": "isApprovedForAll",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _connect() -> "Web3":
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware

    for rpc in RPCS:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
            if w3.is_connected():
                print(f"  ✅ RPC  : {rpc}")
                return w3
        except Exception:
            continue

    print("  ❌ All RPCs failed — check your connection")
    sys.exit(1)


def _send_and_wait(w3, signed_tx, label: str, timeout: int) -> bool:
    """Broadcast a signed TX, wait for receipt, return True on success."""
    try:
        tx_hash = w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"  📤 TX   : 0x{tx_hash.hex()}")
        print(f"  ⏳ Waiting up to {timeout}s...")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        if receipt["status"] == 1:
            print(f"  ✅ {label}: confirmed ✓  (block {receipt['blockNumber']})")
            return True
        print(f"  ❌ {label}: TX reverted on-chain (status=0)")
        return False
    except Exception as e:
        print(f"  ❌ {label}: {e}")
        return False


# ─── Layer 1 — ERC-20 USDC.e approvals ────────────────────────────────────────

def approve_usdc(w3, usdc_contract, wallet: str, private_key: str,
                 spender_name: str, spender_address: str, nonce: int) -> bool:
    """
    Approve `spender_address` to transfer USDC.e from `wallet`.
    Skips if allowance is already >= MAX_APPROVAL // 2.
    """
    from web3 import Web3

    spender_cs = Web3.to_checksum_address(spender_address)

    try:
        current = usdc_contract.functions.allowance(wallet, spender_cs).call()
        if current >= MAX_APPROVAL // 2:
            print(f"\n  ✅ USDC [{spender_name}]: already approved — skipping")
            return True
    except Exception as e:
        print(f"  ⚠️  Could not read allowance for {spender_name}: {e}")

    print(f"\n  🔄 USDC approve → {spender_name}  (nonce={nonce}, gas={GAS_PRICE_GWEI} gwei)")
    try:
        tx = usdc_contract.functions.approve(spender_cs, MAX_APPROVAL).build_transaction({
            "chainId":  137,
            "gas":      GAS_LIMIT_ERC20,
            "gasPrice": w3.to_wei(GAS_PRICE_GWEI, "gwei"),
            "nonce":    nonce,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        return _send_and_wait(w3, signed, f"USDC [{spender_name}]", TIMEOUT)
    except Exception as e:
        print(f"  ❌ USDC approve [{spender_name}] build failed: {e}")
        return False


# ─── Layer 2 — ERC-1155 CTF setApprovalForAll ─────────────────────────────────

def set_approval_for_all(w3, ctf_contract, wallet: str, private_key: str,
                         operator_name: str, operator_address: str, nonce: int) -> bool:
    """
    Call setApprovalForAll(operator, true) on the CTF ERC-1155 contract.
    This lets the exchange move your YES/NO outcome tokens when a market resolves.
    Skips if already approved.
    """
    from web3 import Web3

    operator_cs = Web3.to_checksum_address(operator_address)

    try:
        already = ctf_contract.functions.isApprovedForAll(wallet, operator_cs).call()
        if already:
            print(f"\n  ✅ CTF  [{operator_name}]: setApprovalForAll already set — skipping")
            return True
    except Exception as e:
        print(f"  ⚠️  Could not read isApprovedForAll for {operator_name}: {e}")

    print(f"\n  🔄 CTF  setApprovalForAll → {operator_name}  (nonce={nonce}, gas={GAS_PRICE_GWEI} gwei)")
    try:
        tx = ctf_contract.functions.setApprovalForAll(operator_cs, True).build_transaction({
            "chainId":  137,
            "gas":      GAS_LIMIT_ERC1155,
            "gasPrice": w3.to_wei(GAS_PRICE_GWEI, "gwei"),
            "nonce":    nonce,
        })
        signed = w3.eth.account.sign_transaction(tx, private_key)
        return _send_and_wait(w3, signed, f"CTF  [{operator_name}]", TIMEOUT)
    except Exception as e:
        print(f"  ❌ CTF setApprovalForAll [{operator_name}] build failed: {e}")
        return False


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 57)
    print("  POLYMARKET ALLOWANCE SETUP — v4 (+ CTF setApprovalForAll)")
    print("=" * 57)

    private_key = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
    if not private_key:
        print("  ❌ POLYGON_PRIVATE_KEY not found in .env")
        sys.exit(1)

    from web3 import Web3
    from eth_account import Account

    w3      = _connect()
    account = Account.from_key(private_key)
    wallet  = account.address
    print(f"  Wallet : {wallet}")

    usdc_contract = w3.eth.contract(
        address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI
    )
    ctf_contract = w3.eth.contract(
        address=Web3.to_checksum_address(CTF_CONTRACT), abi=CTF_ABI
    )

    bal = usdc_contract.functions.balanceOf(wallet).call() / 1e6
    print(f"  USDC.e : ${bal:.4f}")

    # ── Nonce accounting ──────────────────────────────────────────────────────
    confirmed_nonce = w3.eth.get_transaction_count(wallet, "latest")
    pending_nonce   = w3.eth.get_transaction_count(wallet, "pending")
    stuck           = pending_nonce - confirmed_nonce
    
    print(f"\n  Confirmed nonce : {confirmed_nonce}")
    print(f"  Pending nonce   : {pending_nonce}")
    
    if stuck:
        print(f"  ⚠️  {stuck} stuck TX(s) detected. High gas ({GAS_PRICE_GWEI} gwei) will attempt to overwrite/clear them.")

    results = {}

    # ── Layer 1 — USDC.e ERC-20 ──────────────────────────────────────────
    print("\n" + "─" * 57)
    print("  LAYER 1 — USDC.e ERC-20 approvals")
    print("─" * 57)

    # TX 1: CTF Exchange
    curr_nonce = w3.eth.get_transaction_count(wallet, "latest")
    results["usdc_ctf"] = approve_usdc(w3, usdc_contract, wallet, private_key, 
                                      "CTF Exchange", CTF_EXCHANGE, curr_nonce)
    _pause(10, "block confirmation")

    # TX 2: Neg Risk Exchange
    curr_nonce = w3.eth.get_transaction_count(wallet, "latest")
    results["usdc_neg"] = approve_usdc(w3, usdc_contract, wallet, private_key, 
                                      "Neg Risk Exchange", NEG_RISK_EXCHANGE, curr_nonce)
    _pause(10, "block confirmation")

    # ── Layer 2 — CTF ERC-1155 ──────────────────────────────────────────
    print("\n" + "─" * 57)
    print("  LAYER 2 — CTF ERC-1155 setApprovalForAll")
    print("─" * 57)

    # TX 3: CTF Exchange
    curr_nonce = w3.eth.get_transaction_count(wallet, "latest")
    results["ctf_ctf"] = set_approval_for_all(w3, ctf_contract, wallet, private_key, 
                                             "CTF Exchange", CTF_EXCHANGE, curr_nonce)
    _pause(10, "block confirmation")

    # TX 4: Neg Risk Exchange
    curr_nonce = w3.eth.get_transaction_count(wallet, "latest")
    results["ctf_neg"] = set_approval_for_all(w3, ctf_contract, wallet, private_key, 
                                             "Neg Risk Exchange", NEG_RISK_EXCHANGE, curr_nonce)

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 57)
    all_ok = all(results.values())

    _status = lambda ok: "✅" if ok else "❌"
    print(f"  {_status(results['usdc_ctf'])} USDC    → CTF Exchange")
    print(f"  {_status(results['usdc_neg'])} USDC    → Neg Risk Exchange")
    print(f"  {_status(results['ctf_ctf'])} CTF     setApprovalForAll → CTF Exchange")
    print(f"  {_status(results['ctf_neg'])} CTF     setApprovalForAll → Neg Risk Exchange")

    if all_ok:
        print("\n  ✅ ALL 4 APPROVALS SET — ready for live trading")
    else:
        print(f"\n  ⚠️  Some approvals failed. Re-run script to retry.")
    print("=" * 57 + "\n")

def _pause(seconds: int, reason: str) -> None:
    print(f"\n  ⏸  Pausing {seconds}s for {reason}...")
    time.sleep(seconds)

if __name__ == "__main__":
    main()