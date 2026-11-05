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
