import os
from web3 import Web3
from eth_account import Account
from dotenv import load_dotenv
from web3.middleware import ExtraDataToPOAMiddleware

load_dotenv()

# 1. Setup Blockchain Connection
w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)

# 2. Load Your Wallet
PK = os.getenv("POLYGON_PRIVATE_KEY")
account = Account.from_key(PK)

# 3. Polymarket Contract Addresses
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# 🔴 PASTE YOUR TOPIC 3 HASH HERE 🔴
CONDITION_ID = "0xc689a60840bcb4b18e2f181fb389ca86f179d0853a9930ae1aef58a2145888bd"

print("🚀 Bypassing API and hitting the Smart Contract directly...")

# 4. The Minimal ABI to force the redeem
abi = [{"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"stateMutability":"nonpayable","type":"function"}]

ctf = w3.eth.contract(address=w3.to_checksum_address(CTF_CONTRACT), abi=abi)

# 5. Build and Fire the Transaction ([1] = YES shares)
tx = ctf.functions.redeemPositions(
    w3.to_checksum_address(USDC_E),
    bytes(32), # Parent collection is always zero for simple markets
    bytes.fromhex(CONDITION_ID[2:]),
    [1] 
).build_transaction({
    "chainId": 137,
    "nonce": w3.eth.get_transaction_count(account.address),
    "gas": 200000,
    "gasPrice": int(w3.eth.gas_price * 1.5)
})

# 6. Sign and Send
signed = w3.eth.account.sign_transaction(tx, PK)
tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)

print(f"✅ BAM! Transaction fired!")
print(f"Check your receipt: https://polygonscan.com/tx/{tx_hash.hex()}")