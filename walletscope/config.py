"""WalletScope config — single source of truth for this app.

Read by scripts/sync-common.py to generate static/config.js, and by
app.py to build the FastAPI app. Edit here; never hand-edit the
generated files (static/config.js, common/, static/ frontend files).
"""
SCOPE = "walletscope"
TITLE = "WalletScope"
BRAND_LETTER = "W"
# No single chain: the dashboard aggregates all chains. "ethereum" is
# only the default context (labels + ENS resolution run on mainnet).
CHAIN = "ethereum"
WALLET_ONLY = True
WALLET_URL = "https://walletscope.vercel.app"
NETPILL = "Multi-chain wallet"
SIBLINGS = [
    {
        "chain": None,
        "name": "Cryptoscope",
        "url": "https://overview.vercel.app"
    },
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
        "chain": "solana",
        "name": "SolScope",
        "url": "https://solscope.vercel.app"
    },
    {
        "chain": "bitcoin",
        "name": "BitScope",
        "url": "https://bitscope.vercel.app"
    },
    {
        "chain": None,
        "name": "USDT Scope",
        "url": "https://usdtscope.vercel.app"
    },
    {
        "chain": None,
        "name": "USDC Scope",
        "url": "https://usdcscope.vercel.app"
    },
    {
        "chain": None,
        "name": "DAI Scope",
        "url": "https://daiscope.vercel.app"
    },
    {
        "chain": None,
        "name": "USDe Scope",
        "url": "https://usdescope.vercel.app"
    },
    {
        "chain": None,
        "name": "FDUSD Scope",
        "url": "https://fdusdsscope.vercel.app"
    },
    {
        "chain": None,
        "name": "PYUSD Scope",
        "url": "https://pyusdsscope.vercel.app"
    },
    {
        "chain": None,
        "name": "TUSD Scope",
        "url": "https://tusdscope.vercel.app"
    },
    {
        "chain": None,
        "name": "USDD Scope",
        "url": "https://usddscope.vercel.app"
    }
]
