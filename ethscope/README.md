# EthScope

An Etherscan-style block explorer for **Ethereum** — blocks, transactions, addresses, tokens, contracts, NFTs, charts, and gas — with a FastAPI backend and a single-page frontend. Single-chain by design: this app serves Ethereum only. The other chains live in their own apps (see [cryptoscope](../README.md)); the header scope switcher jumps between them, and the wallet dashboard lives in [WalletScope](../walletscope) (cross-chain).

**Data sources (all keyless, no API keys anywhere):** [Blockscout](https://eth.blockscout.com) v2 public API; public JSON-RPC endpoint (`ethereum.publicnode.com`) for pending blocks, nonces, and `eth_call`; CoinGecko keyless API for native-coin price history and token USD prices.

## Run locally

```bash
cd ethscope
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --port 8034
# open http://127.0.0.1:8034/
```

`requirements.txt` pins `fastapi==0.162.0`, `uvicorn==0.54.0`, `httpx==0.28.1`.

## Deploy (Vercel)

Serverless Python, root directory `ethscope`:

- `vercel.json` rewrites every path to `/api/index`, and its `buildCommand` re-runs `scripts/sync-common.py sync` so the deploy always bundles the latest shared code
- `api/index.py` re-exports the FastAPI `app` (ASGI)
- The app serves `static/` itself, so the whole site (API + frontend) runs in the one Python function
- `vercel deploy` installs `requirements.txt` automatically

## Layout

```
config.py           Single source of truth for this app (chain, title, siblings) — drives static/config.js
app.py              Thin entrypoint (~15 lines): binds the chain + metadata, builds the app via common/factory.py
common/             GENERATED vendored copy of repo-root common/ — never hand-edit (see ../README.md)
static/             GENERATED vendored copy of repo-root frontend/ + config.js — never hand-edit
api/index.py        Vercel serverless entrypoint (re-exports app)
vercel.json         Vercel rewrites + buildCommand (re-syncs common/ at build time)
requirements.txt    Pinned deps
README.md           This file
```

All data logic lives in `common/` (chain registry, Blockscout/RPC/CoinGecko clients, USD pricing, formatting, Keccak/ABI codec, explorer core, wallet aggregation). This app only configures which chain it serves.

## Chain

| Chain | Blockscout host | Public RPC | Native |
|---|---|---|---|
| `ethereum` | `eth.blockscout.com` | `ethereum.publicnode.com` | ETH |

## Backend endpoints (`/api/ethscope`)

| Endpoint | Blockscout v2 source |
|---|---|
| `GET /stats` | /stats — price, latest block, avg block time, totals, gas |
| `GET /blocks`, `GET /txs` | /blocks`, `/transactions` — latest lists (60s cache) |
| `GET /block/{number\|hash}` | /blocks/{n} |
| `GET /block/{n}/txs` | /transactions?block_number=` (hash resolved to height first) |
| `GET /tx/{hash}` | /transactions/{hash}` — full detail + embedded token transfers, input UTF-8 decode |
| `GET /tx/{hash}/token-transfers` | /transactions/{hash}/token-transfers |
| `GET /tx/{hash}/logs` | /transactions/{hash}/logs |
| `GET /tx/{hash}/internal` | /transactions/{hash}/internal-transactions |
| `GET /address/{h}` | /addresses/{h}` — balance, ENS, contract flags |
| `GET /address/{h}/txs` | /addresses/{h}/transactions |
| `GET /address/{h}/token-transfers` | /addresses/{h}/token-transfers |
| `GET /address/{h}/internal` | /addresses/{h}/internal-transactions |
| `GET /address/{h}/tokens` | /addresses/{h}/token-balances` + CoinGecko fallback — per-token `usd_price`/`usd_value`, top-level `total_usd` |
| `GET /address/{h}/txcount` | RPC `eth_getTransactionCount` — nonce / outgoing-tx count |
| `GET /token/{h}` | /tokens/{h}` — ERC-20 overview |
| `GET /token/{h}/transfers` | /tokens/{h}/transfers |
| `GET /token/{h}/instances` | /tokens/{h}/instances` — NFT instances (ERC-721/1155) |
| `GET /token/{h}/instance/{id}` | /tokens/{h}/instances/{id}` — image/media/animation URLs, metadata attributes, owner |
| `GET /token/{h}/instance/{id}/transfers` | /tokens/{h}/instances/{id}/transfers |
| `GET /contract/{addr}` | /smart-contracts/{addr}` — source, ABI, compiler, proxy, implementations |
| `POST /contract/{addr}/call` | RPC `eth_call` with a built-in minimal ABI codec (address, uint/int, bool, bytesN, bytes, string, arrays) |
| `POST /contract/{addr}/verify` | Forwards to Blockscout verification (v2 multipart, v1 form fallback) — see note below |
| `GET /charts/transactions`, `/charts/market` | `/stats/charts/transactions`, `/stats/charts/market` (10-min cache) |
| `GET /charts/gas` | Computed from the latest ~50 blocks' `base_fee_per_gas` (no Blockscout gas chart exists) |
| `GET /charts/price?days=30` | CoinGecko `market_chart` for the chain's native coin (10-min cache) |
| `GET /gastracker` | `/stats` gas prices + latest base fee + 20-block base-fee history |
| `GET /pending` | RPC `eth_getBlockByNumber("pending", true)` — sampled, capped at 50 |
| `GET /search?q=` | `/search` — addresses, tokens, txs, blocks, ENS |
| `GET /chains`, `GET /docs` | This chain; full endpoint catalogue |

Responses are translated/paraphrased (wei→ETH, gwei, relative ages, compact address objects) rather than raw Blockscout dumps. List endpoints forward pagination params and return Blockscout's `next_page_params`. Upstream failures return `{"error": ...}` (HTTP 502) — never tracebacks. Hot endpoints (stats, latest blocks/txs) have a 60s in-memory TTL cache.

## Etherscan feature mapping

| Etherscan | EthScope |
|---|---|
| Home + universal search | `#/` — search detects tx hash / address / block number / block hash / ENS; live stats cards; 10s-polling latest blocks & txs |
| Block page | `#/block/{n}` — details, gas bar, rewards, withdrawals + tx list |
| Transaction page | `#/tx/{h}` — status badge, fee breakdown, input hex + UTF-8 decode, token transfers, logs, internal txs |
| Address page | `#/address/{h}` — balance, ENS/contract badges, tabs: Transactions / Token Transfers / Internal / Tokens (USD values + `total_usd`); tx count (nonce) via RPC; curated label badge (mainnet only); "View contract" link for contracts; CSV export on Transactions + Token Transfers tabs (up to 5,000 rows / 25 pages); "💼 Wallet Dashboard" button opens the address in WalletScope |
| Token tracker | `#/token/{h}` — ERC-20 overview + transfers; ERC-721/1155 collections show an Instances grid |
| NFT instance | `#/token/{h}/instance/{id}` — large image/media, attributes table, owner, transfers of that instance |
| Contract page | `#/contract/{addr}` — overview (verified badge, compiler, language, license, proxy + implementation), tabs: Source Code, Read Contract (every view/pure ABI function as a form → `POST /contract/{addr}/call`), Write Contract (injected-wallet `eth_sendTransaction` with a hand-rolled JS ABI encoder + Keccak-256, no dependency; read-only notice without a wallet), Verify Contract (JSON form → backend verify, upstream result shown verbatim) |
| Charts | `#/charts` — daily tx count (Blockscout), native price history (CoinGecko, 7/30/90-day selector), base-fee history (computed); hand-rolled SVG line charts, no dependency |
| Gas tracker | `#/gastracker` — slow/average/fast cards, latest base fee, 20-block base-fee chart, avg block time, EIP-1559 explainer |
| Pending txs | `#/pending` — sampled pending block via public RPC, "Sampled — not the full mempool" badge, 15s auto-refresh |
| API docs | `#/api-docs` — endpoint catalogue from `GET /api/ethscope/docs`, with fair-use note |
| Labels | `static/labels.json` — 14 curated well-known mainnet addresses (12 verified against Blockscout, 2 convention-only: burn + null). Shown as small pills next to addresses — mainnet only, since the same address on another chain is a different account/contract |

## Frontend notes

- Single-chain: the header shows this chain (no switcher); the header scope select jumps to the sibling Scope apps, and Wallet links go to WalletScope.
- Contract writes need no dependency: `static/app.js` contains a pure-JS Keccak-256 and a minimal ABI encoder (address, uint/int, bool, bytesN, bytes, string, static/dynamic arrays). Verified in Node with 17 assertions (see VERIFY.md in the repo root).
- Charts are hand-rolled inline SVG (`svgChart()` in `app.js`) — no build step, no CDN.
- All fetches have a 20s client-side timeout so a stalled upstream can never hang the UI; pages show honest error states on 502/404 upstream failures.

## Notes

- Blockscout v2 has no `GET /blocks/{n}/transactions` on this instance; block txs use `GET /transactions?block_number=`.
- Blockscout v2 here rejects `page_size` (422 "Unexpected field") on `/blocks` and `/tokens/{addr}/instances`; list endpoints use the default 50-item page.
- Contract verification is partial: the v2 multipart endpoint (`POST /smart-contracts/{addr}/verification`) times out from this sandbox, so submissions fall through to the v1 form endpoint (`POST /api?module=contract&action=verifysourcecode`), which answers keyless. Upstream rejections are surfaced verbatim, never faked as success. No real contract was verified during testing.
- Gas history is computed from blocks: Blockscout's `gas-price`, `average-gas-price`, `average-block-time`, `gas-used`, `new-addresses` chart actions all return "Unknown API v2 action" on this instance.
- Pending is sampled: Blockscout has no pending-tx list; the endpoint reads one pending block from the public RPC.
- USD values: per-token `exchange_rate` from Blockscout wins; CoinGecko `simple/token_price` is the fallback, but its keyless tier allows only ONE contract address per request (error 10012), so the backend does one cached call per token, max 5 per request. ERC-721/1155 items get null USD fields. A CoinGecko failure never breaks the endpoint (nulls instead).
- Token balances are slow for token-rich addresses: Blockscout `/token-balances` is one unpaginated download (vitalik's mainnet list: 3.36 MB; 15–60s through the sandbox egress proxy). The address token endpoint therefore uses a 45s upstream timeout; a timeout still surfaces as an honest 502, never a hang.
- Keccak-256 for ABI function selectors is implemented in pure Python in `common/abi.py` (stdlib `hashlib.sha3_256` is NIST SHA3, not Keccak — different padding, wrong selectors). The frontend mirrors it in pure JS, unit-tested byte-exact.
- Sandbox quirk: the environment's `NO_PROXY` contains bracketed IPv6 literals (`[::1]`) that httpx's env-proxy parser rejects — `common/clients.py` strips them at import. The egress proxy itself is still used for outbound calls.
