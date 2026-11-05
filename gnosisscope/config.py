"""GnosisScope config — single source of truth for this app.

Read by scripts/sync-common.py to generate static/config.js, and by
app.py to build the FastAPI app. Edit here; never hand-edit the
generated files (static/config.js, common/, static/ frontend files).
"""
SCOPE = "gnosisscope"
TITLE = "GnosisScope"
BRAND_LETTER = "G"
CHAIN = "gnosis"
WALLET_ONLY = False
WALLET_URL = "https://walletscope.vercel.app"
NETPILL = None
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
        "chain": None,
        "name": "WalletScope",
        "url": "https://walletscope.vercel.app"
    }
]
