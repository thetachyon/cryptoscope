# WalletScope

Cross-chain wallet dashboard — one address across Ethereum, Sepolia, Optimism, and Gnosis: native + ERC-20 holdings with USD values, portfolio history, estimated realized PnL, NFT gallery, and a unified activity feed. No API keys anywhere.

**Data sources (all keyless):** [Blockscout](https://eth.blockscout.com) v2 public API per chain; public JSON-RPC endpoints (`*.publicnode.com`); CoinGecko keyless API for token USD prices and daily closes.

## Run locally

```bash
cd walletscope
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --port 8035
# open http://127.0.0.1:8035/  (or /#/wallet/vitalik.eth)
```

`requirements.txt` pins `fastapi==0.162.0`, `uvicorn==0.54.0`, `httpx==0.28.1`.

## Deploy (Vercel)

Serverless Python, root directory `walletscope`:

- `vercel.json` rewrites every path to `/api/index`, and its `buildCommand` re-runs `scripts/sync-common.py sync` so the deploy always bundles the latest shared code
- `api/index.py` re-exports the FastAPI `app` (ASGI)
- The app serves `static/` itself, so the whole site (API + frontend) runs in the one Python function

## Layout

```
config.py           Single source of truth for this app (title, siblings) — drives static/config.js
app.py              Thin entrypoint (~15 lines): builds the app via common/factory.py
common/             GENERATED vendored copy of repo-root common/ — never hand-edit (see ../README.md)
static/             GENERATED vendored copy of repo-root frontend/ + config.js — never hand-edit
api/index.py        Vercel serverless entrypoint (re-exports app)
vercel.json         Vercel rewrites + buildCommand (re-syncs common/ at build time)
requirements.txt    Pinned deps
README.md           This file
```

All aggregation logic lives in `common/wallet.py` (per-chain `EVMAdapter`: address / token_balances / nfts / transactions / token_transfers — a future non-EVM chain plugs in as another adapter without rewriting the aggregation). This app only serves the dashboard.

## Backend endpoints (`/api/walletscope`)

| Endpoint | What it does |
|---|---|
| `GET /wallet/{address}` | Fast aggregate: per-chain native balance + USD + ENS, portfolio native total, value-weighted 24h change (native only at this stage). Token holdings are NOT fetched here (see below) |
| `GET /wallet/{address}/tokens[?chain=]` | Token holdings per chain (top 50 by USD value, totals, counts, NFT count). Slow for token-rich addresses — Blockscout `/token-balances` is one giant unpaginated download. Optional `?chain=` loads one chain only |
| `GET /wallet/{address}/history?days=30` | Portfolio value estimate: today's balances (native + top 5 tokens by value) × each day's historical price, 7/30/90 days. **Estimate, not true history** — see below |
| `GET /wallet/{address}/pnl` | Estimated realized PnL: FIFO over the latest ~50 token transfers/chain, price-at-tx-time via CoinGecko daily closes (90d window). **Estimate with stated coverage** — see below |
| `GET /wallet/{address}/nfts` | NFT holdings across all chains (first 50/chain) with image, collection, token id |
| `GET /wallet/{address}/activity[?chain=&…]` | Recent txs across all chains, merged + time-sorted (top 50), with per-chain `next_page_params`. With `?chain=<id>` + cursor params returns one further shaped page of that chain (Load-more) |
| `GET /search?q=` | Ethereum-mainnet search (ENS / address resolution for the dashboard) |
| `GET /chains`, `GET /docs` | All four chains; full endpoint catalogue |

Upstream failures return `{"error": ...}` (HTTP 502) — never tracebacks. A slow/failed chain returns an `error` for that chain only, never fails the whole dashboard.

## Method and honest limits

- **Two-tier loading.** The aggregate (`GET /wallet/{address}`) fetches native balances only — Blockscout's `/token-balances` is one giant unpaginated download (vitalik's mainnet list is 3.36 MB / 15–60s through a proxy), so holdings load progressively per chain via `/tokens?chain=`. The hero total and the combined 24h change recompute as each chain's holdings arrive; a chain that times out (45s guard) shows an error section, never fails the dashboard.
- **USD pricing.** Blockscout token `exchange_rate` first, CoinGecko `simple/token_price` fallback (one contract per request, max 5 lookups per tokens call — top-3 priced holdings get their 24h change first, remaining budget prices unpriced items). Unpriced tokens have no market price and are excluded from holdings totals. Sepolia ETH has no market value (Blockscout reports no price), so Sepolia balances are excluded from USD totals everywhere, including history.
- **History is an estimate, labeled as such in the UI.** Today's balances (native coins on priced chains + top 5 tokens by current value) multiplied by each day's historical price (CoinGecko daily closes). It assumes today's balances held throughout the window — deposits, withdrawals and trades are ignored — so it is NOT true historical PnL.
- **PnL is an estimate with stated coverage.** FIFO over the latest ~50 token transfers per chain (90-day window): incoming transfers are acquisitions at that day's CoinGecko close, outgoing transfers are disposals matched against lots; self-transfers ignored. Only tokens with CoinGecko price history can be priced — transfers of unlisted/spam tokens are counted as unpriced and excluded, and outgoing transfers with no cost basis in the window are counted as unmatched and excluded. The UI shows priced/unpriced/unmatched counts next to every figure.
- **NFTs** come from Blockscout `/addresses/{h}/nft`, first 50 per chain; cards link out to the instance pages on the chain's Scope app.
- **Activity** merges the first page of txs per chain, sorted by time; Load-more fetches further pages per chain (`/activity?chain=<id>&<cursor>`) and merges client-side.

## Frontend notes

- The dashboard is the whole app: `#/` is the wallet search, `#/wallet/{address}` (or an ENS name) is the dashboard. Explorer nav links are hidden; the header scope select jumps to the chain explorer apps.
- Tx/token/NFT links on the dashboard are absolute links into the chain's Scope app (e.g. a mainnet tx opens on EthScope).
- All fetches have a 20s client-side timeout; wallet calls use 60s because mainnet token lists are multi-MB. Pages show honest error states on 502/404 upstream failures.

## Notes

- Blockscout token objects expose the token address as `address_hash`, not `address` — the wallet PnL, NFT and token-transfer code reads `address_hash` first.
- Sandbox quirk: the environment's `NO_PROXY` contains bracketed IPv6 literals (`[::1]`) that httpx's env-proxy parser rejects — `common/clients.py` strips them at import. The egress proxy itself is still used for outbound calls.
- No Solana or BTC yet (EVM chains only). Solana holdings go into this dashboard first (public Solana RPC + Jupiter price API, keyless), then a full SolScope; BitScope follows the same pattern.
