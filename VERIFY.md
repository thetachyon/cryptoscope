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
