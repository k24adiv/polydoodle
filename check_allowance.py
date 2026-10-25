from web3 import Web3

w3 = Web3(Web3.HTTPProvider('https://rpc.ankr.com/polygon'))

USDC     = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
EXCHANGE = '0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E'
WALLET   = '0xA9270F6ba064c7F2abfc04ae28196a20E0616177'

abi = [
    {
        "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

usdc      = w3.eth.contract(address=Web3.to_checksum_address(USDC), abi=abi)
allowance = usdc.functions.allowance(WALLET, EXCHANGE).call()
balance   = usdc.functions.balanceOf(WALLET).call()

print(f'USDC Balance:   {balance / 1e6:.2f}')
print(f'USDC Allowance: {allowance / 1e6:.2f}')
print(f'Allowance OK:   {allowance > 1_000_000}')