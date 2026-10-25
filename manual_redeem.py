"""
manual_redeem.py
Redeems the April 19 BTC NO position directly using the on-chain conditionId,
bypassing Gamma (which has purged the old market).
"""
import os
from dotenv import load_dotenv
from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account

load_dotenv()

CONDITION_ID  = "0xd5fcff3181d3093da1f2425b2e4b54f78725da18ea585944bb1a7f3482433f2f"
USDC_E        = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NO_TOKEN_ID   = 110243757689783121530347338464011346604475326966545029949140041838990441772957
WALLET        = "0xA9270F6ba064c7F2abfc04ae28196a20E0616177"

# Try both contracts — we'll see which one holds the position
CONTRACTS = {
    "NegRisk CTF Exchange": "0xC5d563A36AE78145C45a50134d48A1215220f80a",
    "Conditional Tokens":   "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
}

REDEEM_ABI = [{
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
}]

BALANCE_ABI = [{
    "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
    "name":   "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function",
}]

w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
assert w3.is_connected(), "RPC connection failed"

pk      = os.getenv("POLYGON_PRIVATE_KEY").strip()
account = Account.from_key(pk)

# ── Step 1: check token balance ───────────────────────────────────────────────
ctf_tokens = w3.eth.contract(
    address=w3.to_checksum_address("0x4d97dcd97ec945f40cf65f87097ace5ea0476045"),
    abi=BALANCE_ABI,
)
bal = ctf_tokens.functions.balanceOf(WALLET, NO_TOKEN_ID).call()
print(f"NO token balance: {bal} ({bal/1e6:.4f} shares)")

if bal == 0:
    print("Balance is 0 — position already redeemed or lost. Nothing to do.")
    exit()

print(f"Tokens found — attempting redemption (indexSet=2 = NO/DOWN)...\n")

# ── Step 2: try redeemPositions on both contracts ─────────────────────────────
condition_bytes = w3.to_bytes(hexstr=CONDITION_ID)
parent_id       = w3.to_bytes(hexstr="0x" + "0" * 64)

for name, addr in CONTRACTS.items():
    print(f"Trying {name} ({addr[:12]}...)...")
    try:
        contract  = w3.eth.contract(address=w3.to_checksum_address(addr), abi=REDEEM_ABI)
        base_fee  = w3.eth.get_block("latest")["baseFeePerGas"]
        nonce     = w3.eth.get_transaction_count(account.address, "pending")

        tx = contract.functions.redeemPositions(
            w3.to_checksum_address(USDC_E),
            parent_id,
            condition_bytes,
            [2],   # NO = indexSet 2
        ).build_transaction({
            "chainId":              137,
            "maxFeePerGas":         base_fee + w3.to_wei(30, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
            "gas":                  250000,
            "nonce":                nonce,
        })

        signed  = w3.eth.account.sign_transaction(tx, pk)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        print(f"  TX sent: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] == 1:
            print(f"  ✅ SUCCESS — USDC.e should be back in your wallet")
            break
        else:
            print(f"  ❌ Reverted — trying next contract...")

    except Exception as e:
        print(f"  Error: {e}\n  Trying next contract...")