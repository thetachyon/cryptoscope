# EthScope — verification log (2026-11-03)

Server: `uvicorn app:app --port 8034` from `~/workspace/ethscope` (venv: fastapi 0.162.0, httpx 0.28.1, uvicorn 0.54.0). All tests against live Blockscout v2 (`https://eth.blockscout.com/api/v2`), Ethereum mainnet.

## Backend endpoints (curl, all HTTP 200 unless noted)

| Test | Result |
|---|---|
| `GET /api/ethscope/stats` | 200 — eth_price_usd 2286.44, latest_block 26336251, avg_block_time_s 12.0, total_transactions 3838068482, gas slow/avg/fast gwei |
| `GET /api/ethscope/blocks` | 200 — items with height/hash/miner (ENS, e.g. titanbuilder.eth)/tx_count/gas_used_pct/base_fee_gwei/burnt_fees_eth |
| `GET /api/ethscope/txs` | 200 — latest txs with status/value_eth/fee_eth |
| `GET /api/ethscope/block/26336566` | 200 — 293 txs, gas 77.82%, burnt 0.00204 ETH, rewards, withdrawals |
| `GET /api/ethscope/block/<hash>` (64-hex) | 200 — resolves by hash |
| `GET /api/ethscope/block/<n>/txs` and `/block/<hash>/txs` | 200 — 50 items/page, `next_page_params` present |
| `GET /api/ethscope/tx/0xd39973…066aa` (vitalik inbound) | 200 — status success, block 26336583 (via `block_number` fix), from/to with vitalik.eth label, value 0.00044226 ETH, fee, nonce 0 |
| `GET /api/ethscope/tx/0x9398c4…be7d8` (swap tx) | 200 — 10 embedded token transfers (WETH 2.353642), method 0xdd225095, 2314-char input, `input_utf8: null` (binary, correct) |
| `GET /api/ethscope/tx/<h>/logs` | 200 — 20 logs, 4 topics on first |
| `GET /api/ethscope/tx/<h>/internal` | 200 — items list |
| `GET /api/ethscope/tx/<h>/token-transfers` | 200 — 14 transfers |
| `GET /api/ethscope/tx/0xabab…` (nonexistent) | **404 `{"error":"Transaction not found"}`** — no traceback |
| `GET /api/ethscope/address/0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045` (vitalik.eth) | 200 — balance 5.780009758611001236 ETH, is_contract true, ens vitalik.eth |
| `GET /api/ethscope/address/<vitalik>/txs` | 200 — 50 items, next_page_params |
| `GET /api/ethscope/address/<vitalik>/token-transfers` | 200 — 50 items (e.g. PUNKSGEN from doom.eth) |
| `GET /api/ethscope/address/<small-addr>/internal` | 200 — works (vitalik's timed out upstream: slow query on hot address, correctly surfaced as 502 `{"error":"Blockscout unreachable (ReadTimeout)"}`) |
| `GET /api/ethscope/address/<small-addr>/tokens` | 200 — `{"items": [...]}` (upstream returns bare list; translated) |
| `GET /api/ethscope/token/0xA0b8…6eB48` (USDC) | 200 — USDC, 6 decimals, supply 46880273187.14, holders 9577679, price 0.999525 |
| `GET /api/ethscope/token/<usdc>/transfers` | 200 — 50 transfers with amounts (e.g. 249.985818 USDC) |
| `GET /api/ethscope/search?q=vitalik.eth` | 200 — address hit first, then tokens |
| `GET /api/ethscope/search?q=USDC` | 200 — USDC token first |

## Frontend

- `GET /` → 200 `text/html`, correct `<title>`; `GET /app.js` → 200 JS; `GET /styles.css` → 200 CSS.
- `node --check static/app.js` → **OK** (536 lines).
- JS reviewed for: hash routing (`#/`, `#/block/`, `#/tx/`, `#/address/`, `#/token/`), search classification (64-hex tx → falls back to block on 404; 40-hex address; digits block; else search API), 10s home polling with cleanup on route change, pager via `next_page_params`, copy-to-clipboard delegation, error banners. Not browser-driven (no live browser in this environment).

## Unit checks (python)

- `api/index.py` imports cleanly → `app` is a FastAPI instance with 22 routes (Vercel ASGI entry OK).
- `decode_input("0x48656c6c6f20576f726c64")` → `"Hello World"`; binary input → `None`; `"0x"` → `None`.
- `wei_to_eth("5780009758611001236")` → `5.780009758611001236`; `token_amount("46880273187141468","6")` → `46880273187.141468`.

## Issues found & fixed during verification

1. **httpx vs sandbox `NO_PROXY`**: env `NO_PROXY` contains bracketed IPv6 (`[::1]`) which httpx 0.28.1's env-proxy parser rejects (`InvalidURL: Invalid port: ':1]'`). Fixed in `app.py`: strip bracketed entries at import; egress proxy still used.
2. **Tx detail `block` null**: Blockscout tx-detail exposes `block_number`, not `block`. Backend now maps it, so the tx page shows the block link instead of "Pending".
3. **No `GET /blocks/{n}/transactions`** on this Blockscout instance (`{"message":"Not found"}`); block txs use `GET /transactions?block_number=` (hash resolved to height first). Documented in README.

## Not verified (out of scope for this environment)

- Live browser rendering of the SPA (no browser tooling here); JS is syntax-checked and logic-reviewed only.
- Vercel deployment (not requested — no deploy performed).


---

# EthScope — backend parity-gap verification (2026-11-05)

Server: `~/workspace/ethscope/venv/bin/uvicorn app:app --port 8035` from `~/workspace/cryptoscope/ethscope` (venv: fastapi 0.162.0, httpx 0.28.1, uvicorn 0.54.0). `app.py` grew 616 → 1,840 lines; no new dependencies (stdlib + fastapi + httpx). All tests against live Blockscout v2 / public RPCs / CoinGecko.

## Unit checks (python, before server tests)

- `keccak_256(b"")` = `c5d24601…85a470`, `keccak_256(b"hello")` starts `1c8aff95` — correct Keccak-256 (not NIST SHA3).
- Selectors: `name()` `06fdde03`, `decimals()` `313ce567`, `totalSupply()` `18160ddd`, `balanceOf(address)` `70a08231`, `transfer(address,uint256)` `a9059cbb` — all match the canonical values.
- Encode/decode round-trips pass for: `string`+`uint256`, `uint256[]`+`address`, `bool`+`bytes32`, negative `int256` (−5), and a real `decimals()` output (`…0006` → `"6"`).
- `api/index.py` imports cleanly → 67 routes (Vercel ASGI entry OK).

## Multichain

| Test | Result |
|---|---|
| `GET /api/ethscope/chains` | 200 — 4 chains with blockscout/rpc/coingecko_platform/native |
| `GET /api/ethscope/ethereum/stats` | 200 — ETH $2233.9, block 26350956, gas slow/avg/fast 0.28/0.73/2.48 |
| `GET /api/ethscope/sepolia/stats` | 200 — block 12071851, coin_price null (expected on Sepolia) |
| `GET /api/ethscope/optimism/stats` | 200 — block 159156728, gas 0.01/0.01/0.04 |
| `GET /api/ethscope/optimism/block/latest` | 200 — height 159156670, 19 txs, base fee 0.00 |
| `GET /api/ethscope/optimism/txs` | 200 — 50 items |
| `GET /api/ethscope/gnosis/stats` | 200 — block 49128080, native xDAI, price 1.0 |
| `GET /api/ethscope/gnosis/block/latest` | 200 — height 49128080, 39 txs |
| `GET /api/ethscope/arbitrum/stats` | **404 `{"error":"Unknown chain"}`** |
| Legacy `GET /api/ethscope/stats`, `/blocks`, `/txs`, `/token/<usdc>`, `/address/<vitalik>`, `/search?q=USDC`, `/block/latest` | all 200, unchanged shapes |

**Bug found & fixed during verification:** Optimism and Gnosis first returned `{"error":"Blockscout returned invalid JSON"}` — `optimism.blockscout.com` 301-redirects to `explorer.optimism.io` and `gnosis.blockscout.com` to `gnosisscan.io`, and httpx does not follow redirects by default. Fixed with `follow_redirects=True` on all httpx clients; all four chains then 200.

## Contracts

| Test | Result |
|---|---|
| `GET /api/ethscope/ethereum/contract/0xA0b8…6eB48` (USDC) | 200 — name FiatTokenProxy, verified true, compiler v0.4.24+commit.e67f0147, solidity, proxy eip1967_oz, impl FiatTokenV2_2, verified_at 2019-04-18, source 11,012 chars, ABI 9 entries, creation_tx `0xe7e0fe…75895` |
| `POST …/contract/<usdc>/call` `name()` | 200 — values `["USD Coin"]` |
| `POST …/call` `decimals()` | 200 — values `["6"]` |
| `POST …/call` `balanceOf(vitalik)` | 200 — values `["39192124"]` (raw units ≈ 39.19 USDC) |
| `POST …/call` nonexistent fn | **400 `{"error":"RPC error: execution reverted"}`** — no traceback |
| `POST …/call` bad arg `"notanaddress"` | **400 `{"error":"Not an address: 'notanaddress'"}`** |
| Legacy `POST /api/ethscope/contract/<usdc>/call` `decimals()` | 200 — `["6"]` |
| `GET …/contract/0x000…0001` (not a contract) | **404 `{"error":"Address is not a contract"}`** |
| `GET …/contract/<vitalik>` | 200 — vitalik.eth is now an EIP-7702 delegated account (proxy_type eip7702, implementation AmbireAccount7702); endpoint correctly reports it as a contract |
| `POST …/contract/0x000…dEaD/verify` dummy payload | 200 `{"ok": false, "message": "Address is not a smart-contract", "via": "v1", "upstream": {"message": "Address is not a smart-contract", "status": "0"}}` — v2 endpoint times out from this sandbox, v1 form endpoint answers keyless; the real Blockscout error is surfaced verbatim, not faked as success. First probe (before `codeformat` was added) got the equally real `"Missing codeformat field"` error. No real contract was verified. |

## Charts / gas tracker / pending / txcount

| Test | Result |
|---|---|
| `GET /api/ethscope/ethereum/charts/transactions` | 200 — 31 points, latest 2026-11-04: 1,989,265 txs |
| `GET /api/ethscope/ethereum/charts/market` | 200 — 30 points, available_supply 122,192,071.86 |
| `GET /api/ethscope/ethereum/charts/gas` | 200 — 50 items, latest block 26351269 base fee 0.27 gwei |
| `GET /api/ethscope/ethereum/charts/price?days=7` | 200 — coin ethereum, 169 points, last $2,235.43 |
| `GET /api/ethscope/gnosis/charts/price?days=7` | 200 — coin xdai, 169 points |
| `GET /api/ethscope/ethereum/gastracker` | 200 — gas 0.33/0.79/2.52, base fee 0.27, 20-block history, avg block 12.0s |
| `GET /api/ethscope/optimism/gastracker` | 200 — gas 0.01/0.01/0.02, 20-block history |
| `GET /api/ethscope/sepolia/charts/gas` | 200 — 50 items |
| `GET /api/ethscope/ethereum/pending` | 200 — sampled true, 50 items, first `0x8391ae…0800f55` |
| `GET /api/ethscope/gnosis/pending` | 200 — sampled true, 50 items |
| `GET /api/ethscope/ethereum/address/<vitalik>/txcount` | 200 — `tx_count: 5970` (matches `eth_getTransactionCount` → `0x1752` probed directly) |
| `GET /api/ethscope/optimism/address/0x4200…0042/txcount` | 200 — `tx_count: 1` |

## NFT

| Test | Result |
|---|---|
| `GET /api/ethscope/ethereum/token/0xED5A…544/instances` (Azuki) | 200 — 50 items, first id 9999 with image_url |
| `GET …/token/<azuki>/instance/1` | 200 — id 1, dweb.link image, 7 attributes (Type Human, Hair Pink Hairband, …), owner `0xC8967D…756a2` |
| `GET …/token/<azuki>/instance/1/transfers` | 200 — 3 items |

## USD holdings

| Test | Result |
|---|---|
| `GET /api/ethscope/ethereum/address/0x39977B…88ee/tokens` (USDC holder) | 200 — 9 items, `total_usd: 246184.02`; USDC entry: balance 150785.309997, `usd_price: 0.999632` (Blockscout exchange_rate), `usd_value: 150729.82` |
| `GET …/address/0xC8967D…756a2/tokens` | 200 — 40 items, `total_usd: 0.0`; spam ERC-20s correctly null USD (no exchange_rate, no CoinGecko price); token `address` now populated (see bug below) |
| `GET …/address/<vitalik>/tokens` | **502 `{"error":"Blockscout unreachable (ReadTimeout)"}`** — upstream slow on the hot address; pre-existing behaviour, honestly surfaced |

**Bug found & fixed during verification:** token-balances items expose the token address as `address_hash` (not `address`) and DO carry `exchange_rate` — the first implementation read `token.address` (always null) and skipped USD enrichment. Fixed to use `address_hash` + `exchange_rate`-first with CoinGecko fallback.

**CoinGecko note:** `simple/token_price` keyless tier allows only ONE contract address per request (error 10012 on a 2-address batch, probed 2026-11-05) — the backend does one cached call per token, max 5 per tokens-endpoint call.

## Docs / labels

| Test | Result |
|---|---|
| `GET /api/ethscope/docs` | 200 — 62 endpoint entries (30 base × legacy + chain-prefixed, plus /chains and /docs) |
| `GET /labels.json` | 200 — 14 labels, 12 verified |
| Label spot-checks via the app | Uniswap V2 Factory corrected address `0x5C69…9cc5aA6f` → contract true, name UniswapV2Factory (the address in the task brief, `…CB7eB5a4`, is 38 hex chars and Blockscout 422s it; corrected from Uniswap's official deployments docs + Etherscan, noted in labels.json). Uniswap V2 Router `0x7a25…488D` → Blockscout lookups timed out 3× (hot address); kept as verified via Uniswap's official deployments docs, noted in labels.json. |

## Not verified (out of scope for this environment)

- Live browser rendering of the SPA (no browser tooling here); frontend is unchanged and still mainnet-only — new backend features (contracts, charts, gas tracker, NFT, pending, multichain) have no frontend pages yet.
- Vercel deployment (not requested — no deploy performed).
- Real contract verification submission (only dummy payloads; the forwarding path is proven by real upstream validation errors).


---

# EthScope — frontend parity-gap verification (2026-11-05)

Server: `~/workspace/ethscope/venv/bin/uvicorn app:app --port 8035` from `~/workspace/cryptoscope/ethscope` (venv: fastapi 0.162.0, httpx 0.28.1, uvicorn 0.54.0). `app.py` untouched (read-only for this work). Frontend only: `static/app.js` 536 → 1,451 lines, `static/index.html` (chain switcher + nav/footer links), `static/styles.css` (+~5 KB: chain select, label pills, gas cards, charts, NFT grid, contract forms, API docs).

## Static checks

| Test | Result |
|---|---|
| `node --check static/app.js` | **OK** |
| CSS braces/parens | 157/157, 103/103 — balanced |
| `GET /`, `/app.js`, `/styles.css`, `/labels.json` | all 200 |
| Served `/` contains `chain-select`, `#/charts`, `#/gastracker`, `#/pending`, `#/api-docs` | yes (nav + footer) |
| Grep audit: every data `get()` call routes through chain-prefixed `get()` (`API + "/" + CHAIN + path`) | **clean** — no unprefixed legacy path remains. Only unprefixed fetches: `getGlobal("/chains")`, `getGlobal("/docs")` (global endpoints) and `fetch("/labels.json")` (static file) |
| `postJson` call sites | only `/contract/{addr}/call` and `/contract/{addr}/verify`, both chain-prefixed |
| Router review (read-through) | 11 routes; `#/token/{addr}/instance/{id}` precedes `#/token/{addr}`; all six new routes present; unknown hash → not-found card. Poll-timer leak on route change during an in-flight refresh fixed with a hash guard in `home()` and `pendingPage()` |

## JS unit tests (Node, `/tmp/test-ethscope-js.js`, 17 assertions, exit 0)

- Keccak-256 matches the backend's verified Python Keccak exactly: empty → `c5d24601…85a470`; `hello` → `1c8aff95…36deac8` (my first hand-typed expected value was truncated from memory — the JS output was confirmed correct against `app.py:keccak_256`).
- Selectors: `decimals()` `313ce567`, `name()` `06fdde03`, `balanceOf(address)` `70a08231`, `transfer(address,uint256)` `a9059cbb`.
- Encoder: `transfer(address,uint256)` calldata byte-exact; `decimals()` → `0x313ce567`; `foo(string,uint256)` dynamic head/tail layout byte-exact; `ethToWeiHex("1.5")` → `0x14d1120d7b160000`.
- Error paths: bad address, `uint8` out-of-range (300), and `parseArg` bool/array cases all behave.
- `svgChart` renders an SVG with polyline for 3 points and a "No chart data" empty state.

## Live API endpoints the new pages depend on (curl, port 8035)

| Test | Result |
|---|---|
| `GET /api/ethscope/chains` | 200 — 4 chains, fields `{id, label, blockscout, rpc, coingecko_platform, native}` (note: field is `native`, not `native_symbol`) |
| `GET /api/ethscope/docs` | 200 — 62 entries (legacy + chain-prefixed pairs) |
| `GET /api/ethscope/ethereum/charts/transactions` | 200 — `chart_data` descending `[{date, transactions_count}]` (frontend sorts ascending) |
| `GET /api/ethscope/ethereum/charts/price?days=30` | 200 — `prices: [[ms, price], …]` |
| `GET /api/ethscope/ethereum/charts/gas` | 200 — `items: [{block, base_fee_gwei, timestamp}]` descending (frontend sorts ascending) |
| `GET /api/ethscope/ethereum/gastracker` | 200 on retry — gas 0.37/0.83/2.56, base fee 0.31, 20-block history |
| `GET /api/ethscope/ethereum/pending` | 200 — sampled, 50 items |
| `GET /api/ethscope/ethereum/address/<vitalik>/txcount` | 200 — `tx_count: 5970` |
| `GET /api/ethscope/ethereum/contract/<usdc>` | 200 on 3rd try (see below); WETH `200` (name WETH9, verified); Uniswap V2 Factory `0x5C69…9cc5aA6f` 200 |
| `POST /api/ethscope/ethereum/contract/<usdc>/call` `name()` | 200 |
| `GET /api/ethscope/ethereum/token/<azuki>/instances`, `/instance/1` | 200 |
| `GET /api/ethscope/sepolia/stats`, `/optimism/gastracker`, `/gnosis/pending` | all 200 |
| `GET /api/ethscope/bogus/stats` | **404 `{"error":"Unknown chain"}`** — frontend surfaces this as an in-UI error, never a hang |

**Transient upstream failures observed during this run (not frontend bugs):** the USDC contract endpoint returned `502 {"error":"Blockscout returned HTTP 500"}` twice, then 200 on the third try; the mainnet gastracker returned 502 once, then 200. Both are Blockscout-side flakiness (the backend verified these same endpoints 200 earlier today). The new pages handle them with honest error states (contract page → error card; gas page → "Gas tracker unavailable" card).

## Deliberate deviations / notes

1. **Contract verify posts JSON, not multipart.** The task brief says multipart, but the backend route (`api_chain_contract_verify`) does `await request.json()` — a multipart body would 400 with "JSON body required". The frontend POSTs JSON `{source_code, contract_name, compiler_version, constructor_args, evm_version, license_type}`; the backend itself forwards multipart to Blockscout. The upstream result is rendered verbatim (success and rejection alike). If the backend is later changed to accept multipart, the form should switch to `FormData`.
2. **Labels are mainnet-only.** `labels.json` is curated for Ethereum mainnet; pills are suppressed on Sepolia/Optimism/Gnosis because the same address there is a different account/contract.
3. **CSV export cap** is 5,000 rows / 25 pages, enforced client-side; the button shows live fetch progress and re-enables after.
4. **Value column header** on the tx CSV is `value_native` (chain-native coin, not always ETH).

## Not verified (out of scope for this environment)

- Live browser rendering of the SPA (no browser tooling here); JS is syntax-checked, unit-tested (Keccak/ABI/SVG), grep-audited, and logic-reviewed only. In particular, the Write Contract `eth_sendTransaction` flow and the wallet-connect button were not exercised against a real injected wallet — the code paths were reviewed, and the no-wallet notice path is the one that renders without MetaMask.
- Real contract verification submission (only the backend's dummy-payload forwarding was proven earlier; the frontend form posts the same JSON shape).
- Vercel deployment (not requested — no deploy performed).

## Tree state

No venv, no `__pycache__` under `~/workspace/cryptoscope/ethscope` (removed the one uvicorn created). `app.py` unmodified.


---

# EthScope — wallet dashboard verification (2026-11-05)

Server: `~/workspace/ethscope/venv/bin/uvicorn app:app --port 8035` from `~/workspace/cryptoscope/ethscope` (venv: fastapi 0.162.0, httpx 0.28.1, uvicorn 0.54.0). No new dependencies, keyless only. Test addresses: vitalik.eth `0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045` and Bitfinex hot wallet `0x742d35Cc6634C0532925a3b844Bc454e4438f44e`.

## Structure (for the planned hoist/split)

- `app.py` split mechanically (AST script, backup `/tmp/app.py.bak`): new `chains.py` (chain registry), `clients.py` (httpx clients, cache, `UpstreamError`, NO_PROXY fix), `pricing.py` (USD pricing + daily price series), `formatting.py` (wei/ETH/token math, ages, shorteners). `app.py` 1,840 → 1,505 lines + wallet wiring.
- `wallet.py` (new): all wallet endpoints; aggregation reads Blockscout only through `EVMAdapter` (address / token_balances / nfts / transactions / token_transfers) so a future non-EVM chain plugs in as another adapter. `api/index.py` unchanged (sibling-module imports work on Vercel).
- Frontend: wallet code is one self-contained section in `static/app.js` (`walletLanding` + `walletPage` + helpers), plus small hooks: header nav "Wallet", address-page "💼 Wallet Dashboard" button, "💼 Wallet" link on address search suggestions, Wallet group in API docs, 24h % column on the address Tokens tab.
- 72 routes total after wiring (`api/index.py` imports cleanly).

## Backend — wallet endpoints (curl, all HTTP 200 unless noted)

| Test | Result |
|---|---|
| `GET /api/ethscope/wallet/<vitalik>` (aggregate) | 200 in 5.09s — total native USD **$13,304.63**, 24h change **−7.68%** (native-only at this stage), ENS `vitalik.eth`, errors []. Ethereum 5.7804 ETH = $12,891.71; Optimism 0.1811 ETH = $404.21; Gnosis 8.7095 xDAI = $8.71; Sepolia 61.72 ETH, USD null (no market price — see below) |
| `GET /wallet/<vitalik>/tokens?chain=optimism` | 200 in 3.47s — total **$225.70**, 412 tokens, 28 priced, 296 NFTs. Top: WETH $78.04, USDT $43.34, USDC $39.17, DAI $35.21 |
| `GET /wallet/<vitalik>/tokens?chain=ethereum` | 200 in 21.6s (retry; first try hit the old 10s `bs()` timeout) — total **$525,406.45**, count 8,193, priced 376, NFTs 1,263. Top: WHITE $311,900.00 (24h +6.07%), KNCL $99,156.21 (−0.94%), MOODENG $77,405.96 (−9.22%), FOLD $9,513.71, OMG $6,027.60, ENS $5,331.21 |
| `GET /wallet/<vitalik>/nfts` | 200 in 3.74s — **200 items** (50/chain), all with images. First: ethereum WEI, NFNFT, UwU #1001 |
| `GET /wallet/<vitalik>/activity` | 200 in 3.26s — **50 items**, merged, sorted desc (first 2026-11-05T05:30:48Z), cursors for all 4 chains; per-chain counts: ethereum 28, sepolia 21, optimism 1, gnosis 0 |
| `GET /wallet/<vitalik>/activity?chain=ethereum&<cursor>` (Load-more) | 200 — 50 items, first ts 2026-10-07T03:09:35Z, strictly older than page 1's last ethereum item (2026-10-18T20:50:47Z), further cursor present |
| `GET /wallet/<vitalik>/history?days=30` | 200 in 33.9s (cold; repeat calls hit the 60s token cache + 600s chart cache) — **31 points**; first 2026-10-06 **$597,297.11**, last 2026-11-05 **$535,006.70**. Included top 5: WHITE $311,900, KNCL $99,156, MOODENG $77,406, FOLD $9,514, OMG $6,028 (all ethereum); 45 priced holdings excluded (top 5 only) |
| `GET /wallet/<vitalik>/pnl` | 200 in 5.29s — realized PnL **$0.00**, proceeds $0, cost $0; priced 0, **unpriced 111**, unmatched 0, tokens priced 4. Rows: sepolia (symbol None) 47 unpriced, ethereum VETO 43 unpriced, gnosis OPPORTUNITY_CREDITS 17 unpriced, optimism SNX 4 unpriced — all unlisted/spam tokens with no CoinGecko history, honestly excluded |
| `GET /wallet/<bitfinex>/pnl` | 200 in 14.9s — realized PnL **$0.00**, **proceeds $19,987,584.78 = cost $19,987,584.78**; priced 48, unpriced 55, unmatched 1, tokens priced 8. Rows: USDT 4 priced (proceeds = cost to the cent — stablecoin at ~$1.00, FIFO matching verified), ENA 18 priced, UNI 16, XAUT 9, USDF 1, sepolia (None) 50 unpriced, gnosis Blast Box 3 unpriced, optimism MAME 2 unpriced |
| `GET /wallet/<bitfinex>` (aggregate, 2nd address) | 200 — total native USD **$222.09** (Optimism $221.99, Gnosis $0.10, mainnet ~$0 — a hot wallet that sweeps), ens null |
| `GET /api/ethscope/ethereum/search?q=vitalik.eth` | 200 — address hit `vitalik.eth` → `0xd8dA…96045` (the path `#/wallet/vitalik.eth` uses) |
| `GET /api/ethscope/docs` | 200 — **6 wallet endpoints** listed (`/wallet/{address}`, `/tokens`, `/history`, `/pnl`, `/nfts`, `/activity`) |
| `GET /api/ethscope/ethereum/address/<vitalik>/tokens` (address Tokens tab, incl. new 24h % col) | 200 in 39.6s — total_usd **$525,406.45**, 8,193 items; WHITE $311,900 (+6.07%), KNCL $99,156 (−0.39%), MOODENG $77,406 (−8.84%) |

## Bugs found & fixed during wallet verification

1. **Aggregate timed out on mainnet tokens.** First design fetched token holdings inside the aggregate; vitalik's 3.36 MB mainnet list blew the 8s per-chain timeout (3 of 4 chains errored, totals 0). Fixed with the two-tier design: aggregate = native balances only (`total_usd` native-only, `tokens_status: "pending"`); holdings load progressively per chain via `/tokens?chain=` (30s → 45s guard).
2. **24h changes missing on top holdings.** The CoinGecko budget was spent pricing unpriced dust/spam tokens first (no CG listing), leaving the big holdings without a change figure. Fixed: Phase A prices the 24h change for the top 3 Blockscout-priced items by value first; Phase B prices unpriced items with the remaining budget. Verified: WHITE +6.07%, KNCL −0.94%, MOODENG −9.22% now populated.
3. **History valued Sepolia ETH at mainnet ETH prices.** `_daily_native_prices` used the CoinGecko coin id directly, so Sepolia's 61.72 "ETH" added ~$137k of phantom value (first history run: last point $151,032, Optimism-only tokens). Fixed: history includes a native series only for chains whose aggregate section has a real USD price. Verified: last point now $535,006.70 ≈ top-5 tokens $504,003 + native $13,305 + CG-close drift.
4. **PnL priced nothing (two causes).** (a) Transfer token objects expose the address as `address_hash`, not `address` — the grouping key and the CoinGecko series lookup both read `address` (always null/empty), so every series fetch hit CoinGecko with an empty contract ("coin not found"). Fixed all `address` reads on token objects to `address_hash` first (also in `formatting.py:token_transfer_short` and `_instance_short`, and the NFT `token_address` field — those fed null token addresses into tx/NFT pages too). (b) XAUT market_chart "coin not found" was my own wrong contract address in the probe, not a CoinGecko gap — the correct `0x68749665FF8D…782F38` returns prices. After the fix, Bitfinex PnL prices 48 transfers across 8 tokens (see table).
5. **Address Tokens tab 502'd on vitalik** (twice, at the 10s `bs()` default timeout). The wallet path already used 45s; the address path did not. Fixed: `_address_tokens` now uses `timeout=45.0`. Verified 200 in 39.6s on retry.
6. **History's first run missed mainnet tokens.** The history endpoint's all-chains token load re-fetched everything (per-chain calls didn't populate its cache) and mainnet timed out inside the 30s guard. Fixed: `_chain_tokens_guarded` caches per (address, chain) for 60s, shared by the frontend's per-chain calls and history. Guard raised 30s → 45s to match the `bs()` timeout.

## Honest limits (also stated in the UI + README)

- **History is an estimate, never true PnL.** Today's balances × historical prices; assumes today's balances held throughout; deposits/withdrawals/trades ignored. Only the top 5 tokens by value are priced in (45 priced holdings excluded for vitalik); chains without a USD price (Sepolia) are excluded entirely.
- **PnL is an estimate with stated coverage.** FIFO over the latest ~50 token transfers per chain, 90-day CoinGecko daily-close window. Only tokens with CoinGecko price history can be priced; unlisted/spam tokens are counted unpriced and excluded (vitalik: 111/111 unpriced → $0 estimate with the gap stated, not hidden). Outgoing transfers with no cost basis in the window are unmatched and excluded. No full cost basis exists keyless — true realized PnL would need every acquisition price.
- **Token holdings are slow for token-rich addresses.** Vitalik's mainnet token list is 3.36 MB; the same download took 15s, 21.6s, 39.6s and 61.6s at different moments today through the sandbox egress proxy, and stalled completely once (direct Blockscout curl, HTTP 000 at 60s). The dashboard degrades per chain (error section, never a whole-dashboard failure) and the frontend uses a 60s timeout on wallet calls.
- **24h change coverage is partial.** Only native holdings + priced tokens that have a 24h change feed the combined figure; the hero states the covered USD amount. Unpriced tokens (7,817 of vitalik's 8,193) are excluded from holdings totals entirely.

## Frontend

- `node --check static/app.js` → **OK**.
- `GET /`, `/app.js`, `/styles.css` → all 200.
- Regression sweep (same run): chains, docs (6 wallet endpoints), ethereum/optimism stats, `block/latest` (height 26351552), address txs (50 items), contract USDC, `charts/price?days=7` (200, `prices` array), gastracker, pending, search — all 200. (My first charts check used the wrong path `/charts/eth-price` and 404'd; the real route is `/charts/price`.)
- Not verified (no browser tooling here): live rendering of `#/wallet/vitalik.eth` — JS is syntax-checked and logic-reviewed only; the `data-chain` click-switch, dust toggle, value sort, history day buttons and activity Load-more paths were reviewed, not clicked. ENS resolve is exercised at the API level (`/search?q=vitalik.eth` → correct address).

## Tree state

No venv, no `__pycache__` under `~/workspace/cryptoscope/ethscope` (checked after the final run). Do NOT push/deploy from here — parent handles that. Vercel deploy remains blocked (403 "Not authorized under scope thetachyon", awaiting Tejas granting the Vercel GitHub App access to the private `cryptoscope` repo).
