# SolScope

Solana explorer — slots, transactions, accounts, SPL tokens, and programs on Solana mainnet. Keyless: data comes from the public Solana RPC (`api.mainnet-beta.solana.com`, with `solana.publicnode.com` for fast methods). No API keys anywhere.

## Run locally

```bash
cd solscope
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --port 8041
# open http://127.0.0.1:8041/  (or /#/tx/<signature>)
```

`requirements.txt` pins the same FastAPI/uvicorn/httpx versions as the other Scopes.

## Deploy (Vercel)

Serverless Python, root directory `solscope`:

- `vercel.json` runs `python3 ../scripts/sync-common.py sync` as its buildCommand, so a deploy always bundles the latest shared code, then rewrites every path to `/api/index`
- `api/index.py` re-exports the FastAPI `app` (ASGI)
- The app serves `static/` itself, so the whole site (API + frontend) runs in the one Python function

## Layout

```
app.py              Thin entrypoint (create_solana_app from common/factory.py)
api/index.py        Vercel serverless entrypoint (re-exports app)
frontend/           Solana SPA source (app.js + index.html) — OVERLAY, see below
static/             Generated: canonical frontend + overlay + config.js
common/             Generated copy of repo-root common/
config.py           Single source of truth for this app's metadata
```

The Solana SPA is an overlay: `scripts/sync-common.py` copies the canonical `frontend/` files into `static/`, then overlays `solscope/frontend/` on top (same filenames only: `app.js`, `index.html`). The EVM SPA does not fit Solana (slots not blocks, signatures not hashes, accounts/programs), so SolScope ships its own views in the same light theme.

## Backend endpoints (`/api/solscope/*`)

| Endpoint | Source |
|---|---|
| `GET /stats` | getSlot + getEpochInfo + getRecentPerformanceSamples + getSupply + CoinGecko SOL price |
| `GET /slots` | Latest confirmed slots (getBlock, signatures only) |
| `GET /slot/{n}` | Slot detail + first 100 signatures |
| `GET /tx/{sig}` | getTransaction (jsonParsed): status, fee, parsed instructions, balance/token changes, logs |
| `GET /address/{addr}` | getAccountInfo + SOL balance + SPL holdings (USD) + recent signatures |
| `GET /address/{addr}/signatures` | getSignaturesForAddress (paginated via `?before=`) |
| `GET /token/{mint}` | Mint info + supply + USD price + top holders |
| `GET /token/{mint}/transfers` | Recent signatures involving the mint |
| `GET /program/{addr}` | Program account + recent invoking signatures |
| `GET /search?q=` | Slot number / signature / base58 address (account/token/program) |

## Notes

- `getBlock` needs `maxSupportedTransactionVersion: 1` (versioned transactions exist on mainnet).
- `getTokenLargestAccounts` is 429-prone on the public RPC — holders are reported unavailable when it refuses, never faked.
- Token-2022 holdings are invisible (mainnet-beta rejects the Token-2022 `getProgramAccounts` query) — same limit as the wallet dashboard.
- Jupiter APIs are unreachable from this sandbox — not used anywhere.
