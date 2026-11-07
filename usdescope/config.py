""" USDe Scope config — single source of truth for this app.

Read by scripts/sync-common.py to generate static/config.js, and by
app.py to build the FastAPI app. Edit here; never hand-edit the
generated files (static/config.js, common/, static/ frontend files).
The dashboard SPA itself lives in usdescope/frontend/ (overlay, see
scripts/sync-common.py) — it replaces the canonical frontend files.
Coin data lives in common/stablecoin.py (STABLECOINS["usdescope"]).
"""
SCOPE = "usdescope"
TITLE = "USDe Scope"
BRAND_LETTER = "E"
CHAIN = "ethereum"
WALLET_ONLY = False
WALLET_URL = "https://walletscope.fly.dev"
NETPILL = "USDe · Ethereum"
SIBLINGS = [
    {
        "chain": None,
        "name": "Cryptoscope",
        "url": "https://cryptoscope-overview.fly.dev"
    },
    {
        "chain": "ethereum",
        "name": "EthScope",
        "url": "https://ethscope.fly.dev"
    },
    {
        "chain": "sepolia",
        "name": "SepoliaScope",
        "url": "https://sepoliascope.fly.dev"
    },
    {
        "chain": "optimism",
        "name": "OptimismScope",
        "url": "https://optimismscope.fly.dev"
    },
    {
        "chain": "gnosis",
        "name": "GnosisScope",
        "url": "https://gnosisscope.fly.dev"
    },
    {
        "chain": "solana",
        "name": "SolScope",
        "url": "https://solscope.fly.dev"
    },
    {
        "chain": None,
        "name": "WalletScope",
        "url": "https://walletscope.fly.dev"
    },
    {
        "chain": "bitcoin",
        "name": "BitScope",
        "url": "https://bitscope.fly.dev"
    },
    {
        "chain": None,
        "name": "USDT Scope",
        "url": "https://usdtscope.fly.dev"
    },
    {
        "chain": None,
        "name": "USDC Scope",
        "url": "https://usdcscope.fly.dev"
    },
    {
        "chain": None,
        "name": "DAI Scope",
        "url": "https://daiscope.fly.dev"
    },
    {
        "chain": None,
        "name": "FDUSD Scope",
        "url": "https://fdusdsscope.fly.dev"
    },
    {
        "chain": None,
        "name": "PYUSD Scope",
        "url": "https://pyusdsscope.fly.dev"
    },
    {
        "chain": None,
        "name": "TUSD Scope",
        "url": "https://tusdscope.fly.dev"
    },
    {
        "chain": None,
        "name": "USDD Scope",
        "url": "https://usddscope.fly.dev"
    }
]
