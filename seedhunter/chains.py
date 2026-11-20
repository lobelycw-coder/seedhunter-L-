EXPLORER = {
    "ethereum": "https://etherscan.io/address/{a}",
    "bsc": "https://bscscan.com/address/{a}",
    "polygon": "https://polygonscan.com/address/{a}",
    "base": "https://basescan.org/address/{a}",
    "arbitrum": "https://arbiscan.io/address/{a}",
    "optimism": "https://optimistic.etherscan.io/address/{a}",
    "avalanche": "https://snowtrace.io/address/{a}",
    "solana": "https://solscan.io/account/{a}",
}
NATIVE = {"ethereum": ("ETH", 18), "bsc": ("BNB", 18), "polygon": ("POL", 18),
          "base": ("ETH", 18), "arbitrum": ("ETH", 18), "optimism": ("ETH", 18),
          "avalanche": ("AVAX", 18), "solana": ("SOL", 9)}
TOKENS = {
    "ethereum": [("USDT", "0xdAC17F958D2ee523a2206206994597C13D831ec7", 6),
                 ("USDC", "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", 6)],
    "bsc": [("USDT", "0x55d398326f99059fF775485246999027B3197955", 18),
            ("USDC", "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d", 18)],
    "polygon": [("USDT", "0xc2132D05D31c914a87C6611C10748AEb04B58e8F", 6),
                ("USDC", "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", 6)],
    "base": [("USDC", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", 6)],
    "arbitrum": [("USDT", "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9", 6),
                 ("USDC", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", 6)],
    "optimism": [("USDT", "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58", 6),
                 ("USDC", "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", 6)],
    "avalanche": [("USDT", "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7", 6),
                  ("USDC", "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", 6)],
}
TRANSFER_TOPIC = "0x70a08231"
EIP1967_IMPL = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
TRANSFER_EVT = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TRANSFER_EVT_1155 = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"
JITO = "https://mainnet.block-engine.jito.wtf/api/v1/bundles"
JITO_TIPS = [
    "96gYZGLnJYVFmbjzopPSU6QiEV5fGqZNyN9nmNhvrZU5",
    "HFqU5x65DOvpqS1jXx8LmSGaGqvz4MnF9sZgLTFqJcN",
    "DttWaMuVvTiduZRnguLF7jNxTgiMBZ1hyAumKUiL2KRL",
    "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT",
]