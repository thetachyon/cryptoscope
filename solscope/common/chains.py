"""Chain registry for the *Scope apps — one entry per supported chain.

Each entry: Blockscout v2 API base, public JSON-RPC URL, CoinGecko
platform/coin ids, and the native-coin symbol. Only chains whose
Blockscout host answers AND whose public RPC answers were kept
(verified 2026-11-05). Dropped: arbitrum.blockscout.com (connection
fails), base.blockscout.com (HTTP 403), polygon.blockscout.com
(HTTP 403 Cloudflare). Do not re-add a dropped chain without re-probing.
"""


# ---------------------------------------------------------------- chains
# Only chains whose Blockscout host answers AND whose public RPC answers were
# kept (verified 2026-11-05). Dropped: arbitrum.blockscout.com (connection
# fails), base.blockscout.com (HTTP 403), polygon.blockscout.com (HTTP 403
# Cloudflare).
CHAINS = {
    "ethereum": {
        "label": "Ethereum",
        "blockscout": "https://eth.blockscout.com/api/v2",
        "rpc": "https://ethereum.publicnode.com",
        "coingecko_platform": "ethereum",
        "coingecko_coin": "ethereum",
        "native": "ETH",
    },
    "sepolia": {
        "label": "Sepolia",
        "blockscout": "https://eth-sepolia.blockscout.com/api/v2",
        "rpc": "https://ethereum-sepolia.publicnode.com",
        "coingecko_platform": "ethereum",
        "coingecko_coin": "ethereum",
        "native": "ETH",
    },
    "optimism": {
        "label": "Optimism",
        "blockscout": "https://optimism.blockscout.com/api/v2",
        "rpc": "https://optimism.publicnode.com",
        "coingecko_platform": "optimistic-ethereum",
        "coingecko_coin": "ethereum",
        "native": "ETH",
    },
    "gnosis": {
        "label": "Gnosis",
        "blockscout": "https://gnosis.blockscout.com/api/v2",
        "rpc": "https://gnosis.publicnode.com",
        "coingecko_platform": "xdai",
        "coingecko_coin": "xdai",
        "native": "xDAI",
    },
}

DEFAULT_CHAIN = "ethereum"


# ---------------------------------------------------------------- wallet chains
# WALLET_CHAINS is what the WalletScope dashboard aggregates over: the EVM
# chains above plus Solana (non-EVM — no Blockscout, no explorer app yet).
# Explorer apps use CHAINS only; never add a non-EVM chain there (the
# scope factory binds Blockscout + eth_call per chain).
WALLET_CHAINS = {
    **CHAINS,
    "solana": {
        "label": "Solana",
        "blockscout": None,
        "rpc": "https://api.mainnet-beta.solana.com",
        "coingecko_platform": "solana",
        "coingecko_coin": "solana",
        "native": "SOL",
        "native_decimals": 9,
        "adapter": "solana",
    },
}
