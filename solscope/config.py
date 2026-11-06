"""SolScope config — single source of truth for this app.

Read by scripts/sync-common.py to generate static/config.js, and by
app.py to build the FastAPI app. Edit here; never hand-edit the
generated files (static/config.js, common/, static/ frontend files).
The Solana SPA itself lives in solscope/frontend/ (overlay, see
scripts/sync-common.py) — it replaces the canonical frontend files.
"""
SCOPE = "solscope"
TITLE = "SolScope"
BRAND_LETTER = "S"
CHAIN = "solana"
WALLET_ONLY = False
WALLET_URL = "https://walletscope.vercel.app"
NETPILL = "Solana Mainnet"
SIBLINGS = [
    {
        "chain": "ethereum",
        "name": "EthScope",
        "url": "https://ethscope.vercel.app"
    },
    {
        "chain": "sepolia",
        "name": "SepoliaScope",
        "url": "https://sepoliascope.vercel.app"
    },
    {
        "chain": "optimism",
        "name": "OptimismScope",
        "url": "https://optimismscope.vercel.app"
    },
    {
        "chain": "gnosis",
        "name": "GnosisScope",
        "url": "https://gnosisscope.vercel.app"
    },
    {
        "chain": None,
        "name": "WalletScope",
        "url": "https://walletscope.vercel.app"
    },
    {
        "chain": "bitcoin",
        "name": "BitScope",
        "url": "https://bitscope.vercel.app"
    }
]
