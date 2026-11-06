# WalletScope

Cross-chain wallet dashboard — one address across Ethereum, Sepolia, Optimism, Gnosis, and Solana: native + token holdings with USD values, portfolio history, estimated realized PnL, NFT gallery, a unified activity feed, and a finance-grade accounting export (USD at time of transaction + FIFO cost basis, QuickBooks/Xero journal columns). Bitcoin addresses get an accounting export too (net-flow BTC rows). No API keys anywhere.

**Data sources (all keyless):** [Blockscout](https://eth.blockscout.com) v2 public API per EVM chain; public JSON-RPC endpoints (`*.publicnode.com`); Solana public RPC (`solana.publicnode.com` for SOL balances, `api.mainnet-beta.solana.com` for SPL token accounts); DexScreener + CoinGecko keyless APIs for Solana token prices, symbols, and daily closes.

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
| `GET /wallet/{address}` | Fast aggregate: per-chain native balance + USD + ENS, portfolio native total, value-weighted 24h change (native only at this stage). Token holdings are NOT fetched here (see below). Accepts an EVM address (0x…) or a Solana base58 address |
| `GET /wallet/{address}/tokens[?chain=]` | Token holdings per chain (top 50 by USD value, totals, counts, NFT count). Slow for token-rich addresses — Blockscout `/token-balances` is one giant unpaginated download. Optional `?chain=` loads one chain only (`?chain=solana` for SPL holdings) |
| `GET /wallet/{address}/history?days=30` | Portfolio value estimate: today's balances (native + top 5 tokens by value) × each day's historical price, 7/30/90 days. **Estimate, not true history** — see below. Solana native + listed SPL tokens are priced in via CoinGecko daily closes |
| `GET /wallet/{address}/pnl` | Estimated realized PnL: FIFO over the latest ~50 token transfers/chain, price-at-tx-time via CoinGecko daily closes (90d window). **Estimate with stated coverage** — see below. EVM chains only; Solana transfers are not included |
| `GET /wallet/{address}/nfts` | NFT holdings across EVM chains (first 50/chain) with image, collection, token id. Solana NFTs are counted in the per-chain breakdown but not shown in this gallery yet |
| `GET /wallet/{address}/activity[?chain=&…]` | Recent txs across EVM chains, merged + time-sorted (top 50), with per-chain `next_page_params`. With `?chain=<id>` + cursor params returns one further shaped page of that chain (Load-more). Solana activity is not wired into this feed yet |
| `GET /wallet/{address}/incoming` | **Invoice reconciliation feed (phase 2, Q3):** incoming transfers only — native + ERC-20 on the EVM chains, SOL + SPL on Solana, newest first. Takes an address only and accepts NO invoice data; matching (amount + due-date window) runs entirely in the browser at `#/invoices`, where invoices live in localStorage only. Sampled window: first page per EVM chain (50 native txs + 50 token transfers per chain), latest 25 Solana signatures; SPL rows carry no symbol (match by mint address); Bitcoin not included |
| `GET /accounting/export?address=&format=csv` | **Accounting export (phase 2, Q2):** finance-grade cross-chain CSV of every send/receive — date, tx hash, chain, coin, amount, USD value at time of transaction, fee in USD, counterparty, type (send/receive), FIFO cost basis + realized gain per send, and QuickBooks/Xero general-journal columns (debit/credit accounts + legs, memo with hash). `format=json` returns the same rows plus summary, per-coin FIFO lots remaining, the full column mapping and honest-limit notes. All coins (EVM native + ERC-20, SOL + SPL, BTC for Bitcoin addresses). One CoinGecko daily-close series per coin; live-price fallbacks are labelled `estimate-current`; unpriced rows keep null USD fields. Sampled window: first page per chain (50 native txs + 50 token transfers per EVM chain, 10 Solana signatures, 25 Bitcoin txs). Token-transfer rows carry no fee (paid in native coin on the parent tx). This replaces the single-chain explorer CSV (no USD values) for bookkeeping |
| `GET /search?q=` | Ethereum-mainnet search (ENS / address resolution for the dashboard) |
| `GET /chains`, `GET /docs` | All five chains; full endpoint catalogue |

Upstream failures return `{"error": ...}` (HTTP 502) — never tracebacks. A slow/failed chain returns an `error` for that chain only, never fails the whole dashboard.

## Method and honest limits

- **Two-tier loading.** The aggregate (`GET /wallet/{address}`) fetches native balances only — Blockscout's `/token-balances` is one giant unpaginated download (vitalik's mainnet list is 3.36 MB / 15–60s through a proxy), so holdings load progressively per chain via `/tokens?chain=`. The hero total and the combined 24h change recompute as each chain's holdings arrive; a chain that times out (45s guard) shows an error section, never fails the dashboard.
- **USD pricing.** Blockscout token `exchange_rate` first, CoinGecko `simple/token_price` fallback (one contract per request, max 5 lookups per tokens call — top-3 priced holdings get their 24h change first, remaining budget prices unpriced items). Unpriced tokens have no market price and are excluded from holdings totals. Sepolia ETH has no market value (Blockscout reports no price), so Sepolia balances are excluded from USD totals everywhere, including history.
- **History is an estimate, labeled as such in the UI.** Today's balances (native coins on priced chains + top 5 tokens by current value) multiplied by each day's historical price (CoinGecko daily closes). It assumes today's balances held throughout the window — deposits, withdrawals and trades are ignored — so it is NOT true historical PnL.
- **PnL is an estimate with stated coverage.** FIFO over the latest ~50 token transfers per chain (90-day window): incoming transfers are acquisitions at that day's CoinGecko close, outgoing transfers are disposals matched against lots; self-transfers ignored. Only tokens with CoinGecko price history can be priced — transfers of unlisted/spam tokens are counted as unpriced and excluded, and outgoing transfers with no cost basis in the window are counted as unmatched and excluded. The UI shows priced/unpriced/unmatched counts next to every figure. The FIFO engine (`common/wallet.py` `_fifo_run`) is shared with the accounting export, which FIFO's every coin (native coins included) over the export's sampled window.
- **Accounting export is an estimate with stated coverage (phase 2, Q2).** Same FIFO engine as PnL, applied per (chain, coin) to every row in the sampled window. Journal mapping: receive → Debit `Crypto Assets:{COIN}` / Credit `Other Income:Crypto Received` (= amount_usd); send → Debit `Cost of Goods Sold:Crypto Disposed` / Credit `Crypto Assets:{COIN}` (= cost_basis_usd); realized gain is reported per send row for separate posting. Fees are reported only where the address paid them (EVM/Solana sender, BTC send rows — the whole tx fee); receives carry no fee. Bitcoin rows are net flow per tx (change outputs cannot be identified). Sepolia ETH has no market value, so its USD fields are always null. Not tax advice.
- **NFTs** come from Blockscout `/addresses/{h}/nft`, first 50 per EVM chain; cards link out to the instance pages on the chain's Scope app. Solana NFT-like accounts (decimals 0, amount 1) are counted in the per-chain breakdown but not shown in the gallery.
- **Activity** merges the first page of txs per EVM chain, sorted by time; Load-more fetches further pages per chain (`/activity?chain=<id>&<cursor>`) and merges client-side. Solana activity is not wired into this feed yet.
- **Invoice reconciliation (phase 2, Q3, `#/invoices`).** Invoices (amount, coin, due date, reference) are entered in the browser and stored in localStorage only — nothing leaves the browser. The page's only request is `GET /wallet/{address}/incoming`, which carries no invoice data. Matching is local: a transfer counts when the coin matches (symbol, or contract/mint address) and its date falls within due date ± a configurable window (default 7 days); each transfer is assigned to at most one invoice (oldest due date first) and transfers are added oldest-first only until the invoiced amount is reached, so one invoice never swallows payments meant for another. Statuses: matched (within a configurable tolerance, default 0.5%), partial, overpaid, unmatched; unmatched incoming transfers are listed as candidates. Works for any coin the wallet aggregates. Honest limits: sampled window (see endpoint row above); matching is by coin + date window only and cannot prove a transfer was meant for an invoice; SPL rows have no symbol, so match SPL invoices by mint address; Bitcoin is not included.
- **Solana holdings.** SOL balance via `solana.publicnode.com` `getBalance`; SPL token accounts via `api.mainnet-beta.solana.com` `getProgramAccounts` on the classic SPL Token program only (Token-2022 accounts are rejected by that RPC — not covered). USD prices: DexScreener per mint (most-liquid pair, incl. 24h change) first, CoinGecko `simple/token_price/solana` for the rest (max 5 mints, largest balances first — the free tier allows one mint per request); symbols from DexScreener, else the CoinGecko contract endpoint, else the truncated mint. Unpriced holdings are counted but excluded from totals. Jupiter's APIs are unreachable from the sandbox and are not used. Solana token links go to Solscan until SolScope exists.
- **Cross-address behaviour.** An EVM address shows zero balances (no errors) on the Solana section, and a Solana address shows zero balances (no errors) on the EVM sections — each side treats the other side's address format as "unseen".

## Frontend notes

- The dashboard is the whole app: `#/` is the wallet search, `#/wallet/{address}` (or an ENS name) is the dashboard. Explorer nav links are hidden; the header scope select jumps to the chain explorer apps.
- Tx/token/NFT links on the dashboard are absolute links into the chain's Scope app (e.g. a mainnet tx opens on EthScope).
- All fetches have a 20s client-side timeout; wallet calls use 60s because mainnet token lists are multi-MB. Pages show honest error states on 502/404 upstream failures.

## Notes

- Blockscout token objects expose the token address as `address_hash`, not `address` — the wallet PnL, NFT and token-transfer code reads `address_hash` first.
- Sandbox quirk: the environment's `NO_PROXY` contains bracketed IPv6 literals (`[::1]`) that httpx's env-proxy parser rejects — `common/clients.py` strips them at import. The egress proxy itself is still used for outbound calls.
- No BTC in the dashboard holdings/activity yet (Bitcoin addresses do get an accounting export — net-flow BTC rows — via `GET /accounting/export`). Solana holdings are in this dashboard (see above); a full SolScope comes next, then BitScope (same wallet-first pattern).
