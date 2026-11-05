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
