# VERIFY — cryptoscope split + hoist (2026-11-05)

The monorepo was split from a single multichain `ethscope/` app into five
thin apps over a shared `common/` package (see [README.md](./README.md)).
All checks below were run locally against live mainnet/testnet data
(uvicorn, one port per app). Nothing was pushed or deployed in this pass.

## What was verified

### Per-app boot + smoke (all 200 unless noted)

| App (port) | Homepage | Stats (latest block) | Block | Tx | Address | Chains | Docs |
|---|---|---|---|---|---|---|---|
| ethscope (8041) | 200 | `ethereum`, 26,351,552 | 26,351,859 | `0x292330d77255afcd…` | `0x4838B106FC…` | single: `[ethereum]` | 32 entries, all `/api/ethscope/…`, no wallet entries |
| sepoliascope (8042) | 200 | `sepolia`, 12,072,439 | 12,072,668 | `0x73d631e6ced9c9da…` | `0xaE2D1E0829…` | single: `[sepolia]` | 32 entries |
| optimismscope (8043) | 200 | `optimism`, 159,160,314 | 159,160,883 | `0x976bb1fcbb6680ed…` | `0xD2954a6587…` | single: `[optimism]` | 32 entries |
| gnosisscope (8044) | 200 | `gnosis`, 49,129,489 | 49,129,954 | `0x2693b792737b465b…` | `0x99609CA33a…` | single: `[gnosis]` | 32 entries |
| walletscope (8045) | 200 | n/a (wallet app) | n/a | n/a | n/a | all 4 chains | wallet-only: `/wallet/…`, `/chains`, `/docs`, `/search` |

Each scope's block/tx/address above was fetched live (block from `/blocks`,
tx from `/txs`, address = that tx's `from`) and returned 200 with matching
ids. `/config.js` and `/app.js` serve 200 in every app.

### Explorer core survived the move (ethscope, port 8041)

- `POST /api/ethscope/contract/0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48/call`
  `decimals()` → `200`, values `['6']` (Keccak/ABI codec now in `common/abi.py`).
- `GET /api/ethscope/charts/transactions` → 200, 31 points.
- `GET /api/ethscope/gastracker` → 200, slow 0.25 / average 0.52 / fast 14.8 gwei.
- `GET /api/ethscope/pending` → 200, `sampled: true`, 50 txs.

### Wallet survived the move (walletscope, port 8045, vitalik.eth)

- `GET /api/walletscope/wallet/0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045` → 200,
  all four chain sections present, `total_native_usd` 13,176.76.
- `GET …/wallet/…/history?days=7` → 200, 8 points.
- `GET …/wallet/…/nfts` → 200, 200 items, per-chain breakdown present.
- `GET …/wallet/…/tokens?chain=sepolia` → 200, 876 tokens, 0 priced
  (Sepolia test tokens have no USD value), `tokens_total_usd` 0.0.
- `GET …/wallet/…/pnl` → 200, rows present.
- `GET /api/walletscope/search?q=vitalik.eth` → 200, resolves to the address.

### Deliberate breaking changes (verified)

- Old chain-prefixed path `GET /api/ethscope/ethereum/stats` → **404**.
- Wallet endpoints on ethscope (`GET /api/ethscope/wallet/…`) → **404**
  (they live in walletscope now).
- The old legacy mainnet aliases (`/api/ethscope/stats`, …) are gone;
  each app serves its own `/api/<scope>/…` prefix only.

### Static + sync checks

- `node --check` passes on all five vendored `static/app.js` files.
- `python3 scripts/sync-common.py check` → "in sync: 5 apps match canonical
  common/ + frontend/".
- No app imports anything from a sibling directory at runtime: the only
  cross-file import outside `common.*` is `from app import app` in each
  `api/index.py`, which is that app's own Vercel entrypoint importing its
  own `app.py` (intended, not a sibling import).

## What did not survive / known limits

- **Nothing was lost functionally**: every endpoint the old app had exists
  in exactly one new app (explorer endpoints under `/api/<scope>/…`,
  wallet endpoints under `/api/walletscope/wallet/…`). The only removals
  are the ones listed above as deliberate.
- **Vercel `buildCommand` is untested live** (deploy is blocked on the
  403 team-scope issue, see repo README). The command
  `python3 ../scripts/sync-common.py sync` was verified to run from an app
  directory locally; if it ever fails in Vercel's build, delete the
  `buildCommand` — the committed vendored copies keep deploys working and
  `sync-common.py check` keeps them honest.
- **Sibling scope URLs are expected domains** (`https://<scope>.vercel.app`)
  until the Vercel projects exist; cross-links and wallet links use them
  and will 404 until the projects are created.
- **Labels are mainnet-only** (unchanged): `labels.json` is vendored into
  every app but pills render only for Ethereum addresses.

## Solana holdings in WalletScope (2026-11-05)

### Sources probed from this sandbox (curl, before building)
- `solana.publicnode.com` getBalance: works, ~0.5s. Indexed methods
  (`getTokenAccountsByOwner`, `getProgramAccounts`) blocked there:
  "Indexed requests require a personal token".
- `api.mainnet-beta.solana.com` getProgramAccounts (classic SPL Token
  program, jsonParsed, dataSize 165, memcmp owner @ offset 32): works,
  ~1-2s. `getTokenAccountsByOwner` on the same host returns a persistent
  "Parse error" (-32700); `getSignaturesForAddress` works.
  Token-2022 getProgramAccounts is rejected ("WrongSize") — not covered.
- Jupiter APIs (`api.jup.ag`, `token.jup.ag`): unreachable from the
  sandbox (connection fails) — not used.
- CoinGecko keyless: SOL price + SPL `simple/token_price/solana` work,
  but the free tier allows ONE mint per request (error 10012 on a batch).
- DexScreener `latest/dex/tokens/{mint}`: works, returns symbol/name/
  priceUsd + 24h change for tokens with a DEX pair.
- Blockchair Solana API: sandbox egress IP is blacklisted — not used.
- Ankr public Solana RPC: requires an API key — not used. DRPC /
  OnFinality public endpoints: time out from the sandbox — not used.

### What was built
- `common/solana.py`: `is_solana_address` (pure-Python base58), SOL
  balance (publicnode, mainnet-beta fallback), SPL accounts
  (mainnet-beta getProgramAccounts, one retry on 429), SOL quote
  (CoinGecko), SPL prices (DexScreener most-liquid pair first, then
  CoinGecko for the rest, max 5, largest balances first; symbols from
  DexScreener, else CoinGecko contract endpoint, else truncated mint).
- `common/wallet.py` now aggregates over `WALLET_CHAINS` (EVM + Solana);
  PnL / NFT gallery / activity stay EVM-only. A Solana address shows
  zero balances (no errors) on EVM sections and vice versa — a 422 from
  Blockscout on a base58 address is swallowed the same way a 404 is.
- `common/factory.py` wallet endpoints accept EVM (0x) or Solana
  (base58) addresses; `/chains` lists all five chains.
- Frontend: `WALLET_CHAINS` + Solana, Solana addresses resolve without
  an ENS lookup, Solana token links go to Solscan (until SolScope),
  activity heading is now "Activity — EVM Chains".

### Verification (uvicorn :8040, live)
- `vines1vzrYbzLMRdu58ou5XTby4qAqVRLmqo36NKPTg`: aggregate $3.56
  (0.039869364 SOL @ $89.03, 24h -6.55%), no errors; tokens: 6 holdings,
  3 priced ($0.0028 total — SMOG $0.0015, SI $0.0009, unlabelled
  55USDUiv… $0.0004), nft_count 1; history 30d: 30 points $4.63 → $3.56.
- `E16prLnWTwfLUYgXRTELYgw4u8QUnN9CAcHceLrDTjN1`: tokens $53.08 —
  JitoSOL 0.251436 × $116.76 = $29.36 (24h -6.68%), PSOL 0.240585 ×
  $98.62 = $23.73 (24h -6.51%); third holding (12.7M units of an
  unlisted pump token) unpriced; aggregate native $0.56, no errors.
- EVM regression: vitalik.eth aggregate $13,300.05, 24h -8.53%, no
  errors; Solana section zero with no error; `tokens?chain=solana`
  returns an empty section, no error.
- Invalid address → 400 with a clear message. `/chains` lists all five
  chains; `/docs` lists solana. PnL/NFTs/activity for a Solana address
  return empty EVM results, no errors. Home + app.js 200.
- `node --check` passes on the frontend app.js; `scripts/sync-common.py
  check` → "in sync: 5 apps match canonical common/ + frontend/".

### Honest limits
- Classic SPL Token program only — Token-2022 holdings are invisible
  (mainnet-beta rejects the query). NFT-like accounts (decimals 0,
  amount 1) are counted, not listed as fungible holdings, and are not
  shown in the NFT gallery.
- Prices: only tokens with a DexScreener pair or a CoinGecko listing
  get a USD value (vines: 3 of 6 priced); CoinGecko fallback is capped
  at 5 mints per tokens call, largest balances first.
- PnL, NFT gallery and activity are EVM-only; the history chart prices
  Solana native + listed SPL tokens via CoinGecko daily closes where
  available (unlisted SPL tokens contribute nothing to the chart).
- Live browser rendering of `#/wallet/<solana-address>` was not
  possible here — JS is syntax-checked and logic-reviewed only.

## SolScope (2026-11-05)

### Sources probed from this sandbox (curl, before building)
- `solana.publicnode.com`: getSlot, getRecentPerformanceSamples,
  getBalance work (~0.5s). getSupply is indexed there ("Indexed requests
  require a personal token"). Indexed methods are blocked.
- `api.mainnet-beta.solana.com`: getSupply, getBlock (needs
  maxSupportedTransactionVersion 1), getTransaction (jsonParsed),
  getSignaturesForAddress, getAccountInfo (jsonParsed), getTokenSupply,
  getSlotLeader, getEpochInfo all work. getTokenLargestAccounts is
  persistently rate-limited (429 direct, 403 via the app) — see limits.
- Jupiter APIs unreachable from the sandbox — not used anywhere.

### What was built
- `common/solana_explorer.py`: RPC routing (fast methods publicnode
  first, indexed methods mainnet-beta first; 429/403 retried once per
  endpoint after 1.5s), stats/slots/slot/tx/address/signatures/token/
  transfers/program/search + docs payload; parsed-instruction decoding
  (System/Token/Associated/Memo/Stake/Vote/BPF/Compute-Budget/lookup-table
  names) + account + SPL token balance changes.
- `common/factory.py`: `create_solana_app` — `/api/solscope/*` (12
  endpoints + /chains + /docs).
- `solscope/`: thin app (config + entrypoint + vercel.json + api/index.py)
  + `solscope/frontend/` overlay SPA (665 lines, own views: home, slot,
  tx, address, token, program, api-docs; light theme, hash routing, 10s
  home polling). `scripts/sync-common.py` gained per-app frontend
  overlays (solscope's files replace the canonical ones in static/).
- SolScope added to every app's SIBLINGS (cross-links).

### Bugs found & fixed
1. DexScreener priced the wrong token: for mints that are the QUOTE
   token in a pair (e.g. USDC in PUMP/USDC), baseToken/priceUsd describe
   the other token — USDC showed as "PUMP $0.003876". Fixed in
   `common/solana.py`: only pairs where baseToken.address == mint are
   used; others fall through to CoinGecko. USDC now $1.0013. (Same bug
   affected the wallet dashboard's SPL pricing — fixed there too.)
2. My test JitoSOL mint was mistyped (decoded to 31 bytes, not 32);
   correct mint is `J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn`
   (taken from the wallet's own verified holdings).

### Verified live (uvicorn :8041, real mainnet data)
- `/stats`: slot 465,895,740, epoch 1078, TPS 2,222.1, SOL $89.21
  (-6.83% 24h), circulating 590,287,408.88, total 637,066,664.13.
- `/slots?limit=3`: 465,895,816 (453 txs), 465,895,815 (445),
  465,895,814 (486); `/slot/latest`: 465,895,834, 544 txs, 100
  signatures shown.
- `/tx/28tFJX1d…` (slot 465,894,558): success, fee 0.000005 SOL,
  instructions [Unknown Program, System Program transfer], 7 accounts,
  5 logs, 531 compute units.
- `/address/vines1vzr…`: account, 0.039869364 SOL, 6 SPL holdings,
  3 priced ($0.0028), 25 signatures.
- `/address/E16prLnW…`: account, 0.006261482 SOL, SPL $53.02, 15 sigs.
- `/token/EPjFWdd5…` (USDC): symbol USDC, 6 decimals, supply
  8,905,355,695.76, price $1.0013; `/token/J1toso1u…` (JitoSOL): price
  $116.63, supply 7,835,727.46.
- `/token/EPjFWdd5…/transfers`: 25 items.
- `/program/TokenkegQ…`: executable, owner BPF upgradeable loader,
  25 invoking signatures.
- `/search`: address → account, `465895800` → slot, garbage → 404.
- Frontend: `node --check` passes; `/`, `/app.js`, `/config.js` all
  200 with correct content types; overlay verified (static/app.js is
  the 665-line Solana SPA, not the canonical EVM one).
- `scripts/sync-common.py check` → "in sync: 6 apps".
- EVM regression: ethscope `/stats` 200 (ETH $2,232.17).

### Honest limits
- Top holders are usually unavailable: `getTokenLargestAccounts` is
  persistently rate-limited on the public RPC (429/403) — the token
  page shows "Top holders unavailable: ..." instead of holders, never
  invented. It may succeed intermittently.
- Token-2022 holdings invisible (same limit as the wallet dashboard).
- Parsed-instruction coverage: jsonParsed instructions decode known
  programs; unknown programs show program id + raw data only.
- Slot leaders are not shown (getSlotLeader only reports the current
  slot's leader, not historical ones).
- Live browser rendering of the SolScope pages was not possible here —
  JS is syntax-checked and logic-reviewed only.
- Vercel `buildCommand` (sync) untested live — deploy still blocked
  (403 team scope); the committed vendored copies are the fallback.
