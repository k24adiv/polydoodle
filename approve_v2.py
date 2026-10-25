import os
from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from eth_account import Account

RPC     = "https://polygon-bor-rpc.publicnode.com"
USDC    = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # native USDC on Polygon
MAX     = 2**256 - 1  # max uint256 approval

# V2 exchange contracts (from get_balance_allowance output)
V2_CONTRACTS = [
    "0xE111180000d2663C0091e4f400237545B87B996B",
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
    "0xe2222d279d744050d28e00520010520000310F59",
]

ERC20_ABI = [
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
     "name":"approve","outputs":[{"name":"","type":"bool"}],
     "stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],
     "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
     "stateMutability":"view","type":"function"},
]

w3      = Web3(Web3.HTTPProvider(RPC))
w3.middleware_onion.inject(__import__('web3.middleware', fromlist=['ExtraDataToPOAMiddleware']).ExtraDataToPOAMiddleware, layer=0)
pk      = os.getenv("POLYGON_PRIVATE_KEY")
account = Account.from_key(pk)
usdc    = w3.eth.contract(address=w3.to_checksum_address(USDC), abi=ERC20_ABI)

bal = usdc.functions.balanceOf(account.address).call()
print(f"Wallet USDC balance: {bal / 1e6:.2f}")
print(f"Wallet: {account.address}\n")

for contract in V2_CONTRACTS:
    spender = w3.to_checksum_address(contract)
    nonce   = w3.eth.get_transaction_count(account.address, "pending")
    base    = w3.eth.get_block("latest")["baseFeePerGas"]
    tx      = usdc.functions.approve(spender, MAX).build_transaction({
        "chainId": 137,
        "nonce": nonce,
        "maxFeePerGas": base + w3.to_wei(30, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        "gas": 100000,
    })
    signed  = w3.eth.account.sign_transaction(tx, pk)
    txhash  = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(txhash, timeout=60)
    status  = "✅ OK" if receipt["status"] == 1 else "❌ FAILED"
    print(f"{status} Approved {contract[:20]}... | TX: {txhash.hex()[:20]}...")