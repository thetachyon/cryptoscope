# EthScope

An Etherscan-style block explorer — blocks, transactions, addresses, tokens, contracts, NFTs, charts, and gas — with a FastAPI backend and a single-page frontend. **Multichain**: Ethereum mainnet, Sepolia, Optimism, Gnosis. **Wallet dashboard** (`#/wallet/{address}`): one address across all four chains — native + ERC-20 holdings with USD values, portfolio history, estimated realized PnL, NFT gallery, unified activity feed.

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
chains.py           Chain registry (CHAINS + DEFAULT_CHAIN) — hoist target for cryptoscope/common/
clients.py          Shared httpx clients, cache, UpstreamError, NO_PROXY fix — hoist target
pricing.py          USD pricing (Blockscout exchange_rate first, CoinGecko fallback) + daily price series — hoist target
formatting.py       wei/ETH/token math, ages, addr/tx/block/transfer shorteners
wallet.py           Wallet dashboard backend (aggregate/tokens/history/pnl/nfts/activity) + per-chain EVM adapter — hoist target
api/index.py        Vercel serverless entrypoint (re-exports app)
static/index.html   App shell
static/app.js       Hash-routed SPA (#/, #/block/{n}, #/tx/{h}, #/address/{h}, #/token/{h}, #/wallet/{address})
static/styles.css   Etherscan-like light theme
static/labels.json  Curated well-known mainnet addresses (verified 2026-11-05)
vercel.json         Vercel rewrites
requirements.txt    Pinned deps
VERIFY.md           What was tested and the results
```

Cross-chain plumbing (chain registry, per-chain clients, USD pricing, fan-out aggregation) lives in the separated modules above so it can be hoisted into `cryptoscope/common/` as a shared vendored package; the wallet dashboard's backend (`wallet.py`) and frontend route are self-contained so the dashboard can move to its own `cryptoscope/walletscope/` app. Wallet aggregation talks to chains through a small adapter interface (`EVMAdapter` in `wallet.py`: address / token_balances / nfts / transactions / token_transfers) so a future non-EVM chain (e.g. Solana) can plug in as another adapter without rewriting the aggregation.

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
| `GET /wallet/{address}` | Fast aggregate: per-chain native balance + USD + ENS, portfolio native total, value-weighted 24h change (native only at this stage). Token holdings are NOT fetched here (see below) |
| `GET /wallet/{address}/tokens[?chain=]` | Token holdings per chain (top 50 by USD value, totals, counts, NFT count). Slow for token-rich addresses — Blockscout `/token-balances` is one giant unpaginated download. Optional `?chain=` loads one chain only |
| `GET /wallet/{address}/history?days=30` | Portfolio value estimate: today's balances (native + top 5 tokens by value) × each day's historical price, 7/30/90 days. **Estimate, not true history** — see Wallet dashboard below |
| `GET /wallet/{address}/pnl` | Estimated realized PnL: FIFO over the latest ~50 token transfers/chain, price-at-tx-time via CoinGecko daily closes (90d window). **Estimate with stated coverage** — see below |
| `GET /wallet/{address}/nfts` | NFT holdings across all chains (first 50/chain) with image, collection, token id |
| `GET /wallet/{address}/activity[?chain=&…]` | Recent txs across all chains, merged + time-sorted (top 50), with per-chain `next_page_params`. With `?chain=<id>` + cursor params returns one further shaped page of that chain (Load-more) |

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
| Wallet dashboard | `#/wallet/{address}` (+ ENS via the search path) — portfolio total + 24h change, per-chain breakdown (values + share %), holdings table (sortable by value, dust &lt;$1 hidden by default), portfolio history chart (7/30/90d), estimated realized PnL, NFT gallery, unified activity feed with per-chain Load-more. Header nav "Wallet", "💼 Wallet Dashboard" button on every address page, "💼 Wallet" link on address search suggestions. Chain badges on every cross-chain row; tx/token/instance links switch the selected chain before routing |

## Wallet dashboard — method and honest limits

- **Two-tier loading.** The aggregate (`GET /wallet/{address}`) fetches native balances only — Blockscout's `/token-balances` is one giant unpaginated download (vitalik's mainnet list is 3.36 MB / 15–60s through a proxy), so holdings load progressively per chain via `/tokens?chain=`. The hero total and the combined 24h change recompute as each chain's holdings arrive; a chain that times out (45s guard) shows an error section, never fails the dashboard.
- **USD pricing.** Blockscout token `exchange_rate` first, CoinGecko `simple/token_price` fallback (one contract per request, max 5 lookups per tokens call — top-3 priced holdings get their 24h change first, remaining budget prices unpriced items). Unpriced tokens have no market price and are excluded from holdings totals. Sepolia ETH has no market value (Blockscout reports no price), so Sepolia balances are excluded from USD totals everywhere, including history.
- **History is an estimate, labeled as such in the UI.** Today's balances (native coins on priced chains + top 5 tokens by current value) multiplied by each day's historical price (CoinGecko daily closes). It assumes today's balances held throughout the window — deposits, withdrawals and trades are ignored — so it is NOT true historical PnL. 45 priced holdings are excluded from the chart (top 5 only) for vitalik.
- **PnL is an estimate with stated coverage.** FIFO over the latest ~50 token transfers per chain (90-day window): incoming transfers are acquisitions at that day's CoinGecko close, outgoing transfers are disposals matched against lots; self-transfers ignored. Only tokens with CoinGecko price history can be priced — transfers of unlisted/spam tokens are counted as unpriced and excluded, and outgoing transfers with no cost basis in the window are counted as unmatched and excluded. The UI shows priced/unpriced/unmatched counts next to every figure. For vitalik the window is dominated by unlisted tokens (111 unpriced of 111), so the estimate is $0 with the gap stated; for the Bitfinex hot wallet the engine prices ENA/UNI/XAUT/USDT/USDF and matches $19.99M of USDT disposals to cost at ~$1.00 exactly (see VERIFY.md).
- **NFTs** come from Blockscout `/addresses/{h}/nft`, first 50 per chain; cards link to the existing `#/token/{addr}/instance/{id}` pages (chain switched automatically).
- **Activity** merges the first page of txs per chain, sorted by time; Load-more fetches further pages per chain (`/activity?chain=<id>&<cursor>`) and merges client-side.

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
- All fetches have a 20s client-side timeout so a stalled upstream can never hang the UI; pages show honest error states on 502/404 upstream failures. Wallet calls use a 60s timeout (`getWallet`/`getChain`) because mainnet token lists are multi-MB.
- Wallet cross-chain links carry `data-chain`; a delegated click handler switches the selected chain (header select + netpill) before the hashchange routes, so a tx/token/instance on another chain renders on that chain.

## Notes

- Blockscout v2 has no `GET /blocks/{n}/transactions` on this instance; block txs use `GET /transactions?block_number=`.
- Blockscout v2 here rejects `page_size` (422 "Unexpected field") on `/blocks` and `/tokens/{addr}/instances`; list endpoints use the default 50-item page.
- `optimism.blockscout.com` 301-redirects to `explorer.optimism.io` and `gnosis.blockscout.com` to `gnosisscan.io`; the backend follows redirects (httpx `follow_redirects=True`), added 2026-11-05 after both chains first returned "invalid JSON".
- Contract verification is partial: the v2 multipart endpoint (`POST /smart-contracts/{addr}/verification`) times out from this sandbox, so submissions fall through to the v1 form endpoint (`POST /api?module=contract&action=verifysourcecode`), which answers keyless — a dummy submission on 2026-11-05 got the real Blockscout error "Address is not a smart-contract", proving the forwarding path works. Upstream rejections are surfaced verbatim, never faked as success. No real contract was verified during testing.
- Gas history is computed from blocks: Blockscout's `gas-price`, `average-gas-price`, `average-block-time`, `gas-used`, `new-addresses` chart actions all return "Unknown API v2 action" on this instance.
- Pending is sampled: Blockscout has no pending-tx list (`/transactions?type=pending` is invalid); the endpoint reads one pending block from the public RPC.
- USD values: per-token `exchange_rate` from Blockscout wins; CoinGecko `simple/token_price` is the fallback, but its keyless tier allows only ONE contract address per request (error 10012), so the backend does one cached call per token, max 5 per request. ERC-721/1155 items get null USD fields. A CoinGecko failure never breaks the endpoint (nulls instead).
- Token balances are slow for token-rich addresses: Blockscout `/token-balances` is one unpaginated download (vitalik's mainnet list: 3.36 MB; 15–60s through the sandbox egress proxy, which varies wildly). The wallet and address token endpoints therefore use a 45s upstream timeout (raised from the 10s default after vitalik's address Tokens tab 502'd twice); a timeout still surfaces as an honest per-chain error section / 502, never a hang. The wallet's per-chain token sections are cached 60s so the history endpoint reuses the frontend's downloads.
- Blockscout token objects expose the token address as `address_hash`, not `address` — the wallet PnL, NFT and token-transfer code reads `address_hash` first. (The first wallet PnL build read `address` and priced nothing; fixed 2026-11-05, see VERIFY.md.)
- Keccak-256 for ABI function selectors is implemented in pure Python in `app.py` (stdlib `hashlib.sha3_256` is NIST SHA3, not Keccak — different padding, wrong selectors).
- Sandbox quirk: the environment's `NO_PROXY` contains bracketed IPv6 literals (`[::1]`) that httpx's env-proxy parser rejects — `app.py` strips them at import. The egress proxy itself is still used for outbound calls.
