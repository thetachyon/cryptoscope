# Cryptoscope

Home for Tejas's crypto tooling — explorers, forensics, and analytics, each in its own directory, each independently deployable.

## Apps

| App | What it is | Vercel root directory |
|---|---|---|
| [overview](./overview) | **Cross-chain analytics landing page** — global market, per-chain comparison, stablecoin peg table, Wormhole bridge flows, top movers, and one search box that routes to the right Scope. Every Scope links back here. | `overview` |
| [ethscope](./ethscope) | Etherscan-style Ethereum mainnet explorer (search, blocks, transactions, addresses, tokens, contracts, NFTs, charts, gas). | `ethscope` |
| [sepoliascope](./sepoliascope) | Same explorer for Sepolia. | `sepoliascope` |
| [optimismscope](./optimismscope) | Same explorer for Optimism. | `optimismscope` |
| [gnosisscope](./gnosisscope) | Same explorer for Gnosis. | `gnosisscope` |
| [walletscope](./walletscope) | Cross-chain wallet dashboard (portfolio, history, PnL estimate, NFTs, activity) across all four chains. | `walletscope` |
| [solscope](./solscope) | Full Solana explorer (slots, transactions, accounts, SPL tokens, programs). | `solscope` |
| [bitscope](./bitscope) | Bitcoin explorer (blocks, transactions, addresses, mempool, fees). | `bitscope` |
| [usdtscope](./usdtscope) | Tether USD dashboard (peg, supply, holders, transfers, depegs, reserves). | `usdtscope` |
| [usdcscope](./usdcscope) | USD Coin dashboard (same). | `usdcscope` |
| [daiscope](./daiscope) | Dai dashboard (same). | `daiscope` |
| [usdescope](./usdescope) | Ethena USDe dashboard (same). | `usdescope` |
| [fdusdsscope](./fdusdsscope) | First Digital USD dashboard (same). | `fdusdsscope` |
| [pyusdsscope](./pyusdsscope) | PayPal USD dashboard (same). | `pyusdsscope` |
| [tusdscope](./tusdscope) | TrueUSD dashboard (same). | `tusdscope` |
| [usddscope](./usddscope) | Decentralized USD dashboard (same; Tron-native via TronScan). | `usddscope` |

Every app is keyless — data via the public Blockscout v2 API, public JSON-RPC endpoints, the public Solana RPC, mempool.space / Blockstream Esplora, DeFiLlama's stablecoins API, TronScan/TronGrid, and CoinGecko's keyless tier. No API keys anywhere.

## Feature 1 — Risk scores on addresses

Every address page (all four EVM scopes, SolScope, BitScope) shows a
Risk card: score 0–100, level (low / elevated / high / critical), and
human-readable reasons — never a bare number. The API is
`GET /api/<scope>/address/<addr>/risk` (see each app's `/docs`).

- **Label set:** `common/risk.py` vendors 514 OFAC SDN crypto
  addresses (U.S. Treasury SDN list `sdn.csv`, downloaded 2026-11-05;
  521 mentions, 514 unique). 94 EVM-format, 260 Bitcoin, 2 Solana.
  No live Cryptanalysis call (its Vercel deployment is SSO-gated) and
  no API key — the scoring is self-contained.
- **Scoring:** direct SDN match = 100/critical; N-hop proximity to a
  sanctioned address via a bounded funder walk = hop 1: 80 (high),
  hop 2: 60, hop 3: 40 (elevated); sanctioned-mixer interaction
  (Blender.io / Sinbad — the mixers that remain on the SDN list, 13
  addresses) = hop 1: 70, hop 2: 50, hop 3: 40 (always elevated).
  Nothing found = 0/low, with the coverage stated.
- **Funder walk limits (stated in the API response and the UI):** the
  walk runs only over the transactions the address page already loads
  — EVM: first tx page (up to 50) + one page each for up to 10
  first-hop and 5 second-hop counterparties, 3 hops max; Bitcoin: 25
  most recent txs, same 10/5 budgets, 3 hops max; Solana: 1 hop only,
  counterparties from the 10 most recent transactions. A low score is
  not proof an address is clean.
- **Per-chain honesty:** Solana scoring is mainly a direct match
  against the 2 SDN Solana addresses plus the 1-hop check (which can
  only match those same 2 addresses); Bitcoin covers the 260 SDN BTC
  addresses. Tornado Cash is NOT in the set — OFAC removed it from the
  SDN list in March 2025. Scores are a heuristic screening signal,
  not a legal determination.

## Feature 2 — Security scanner on tokens

Every token page (all four EVM scopes, SolScope) shows a Security
card: per-check pass / warn / fail / not-detectable badges plus an
overall verdict (low / elevated / high) with explicit reasons. Token
contract address pages show the same card. The API is
`GET /api/<scope>/token/<addr>/security` (see each app's `/docs`).
Logic lives in `common/security.py`; UI in `frontend/app.js` (EVM)
and the `solscope/frontend/` overlay (Solana).

- **EVM checks:** (a) Owner powers — function names from the verified
  ABI (proxy implementation ABIs merged in) + a live `owner()`
  eth_call; renounced or no-owner = pass, live owner with mint /
  pause / blacklist / upgrade powers = warn (never a fail on its
  own). (b) Holder concentration — top-10 holders' share of supply
  from Blockscout holders (first 50) — pass ≤ 50%, warn (50, 80],
  fail > 80%. (c) Buy/sell tax — read live only if the ABI exposes a
  standard tax function; otherwise "not detectable", never an
  invented 0%. (d) Honeypot simulation — read-only eth_call, no
  transaction sent: buy via the Uniswap-V2 router (0.01 ETH, only
  when the V2 factory itself returns a pair for the token) + sell as
  a transfer of 1 token from the largest holder to the pair; an
  execution revert on either leg = fail, anything unrunnable =
  "simulation unavailable" with the reason. (e) Liquidity —
  DexScreener-tracked pairs on the chain (total USD + top pair) plus
  on-chain V2 reserves and the share of LP tokens held by the dead
  address where a V2 pair exists — pass ≥ $1M tracked, warn
  $10k–$1M, fail < $10k with pairs present, "not determinable" when
  no liquidity source answers.
- **Scoring:** fail = 3 points, warn = 1, pass / unknown / n/a = 0.
  Any fail → high; ≥ 2 points → elevated; otherwise low.
- **Solana checks:** only what is real there — mint authority and
  freeze authority from the mint account, plus holder concentration
  from the largest-accounts list. Owner powers, tax, honeypot
  simulation and DEX liquidity are listed as EVM-only (n/a) and are
  never pretended to have run.
- **Honest limits (stated in the API response and the UI):** a low
  verdict is not proof a token is safe — tax can hide in transfer
  logic with no standard tax function, ownership can sit behind a
  proxy or multisig the scan cannot see through, a passing eth_call
  simulation does not guarantee a real swap succeeds, and liquidity
  totals cover DexScreener-tracked pairs on the chain only. A single
  transient Blockscout failure on the holders call is retried once
  (2s) before the concentration check and the sell simulation
  degrade to unknown. Verified live 2026-11-05: USDC on Ethereum →
  low (owner warn only; top-10 24.7%, tax not detectable, $10.7M
  tracked liquidity, both simulation legs succeeded); USDC.e on
  Optimism → elevated (owner warn + thin $594k tracked liquidity;
  buy leg unavailable — no V2 pair — sell leg passed); USDC on
  Solana → elevated (live mint + freeze authorities; holder data
  unavailable — public RPC 403 on getTokenLargestAccounts).

## Feature 3 — Plain-English transactions ("What happened")

Every transaction page (all four EVM scopes, SolScope, BitScope)
shows a "What happened" card at the top: one plain sentence plus a
compact token-flow summary (token, amount, from → to). The API is
`GET /api/<scope>/tx/<hash>/explain` (see each app's `/docs`).
Logic lives in `common/explain.py`; UI in `frontend/app.js` (EVM)
and the `solscope/frontend/` + `bitscope/frontend/` overlays.

- **EVM:** method names come from a local map of canonical
  signatures (selectors computed with the Keccak-256 in
  `common/abi.py`: transfer/transferFrom, approve, Uniswap V2/V3
  swaps, mint/burn, deposit/withdraw, add/remove liquidity, NFT
  transfers, multicall, …), then the keyless 4byte.directory public
  API — a 4byte candidate is accepted only if its function name is
  in a curated known-names set, because 4byte returns several
  colliding signatures per selector and the first result is often
  junk. Swap amounts come from the transaction's token transfers
  (actual movements), never from the call's minimum-out arguments.
  Covered: plain native transfer, ERC-20 transfer, approval
  (unlimited flagged), swap (token→token, ETH→token, token→ETH),
  NFT transfer, contract deployment, mint/burn, wrap/unwrap,
  liquidity add/remove, multicall (inner calls named, not decoded).
  An undecodable method is reported honestly — "method 0x… is not
  in the signature database" — never invented.
- **Solana:** sentences from the parsed instructions (System/SPL
  transfers with amounts, Jupiter/Raydium/Orca swaps named by
  program) plus SOL balance changes and SPL token changes as flows.
  Token symbols are not resolved — mints are shown shortened.
- **Bitcoin:** inputs → outputs in plain words; coinbase
  transactions are reported as miner rewards. Change outputs cannot
  be identified from chain data alone, so every output is listed.
- **Honest limits (stated in the API response and the UI):** a
  failed transaction is described as an attempt, not a completed
  action; multicall inner calls and proxy/meta-transaction
  internals are not decoded (the token flows still show what
  moved); V3 swap amounts come from the transfer logs, so a swap
  whose output is an internal ETH transfer states the output amount
  is not visible; token→ETH swaps likewise. Verified live
  2026-11-05/06: ETH transfer, USDC transfer (1,035), Uniswap swap
  (0.03 ETH → 60,460.57 Gato), USDC approval (59,995, unlimited
  check), unknown-method honesty, Optimism, Solana Jupiter swap +
  USDC SPL transfer, Bitcoin coinbase.

## Feature 4 — Cross-chain identity

Every address page (all four EVM scopes, SolScope, BitScope) shows a
"Cross-chain identity" card. The API is
`GET /api/<scope>/address/<addr>/crosschain` (see each app's `/docs`).
Logic lives in `common/crosschain.py`; UI in `frontend/app.js` (EVM)
and the `solscope/frontend/` + `bitscope/frontend/` overlays.

- **Same-EOA presence (EVM only):** one row per live EVM chain
  (Ethereum, Sepolia, Optimism, Gnosis) with native balance and
  latest activity, fetched in parallel from Blockscout
  (`/addresses/{addr}` + first page of `/addresses/{addr}/transactions`,
  6s timeout each). A chain that fails shows "unavailable" and never
  blocks the section; a chain that 404s is "no activity". Blockscout
  returns `transactions_count` as null for some addresses (vitalik.eth
  included), so the count is shown only when upstream provides it and
  latest activity is the primary activity signal.
- **Bridge linkage:** Wormhole transfers involving the address via the
  Wormholescan keyless API (`/api/v1/transactions?address=`, probed
  2026-11-05 — it answers 200 for EVM and Solana addresses and returns
  the transfers the address originated or received). Wormhole only —
  no all-bridge total exists keyless (DeFiLlama bridges is paywalled).
  Amounts are shown only when Wormholescan standardizes them;
  otherwise the transfer is listed with "amount not reported".
- **WalletScope link:** "Open full portfolio in WalletScope →" (EVM +
  Solana; WalletScope has no Bitcoin, so BitScope pages say so instead
  of linking).
- **Honest limits (stated in the API response and visibly in the UI,
  under the section heading):** EVM ↔ Solana ↔ Bitcoin linkage is
  shown ONLY when an actual bridge transfer is observed. There is no
  keyless way to prove two different-format addresses belong to the
  same person, and nothing here implies otherwise. The same-address
  rows are one account by construction (the same address on every EVM
  chain), not an identity claim. On SolScope/BitScope, EVM presence is
  not applicable (different address format) — those pages show bridge
  linkage only, where observed.
- **Wormholescan lookup limits (probed 2026-11-05):** the lookup
  matches the origin-tx sender and the destination address; it does
  NOT match an address that merely appears as a token sender inside a
  contract-initiated transfer (the Polygon sender of a transfer to
  vitalik.eth returns 0 results for its own lookup). Mayan Swift
  transfers standardize no sender (the origin-tx sender is a Mayan
  driver address), so no sender is shown for those. Batch transfers
  can carry no destination chain in the record — those render as
  "Unknown chain", never guessed. Verified live 2026-11-05: vitalik.eth
  — 5.780441042197210512 ETH / latest Nov 4, 2026; Sepolia
  61.723263527250847231 ETH / latest Nov 5, 2026; Optimism
  0.181079591875347535 ETH / latest Oct 19, 2026; Gnosis
  8.709546191240796693 xDAI / latest May 19, 2026; 3 Wormhole transfers
  (Optimism→Base, BNB Chain→Ethereum 1.53970797, Polygon→Ethereum 1).
  Screenshot: `~/workspace/cryptoscope-snapshots/crosschain-vitalik.png`.

## Layout

```
common/             Shared Python package — the single source of truth.
                    Chain registry, per-chain Blockscout clients, USD
                    pricing, formatting, Keccak/ABI codec, explorer core,
                    wallet aggregation, Solana/Bitcoin clients,
                    stablecoin dashboards (DeFiLlama + CoinGecko +
                    Blockscout/TronScan), address risk scoring (vendored
                    OFAC SDN labels + funder walk), token security
                    scanning, plain-English transaction explanations,
                    cross-chain identity (same-EOA presence + Wormhole
                    bridge linkage), thin-app factories.
frontend/           Shared SPA (index.html, app.js, styles.css,
                    labels.json). Per-app behaviour comes from each app's
                    generated static/config.js.
scripts/
  sync-common.py    Vendors common/ + frontend/ into every app and
                    generates each app's static/config.js.
                    `sync` (default) copies; `check` verifies the copies
                    match the canonical sources.
ethscope/           Ethereum-only explorer (thin: config.py + app.py)
sepoliascope/       Sepolia explorer (thin)
optimismscope/      Optimism explorer (thin)
gnosisscope/        Gnosis explorer (thin)
solscope/           Solana explorer (thin + frontend/ overlay)
bitscope/           Bitcoin explorer (thin + frontend/ overlay)
walletscope/        Cross-chain wallet dashboard (thin)
usdtscope/          Tether USD dashboard (thin + frontend/ overlay)
usdcscope/          USD Coin dashboard (thin + frontend/ overlay)
daiscope/           Dai dashboard (thin + frontend/ overlay)
usdescope/          Ethena USDe dashboard (thin + frontend/ overlay)
fdusdsscope/        First Digital USD dashboard (thin + frontend/ overlay)
pyusdsscope/        PayPal USD dashboard (thin + frontend/ overlay)
tusdscope/          TrueUSD dashboard (thin + frontend/ overlay)
usddscope/          Decentralized USD dashboard (thin + frontend/ overlay)
VERIFY.md           Split verification log (per-app smoke tests)
```

Each app is thin: a `config.py` (its single source of truth — chain, title, sibling Scope URLs), a ~15-line `app.py` entrypoint that builds the FastAPI app from `common/factory.py`, plus `api/index.py`, `vercel.json`, `requirements.txt`, and the generated `common/` + `static/` copies. All data logic lives in `common/`; all UI lives in `frontend/`.

## How common/ syncing works (vendoring)

Vercel builds run inside the app's root directory, so a deployed app cannot import a sibling `common/` at runtime. The fix is a build-time copy, with the committed copies as the guarantee:

1. **Canonical sources** are `common/` and `frontend/` at the repo root. Edit those, never the copies inside an app.
2. **`scripts/sync-common.py sync`** copies `common/` → `<app>/common/` and `frontend/` → `<app>/static/` for every app (any subdirectory with a `config.py`), and regenerates `<app>/static/config.js` from that app's `config.py`. It never touches anything else in an app.
3. **Each app's `vercel.json`** runs the same script as its `buildCommand`, so every deploy re-vendors the latest shared code even if a committed copy is stale.
4. **`scripts/sync-common.py check`** verifies every vendored copy (by SHA-256) and every generated `config.js` matches the canonical sources. Run it before pushing; it is the guard that the copies are in sync.
5. **If the build-time copy ever proves unworkable** (e.g. Vercel changes how `buildCommand` works), delete the `buildCommand` from the apps' `vercel.json` files — the committed copies keep deploys working, and `check` keeps them honest.

## Adding a new Scope

1. Create `<name>scope/` with a `config.py` (copy an existing one: `SCOPE`, `TITLE`, `BRAND_LETTER`, `CHAIN`, `SIBLINGS`), a thin `app.py` (copy `sepoliascope/app.py`, changing nothing except the docstring), plus `api/index.py`, `vercel.json`, `requirements.txt`, `.gitignore` (all copyable verbatim from any scope app).
2. Add the chain to `common/chains.py` (Blockscout host + public RPC + CoinGecko ids) — probe both hosts first; chains whose Blockscout host or RPC does not answer are dropped, not added.
3. Run `python3 scripts/sync-common.py sync`, boot the app, smoke-test it, and add it to every app's `SIBLINGS` in their `config.py` files (then re-sync).
4. Create a Vercel project with root directory `<name>scope`.

## Chains

| Chain | Blockscout host | Public RPC | Native |
|---|---|---|---|
| `ethereum` | `eth.blockscout.com` | `ethereum.publicnode.com` | ETH |
| `sepolia` | `eth-sepolia.blockscout.com` | `ethereum-sepolia.publicnode.com` | ETH |
| `optimism` | `optimism.blockscout.com` (301 → `explorer.optimism.io`) | `optimism.publicnode.com` | ETH |
| `gnosis` | `gnosis.blockscout.com` (301 → `gnosisscan.io`) | `gnosis.publicnode.com` | xDAI |

Dropped chains (do not re-add without re-probing): Arbitrum (`arbitrum.blockscout.com` connection fails), Base (`base.blockscout.com` HTTP 403), Polygon (`polygon.blockscout.com` HTTP 403 Cloudflare).

## Queue

Solana holdings into the wallet dashboard ✓ (SOL + SPL, keyless — see `walletscope/README.md`) → **SolScope** ✓ (full Solana explorer, own directory — see `solscope/README.md`) → **BitScope** ✓ (Bitcoin explorer via keyless mempool.space / Blockstream Esplora — see `bitscope/README.md`) → **stablecoin Scopes** ✓ (Top 8: USDT, USDC, DAI, USDe, FDUSD, PYUSD, TUSD, USDD — peg status, supply, holders, transfers, depeg history, reserves — see `VERIFY.md` and each `<coin>scope/README.md`).

## Deploy status

Vercel deploy is blocked: `create_git_project` returns 403 "Not authorized under scope thetachyon" — the Vercel GitHub App has no access to the private `cryptoscope` repo (or the MCP connection lost team scope). Fix: add `cryptoscope` to the Vercel GitHub App's repository access, or import each app in the Vercel dashboard with its root directory. Each app is independently deployable once that is fixed.
