# EthScope

An Etherscan-style block explorer — blocks, transactions, addresses, tokens, contracts, NFTs, charts, and gas — with a FastAPI backend and a single-page frontend. **Multichain**: Ethereum mainnet, Sepolia, Optimism, Gnosis.

**Data sources (all keyless, no API keys anywhere):** [Blockscout](https://eth.blockscout.com) v2 public API per chain; public JSON-RPC endpoints (`*.publicnode.com`) for pending blocks, nonces, and `eth_call`; CoinGecko keyless API for native-coin price history and token USD prices.

## Run locally

```bash
cd ethscope
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --port 8034
# open http://127.0.0.1:8034/
```

`requirements.txt` pins `fastapi==0.162.0`, `uvicorn==0.54.0`, `httpx==0.28.1`.

## Deploy (Vercel)

Serverless Python, same pattern as the Cryptanalysis service:

- `vercel.json` rewrites every path to `/api/index`
- `api/index.py` re-exports the FastAPI `app` (ASGI)
- The app serves `static/` itself, so the whole site (API + frontend) runs in the one Python function
- `vercel deploy` installs `requirements.txt` automatically

## Layout

```
app.py              FastAPI backend — /api/ethscope/* proxies Blockscout v2 (+ RPC + CoinGecko)
api/index.py        Vercel serverless entrypoint (re-exports app)
static/index.html   App shell
static/app.js       Hash-routed SPA (#/, #/block/{n}, #/tx/{h}, #/address/{h}, #/token/{h})
static/styles.css   Etherscan-like light theme
static/labels.json  Curated well-known mainnet addresses (verified 2026-11-05)
vercel.json         Vercel rewrites
requirements.txt    Pinned deps
VERIFY.md           What was tested and the results
```

## Chains

| Chain | Blockscout host | Public RPC | Native |
|---|---|---|---|
| `ethereum` | `eth.blockscout.com` | `ethereum.publicnode.com` | ETH |
| `sepolia` | `eth-sepolia.blockscout.com` | `ethereum-sepolia.publicnode.com` | ETH |
| `optimism` | `optimism.blockscout.com` (301 → `explorer.optimism.io`) | `optimism.publicnode.com` | ETH |
| `gnosis` | `gnosis.blockscout.com` (301 → `gnosisscan.io`) | `gnosis.publicnode.com` | xDAI |

Every endpoint exists twice: a legacy mainnet alias (`/api/ethscope/stats`, …) and a chain-prefixed form (`/api/ethscope/{chain}/stats`, …). Unknown chain → 404 `{"error": "Unknown chain"}`. `GET /api/ethscope/chains` lists the chains; `GET /api/ethscope/docs` lists every endpoint (generated from one literal list in `app.py`).

Dropped chains (do not re-add without re-probing): Arbitrum (`arbitrum.blockscout.com` connection fails), Base (`base.blockscout.com` HTTP 403), Polygon (`polygon.blockscout.com` HTTP 403 Cloudflare).

## Backend endpoints (`/api/ethscope/*`, `{chain}` = ethereum | sepolia | optimism | gnosis)

| Endpoint | Blockscout v2 source |
|---|---|
| `GET /stats` | `/stats` — price, latest block, avg block time, totals, gas |
| `GET /blocks`, `GET /txs` | `/blocks`, `/transactions` — latest lists (60s cache) |
| `GET /block/{number\|hash}` | `/blocks/{n}` |
| `GET /block/{n}/txs` | `/transactions?block_number=` (hash resolved to height first) |
| `GET /tx/{hash}` | `/transactions/{hash}` — full detail + embedded token transfers, input UTF-8 decode |
| `GET /tx/{hash}/token-transfers` | `/transactions/{hash}/token-transfers` |
| `GET /tx/{hash}/logs` | `/transactions/{hash}/logs` |
| `GET /tx/{hash}/internal` | `/transactions/{hash}/internal-transactions` |
| `GET /address/{h}` | `/addresses/{h}` — balance, ENS, contract flags |
| `GET /address/{h}/txs` | `/addresses/{h}/transactions` |
| `GET /address/{h}/token-transfers` | `/addresses/{h}/token-transfers` |
| `GET /address/{h}/internal` | `/addresses/{h}/internal-transactions` |
| `GET /address/{h}/tokens` | `/addresses/{h}/token-balances` + CoinGecko fallback — per-token `usd_price`/`usd_value`, top-level `total_usd` |
| `GET /address/{h}/txcount` | RPC `eth_getTransactionCount` — nonce / outgoing-tx count |
| `GET /token/{h}` | `/tokens/{h}` — ERC-20 overview |
| `GET /token/{h}/transfers` | `/tokens/{h}/transfers` |
| `GET /token/{h}/instances` | `/tokens/{h}/instances` — NFT instances (ERC-721/1155) |
| `GET /token/{h}/instance/{id}` | `/tokens/{h}/instances/{id}` — image/media/animation URLs, metadata attributes, owner |
| `GET /token/{h}/instance/{id}/transfers` | `/tokens/{h}/instances/{id}/transfers` |
| `GET /contract/{addr}` | `/smart-contracts/{addr}` — source, ABI, compiler, proxy, implementations |
| `POST /contract/{addr}/call` | RPC `eth_call` with a built-in minimal ABI codec (address, uint/int, bool, bytesN, bytes, string, arrays) |
| `POST /contract/{addr}/verify` | Forwards to Blockscout verification (v2 multipart, v1 form fallback) — see note below |
| `GET /charts/transactions`, `/charts/market` | `/stats/charts/transactions`, `/stats/charts/market` (10-min cache) |
| `GET /charts/gas` | Computed from the latest ~50 blocks' `base_fee_per_gas` (no Blockscout gas chart exists) |
| `GET /charts/price?days=30` | CoinGecko `market_chart` for the chain's native coin (10-min cache) |
| `GET /gastracker` | `/stats` gas prices + latest base fee + 20-block base-fee history |
| `GET /pending` | RPC `eth_getBlockByNumber("pending", true)` — sampled, capped at 50 |
| `GET /search?q=` | `/search` — addresses, tokens, txs, blocks, ENS |
| `GET /chains`, `GET /docs` | Chain list; full endpoint catalogue |

Responses are translated/paraphrased (wei→ETH, gwei, relative ages, compact address objects) rather than raw Blockscout dumps. List endpoints forward pagination params and return Blockscout's `next_page_params`. Upstream failures return `{"error": ...}` (HTTP 502) — never tracebacks. Hot endpoints (stats, latest blocks/txs) have a 60s in-memory TTL cache.

## Etherscan feature mapping

| Etherscan | EthScope |
|---|---|
| Home + universal search | `#/` — search detects tx hash / address / block number / block hash / ENS; live stats cards; 10s-polling latest blocks & txs |
| Block page | `#/block/{n}` — details, gas bar, rewards, withdrawals + tx list |
| Transaction page | `#/tx/{h}` — status badge, fee breakdown, input hex + UTF-8 decode, token transfers, logs, internal txs |
| Address page | `#/address/{h}` — balance, ENS/contract badges, tabs: Transactions / Token Transfers / Internal / Tokens (USD values + `total_usd`); tx count (nonce) via RPC; curated label badge; "View contract" link for contracts; CSV export on Transactions + Token Transfers tabs (up to 5,000 rows / 25 pages) |
| Token tracker | `#/token/{h}` — ERC-20 overview + transfers; ERC-721/1155 collections show an Instances grid |
| NFT instance | `#/token/{h}/instance/{id}` — large image/media, attributes table, owner, transfers of that instance |
| Contract page | `#/contract/{addr}` — overview (verified badge, compiler, language, license, proxy + implementation), tabs: Source Code (full source + additional sources), Read Contract (every view/pure ABI function as a form → `POST /contract/{addr}/call`), Write Contract (injected-wallet `eth_sendTransaction` with a hand-rolled JS ABI encoder + Keccak-256, no dependency; read-only notice without a wallet), Verify Contract (JSON form → backend verify, upstream result shown verbatim) |
| Charts | `#/charts` — daily tx count (Blockscout), native price history (CoinGecko, 7/30/90-day selector), base-fee history (computed); hand-rolled SVG line charts, no dependency |
| Gas tracker | `#/gastracker` — slow/average/fast cards, latest base fee, 20-block base-fee chart, avg block time, EIP-1559 explainer |
| Pending txs | `#/pending` — sampled pending block via public RPC, "Sampled — not the full mempool" badge, 15s auto-refresh, per-chain |
| API docs | `#/api-docs` — endpoint catalogue from `GET /api/ethscope/docs` (deduped, grouped), with fair-use note |
| Multichain | Header chain switcher (Ethereum / Sepolia / Optimism / Gnosis, from `GET /api/ethscope/chains`, persisted in localStorage); every API call is chain-prefixed; unknown/unreachable chain surfaces an honest in-UI error, never a hang |
| Labels | `static/labels.json` — 14 curated well-known mainnet addresses (12 verified against Blockscout, 2 convention-only: burn + null). Shown as small pills next to addresses (address page, tx from/to, block miner, token page) — mainnet only, since the same address on another chain is a different account/contract |

## Not included (roadmap)

- ENS name registration pages
- API keys for power users
- Arbitrum / Base / Polygon chains (blocked upstream — see Chains above)

## Frontend notes (2026-11-05)

- Chain state lives in `localStorage` (`ethscope-chain`); the switcher is populated from `GET /api/ethscope/chains` with a built-in 4-chain fallback if that fetch fails (the header pill turns amber). Every data fetch goes through `get()`, which prefixes the selected chain; the only unprefixed fetches are `GET /chains`, `GET /docs` (global endpoints) and `/labels.json` (static file).
- Contract writes need no dependency: `static/app.js` contains a pure-JS Keccak-256 (64-bit lanes as `[hi, lo]` pairs; verified against the backend's Python Keccak — empty → `c5d24601…`, `decimals()` → `313ce567`, `transfer(address,uint256)` → `a9059cbb`) and a minimal ABI encoder (address, uint/int, bool, bytesN, bytes, string, static/dynamic arrays). Verified in Node with 17 assertions (see VERIFY.md).
- Contract verification posts **JSON** to the backend (`POST /contract/{addr}/verify`), not multipart: the backend route parses JSON and forwards multipart to Blockscout itself. The upstream result is rendered verbatim.
- Charts are hand-rolled inline SVG (`svgChart()` in `app.js`) — no build step, no CDN.
- Labels are mainnet-only by design: the same address on Sepolia/Optimism/Gnosis is a different account/contract, so pills are suppressed off mainnet.
- All fetches have a 20s client-side timeout so a stalled upstream can never hang the UI; pages show honest error states on 502/404 upstream failures.

## Notes

- Blockscout v2 has no `GET /blocks/{n}/transactions` on this instance; block txs use `GET /transactions?block_number=`.
- Blockscout v2 here rejects `page_size` (422 "Unexpected field") on `/blocks` and `/tokens/{addr}/instances`; list endpoints use the default 50-item page.
- `optimism.blockscout.com` 301-redirects to `explorer.optimism.io` and `gnosis.blockscout.com` to `gnosisscan.io`; the backend follows redirects (httpx `follow_redirects=True`), added 2026-11-05 after both chains first returned "invalid JSON".
- Contract verification is partial: the v2 multipart endpoint (`POST /smart-contracts/{addr}/verification`) times out from this sandbox, so submissions fall through to the v1 form endpoint (`POST /api?module=contract&action=verifysourcecode`), which answers keyless — a dummy submission on 2026-11-05 got the real Blockscout error "Address is not a smart-contract", proving the forwarding path works. Upstream rejections are surfaced verbatim, never faked as success. No real contract was verified during testing.
- Gas history is computed from blocks: Blockscout's `gas-price`, `average-gas-price`, `average-block-time`, `gas-used`, `new-addresses` chart actions all return "Unknown API v2 action" on this instance.
- Pending is sampled: Blockscout has no pending-tx list (`/transactions?type=pending` is invalid); the endpoint reads one pending block from the public RPC.
- USD values: per-token `exchange_rate` from Blockscout wins; CoinGecko `simple/token_price` is the fallback, but its keyless tier allows only ONE contract address per request (error 10012), so the backend does one cached call per token, max 5 per request. ERC-721/1155 items get null USD fields. A CoinGecko failure never breaks the endpoint (nulls instead).
- Keccak-256 for ABI function selectors is implemented in pure Python in `app.py` (stdlib `hashlib.sha3_256` is NIST SHA3, not Keccak — different padding, wrong selectors).
- Sandbox quirk: the environment's `NO_PROXY` contains bracketed IPv6 literals (`[::1]`) that httpx's env-proxy parser rejects — `app.py` strips them at import. The egress proxy itself is still used for outbound calls.
