# Cryptoscope Overview

Cross-chain analytics landing page for the whole Cryptoscope repo —
global market, per-chain comparison, stablecoin peg table, Wormhole
bridge flows, top movers, and one search box that routes to the right
Scope app. Each Scope provides its specific crypto's analytics; this
app is the front door.

**Data sources:** CoinGecko keyless API (`/global`, `/coins/markets`),
DeFiLlama stablecoins API (via the stablecoin scopes), Wormholescan
keyless API (Wormhole bridge scorecards + hourly tx counts), Blockscout
v2 (EVM chains), public Solana RPC, mempool.space / Blockstream
Esplora (Bitcoin). **Keys:** none. No API keys anywhere.

**Bridge data note:** DeFiLlama's bridges API is paywalled (probed
2026-11-06 — it answers "Upgrade to the paid API plan"), so bridge
flows are Wormhole-only, labelled as such. No all-bridge total is
available keyless.

## Run locally

```bash
cd overview
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --port 8043
# open http://127.0.0.1:8043/
```

`requirements.txt` pins `fastapi==0.162.0`, `uvicorn==0.54.0`, `httpx==0.28.1`.

## Deploy (Vercel)

Serverless Python, same pattern as the other Scope apps:

- `vercel.json` runs `python3 ../scripts/sync-common.py sync` as its
  `buildCommand` (re-vendors `common/` + `static/` at build time) and
  rewrites every path to `/api/index`
- `api/index.py` re-exports the FastAPI `app` (ASGI)

## Backend endpoints (`/api/overview/*`)

| Endpoint | What it returns |
|---|---|
| `GET /overview` | Full aggregate: global, chains, stablecoins, bridges, movers. Each section degrades independently. |
| `GET /stats` | Global market: total market cap, BTC/ETH dominance, 24h change, total stablecoin supply. |
| `GET /chains` | One row per chain (Ethereum, Optimism, Gnosis, Sepolia, Solana, Bitcoin): price, latest block/slot, fee. |
| `GET /stablecoins` | One row per stablecoin scope: price, peg deviation, health, supply, market cap. |
| `GET /bridges` | Wormhole bridge flows (24h/7d/30d volume, messages, TVL, hourly tx counts). |
| `GET /movers` | Top gainers / losers among the top 50 coins by market cap (CoinGecko). |
| `GET /search?q=` | Routes a query to the right Scope. Ambiguous queries (plain number = block/slot/height; bare 64-hex = EVM tx or BTC txid) return candidates instead of guessing. |
| `GET /docs` | This endpoint list. |

## Search routing

- Stablecoin symbol/name (`USDC`, `tether`) → its stablecoin scope
- `0x…` address / tx hash, `*.eth` name, block number → EthScope
- Solana base58 address / signature → SolScope
- Bitcoin address (`1…`, `3…`, `bc1…`) → BitScope
- Plain number → candidates: Ethereum block, Solana slot, Bitcoin block
- Bare 64-hex → candidates: Ethereum tx, Bitcoin tx

## Layout

```
config.py           App metadata (SCOPE, TITLE, SIBLINGS incl. all 15 scopes)
app.py              Thin entrypoint (create_overview_app from common/factory.py)
api/index.py        Vercel serverless entrypoint (re-exports app)
frontend/index.html Overview landing SPA shell (overlay, see scripts/sync-common.py)
frontend/app.js     Landing-page SPA (hero + search, market cards, chain table,
                    stablecoin peg table, bridge flows, top movers)
vercel.json         Vercel buildCommand + rewrites
requirements.txt    Pinned deps
```

All data logic lives in `common/overview.py`; all UI lives in
`overview/frontend/`.
