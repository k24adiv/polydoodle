# fix_last_approval.py
import os
from dotenv import load_dotenv
load_dotenv()

from web3 import Web3
from eth_account import Account
from web3.middleware import ExtraDataToPOAMiddleware

w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
pk      = os.getenv("POLYGON_PRIVATE_KEY")
account = Account.from_key(pk)

PUSD    = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
TARGET  = "0xe2222d279d744050d28e00520010520000310F59"
MAX     = 2**256 - 1

ERC20_ABI = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"stateMutability":"nonpayable","type":"function"}]

pusd  = w3.eth.contract(address=w3.to_checksum_address(PUSD), abi=ERC20_ABI)
nonce = w3.eth.get_transaction_count(account.address, "pending")
base  = w3.eth.get_block("latest")["baseFeePerGas"]

tx = pusd.functions.approve(w3.to_checksum_address(TARGET), MAX).build_transaction({
    "chainId": 137, "nonce": nonce,
    "maxFeePerGas": base + w3.to_wei(30, "gwei"),
    "maxPriorityFeePerGas": w3.to_wei(30, "gwei"),
    "gas": 150000,
})
signed  = w3.eth.account.sign_transaction(tx, pk)
txhash  = w3.eth.send_raw_transaction(signed.raw_transaction)
receipt = w3.eth.wait_for_transaction_receipt(txhash, timeout=60)
print("✅ Done" if receipt["status"] == 1 else "❌ Failed")
print(f"TX: {txhash.hex()[:20]}...")