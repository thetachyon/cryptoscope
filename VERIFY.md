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
- **Sibling scope URLs are expected domains** (`https://<scope>.fly.dev`)
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

## BitScope (2026-11-05)

Bitcoin explorer in its own directory (`bitscope/`), mempool.space primary
+ Blockstream Esplora fallback, keyless. All checks run locally against
live mainnet data (uvicorn :8042). Nothing pushed or deployed in this pass.

### API probing (curl, before building)

- mempool.space `/blocks/tip/height` → 974732; `/blocks` → 10 blocks;
  `/v1/fees/recommended` → all 1 sat/vB; `/mempool` → count/vsize/total_fee
  + fee_histogram; `/v1/difficulty-adjustment` → progress 49.8%, change
  −2.98%; `/v1/mining/hashrate/3d` → 1.02 EH/s; `/v1/historical-price`
  → hourly USD prices; `/mempool/recent` → 10 recent txids with fee/vsize.
- Esplora `/blocks/tip/height` → same 974732; `/tx/:txid`,
  `/address/:addr` work identically.
- CoinGecko BTC price works (fallback for the price endpoint).

### Per-endpoint verification (all 200 unless noted)

- `GET /api/bitscope/stats` → height 974732 (int), difficulty
  1.4836e14, hashrate 1.0255e21 H/s, mempool 52,159 txs / 19,453,167
  vBytes, fees fastest/half-hour/hour/economy/minimum all 1 sat/vB,
  difficulty change −3.03%, next retarget 975,744.
- `GET /api/bitscope/blocks` → 10 items, first height 974732,
  next_height 974722.
- `GET /api/bitscope/block/latest` → height 974732, 1,026 txs, pool
  SECPOOL, total fees 769,747 sat, reward 313,269,747 sat.
- `GET /api/bitscope/block/974732` → same block by height.
- `GET /api/bitscope/block/latest/txs` → 25 items, first is the coinbase
  `557e04f9…`, next_start 25.
- `GET /api/bitscope/tx/642ea37c…` → fee 27,467 sat, 249.7 sat/vB,
  vsize 110, weight 437, 1 confirmation, in 0.29378782 BTC,
  out 0.29351315 BTC, 1 input (bc1q9vftx…), 1 output (bc1q9wz5n…).
- `GET /api/bitscope/tx/<unconfirmed mempool txid>` →
  confirmed false, 0 confirmations, fee 1,068 sat, RBF fields present
  (nulls — not an RBF replacement).
- `GET /api/bitscope/address/1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa` →
  balance 57.70614572 BTC, tx_count 67,330, unconfirmed 0.
- `GET …/address/…/txs` → 50 items, next_after cursor present.
- `GET …/address/…/utxo` → honest note, not data:
  "Too many unspent transaction outputs (>500). Contact support to
  raise limits." (upstream refuses the set; shown as a note in the UI).
- `GET /api/bitscope/mempool` → count 52,294, fees all 1 sat/vB,
  7 fee-rate bands, 10 recent txs.
- `GET /api/bitscope/search?q=974732` → block; `?q=<txid>` →
  transaction; `?q=1A1zP1…` → address; `?q=zzzznotreal` → 404.
- `GET /api/bitscope/docs` → 12 endpoints; `/chains` → bitcoin only.
- Invalid address → 400; bad txid → 400.
- Frontend: `node --check` passes on `bitscope/frontend/app.js`;
  `/`, `/app.js`, `/styles.css`, `/config.js` all 200.
- `scripts/sync-common.py check` → "in sync: 7 apps".
- EVM regression: ethscope `/stats` 200 (ETH $2,233.77), `/docs`
  32 entries — factory.py edit did not disturb the EVM apps.

### Bugs found & fixed this session

1. `/stats` and `/block/latest` returned `{"error": "974732"}`:
   mempool.space returns tip height and block-height lookups as bare
   text, not JSON, and the client treated any plain-text body as an
   error. Fixed with a `text_ok` flag on the two text endpoints
   (`common/bitcoin.py`); stats height also coerced to int.

### Honest limits

- UTXO lists are refused by mempool.space for very large sets (>500
  UTXOs) — the address page shows the upstream's note, never invented
  UTXOs. Normal addresses list up to 100 UTXOs.
- `/mempool/recent` returns only the 10 most recent unconfirmed txs
  (upstream limit), not 25.
- Miner names come from mempool.space v1 block extras (`pool.name`);
  if that call fails, the block page says "pool unknown" rather than
  guessing. Esplora fallback has no pool data.
- The v1-only endpoints (fees, difficulty adjustment, hashrate, pool
  extras, RBF, historical price) are mempool.space-only; if
  mempool.space is down they degrade to nulls instead of failing the
  page (Esplora cannot serve them).
- Fee-rate bands are a coarse bucketing of the mempool fee histogram,
  not per-transaction data.
- Live browser rendering of the BitScope pages was not possible here —
  JS is syntax-checked and logic-reviewed only.
- Vercel `buildCommand` (sync) untested live — deploy still blocked
  (403 team scope); the committed vendored copies are the fallback.

## Stablecoin Scopes (2026-11-05)

Eight focused dashboards — one per coin, thin apps over `common/stablecoin.py`
(`create_stablecoin_app` in `common/factory.py`) with one shared frontend
overlay SPA (`#/`, `#/holders`, `#/transfers`, `#/depegs`, `#/reserves`,
`#/api-docs`; peg gauge, hand-rolled SVG price/supply charts with depeg
markers, holders table, transfers list, reserves section). All checks run
locally against live data (uvicorn, one port per app, 8051–8058). Nothing
pushed or deployed in this pass.

### API probing (curl, before building)

- DeFiLlama `GET /stablecoins?includePrices=true` → `peggedAssets` with
  numeric ids: USDT=1, USDC=2, DAI=5, TUSD=7, USDD=14, FDUSD=119,
  PYUSD=120; **USDe=146** (symbol is case-sensitive `USDe` — a filter for
  `USDE` misses it; its id was resolved from the list at build time).
- DeFiLlama `GET /stablecoin/{id}` → description, `address`, `auditLinks`,
  `price`, `currentChainBalances` {chain: {peggedUSD}}, and — despite the
  name — `tokens`, a **daily total-supply history** [{date, circulating:
  {peggedUSD}}] (USDT 3,264 pts from 2017-11-29; USDe 1,061 from
  2023-12-11; USDD 1,567 from 2022-05-02). No `pegHistory` field (empty).
- DeFiLlama `/stablecoincharts/*` 404s in every variant — not used. Price
  history is `GET /stablecoinprices` (2,132 daily entries, ~6.4 MB,
  cached 10 min per app process; tether in all entries, ethena-usde in
  1,052).
- CoinGecko `/coins/{gecko_id}` works keyless for all 8 ids (tether,
  usd-coin, dai, ethena-usde, first-digital-usd, paypal-usd, true-usd,
  usdd) — price/market-cap/supply fallback + homepage/whitepaper links.
- Blockscout v2 `eth.blockscout.com` `/tokens/{addr}`,
  `/tokens/{addr}/holders`, `/tokens/{addr}/transfers` work for all 7
  Ethereum-primary contracts (addresses verified against DeFiLlama
  `address` + CoinGecko; `/transfers?page_size=5` returns 422 — default
  50/page used instead). Holders items carry `value` (raw base units);
  transfers carry `total: {decimals, value}`.
- Tron: `api.trongrid.io` works keyless (`/wallet/getnowblock` →
  87,749,987). **USDD contract is `TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz`**
  (DeFiLlama `address`, confirmed live via `/wallet/getcontract` AND
  TronScan token info: symbol USDD, "Decentralized USD"). The believed
  address `TPYmHEhy5nXmKtc5f6KgQVPSMURkCPSoW9` is NOT a contract (empty
  getcontract response). `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` is USDT on
  Tron, not USDD. TronScan keyless
  `apilist.tronscan.org/api/token_trc20` (info), `/token_trc20/holders`
  and `/token_trc20/transfers` work — but only with the
  **`contract_address=` parameter name** (`contract=` is silently
  ignored by transfers — it returned USDT transfers — and 400s holders).
- Reserves: no keyless machine-readable API exposes live reserve /
  collateral figures for any coin (Ethena app API 307, api.ethena.fi
  403, daistats.com/oasis/makerdao API hosts unreachable or 404). The
  `/reserves` endpoint therefore returns issuer attestation/transparency
  links + market figures labelled as market figures, `reserve_figures:
  null` — never invented numbers.

### What was built

- `common/stablecoin.py`: per-coin config (DeFiLlama id, gecko id,
  primary chain + contract, decimals, issuer, attestation links),
  DeFiLlama + CoinGecko + Blockscout + TronScan clients, peg/depeg
  computation (depeg = |price − $1| > 50 bps; health: ≤5 bps pegged,
  ≤50 watch, >50 depegged), `sc_stats/peg/supply/holders/transfers/
  depegs/reserves/chains/search` + docs payload.
- `common/factory.py`: `create_stablecoin_app` — `/api/<scope>/*`
  (11 endpoints: `/chains`, `/docs`, `/stats`, `/peg`, `/supply`,
  `/holders`, `/transfers`, `/depegs`, `/reserves`, `/search`).
- 8 thin apps (`usdtscope`, `usdcscope`, `daiscope`, `usdescope`,
  `fdusdsscope`, `pyusdsscope`, `tusdscope`, `usddscope` — double-s is
  the consistent pattern) each with config + entrypoint + vercel.json +
  api/index.py + `frontend/` overlay SPA + README. All 8 added to every
  app's `SIBLINGS` (15 apps × 14 siblings now).

### Per-coin verification (all 200 unless noted)

| Coin (port) | Price | Dev (bps) | Health | Supply | Holders (primary) | Top holder | Depeg days (worst) |
|---|---|---|---|---|---|---|---|
| USDT (8051) | $0.99853 | −14.70 | watch | $177.71B | 17,116,460 | 14.00B ($13.98B) | 48 / 2,131 (+113.56 bps, 2021-03-01) |
| USDC (8052) | $0.99991 | −0.87 | pegged | $70.72B | 9,560,654 | 3.44B ($3.44B) | 53 / 2,131 (−389.35 bps, 2023-03-12, $0.9611 SVB) |
| DAI (8053) | $1.00020 | +2.04 | pegged | $4.68B | 770,491 | 788.1M ($788.2M) | 48 / 2,131 (−374.51 bps, 2023-03-12) |
| USDe (8054) | $0.99886 | −11.35 | watch | $4.56B | 47,200 | 2.09B ($2.09B) | 18 / 1,052 (+250 bps, 2023-12-25; most recent −161.85 bps, 2026-02-01) |
| FDUSD (8055) | $0.99613 | −38.66 | watch | $289.78M | 4,343 | 100.0M ($99.6M) | 39 / 1,191 (+230 bps, 2024-08-08) |
| PYUSD (8056) | $0.99953 | −4.71 | pegged | $2.66B | 114,600 | 556.5M ($556.2M) | 8 / 1,167 (+150 bps, 2023-08-28) |
| TUSD (8057) | $0.99811 | −18.93 | watch | $484.48M | 346,905 | 97.6M ($97.4M) | 131 / 2,131 (−356.45 bps, 2024-02-29) |
| USDD (8058) | $0.99731 | −26.95 | watch | $1.51B | 10,000* | 403.1M ($402.0M) | 273 / 1,644 (−415 bps, 2022-06-20, $0.9585) |

\* USDD holder total is TronScan's capped list size (10,000), not necessarily the true holder count.

Per-coin detail (all live):
- **USDT**: mcap $177.57B; 7d −1.7% / 30d −3.4% supply; 102 chains, top Tron $92.51B, Ethereum $68.17B; peg30: 29 pts, 0 depeg days; supply90: 89 pts; holders page 2 works (next_page_params, first 9.18B); transfers 50, largest in page $137,198; most recent depeg +60 bps 2024-08-08.
- **USDC**: mcap $70.34B; 7d −0.34% / 30d −4.29%; 125 chains, top Ethereum $43.09B; transfers 50, largest in page $49,996; most recent depeg +80 bps 2024-11-11.
- **DAI**: mcap $4.52B; 7d −0.49% / 30d −1.67%; 36 chains, top Ethereum $4.11B; transfers 50, largest in page $263,143; most recent depeg −54.15 bps 2024-05-08; reserves page lists Sky app + DAI Stats + MakerDAO links.
- **USDe**: mcap $4.52B; 7d +0.79% / 30d −7.83%; 30 chains, top Ethereum $2.50B; transfers 50, largest in page $883,323; reserves page lists Ethena audits + app.
- **FDUSD**: mcap $288.76M; 7d −2.88% / 30d −9.59%; 6 chains, top Ethereum $178.7M; transfers 50, largest in page $4,084,337; most recent depeg −51.64 bps 2025-11-19.
- **PYUSD**: mcap $2.70B; 7d −9.53% / 30d −7.86%; 18 chains, top Ethereum $1.73B; transfers 50, largest in page $11,581; most recent depeg +90 bps 2024-08-08.
- **TUSD**: mcap $494.04M; 7d −0.0% / 30d −0.0% (supply frozen — last-7d daily deltas all $0); 9 chains, top Ethereum $313.4M; transfers 50, largest in page $1,916; most recent depeg −51.73 bps 2026-08-01.
- **USDD**: mcap $1.50B (TronScan token mcap $1,154,687,304, vol24h $6,519,468); 7d −0.77% / 30d −5.26%; 4 chains, top Tron $1.154B; holders via TronScan (50/page, `?start=` pagination verified to page 2, next_start 100); transfers via TronScan (50, largest in page $211,673); most recent depeg +80 bps 2025-02-05; supply90 returns only 39 points — see limits.
- Every app: `/docs` 11 endpoints; `/chains` correct count; `/reserves`
  `reserve_figures: null` with attestation links (1–3 per coin);
  `/search?q=zzzznotreal` → 404 with a clear message; `/`, `/app.js`
  (33,992 B dashboard SPA), `/config.js`, `/styles.css` all 200.
- `node --check` passes on all 15 vendored `static/app.js` files;
  `python3 scripts/sync-common.py check` → "in sync: 15 apps match
  canonical common/ + frontend/".
- EVM regression: ethscope `/stats` 200 (height 26,351,551) after the
  factory.py edit.

### Bugs found & fixed

1. `/tokens/{addr}/transfers?page_size=5` returns 422 from Blockscout —
   the transfers/holders endpoints are called with default pagination
   (50/page) and the returned `next_page_params` instead.
2. TronScan transfers with `contract=` silently returns the wrong
   coin's transfers (USDT) — fixed to `contract_address=` (holders
   400s with `contract=`). Both endpoints use `contract_address=`.
3. Frontend Load-more rank renumbering used a regex over row HTML that
   could also rewrite a numeric balance cell — replaced with an
   explicit `startIdx` parameter.

### Honest limits

- **Holders/transfers cover the primary chain only** (Ethereum for
  7 coins, Tron for USDD). Cross-chain holders are not aggregated:
  USDT's Ethereum holders (17.1M) exclude its larger Tron base; FDUSD
  (4,343 Ethereum holders) and TUSD (346,905) are majority non-Ethereum
  coins — their READMEs say so.
- **Transfers are latest-first (50/page), not size-filtered.** The
  largest transfer in the latest page is reported per coin above, but
  the list is not a "largest transfers" ranking.
- **Reserves: no reserve numbers anywhere.** No keyless API exposes
  live reserve/collateral figures; the reserves page is attestation
  links + market figures labelled as market figures. DAI collateral
  composition and USDe backing are link-outs only.
- **USDD holder total is capped at 10,000** by TronScan (shown with an
  explicit note in the API + UI); the true holder count is unknown.
- **USDD supply history has an 83-day DeFiLlama data gap**
  (2026-07-07 → 2026-09-28, no daily points) — the supply chart draws
  a straight line across it; `/supply?days=90` returns 39 points.
- **Depeg history is daily closes only** (DeFiLlama `/stablecoinprices`);
  intraday depegs larger than the daily close are not shown.
- **Peg health is a labelling choice:** ≤5 bps pegged, ≤50 bps watch,
  >50 bps depegged. USDT at −14.7 bps and USDD at −26.95 bps read as
  "watch" — normal wobble for those coins, not distress.
- `/stablecoinprices` is a ~6.4 MB download, cached 10 minutes per app
  process; the first `/peg` or `/depegs` call per app is slow (~seconds).
- Live browser rendering of the dashboard pages was not possible here —
  JS is syntax-checked and logic-reviewed only.
- Vercel `buildCommand` (sync) untested live — deploy still blocked
  (403 team scope); the committed vendored copies are the fallback.

## Overview app (2026-11-06)

Cross-chain analytics landing page (`overview/`): global market cards,
chain comparison table, stablecoin peg table, Wormhole bridge flows,
top movers, and a search box that routes to the right Scope. All 15
existing apps link back to it via `SIBLINGS` (first entry, "Cryptoscope").

### API probing (curl, before building)

- CoinGecko `/global` works (total mcap ~$2.58T at probe time).
- CoinGecko `/coins/markets` works (BTC $76,279, −5.41% 24h).
- **DeFiLlama bridges API is PAYWALLED** — `bridges.llama.fi` and
  `api.llama.fi/bridges` both answer "Upgrade to the paid API plan".
  Bridge flows therefore come from **Wormholescan** `/api/v1/scorecards`
  (works: 24h volume $52.4M, 7d $329M, 30d $1.25B, total $77.6B,
  TVL $1.33B) + `/api/v1/last-txs` (hourly counts), labelled
  Wormhole-only — never all-bridge totals.

### What was built

- `common/overview.py` — `ov_global`, `ov_chains`, `ov_stablecoins`,
  `ov_bridges`, `ov_movers`, `ov_overview` (aggregate), `ov_search`,
  `ov_docs_payload`. Per-section independent degradation: a failed
  upstream blanks its section with a note, never the whole page.
  Caching: aggregate 120s, sections 300s, chains 60s.
- `common/factory.py` — `create_overview_app` (mounts `/api/overview/*`,
  serves `static/`, SPA fallback to `index.html`).
- `overview/` — thin app: `config.py` (SIBLINGS = all 15 scopes +
  WalletScope), `app.py`, `api/index.py`, `vercel.json`, `requirements.txt`,
  `frontend/index.html` + `frontend/app.js` (hero + search, market cards,
  chain table, stablecoin peg table, bridge section, top movers, footer
  nav to all scopes), `README.md`.
- Search check order: stablecoin name/symbol → `.eth` → `0x`
  address/tx → BTC address → plain number → 64-hex → Solana signature
  (87–88 base58) → Solana address → 404. Plain numbers and bare 64-hex
  return `ambiguous` with candidates instead of guessing.

### Verified live (uvicorn :8043, 2026-11-06)

| Endpoint | Result |
|---|---|
| `GET /` | 200 (landing SPA) |
| `GET /api/overview/docs` | 200, 8 endpoints |
| `GET /api/overview/stats` | 200 — total mcap $2.58T, BTC dominance 59.2%, 24h −7.13%, stablecoin supply $262.6B |
| `GET /api/overview/chains` | 200 — ETH $2,244.26 (−7.46%), block 26,352,162, 0.37 gwei; SOL $90.21 (−6.03%), slot 465,913,840, 1,670.5 TPS; BTC $75,989 (−5.52%), height 974,740, 3 sat/vB, mempool 53,043; Optimism block 159,160,366; Gnosis 49,130,905; Sepolia 12,073,027 |
| `GET /api/overview/stablecoins` | 200 — all 8 rows live, matching the scopes' pegs: USDC $0.99981 (−1.92 bps, pegged), DAI $1.00020 (+2.04, pegged), PYUSD $0.99953 (−4.71, pegged), USDT $0.99846 (−15.4, watch), USDe $0.99886 (−11.35, watch), TUSD $0.99824 (−17.59, watch), USDD $0.99731 (−26.95, watch), FDUSD $0.99613 (−38.66, watch); total $262.6B |
| `GET /api/overview/bridges` | 200 — Wormhole 24h $52.49M, 7d $329.2M, 30d $1.25B, total $77.6B, TVL $1.33B, 19,047 messages/24h |
| `GET /api/overview/movers` | 200 — gainers RAIN +6.02%, ZEC +1.52%; losers ENA −9.86%, UNI −9.02% |
| `GET /api/overview/overview` | 200 — all 6 sections present, no section errors |
| `GET /api/overview/search` | USDC → usdcscope; `0xd8dA…` → EthScope address; `bc1q…` → BitScope; `26352162` → ambiguous (ETH block / SOL slot / BTC block); `0x`+64hex → EthScope tx; bare 64-hex → ambiguous (ETH tx / BTC tx); `vitalik.eth` → EthScope; Solana address → SolScope; unknown → honest "No match" error |
| EVM regression | ethscope `:8044` `/api/ethscope/stats` 200, home 200, latest block 26,352,163 |

### Honest limits

- **Bridge flows are Wormhole-only.** DeFiLlama's bridges API is
  paywalled; no keyless all-bridge total exists. Labelled as such in
  the API note and the UI.
- **Optimism 24h change is Ethereum's CoinGecko change** (native ETH);
  Blockscout stats for Optimism carries no change field. Gnosis change
  (−0.02%) comes from Blockscout stats; Sepolia has no price (testnet)
  and shows "—".
- **Solana fee is blank** — no fee field is exposed by the public RPC
  path used; TPS comes from `getRecentPerformanceSamples`.
- **Movers are the top 50 coins by market cap only** (CoinGecko
  `/coins/markets`, one page); small-cap movers are not shown.
- **Search does not validate existence** — a well-formed hash routes to
  the Scope's tx page even if the tx doesn't exist there (the Scope
  shows its own not-found state). Ambiguous queries show candidates
  rather than guessing.
- Live browser rendering of the landing page was not possible here —
  JS is syntax-checked (`node --check` passes) and logic-reviewed only.
- `scripts/sync-common.py check` passes for all 16 apps.

## Feature 1 — Risk scores on addresses (2026-11-05)

Port of the Cryptanalysis forensics scoring into Cryptoscope as a
self-contained keyless module (`common/risk.py` — no live
Cryptanalysis call, no API key). No local Cryptanalysis copy existed
under `~/workspace/`, so the label set was rebuilt from the primary
source: OFAC SDN `sdn.csv`, downloaded 2026-11-05 from
treasury.gov/ofac/downloads/sdn.csv (521 "Digital Currency Address"
mentions → 514 unique addresses; 94 EVM-format, 260 Bitcoin, 2
Solana; program names repaired only for the CSV's dropped leading
bracket). Verified live against uvicorn (ports 8051/8052/8053); nothing
pushed.

### Endpoint verification (all 200, reasons present)

| Check | Result |
|---|---|
| EVM sanctioned address (OFAC-listed ETH address from the set) `GET /api/ethscope/address/<addr>/risk` | 100 / critical — "on the OFAC SDN list as MESRI, Behzad ([HRIT-IR] [CYBER2]; ETH address). Direct sanctions match." Walk skipped (max score already set). |
| vitalik.eth (`0xd8dA…96045`) `GET /api/ethscope/address/…/risk` | 0 / low — 401 counterparties checked, 3 hops, 626 txs scanned, ~16s. |
| Solana sanctioned address (OFAC-listed SOL address from the set) `GET /api/solscope/address/…/risk` | 100 / critical — "on the OFAC SDN list as SOKOLOVSKI, Rolan ([ILLICIT-DRUGS-EO14059]; SOL address)." |
| Solana clean address (`vines1vzr…`) `GET /api/solscope/address/…/risk` | 0 / low — 26 counterparties, 1 hop, 10 txs scanned, ~5s. |
| Bitcoin sanctioned address (OFAC-listed BTC address from the set) `GET /api/bitscope/address/…/risk` | 100 / critical — "on the OFAC SDN list as MESRI, Behzad ([HRIT-IR] [CYBER2]; XBT address)." |
| Bitcoin genesis address (`1A1zP1…DivfNa`) `GET /api/bitscope/address/…/risk` | 0 / low — 7,602 counterparties checked, 3 hops, 531 txs scanned, ~10s. |
| Scoring unit checks (`common.risk.assess`) | 1-hop sanctioned non-mixer = 80/high; 1-hop sanctioned mixer (Blender.io) = 70/elevated; 3-hop sanctioned = 40/elevated. |
| Docs | Risk endpoint listed: ethscope `/docs` 33 entries, solscope 13, bitscope 13. |
| EVM regression | ethscope home 200, `/api/ethscope/stats` 200 (latest block 26,353,148). SolScope/BitScope home 200. |
| UI | Playwright screenshot (desktop 1280×900) of vitalik.eth's address page shows the Risk card: "0 / 100 · low", LOW badge, both reasons, family + coverage + method notes — `~/workspace/cryptoscope-snapshots/risk-address-eth.png`. |
| Sync | `scripts/sync-common.py check` → "in sync: 16 apps match canonical common/ + frontend/". Risk route registered in all six explorer apps (eth/sepolia/optimism/gnosis/sol/btc). |

### Honest limits

- The walk only covers the transactions the address page loads (first
  page; EVM ≤50 txs, BTC 25 txs, SOL 10 txs) with 10/5 hop budgets —
  a low score is not proof an address is clean. Stated in the API
  `coverage.note` and the UI.
- Solana is 1 hop only and the SDN list holds just 2 Solana addresses;
  the 1-hop walk can only match those same 2 addresses. Stated in the
  `family_note` and the UI.
- Tornado Cash is NOT in the vendored set (OFAC delisted it March
  2025); mixer detection covers Blender.io / Sinbad only (13
  addresses).
- Full-walk latency on busy addresses is 5–16s (server-side, cached
  10 min per address); the frontend fetches risk with a 60s timeout so
  the card never hangs the page, and a failure renders as "Risk score
  unavailable" instead of blocking the address page.
- One SDN BTC entry is truncated in OFAC's own CSV
  (`3F6bbvS1krsc1qR8FsbTDfYQyvkMm`) and can never match a real
  address; kept verbatim.

## Feature 2 — Security scanner on tokens (2026-11-05)

Token/contract risk verdicts, EVM first (`common/security.py`,
zero new dependencies, all keyless). Verified live against uvicorn
(ethscope 8051, optimismscope 8052, solscope 8053); nothing pushed.
Feature 1 untouched — its risk endpoint re-verified 200 below.

### Endpoint verification (all 200, per-check results real)

| Check | Result |
|---|---|
| USDC on Ethereum `GET /api/ethscope/token/0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48/security` | **low** (1 point, ~17s). Owner warn — live owner `0xfcb19e6a322b27c06842a71e8c725399f049ae3a` (real `owner()` eth_call via public RPC) with mint/pause/blacklist/updatePauser/updateBlacklister/updateMasterMinter/configureMinter/upgradeTo/transferOwnership from the merged proxy+implementation ABI, `paused: false`. Concentration pass — top-10 24.7%, top-1 7.35% (Blockscout holders, 9,561,901 holders). Tax unknown — no standard tax function in the ABI ("not detectable"). Liquidity pass — $10,704,538 DexScreener-tracked across 2 pairs, V2 pair `0xB4e16d…` reserves readable, LP burned 0.0%. Honeypot pass — buy simulation (0.01 ETH → USDC via V2 router, from vitalik.eth as the read-only eth_call sender) AND sell simulation (transfer of 1 USDC from the largest holder to the pair) both succeeded. |
| USDC.e on Optimism `GET /api/optimismscope/token/0x7F5c764cBc14f9669B88837ca1490cCa17c31607/security` | **elevated** (2 points, ~10s). Owner warn (live owner `0x9028967b…`, mint/pause/blacklist powers). Concentration pass — top-10 27.0%, top-1 5.9%. Tax unknown. Liquidity warn — $594,122 DexScreener-tracked across 30 pairs, thin. Honeypot pass — sell transfer succeeded; buy leg honestly unavailable (no V2 pair for the token on Optimism; top DEX pair is a V3 pool). |
| USDC on Solana `GET /api/solscope/token/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v/security` | **elevated** (2 points, ~13s). Mint authority warn — live (`BJE5MMbqXjVwjAF7oxwPYXnTXDyspzZyt4vwenNw5ruG`); freeze authority warn — live (`7dGbd2QZcCKcTndnHcTL8q7SMVXAkp688NTQYwrRCrar`). Holder concentration unknown — public RPC 403 on `getTokenLargestAccounts` (stated, not faked). Owner powers / tax / honeypot / liquidity all `na` — labelled EVM-only. |
| Docs | Security endpoint listed: ethscope `/docs` 34 entries, solscope 14. |
| EVM regression | ethscope home 200, `/api/ethscope/stats` 200; Feature 1 `GET /api/ethscope/address/0xd8dA…96045/risk` → 200, 0/low. |
| UI | Playwright screenshot (desktop 1280 wide, full page) of the USDC token page shows the Security card: LOW RISK badge, all five checks with pass/warn/not-detectable badges, method + limits footnotes — `~/workspace/cryptoscope-snapshots/security-token-usdc.png`. Security card also loads on token-contract address pages (same endpoint). |

### Bugs found live and fixed during verification

- **False honeypot on Optimism (first run: USDC.e scored high).**
  The buy leg ran the V2 router against the DexScreener top pair,
  which on Optimism is a V3 pool — the router call reverted for
  plumbing reasons, not because the token blocks buys. Fixed: the
  router buy leg only runs when the V2 factory itself returns a pair
  for the token (`pair_via == "uniswap-v2-factory"`); otherwise the
  buy leg is "unavailable" and the sell-transfer leg still decides.
  Re-verified: USDC.e is elevated (owner warn + thin liquidity), not
  high.
- **Transient Blockscout holders failure degraded two checks at
  once.** One run returned concentration unknown + honeypot unknown
  because a single holders fetch failed (no top holder → no sell
  simulation). Fixed: the holders fetch is retried once after 2s.
  Re-verified: full USDC/Ethereum result (all five checks resolved)
  on the next run.

### Honest limits

- Tax is only detectable when the ABI exposes a standard tax
  function; a tax hidden in transfer logic reports "not detectable".
- DexScreener's `/latest/dex/tokens` list is capped/truncated (for
  USDC on Ethereum it returned 2 pairs totalling $10.7M while the
  WETH/USDC V2 pair alone holds ~$19.2M) — liquidity totals are a
  lower bound over DexScreener-tracked pairs, stated as such.
- The V2 buy simulation needs a V2 pair; tokens whose liquidity is
  V3-only (or on chains without a V2 deployment, e.g. Sepolia's
  DexScreener gap) get the buy leg as "unavailable", never a faked
  pass/fail. Sepolia was not live-tested (no suitable token pair);
  the endpoint is registered identically in all four EVM scopes.
- The sell simulation transfers from the largest holder to the pair
  via read-only eth_call — it proves the token's transfer does not
  revert for a holder, not that a real router swap succeeds
  (slippage, MEV, approvals). Stated in the API `limits` and the UI.
- Solana holder concentration depends on the public RPC answering
  `getTokenLargestAccounts` — it 403'd during verification and the
  check degraded to unknown honestly.
- Scan latency is 10–20s server-side (many upstream calls,
  sequential); results are cached 300s per token and the frontend
  fetches with a 60s timeout, rendering "Security scan unavailable"
  on failure instead of blocking the page.

## Feature 3 — Plain-English transactions (2026-11-06)

"What happened" card on every transaction page (`common/explain.py`,
zero new dependencies, all keyless). Verified live against uvicorn
(ethscope 8123, optimismscope 8124, solscope 8125, bitscope 8126);
nothing pushed. Features 1 and 2 untouched — both endpoints
re-verified 200 below.

### Endpoint verification (all 200, amounts spot-checked against raw tx data)

| Check | Result |
|---|---|
| Plain ETH transfer `GET /api/ethscope/tx/0xeff2196a…682e83/explain` | **native_transfer** — "Sent 1.12048333168995778 ETH from 0xc22C449B...AA69a6C3 to 0xb3bDe58d...648a45c7." Raw tx confirms value 1.12048333168995778 ETH, input `0x`. |
| ERC-20 transfer `GET /api/ethscope/tx/0xb5b6fcab…52d801/explain` | **erc20_transfer** — "Sent 1035 USDC from 0x7Ea36276...d5EC325f to 0x3DAA55A8...038c39Ee." Method `transfer(address,uint256)` (local). Flow amount 1035 USDC matches the token transfer. |
| Uniswap swap `GET /api/ethscope/tx/0xf992b18e…954d1f37/explain` | **swap** — "Swapped 0.03 ETH for 60460.570297 Gato via 0x7a250d56...59F2488D." Method `swapExactETHForTokensSupportingFeeOnTransferTokens(...)` (local). Raw tx value 0.03 ETH; Gato transfer 60460.570297 matches the token transfer from the pair. |
| Approval `GET /api/ethscope/tx/0x0d27868c…ee803f5/explain` | **approval** — "Approved 0x88888881...ebb4e3ce to spend 59995 USDC on behalf of 0x5731d049...2F6Cf314." Raw calldata decodes to 59,995,000,000 units = 59,995 USDC (6 decimals). |
| Unknown method `GET /api/ethscope/tx/0x82f64f06…e01e13/explain` (selector `0x99e1d016`) and `0x5c2718de…e4ec/explain` (`0x518702b2`) | **unknown** — "Contract interaction with 0xF43fF2DA...D7f82C22 — method 0x99e1d016 is not in the signature database, so what it does cannot be described here." No invented description. 4byte lookup returns no curated name for either selector. |
| 4byte fallback (unit-level, live) | `claim()` selector `0x4e71d92d` resolves via 4byte.directory to `claim()`; junk selector `0x99e1d016` resolves to None. Colliding junk (e.g. `workMyDirefulOwner` for `0xa9059cbb`) is never accepted — the local map wins for known selectors. |
| Optimism (2nd EVM chain) `GET /api/optimismscope/tx/0xc7d9e3e9…e622d2/explain` | **unknown** — honest "not in the signature database" wording for method `0xcbce98b0`. |
| Solana Jupiter swap `GET /api/solscope/tx/U6qz6ejW…ctrDuJ/explain` | **swap** — "Swapped tokens via Jupiter Aggregator (token changes are listed in the flows below)." 11 flows (SPL + SOL balance changes). |
| Solana SPL transfer `GET /api/solscope/tx/2hW8Bd6E…LejSoX/explain` | **spl_transfer** — "Sent 10.0 tokens (mint EPjF…Dt1v) from 6VHz…HhT4 to 7R91…2k4r." (USDC mint, 10 USDC.) |
| Solana unknown program `GET /api/solscope/tx/2JZj7F17…2EbaDp7/explain` | **contract_call** — "Called unknown program hydH…L1yAJ." with the program id shown, not a bare "Unknown Program". |
| Bitcoin coinbase `GET /api/bitscope/tx/5c9d44f4…3232932/explain` | **coinbase** — "Miner reward: created 3.13865652 BTC (block subsidy + fees) paid to 3K9KZZPB8NRwZVP5wNKX4VYhnswrJxpgZ4." 2 value-bearing output flows. |
| Deployment + NFT transfer (synthetic, mocked tx detail) | Deployment — "Deployed a new contract at 0x22222222...22222222 from 0x11111111...11111111, sending 0.5 ETH to it." NFT — "Transferred BAYC token #1234 from 0x11111111...11111111 to 0x44444444...44444444." No live deployment or NFT transfer surfaced in ~300 scanned latest-feed txs, so these two branches are verified by unit test, not live. |
| Docs | Explain endpoint listed in ethscope, solscope and bitscope `/docs`. |
| EVM regression | ethscope home 200, `/api/ethscope/stats` 200; Feature 1 `GET /api/ethscope/address/0xd8dA…96045/risk` → 200, 0/low; Feature 2 `GET /api/ethscope/token/0xA0b8…6eB48/security` → 200, low (owner warn, concentration pass, tax unknown, liquidity pass, honeypot pass). |
| UI | Playwright screenshots (desktop 1280×900) of tx pages showing the "What happened" card at the top: `~/workspace/cryptoscope-snapshots/explain-tx-swap.png` (swap sentence + method + 4-row flow table), `explain-tx-transfer.png` (USDC transfer), `explain-tx-eth.png` (plain ETH), `explain-tx-sol-swap.png` (Jupiter swap), `explain-tx-btc.png` (coinbase). |
| Sync | `scripts/sync-common.py check` → "in sync: 16 apps match canonical common/ + frontend/". Explain route registered in all six explorer apps (eth/sepolia/optimism/gnosis/sol/btc). |

### Bugs found live and fixed during verification

- **Blockscout 422s on some tx-detail lookups.** Two txs from the
  live feed (`0xc7ea0b13…` plain transfer, `0xe9633ecd…` Gato token
  creation) return HTTP 422 from Blockscout on the plain `/tx/`
  endpoint too — upstream, not the explain code. Replaced both test
  txs with live ones whose detail endpoint answers.
- **SOL amounts in scientific notation.** Tiny SOL changes rendered
  as `4.2721e-05` / `8.603e-06` (raw float formatting). Fixed: SOL
  and SPL amounts are formatted fixed-point (9 decimals, trailing
  zeros stripped).
- **Bitcoin zero-value outputs in the flow table.** OP_RETURN outputs
  (value 0, no address) appeared as `BTC 0 — —` rows. Fixed:
  zero-value outputs are excluded from flows (the sentence still
  counts all outputs).

### Honest limits

- Multicall inner calls are named, not decoded — the token flows
  still show what moved. Proxy / meta-transaction internals are not
  decoded either.
- V3 swap amounts come from the transfer logs; a swap whose output
  is an internal ETH transfer states the output amount is not
  visible there. Token→ETH swaps state the exact ETH amount is an
  internal transfer, not shown.
- Solana token symbols are not resolved — SPL mints are shown
  shortened; SOL balance-change flows include the fee payer's fee.
- Bitcoin change outputs cannot be identified from chain data alone;
  every output is listed, including any change back to the sender.
- Deployment and NFT-transfer sentences are verified by unit test
  only (no live instance surfaced in the scanned feed); the code
  paths are the same ones the live-tested branches use.
- The 4byte.directory lookup accepts only curated function names;
  a real method whose name is outside the curated set reports as
  unknown rather than risking a colliding junk signature.
- Explain latency is one Blockscout tx-detail fetch (+ one
  4byte lookup only for unknown selectors, cached 1h); the frontend
  fetches with a 30s timeout and renders "Explanation unavailable"
  on failure instead of blocking the tx page.

## Feature 4 — Cross-chain identity (2026-11-05)

"Cross-chain identity" card on every address page (`common/crosschain.py`,
zero new dependencies, all keyless). Verified live against uvicorn
(ethscope 8041, optimismscope 8043, solscope 8046, bitscope 8047,
overview 8048); nothing pushed. Features 1–3 untouched — all three
endpoints re-verified 200 below.

### Endpoint verification (all 200, balances/activity spot-checked against raw upstream data)

| Check | Result |
|---|---|
| vitalik.eth `GET /api/ethscope/address/0xd8dA…96045/crosschain` | 200. EVM presence, all four chains **active**: Ethereum 5.780441042197210512 ETH, latest Nov 4, 2026; Sepolia 61.723263527250847231 ETH, latest Nov 5, 2026; Optimism 0.181079591875347535 ETH, latest Oct 19, 2026; Gnosis 8.709546191240796693 xDAI, latest May 19, 2026. Balances match raw Blockscout `/addresses/` responses to the wei. 3 Wormhole transfers (below). Honest-limits `note` present verbatim; `walletscope_url` present. |
| Wormhole transfers for vitalik.eth | 3, all direction **in**: Feb 23, 2026 Optimism → Base (MAYAN_SWIFT, amount not reported — Wormholescan standardizes none for it); Dec 20, 2025 BNB Chain → Ethereum, 1.53970797 (MAYAN_SWIFT); Apr 16, 2025 Polygon → Ethereum, 1 (GENERIC_RELAYER, NATIVE_TOKEN_TRANSFER). Amounts match raw Wormholescan `standardizedProperties.amount` + `normalizedDecimals` (8). |
| 2nd EVM chain `GET /api/optimismscope/address/0xd8dA…96045/crosschain` | 200, same presence + bridge payload (scope_chain `optimism`). |
| Solana `GET /api/solscope/address/EnxFkSMoJkEzYUkWHubEXYKw2y9HQVUEHGmXrMyQniKu/crosschain` | 200. `kind: solana`, `evm_presence: null` (not applicable), `walletscope_url` present. 50 Wormhole transfers (Mayan driver address), direction **out**, first three Solana → Unknown chain (no destination chain in the record — rendered honestly, never guessed). |
| Bitcoin `GET /api/bitscope/address/1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa/crosschain` | 200. `kind: bitcoin`, `evm_presence: null`, `walletscope_url: null` (WalletScope has no Bitcoin), 0 bridge transfers, no error. |
| Overview search routing | `GET /api/overview/search?q=0xd8dA…96045` → `{"type":"address","chain":"ethereum","url":"https://ethscope.vercel.app/#/address/0xd8dA…96045"}`; `?q=vitalik.eth` → same pattern with the ENS name. The address page it lands on carries the Cross-chain section (screenshot below). |
| Docs | Crosschain endpoint listed in ethscope (36 endpoints total), solscope and bitscope `/docs`. |
| Regressions | ethscope home 200, `/api/ethscope/stats` 200; Feature 1 `GET /api/ethscope/address/0xd8dA…96045/risk` → 200; Feature 2 `GET /api/ethscope/token/0xA0b8…6eB48/security` (USDC) → 200; Feature 3 `GET /api/ethscope/tx/0xf992b18e…954d1f37/explain` (swap) → 200. |
| UI | Playwright screenshot (desktop 1280×900) of vitalik.eth's address page showing the Cross-chain section with all four chain rows, the three bridge transfers, the honest-limits note and the WalletScope button: `~/workspace/cryptoscope-snapshots/crosschain-vitalik.png`. |
| Sync | `scripts/sync-common.py check` → "in sync: 16 apps match canonical common/ + frontend/". Crosschain route registered in all six explorer apps (eth/sepolia/optimism/gnosis/sol/btc). |

### Bugs found live and fixed during verification

- **Blockscout 422 on `page_size` for address transactions.** The
  `/addresses/{addr}/transactions` endpoint rejects `page_size`
  ("Unexpected field: page_size"), which sank every presence row to
  "unavailable" (the gather propagated the failure). Fixed: the
  latest-activity fetch uses the default first page, and its failure
  is best-effort — it can only null `latest_tx`, never the row.
- **Blockscout `transactions_count` is null for some addresses.**
  vitalik.eth's raw address response has `"transactions_count": null`
  on all four chains (the address page shows "—" for the same reason).
  An early version coerced null to 0, which would have displayed a
  false "0 txs". Fixed: the count is reported only when upstream
  provides it; latest activity is the primary activity signal.
- **Pending tx with null timestamp as "latest".** Sepolia's first tx
  row is pending (`block_number` and `timestamp` null). Fixed: the
  latest-tx picker takes the first *timestamped* row, falling back
  to the first row only if none is timestamped.
- **Mayan Swift sender misattribution.** The first version fell back
  to `originTx.from` for the displayed sender, which is a Mayan
  driver address on the emitter chain (Solana), not the user — shown
  next to a "BNB Chain" route it would have been misleading. Fixed:
  the displayed sender comes only from Wormholescan's standardized
  `fromAddress` (null for Mayan Swift); direction detection still
  uses `originTx.from` internally.
- **Stale-server trap during verification.** `kill $(cat pidfile)`
  killed the nohup wrapper, not the uvicorn child, so the old code
  kept serving port 8041 and the new process died on bind. Fixed by
  killing the actual uvicorn pid (from `ss -ltnp`) and re-verifying
  the served code changed. (Process hygiene note for future sessions,
  not a product bug.)

### Honest limits

- EVM ↔ Solana ↔ Bitcoin linkage is shown ONLY when an actual bridge
  transfer is observed; the section carries that wording visibly, and
  the API response carries it in the `note` field. There is no keyless
  way to prove two different-format addresses belong to the same
  person.
- Same-EOA rows are one account by construction (the same address
  string on every EVM chain), not an identity claim. The "Contract"
  badge on a row is Blockscout's own `is_contract` flag (it reports
  vitalik.eth as a contract on Ethereum and Optimism — upstream data,
  repeated verbatim, same as the address page).
- Wormhole only — no all-bridge total exists keyless (DeFiLlama
  bridges is paywalled, probed 2026-11-06). The Wormholescan lookup
  matches the origin-tx sender and the destination address; it does
  NOT match an address that merely appears as a token sender inside a
  contract-initiated transfer (probed: the Polygon sender of a
  transfer to vitalik.eth returns 0 results for its own lookup).
- Mayan Swift transfers standardize no sender and sometimes no
  amount; batch transfers can carry no destination chain in the
  record — all three render as absent/unknown, never invented or
  guessed. Chain labels for unmapped Wormhole chain ids render as
  "Chain {id}".
- Wormhole does not bridge native Bitcoin, so BitScope bridge sections
  are expected to be empty unless the address appears as a bridge
  destination.
- Presence latency is 8 parallel Blockscout calls (2 per chain, 6s
  timeout each) + one Wormholescan call, cached 120s; the frontend
  fetches with a 30s timeout and renders "Cross-chain identity
  unavailable" on failure instead of blocking the address page.

## Phase 2, P2 — Recipient pre-flight compliance check (2026-11-06)

### What was built

- `GET /api/walletscope/preflight?address=&coin=&chain=&amount=` (new
  `common/preflight.py`, wired in `common/factory.py` `create_wallet_app`,
  listed in `DOCS_GLOBAL`). One verdict (pass / warn / block) from five
  checks: sanctions risk (reuses `common/risk.py`, OFAC SDN 514, never
  rebuilt), EOA vs contract, recipient holds the coin, frozen/blacklisted
  (recipient + up to 10 recent counterparties), first-seen + activity
  recency. Works for EVM, Solana and Bitcoin addresses and ENS names;
  coin is a stablecoin symbol (mainnet contracts only), native symbol,
  0x contract, or SPL mint. Nothing creates or signs a transaction.
- Frontend `#/preflight` (+ `#/preflight/<addr>`, auto-runs) in
  `frontend/app.js`, walletscope-only route; explorer apps redirect to
  walletscope. One-click "🛡️ Pre-flight check" button on every address
  page (links out to walletscope) and on the wallet page, plus nav/footer
  links and a wallet-landing link. Stablecoin symbols pin the chain
  select to Ethereum (their contracts are mainnet-only).
- `labels.json` extended 14 → 16 entries, both verified live against
  Blockscout `/addresses` on 2026-11-06: Uniswap V3 SwapRouter
  (`0xE592427A…`, name SwapRouter) and Uniswap V3 Positions NFT
  (`0xC36442b4…`, name Uniswap V3 Positions NFT-V1, UNI-V3-POS). The
  preflight page shows the label pill via the existing `labelFor()`.

### Endpoint verification (all 200 unless noted; walletscope :8045)

- `?address=0xd8dA…96045&coin=USDC&amount=100` (vitalik.eth) →
  **pass**: risk 0/100 low (257 counterparties, 538 txs, 3 hops); EOA
  with EIP-7702 delegation to `0x5a7f…f96f6d`; holds 39.192124 USDC
  (< 100 planned); not frozen (`isBlacklisted()` false); 10
  counterparties checked, none frozen; 78,427 txs, last activity
  2026-11-06, first-seen honestly unknown (250 of 78,427 scanned).
- `?address=0x0330070f…e54a&coin=USDC` (OFAC SDN, AMNOKGANG TECHNOLOGY
  DEVELOPMENT COMPANY [DPRK4]) → **block**: risk 100/critical, direct
  hit; recipient frozen/blacklisted for USDC (`isBlacklisted()` true);
  3 counterparties checked, none frozen; 4 txs, first 2023-04-21,
  last 2024-12-04.
- `?address=vitalik.eth&coin=ETH` → 200, pass, resolves via ENS,
  holds 5.793818533768043911 ETH; freeze check n/a (native coin).
- No coin (`?address=0xd8dA…96045`) → 200, pass; freeze probed on
  USDC + USDT (both not frozen). `?coin=TUSD` → warn: TUSD freeze
  status cannot be verified on-chain (write-only blacklist).
- Solana `vines1vzrYbzLMRdu58ou5XTby4qAqVRLmqo36NKPTg&coin=SOL` → 200,
  pass, holds 0.039869364 SOL, risk 0/low (1-hop walk). Same address
  with `&coin=EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v` (USDC mint)
  → 200, pass; mint freeze authority exists (`7dGbd2QZ…`), recipient
  holds no token account for the mint.
- Bitcoin `1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa&coin=BTC` → 200, pass,
  holds 57.70674218 BTC, risk 0/low; 67,384 txs, last activity
  2026-11-06. Bitcoin SDN `bc1q2lpgjntr348pfvxhfy33ehmdzy3gmx8w4052z6`
  (TASK FORCE RUSICH [RUSSIA-EO14024]) → 200, **block**, risk
  100/critical; 3 txs, first 2022-09-07 exact.
- USDC contract as recipient (`0xA0b8…eb48&coin=USDC`) → 200,
  **block**: recipient frozen (`isBlacklisted(USDC contract)` is
  genuinely true on-chain — Circle blacklists its own contract
  address, verified live) + contract warn; activity pass, 1,487 txs,
  recency reads "250 pending (unconfirmed) transaction(s) at the top
  of the list" (see bugs below).
- `?address=not-an-address` → 400; missing `address` → 400.
- `GET /api/walletscope/docs` lists `/preflight` (11 entries).
- Static: `/`, `/app.js`, `/labels.json` (16 labels) all 200;
  `node --check` passes on `frontend/app.js`;
  `python3 scripts/sync-common.py check` → "in sync: 16 apps".
- UI rendering was NOT verified in a live browser (no browser in this
  session): the page markup, route wiring and `node --check` were
  verified; the rendered page still needs one browser pass.

### Bugs found live and fixed during verification

- Blockscout's `/addresses/{hash}` record does NOT carry
  `transactions_count` (absent even for vitalik.eth) — the count now
  comes from `/addresses/{hash}/counters` (string field), and the
  zero-transaction verdict follows the scanned evidence, not the
  counter (a live run once read 0 while 250 txs were scanned).
- Blockscout reports `is_contract: True` for EIP-7702 delegated EOAs
  (vitalik.eth delegates to AmbireAccount7702). Address type now uses
  `eth_getCode`: empty = EOA, `0xef01…` (46 or 48 hex chars observed
  live) = delegated EOA, anything else = contract; Blockscout is the
  fallback when the RPC fails.
- `common/bitcoin.py` `btc_address_txs(after=…)` built
  `/txs/:last_seen_txid`, a route that does not exist on mempool.space
  or Esplora (both 404 "endpoint does not exist") — fixed to
  `/txs/chain/:last_seen_txid`. This also repairs BitScope's address
  load-more: verified live, page 1 200 (50 items) → page 2 200
  (25 items).
- `_classify` called async `_resolve_evm_name` without awaiting it
  (ENS names and invalid addresses 500'd with `'coroutine' object has
  no attribute 'lower'`) — `_classify` is now async.
- The frozen-counterparty probe flagged the USDC contract itself as a
  frozen counterparty (it is the `to` of every `transfer()` call, and
  Circle blacklists it). Checked token contracts are now excluded
  from the counterparty set.
- Hot contracts sit under a permanent pile of pending txs (all 250
  scanned USDC-contract txs are `result: pending`, no timestamp) —
  recency now says so explicitly instead of dropping the line, and
  uses the latest *confirmed* tx when one is in the scanned pages.

### Honest limits

- A pass is not proof the recipient is clean: the risk walk covers
  only the loaded transactions, the SDN list is dated 2026-11-05, and
  the issuer blacklist is not enumerable on-chain (recipient + 10
  recent counterparties probed only).
- Stablecoin symbols resolve to Ethereum-mainnet contracts only; on
  another EVM chain pass the 0x contract address. USDD is Tron-native
  — its EVM balance/freeze checks do not apply. TUSD freeze status is
  honestly unknown (write-only blacklist, no on-chain read).
- First-seen is exact only when the scanned pages cover the whole
  history (small tx counts); for high-activity addresses it is
  reported unknown with the reason, never guessed.
- Solana freeze covers the mint's freeze authority + the recipient's
  token-account state only; counterparty freeze is not checked on
  Solana. Bitcoin has no issuer freeze.
- Scores are a heuristic screening signal, not a legal determination.

## Phase 2, Q2 — Accounting export, USD-at-time + FIFO cost basis (2026-11-06)

### What was built

- `common/accounting.py` (new): `accounting_export(address)` + CSV
  renderer. `GET /api/walletscope/accounting/export?address=&format=csv`
  (JSON variant `format=json`), wired in `common/factory.py`
  `create_wallet_app`; listed in `/docs` (14 endpoints) via a
  `DOCS_GLOBAL` entry in `common/explorer.py`. Accepts EVM, Solana and
  Bitcoin addresses (400 otherwise; missing address / bad format → 400).
- FIFO reuse, not re-implementation: the PnL loop in `common/wallet.py`
  was extracted verbatim into `_fifo_run(transfers, daily)` (same
  7-day lookback, same unpriced-skip, same matched-only proceeds/cost,
  plus a per-disposal lot breakdown and lots-remaining) and `_wallet_pnl`
  now calls it. The export FIFO's every (chain, coin) group — native
  coins included — through that same engine.
- Pricing: one CoinGecko daily-close series per distinct coin, fetched
  once for the whole export window (native via `_daily_native_prices`,
  ERC-20/SPL via `_cg_contract_daily_prices`, BTC via one
  `market_chart` call), then priced per coin-day from the series.
  Live-price fallback is labelled `estimate-current`; no price at all
  → null USD fields, never invented. 5-min cache, like PnL.
- Journal columns (documented in the JSON `column_mapping`, the UI card
  and `walletscope/README.md`): receive → Debit `Crypto
  Assets:{COIN}` / Credit `Other Income:Crypto Received` (=
  amount_usd); send → Debit `Cost of Goods Sold:Crypto Disposed` /
  Credit `Crypto Assets:{COIN}` (= cost_basis_usd); realized gain per
  send row for separate posting; memo carries the tx hash. Every
  journal row balances (debit_usd == credit_usd) or is empty.
- UI: "Accounting Export" card on the wallet dashboard (`#/wallet/…`)
  with direct CSV / JSON download links, the journal mapping, the
  sampled-window note, and a link to the single-chain explorer CSV on
  the address page (wallet-only page, like Quote / Pre-flight).

### Verified live (uvicorn walletscope :8035, 2026-11-06)

- EVM test address `0x7C2080F3fC32Be38193262144AC98302f3b6b9A6`
  (picked from live data: 11 Ethereum txs, 14 token transfers):
  `format=csv` → 200, 11,146 bytes, 26 rows × 21 columns, parses clean
  with Python `csv` (0 unbalanced journal rows); `format=json` → 200,
  26 rows, 18 receives / 8 sends, total received $367.43, total sent
  $366.62, total fees $0.18, total cost basis $366.63, total realized
  gain −$0.01. 22 rows `at-time`, 0 `estimate-current`, 4 `unavailable`
  (WATTOIN + 3 Optimism spam tokens — amounts kept, USD null).
  Chains: ethereum + optimism; Sepolia 0 rows; Gnosis errored both runs
  (sandbox egress-proxy disconnects / 403 — reported in `errors`, the
  export still succeeds with the other chains).
- FIFO spot-checks (real rows): 2026-09-06 USDC send of 215 consumed
  `15 @ $1.0000 (acq 2026-08-26); 100 @ $1.0000 (acq 2026-08-27);
  100 @ $1.0000 (acq 2026-08-27)` — oldest-first, cost $215.00,
  gain −$0.01. 2026-10-05 ETH send of 0.0002995 consumed two lots
  (0.00001949 @ $2,507.87 acq 2026-09-06 + 0.00028 @ $2,691.99 acq
  2026-09-25). The address then transacted live mid-verification: a
  2026-11-06 50-USDT send consumed both 25-lots from 2026-10-27
  (`25 @ $0.9989` × 2, cost $49.94, gain +$0.01) — the 24→26 row
  growth between runs is that new on-chain activity, not a bug.
- Fee attribution: only sends carry `fee_usd` (3 ETH sends: $0.04 /
  $0.10 / $0.04, total $0.18); receives carry no fee (the sender paid
  it). Token-transfer rows carry no fee (Blockscout's token-transfer
  endpoint does not return it) — stated in the notes.
- Solana `vines1vzrYbzLMRdu58ou5XTby4qAqVRLmqo36NKPTg` → 200, 9 rows,
  all SOL receives, all `at-time` (e.g. 2026-03-19: 0.023737364 SOL,
  $2.14; 2026-03-06: 0.015 SOL, $1.33), total received $3.56, 0 errors
  after the EVM-fanout guard (see bugs below).
- Bitcoin `1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa` (genesis address) →
  200, 25 rows (first page), all BTC receives, all `at-time` (e.g.
  2026-11-06: 0.00000546 BTC, $0.41), total received $33.13,
  total fees $0.00 (receives carry no fee), 0 errors.
- PnL regression (same server, vitalik.eth): realized $0.00,
  proceeds $0.00, cost $0.00, priced 0, unpriced 67, unmatched 0 —
  identical to the pre-refactor baseline recorded before `_fifo_run`
  was extracted.
- Error handling: no `address` → 400; `address=not-an-address` → 400;
  `format=xml` → 400. `/docs` lists `/accounting/export`.
  Second JSON call served from cache in 0.004s.
- Static: served `/app.js` contains the Accounting Export card
  ("Download CSV" link); `node --check` passes on `frontend/app.js`;
  `python3 scripts/sync-common.py check` → "in sync: 16 apps".

### Bugs found live and fixed during verification

- Fee on receives: native rows initially carried the transaction fee
  even when the address was the recipient (the sender paid it).
  Fees are now attached only where the address paid them (EVM sender,
  Solana fee-payer signer, BTC send rows).
- Solana addresses fanned out to the EVM collectors: Blockscout 422s
  on a base58 address, so every EVM chain errored for Solana wallets.
  EVM collection is now gated on the EVM address regex (same guard as
  `_wallet_activity`); Solana errors are empty.
- SOL amounts printed float artifacts (`0.023737364000000004`) because
  `sol_tx` balance changes arrive as floats. SOL/SPL amounts are now
  quantized to lamport precision (9dp) before formatting.
- Bitcoin fee on receives: the genesis-address export initially
  reported $18.49 of fees on receive rows. BTC fees are now reported
  only on sends, and the notes say the fee is the whole transaction's
  fee (the address's share is unknown when several senders fund one
  transaction).

### Honest limits

- Sampled window: first page per chain (50 native txs + 50 token
  transfers per EVM chain, 10 Solana signatures, 25 Bitcoin txs).
  Older history is NOT fetched — the export is complete only for
  addresses whose whole history fits the window.
- `estimate-current` rows are priced at today's live price, not at
  transaction time; for old transactions that is a weak estimate, and
  their FIFO lots inherit it (flagged per row).
- Unpriced rows (unlisted/spam tokens, Sepolia ETH) keep their amount
  with null USD fields; their FIFO lots are unpriced and unmatched
  disposals get null cost basis, never guessed.
- Token-transfer rows carry no fee_usd. Solana counterparties are
  best-effort (largest opposite balance change). Bitcoin rows are net
  flow per transaction.
- This is an estimate, not tax advice: transfers to exchanges, gifts
  and internal moves are all treated as disposals.
- UI rendering was NOT verified in a live browser (no browser in this
  session): the card markup, download-link construction and
  `node --check` were verified; the rendered page still needs one
  browser pass.

## Phase 3, item 7 — Attestation monitor API (2026-11-06)

### What was built

- `GET /api/overview/attestations` (new `common/attestation_monitor.py`,
  wired in `common/factory.py` `create_overview_app`): all 8 coins'
  attestation status in one call. One gather of `sc_reserves` per coin;
  the feature-3 tracker embedded verbatim per coin, headline fields
  copied from it (never recomputed). Coins ordered overdue first.
  Response carries `summary` (counts + by-status symbol lists),
  `history` (current-status-only, see limits), `methodology`, `limits`,
  `note`, `as_of`. Cached 300s.
- Home decision: overview-only. No per-scope endpoint was added — the
  per-scope tracker already exists (`GET /api/<scope>/reserves` ->
  `attestation_tracker`, untouched), mirroring item 6's call.
- Docs: `_OV_DOCS` entry in `common/overview.py` with `paid_api: true`,
  a real `example_response` (live response, FDUSD + USDC rows in full),
  `free_tier` (keyless today, ~9s cold call) and `paid_tier` metering
  plan (stored history, status-change webhooks, export). Listed in the
  Paid API section of `#/api-docs`.
- UI: `#/attestations` dedicated page + "Stablecoin Attestations" home
  card in `overview/frontend/app.js` (shared table renderer; overdue
  flags, reserve ratios, per-coin source links to the latest report
  and to each scope's `/reserves` API). No scope reserves page exists
  in the canonical scope SPA to link from (verified: no reserves
  rendering in `frontend/app.js`), so the overview page links out to
  the per-scope reserves APIs instead.
- Nothing pushed. `python3 scripts/sync-common.py check` →
  "in sync: 16 apps match canonical common/ + frontend/".

### Verified live (uvicorn overview :8060, 2026-11-06)

- `GET /api/overview/attestations` → 200 in 8.3s cold (8.7s first
  run), 8 coins included, 0 missing. Order: USDC, FDUSD, PYUSD
  (overdue), USDT (current), TUSD (live), DAI, USDD (on-chain), USDe
  (no attestation). Summary counts: overdue 3, current 1, live 1,
  on_chain 2, no_attestation 1, due_soon 0.
- FDUSD: overdue by 223 days — last attestation 2026-02-28, AOGB CPA
  Limited, monthly, next expected 2026-03-28, days since 251,
  reserve-to-supply 1.345x (dated Feb reserves vs current supply),
  reserve-to-outstanding null (no outstanding stated).
- USDC: September attestation confirmed — Deloitte, last 2026-09-30,
  monthly, next expected 2026-10-30, overdue by 7 days (37 days
  since), reserve-to-supply 1.0485x, reserve-to-outstanding 1.0011x
  (dated $73,956,687,701 reserves as of 2026-09-30).
- PYUSD: also overdue by 7 days (KPMG LLP, last 2026-09-30, next
  2026-10-30), r2s 1.0368x, r2o 1.0009x. USDT: current (BDO,
  quarterly, last 2026-09-30, next 2026-12-30), r2s 1.0804x,
  r2o 1.0435x. TUSD live (The Network Firm, real-time feed);
  DAI on-chain (no auditor); USDD on-chain (SlowMist, contract
  auditor, not reserve attestor); USDe no attestation (Quantstamp,
  contract auditor). All four carry null reserve ratios, as designed.
- `GET /api/overview/docs` → 200, 19 endpoints; the attestations
  entry carries `paid_api: true`, a real `example_response` (FDUSD
  overdue_by 223, USDC Deloitte 2026-09-30), `free_tier` and
  `paid_tier`. Paid-API section now has 2 entries.
- Static: `/`, `/app.js` (74,578 bytes, contains
  `attestationMonitorPage` / `attestationsTableHtml` / `#/attestations`),
  `/config.js`, `/styles.css` all 200. `node --check` passes on
  `overview/frontend/app.js`.
- Second attestations call served from cache (PYUSD r2s moved
  1.0348x → 1.0368x between the two cold runs as live supply moved —
  the ratio is live-computed, not frozen).

### Honest limits

- Current-status-only: no auditor-change history exists in the repo
  (the tracker holds current status only); past auditors appear solely
  as prose in a coin's note (FDUSD Prescient/Prism/AOGB, PYUSD
  Withum-before-KPMG). No history is invented; a stored history is a
  planned paid-tier feature.
- "Overdue" means past the next reporting date per cadence, not past a
  publication deadline — USDC/PYUSD are 7 days overdue only because
  their October reports are not yet verified, and issuers typically
  publish weeks after the reporting date.
- Reserve figures are dated published figures from the named
  attestation only, never live/estimated; the reserve-to-supply ratio
  divides that dated total by current DeFiLlama supply (both dates
  stated in the tracker).
- UI rendering was NOT verified in a live browser (no browser in this
  session): the table markup, route and `node --check` were verified;
  the rendered page still needs one browser pass.

## Phase 3, item 13 — Plain-English transaction API (2026-11-06)

### What was built

- New dedicated endpoint `GET /api/walletscope/explain?chain=&hash=`
  (a productized wrapper, NOT a rename): `common/explain_api.py`
  reuses `common/explain.py` wholesale (EVM / Solana / Bitcoin
  explainers, feature 3 — local canonical signature map + curated
  4byte.directory fallback, "What happened" sentences, token flows)
  and only adds the cross-chain dispatch on `?chain=` (ethereum /
  sepolia / optimism / gnosis / solana / bitcoin — the same
  SETTLE_CHAINS as the settlement tracker) plus the contract fields
  (`explain`, `hash`, `chain_label`, `api_url`, `method`, `limits`).
  Response cached 60s server-side. No method decoding or sentence
  building was re-implemented; zero new dependencies, all keyless.
- One rename vs the per-scope payload: the explainer's `method` (the
  decoded text signature, e.g. `transfer(address,uint256)`) moves to
  `method_signature` in this contract, because `method` carries the
  contract method description here, as in every other phase-3
  wrapper. The per-scope `GET /api/<scope>/tx/{h}/explain`
  endpoints are untouched and keep `method` as the signature
  (verified unchanged below).
- Docs: one DOCS_GLOBAL entry in `common/explorer.py` with
  `paid_api: true`, the real live swap response as
  `example_response`, `free_tier` (keyless today) and `paid_tier`
  (metered per explanation, batch, larger signature database).
  Walletscope `/docs` now lists 9 paid-API entries; the `#/api-docs`
  hub groups it under "Paid API" with example response + tiers
  rendered (existing hub code, reused).
- UI: `#/explain` demo page in walletscope (`explainPage` in the
  shared `frontend/app.js`, routes in both router tables with the
  explorer→walletscope redirect). The result reuses the existing
  `explainCardHtml` — the same "What happened" card the tx pages
  show — extended with an optional `{chain}` so flow-row address
  links point at the right Scope app (`linkAddrC`) and native flows
  are labelled with that chain's native coin (`chainNativeSym`);
  without it the card renders exactly as on tx pages. The page adds
  an API contract card (method, limits, raw-JSON link, explorer
  link, docs link) and 7 Try links. The tx page gained a
  "Plain-English → Explain this transaction" detail row (next to
  Settlement / Screen transaction) and the wallet landing's tool
  list gained an "Explain a transaction" link.

### Verified live (uvicorn walletscope :8131, ethscope :8132, solscope :8134, bitscope :8135, 2026-11-06)

| Check | Result |
|---|---|
| Uniswap swap `GET /api/walletscope/explain?chain=ethereum&hash=0xf992b18e…954d1f37` | **swap** — "Swapped 0.03 ETH for 60460.570297 Gato via 0x7a250d56...59F2488D." Identical sentence to the feature-3 verification; `method_signature` `swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)` (local), 4 flows. |
| Plain transfer `…&hash=0x88a0447f…54fb6c2e` (block 26359307) | **native_transfer** — "Sent 0.00185 ETH from 0xde1C31ef...D44bfe78 to 0xeF005Ad9...e36a74b4." Raw tx confirms value_eth 0.00185, block 26359307. |
| ERC-20 transfer `…&hash=0x19d0c4b4…fdbb01` | **erc20_transfer** — "Sent 548 USDT from 0x6E8b145B...DCD2F15c to 0x6872b663...e13Df7dE." Method `transfer(address,uint256)` (local). |
| Approval `…&hash=0x5fc91846…c71cba023a` | **approval** — "Approved 0x1231deb6...486f4eae to spend 20 USDC on behalf of 0x9043AB6a...EFFFae0e." Method `approve(address,uint256)` (local). |
| Unknown method `…&hash=0xb37711ba…034c390a` | **unknown** — "Contract interaction with 0xFEEEEEE4...de0a7ffC — method 0x6aee6bdf is not in the signature database, so what it does cannot be described here." No invented description. |
| Solana `…?chain=solana&hash=soGqMCJj…nddLS3WF` | **contract_call** — "Called unknown program dijk…1mBu. Called unknown program dijk…1mBu." (two identical instructions; inherited explainer wording, identical on the per-scope endpoint). |
| Bitcoin coinbase `…?chain=bitcoin&hash=45a07546…a6ba606` | **coinbase** — "Miner reward: created 3.14971558 BTC (block subsidy + fees) paid to 1K6KoYC69NnafWJ7YgtrpwJxBLiijWqwa6." 2 flows. |
| Error cases | Missing hash → 400 "chain and hash are required"; unknown chain `tron` → 400 with the expected-chain list; malformed hash → 400; unknown tx `0x000…0` → 404 "Transaction not found" (passthrough); bad Solana sig → 400. |
| Per-scope endpoints unchanged | `GET /api/ethscope/tx/0xf992b18e…/explain` → same swap sentence, `method` still the text signature. `GET /api/solscope/tx/<sig>/explain` → identical Solana sentence. |
| Docs | `GET /api/walletscope/docs` → 200, 29 endpoints, 9 `paid_api` entries including `/api/walletscope/explain` with the real example response + free/paid tiers. |
| Static | `/`, `/app.js` (300,365 bytes, contains `explainPage` + all 7 Try-link hashes), `/styles.css`, `/config.js`, `/labels.json` all 200 on walletscope. `node --check` passes on `frontend/app.js`. |
| Sync | `scripts/sync-common.py check` passes ("in sync: 16 apps"). |

### Bugs / observations found live during verification

- A first plain-transfer candidate (`0xc61417c9…`, "Sent 0.0000001
  ETH") was visible in the latest-txs feed but 404'd on Blockscout
  minutes later: it was a pending transaction that got dropped /
  never mined. The feed mixes pending and confirmed txs; the
  recorded example above (`0x88a0447f…`, block 26359307) was
  confirmed via the raw tx before recording. Not a code bug — the
  endpoint correctly surfaces the upstream 404.
- Blockscout intermittently 404'd two other feed txs for a few
  minutes mid-run, then served them again (the unknown-method tx
  404'd via the app, then returned 200 direct from Blockscout on
  retry). Upstream flakiness, surfaced honestly as 404, not masked.
- The original feature-3 example hashes survive only truncated in
  this file (`0xeff2196a…682e83`, `0xb5b6fcab…52d801`,
  `0x0d27868c…ee803f5`), so the plain-transfer and ERC-20 examples
  above are fresh live equivalents with the same shapes, not
  re-verifications of the same hashes. The swap hash is the same
  one and re-verifies identically.

### Honest limits

- A method the signature database does not know is reported
  honestly ("not in the signature database") — never invented; the
  4byte fallback accepts only curated KNOWN_NAMES, so colliding junk
  is never trusted.
- Multicall inner calls are not decoded and proxy / meta-tx
  internals are not decoded (`execute` shows only the forwarded-to
  address + wei) — the sentence says so rather than guessing.
- Swap amounts come from token transfers (actual movements), never
  from minimum-out arguments; a token → native swap cannot show the
  exact native amount (internal transfer) and the sentence says so.
- Only the first page of token transfers is shown when a tx has
  more; the note says so.
- Solana token symbols are not resolved (SPL mints shown
  shortened); SOL balance changes include the fee payer's fee.
  Bitcoin change outputs cannot be identified — every output is
  listed.
- UI rendering was NOT verified in a live browser (no browser in this
  session): the page markup, route, served `app.js` and the API
  behind every Try link were verified; the rendered `#/explain`
  page still needs one browser pass.
