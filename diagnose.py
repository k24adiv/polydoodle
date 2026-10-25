"""
diagnose.py — Check wallet state before retrying approvals
Run this to see exactly what's going on with pending TXs and gas.
"""

import os
from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from web3.middleware import ExtraDataToPOAMiddleware
from eth_account import Account

USDC_E          = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CTF_EXCHANGE    = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK        = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
MAX_APPROVAL    = 2**256 - 1

ERC20_ABI = [
    {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],
     "name":"allowance","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],
     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"}
]

private_key = os.getenv("POLYGON_PRIVATE_KEY","").strip()
wallet = Account.from_key(private_key).address

RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon-rpc.com",
    "https://rpc-mainnet.matic.quiknode.pro",
]

print("\n── RPC CONNECTIVITY ─────────────────────────────────")
w3 = None
for rpc in RPCS:
    try:
        _w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
        _w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        block = _w3.eth.block_number
        gasprice = _w3.eth.gas_price / 1e9
        print(f"  ✅ {rpc}")
        print(f"     Block: {block} | Current gas price: {gasprice:.1f} gwei")
        if w3 is None:
            w3 = _w3
    except Exception as e:
        print(f"  ❌ {rpc}: {e}")

if not w3:
    print("No RPC working — check internet connection")
    exit()

print(f"\n── WALLET STATE ─────────────────────────────────────")
print(f"  Address: {wallet}")

# Confirmed nonce (TXs mined)
confirmed_nonce = w3.eth.get_transaction_count(wallet, "latest")
# Pending nonce (includes mempool TXs)
pending_nonce   = w3.eth.get_transaction_count(wallet, "pending")

print(f"  Confirmed nonce : {confirmed_nonce}")
print(f"  Pending nonce   : {pending_nonce}")
if pending_nonce > confirmed_nonce:
    stuck = pending_nonce - confirmed_nonce
    print(f"  ⚠️  {stuck} TX(s) stuck in mempool waiting to be mined")
else:
    print(f"  ✅ No pending TXs — mempool is clear")

pol_balance = w3.eth.get_balance(wallet) / 1e18
print(f"  POL balance     : {pol_balance:.4f} POL")

print(f"\n── USDC.e ALLOWANCES ────────────────────────────────")
usdc = w3.eth.contract(address=Web3.to_checksum_address(USDC_E), abi=ERC20_ABI)

balance = usdc.functions.balanceOf(wallet).call() / 1e6
print(f"  USDC.e balance  : ${balance:.2f}")

for name, addr in [("CTF Exchange", CTF_EXCHANGE), ("Neg Risk Exchange", NEG_RISK)]:
    allowance = usdc.functions.allowance(
        wallet, Web3.to_checksum_address(addr)
    ).call()
    if allowance > MAX_APPROVAL // 2:
        print(f"  ✅ {name}: APPROVED")
    elif allowance > 0:
        print(f"  ⚠️  {name}: partial allowance ({allowance/1e6:.2f} USDC.e) — needs reset")
    else:
        print(f"  ❌ {name}: NOT approved")

print(f"\n── RECOMMENDED GAS PRICE ────────────────────────────")
current = w3.eth.gas_price / 1e9
recommended = max(current * 1.3, current + 30)
print(f"  Current network : {current:.1f} gwei")
print(f"  Recommended     : {recommended:.0f} gwei (use this in next run)")
print()
