"""EthScope config — single source of truth for this app.

Read by scripts/sync-common.py to generate static/config.js, and by
app.py to build the FastAPI app. Edit here; never hand-edit the
generated files (static/config.js, common/, static/ frontend files).
"""
SCOPE = "ethscope"
TITLE = "EthScope"
BRAND_LETTER = "E"
CHAIN = "ethereum"
WALLET_ONLY = False
WALLET_URL = "https://walletscope.vercel.app"
NETPILL = None
SIBLINGS = [
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
        "chain": "solana",
        "name": "SolScope",
        "url": "https://solscope.vercel.app"
    },
    {
        "chain": None,
        "name": "WalletScope",
        "url": "https://walletscope.vercel.app"
    }
]
