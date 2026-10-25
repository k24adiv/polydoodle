# wrap_to_pusd.py
import os
from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from eth_account import Account
from web3.middleware import ExtraDataToPOAMiddleware

RPC      = "https://polygon-bor-rpc.publicnode.com"
USDCE    = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PUSD     = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
ONRAMP   = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
MAX      = 2**256 - 1

V2_CONTRACTS = [
    "0xE111180000d2663C0091e4f400237545B87B996B",
    "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296",
    "0xe2222d279d744050d28e00520010520000310F59",
]

ERC20_ABI = [
    {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"},
    {"inputs":[{"name":"account","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
]
ONRAMP_ABI = [
    {"inputs":[{"name":"_asset","type":"address"},{"name":"_to","type":"address"},{"name":"_amount","type":"uint256"}],"name":"wrap","outputs":[],"stateMutability":"nonpayable","type":"function"},
]

w3 = Web3(Web3.HTTPProvider(RPC))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
pk      = os.getenv("POLYGON_PRIVATE_KEY")
account = Account.from_key(pk)

usdce  = w3.eth.contract(address=w3.to_checksum_address(USDCE),  abi=ERC20_ABI)
pusd   = w3.eth.contract(address=w3.to_checksum_address(PUSD),   abi=ERC20_ABI)
onramp = w3.eth.contract(address=w3.to_checksum_address(ONRAMP), abi=ONRAMP_ABI)

bal = usdce.functions.balanceOf(account.address).call()
print(f"USDC.e balance: {bal / 1e6:.2f}")

def send(tx):
    signed  = w3.eth.account.sign_transaction(tx, pk)
    txhash  = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(txhash, timeout=60)
    status  = "✅" if receipt["status"] == 1 else "❌"
    print(f"  {status} TX: {txhash.hex()[:20]}...")
    return receipt

def build(fn, nonce):
    base = w3.eth.get_block("latest")["baseFeePerGas"]
    return fn.build_transaction({
        "chainId": 137, "nonce": nonce,
        "maxFeePerGas": base + w3.to_wei(30, "gwei"),
        "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
        "gas": 150000,
    })

# Step 1: Approve onramp to spend USDC.e
print("\nStep 1: Approving onramp for USDC.e...")
nonce = w3.eth.get_transaction_count(account.address, "pending")
send(build(usdce.functions.approve(w3.to_checksum_address(ONRAMP), bal), nonce))

# Step 2: Wrap USDC.e → pUSD
print("Step 2: Wrapping USDC.e → pUSD...")
nonce = w3.eth.get_transaction_count(account.address, "pending")
send(build(onramp.functions.wrap(w3.to_checksum_address(USDCE), account.address, bal), nonce))

pusd_bal = pusd.functions.balanceOf(account.address).call()
print(f"  pUSD balance: {pusd_bal / 1e6:.2f}")

# Step 3: Approve V2 exchange contracts for pUSD
print("Step 3: Approving V2 contracts for pUSD...")
for contract in V2_CONTRACTS:
    nonce = w3.eth.get_transaction_count(account.address, "pending")
    print(f"  Approving {contract[:20]}...")
    send(build(pusd.functions.approve(w3.to_checksum_address(contract), MAX), nonce))

print("\nDone — ready to trade.")