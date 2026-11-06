# USDD Scope

Decentralized USD (USDD) stablecoin dashboard — live peg status, supply, holders,
transfers, depeg history and reserves — with a FastAPI backend and a
single-page frontend.

**Data source:** [DeFiLlama stablecoins API](https://stablecoins.llama.fi)
(peg price, supply history, per-chain breakdown) + [CoinGecko](https://www.coingecko.com)
keyless API (market cap / supply fallback) + TronScan keyless API (holders/transfers) + TronGrid (contract verification).
**Keys:** none. No API keys anywhere.

## Coin facts

| Field | Value |
|---|---|
| Symbol / name | USDD / Decentralized USD |
| Issuer | TRON DAO Reserve |
| DeFiLlama id | 14 |
| CoinGecko id | `usdd` |
| Primary chain (holders/transfers) | tron |
| Contract | `TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz` |

## Run locally

```bash
cd usddscope
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --port 80XX
# open http://127.0.0.1:80XX/
```

`requirements.txt` pins `fastapi==0.162.0`, `uvicorn==0.54.0`, `httpx==0.28.1`.

## Deploy (Vercel)

Serverless Python, same pattern as the other Scope apps:

- `vercel.json` runs `python3 ../scripts/sync-common.py sync` as its
  `buildCommand` (re-vendors `common/` + `static/` at build time) and
  rewrites every path to `/api/index`
- `api/index.py` re-exports the FastAPI `app` (ASGI)
- The app serves `static/` itself, so the whole site (API + frontend)
  runs in the one Python function
- `vercel deploy` installs `requirements.txt` automatically

## Layout

```
config.py           App metadata (single source of truth)
app.py              Thin entrypoint — builds the app via common/factory.py
api/index.py        Vercel serverless entrypoint (re-exports app)
frontend/app.js     Dashboard SPA (#/, #/holders, #/transfers, #/depegs,
                    #/reserves, #/api-docs)
frontend/index.html App shell (overlay — replaces the canonical one)
vercel.json         Vercel buildCommand + rewrites
requirements.txt    Pinned deps
```

All data logic lives in `common/stablecoin.py` (DeFiLlama + CoinGecko
clients, peg/depeg computation, per-coin config) and the factory in
`common/factory.py` (`create_stablecoin_app`).

## Backend endpoints (`/api/usddscope/*`)

| Endpoint | Source |
|---|---|
| `GET /stats` | price vs $1, deviation (bps), peg health, supply, chains, holder count, market cap |
| `GET /peg?days=30` | daily price history + depeg days in window (DeFiLlama `/stablecoinprices`) |
| `GET /supply?days=90` | daily supply history + per-chain breakdown + recent mint/burn deltas |
| `GET /holders` | top holders on the primary chain, with USD values |
| `GET /transfers` | recent transfers on the primary chain, with USD values |
| `GET /depegs` | every day in full price history where \|price − $1\| > 50 bps |
| `GET /reserves` | issuer attestation/transparency links + market figures (no invented reserve numbers) |
| `GET /chains` | per-chain supply breakdown |
| `GET /search?q=` | holder address or transaction hash on the primary chain |

## Notes

- A depeg day is a day where the DeFiLlama daily close deviated from
  $1.00 by more than 50 bps. Daily closes only — intraday extremes are
  not shown.
- Tron-native: holders/transfers come from TronScan (holder list capped at 10,000). Tron holds ~$1.15B of ~$1.50B total. The believed address TPYmHEhy5nXmKtc5f6KgQVPSMURkCPSoW9 is NOT a contract — verified empty via TronGrid.
- DeFiLlama's USDD supply history has an 83-day data gap (2026-07-07 → 2026-09-28, no daily points) — the supply chart draws a straight line across it. Verified 2026-11-05.
- Reserves: no keyless machine-readable API exposes live reserve /
  collateral figures for this coin (probed 2026-11-05). The reserves
  page links to the issuer's attestations and shows market figures
  labelled as market figures — never invented reserve numbers.
- DeFiLlama price history is cached 10 minutes server-side; coin
  detail 2 minutes.
