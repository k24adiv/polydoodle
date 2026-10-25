"""
check_positions.py — Diagnose why wins aren't showing up
Run: python check_positions.py <market_slug>
Example: python check_positions.py btc-updown-15m-1775715300
"""

import os, sys, requests
from dotenv import load_dotenv
load_dotenv()

# ── CONFIG ────────────────────────────────────────────────────────────────────
CTF_CONTRACT = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E       = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
GAMMA_URL    = "https://gamma-api.polymarket.com/events"

CTF_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}, {"name": "id", "type": "uint256"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSets",          "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "collateralToken",    "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId",        "type": "bytes32"},
            {"name": "indexSet",           "type": "uint256"}
        ],
        "name": "getCollectionId",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "collectionId",    "type": "bytes32"}
        ],
        "name": "getPositionId",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

USDC_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# ── HELPERS ───────────────────────────────────────────────────────────────────
def get_w3():
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    ankr_key = os.getenv("ANKR_KEY", "").strip()
    rpc = f"https://rpc.ankr.com/polygon/{ankr_key}" if ankr_key else "https://polygon-bor-rpc.publicnode.com"
    w3 = Web3(Web3.HTTPProvider(rpc))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        print(f"  ❌ Cannot connect to RPC: {rpc}")
        sys.exit(1)
    return w3

def get_wallet():
    from eth_account import Account
    key = os.getenv("POLYGON_PRIVATE_KEY", "").strip()
    if not key:
        print("  ❌ POLYGON_PRIVATE_KEY not set in .env")
        sys.exit(1)
    acc = Account.from_key(key)
    return acc.address

def fetch_market(slug):
    r = requests.get(GAMMA_URL, params={"slug": slug}, timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data:
        return None, None
    event = data[0]
    markets = event.get("markets", [])
    condition_id = None
    for m in markets:
        if isinstance(m, dict):
            cid = m.get("conditionId") or m.get("condition_id")
            if cid:
                condition_id = cid
                break
    return event, condition_id

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    if not slug:
        print("Usage: python check_positions.py <market_slug>")
        print("Example: python check_positions.py btc-updown-15m-1775715300")
        sys.exit(1)

    print("\n" + "="*60)
    print(f"  Position Diagnostic: {slug}")
    print("="*60)

    # 1. Fetch market info
    print("\n[1] Fetching market info from Gamma API...")
    event, condition_id = fetch_market(slug)
    if not event:
        print("  ❌ Market not found on Gamma API")
        sys.exit(1)

    print(f"  Title:        {event.get('title')}")
    print(f"  Closed:       {event.get('closed')}")
    print(f"  Condition ID: {condition_id}")

    markets = event.get("markets", [])
    for i, m in enumerate(markets):
        if isinstance(m, dict):
            print(f"  Market [{i}] resolved={m.get('resolved')} closed={m.get('closed')} active={m.get('active')}")
            print(f"           question: {m.get('question', 'n/a')[:60]}")

    if not condition_id:
        print("  ❌ No condition ID found — can't proceed")
        sys.exit(1)

    # 2. Connect Web3
    print("\n[2] Connecting to Polygon...")
    w3 = get_w3()
    wallet = get_wallet()
    print(f"  Wallet:  {wallet}")
    print(f"  Block:   {w3.eth.block_number}")

    # 3. USDC.e balance
    print("\n[3] Checking USDC.e wallet balance...")
    usdc = w3.eth.contract(address=w3.to_checksum_address(USDC_E), abi=USDC_ABI)
    usdc_bal = usdc.functions.balanceOf(wallet).call()
    print(f"  USDC.e balance: {usdc_bal / 1e6:.4f} USDC")

    # 4. CTF position token balances
    print("\n[4] Checking CTF position token balances...")
    ctf = w3.eth.contract(address=w3.to_checksum_address(CTF_CONTRACT), abi=CTF_ABI)

    condition_bytes = bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id)
    parent = bytes(32)

    results = {}
    for index_set in [1, 2]:
        try:
            collection_id = ctf.functions.getCollectionId(
                w3.to_checksum_address(USDC_E),
                parent,
                condition_bytes,
                index_set
            ).call()
            position_id = ctf.functions.getPositionId(
                w3.to_checksum_address(USDC_E),
                collection_id
            ).call()
            balance = ctf.functions.balanceOf(wallet, position_id).call()
            label = "YES (outcome 1)" if index_set == 1 else "NO  (outcome 2)"
            print(f"  {label} — position_id: {position_id} — balance: {balance / 1e6:.4f} tokens")
            results[index_set] = balance
        except Exception as e:
            print(f"  ⚠️  Could not fetch balance for indexSet={index_set}: {e}")
            results[index_set] = 0

    # 5. Verdict
    print("\n[5] Verdict...")
    total_tokens = sum(results.values())

    if total_tokens == 0:
        print("""
  ❌ ZERO CTF tokens in your wallet for this market.
  
  This means one of:
  a) The CLOB order was placed but never FILLED (partial/no fill)
  b) Polymarket's CLOB held the position — never sent tokens to your wallet
  c) The order was placed on a different wallet/address
  
  → Running auto_redeem.py will REVERT (nothing to redeem).
  → Check the Polymarket UI under 'Activity' or 'Portfolio' instead.
  → If the win shows there, use the UI Redeem button directly.
  → Share your PolygonScan wallet URL for us to check on-chain fills.
""")
    else:
        print(f"""
  ✅ Found {total_tokens / 1e6:.4f} position tokens in wallet.
  → You CAN run: python auto_redeem.py {slug}
  → Expected USDC.e back: ~{total_tokens / 1e6:.4f}
""")

    print("="*60)

if __name__ == "__main__":
    main()
