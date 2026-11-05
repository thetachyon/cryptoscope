# EthScope

An Etherscan-style Ethereum **mainnet** block explorer — blocks, transactions, addresses, tokens, and search — with a FastAPI backend and a single-page frontend.

**Data source:** [Blockscout](https://eth.blockscout.com) v2 public API (`https://eth.blockscout.com/api/v2`).
**Keys:** none. No API keys anywhere.

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
app.py              FastAPI backend — /api/ethscope/* proxies Blockscout v2
api/index.py        Vercel serverless entrypoint (re-exports app)
static/index.html   App shell
static/app.js       Hash-routed SPA (#/, #/block/{n}, #/tx/{h}, #/address/{h}, #/token/{h})
static/styles.css   Etherscan-like light theme
vercel.json         Vercel rewrites
requirements.txt    Pinned deps
VERIFY.md           What was tested and the results
```

## Backend endpoints (`/api/ethscope/*`)

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
| `GET /address/{h}/tokens` | `/addresses/{h}/token-balances` |
| `GET /token/{h}` | `/tokens/{h}` — ERC-20 overview |
| `GET /token/{h}/transfers` | `/tokens/{h}/transfers` |
| `GET /search?q=` | `/search` — addresses, tokens, txs, blocks, ENS |

Responses are translated/paraphrased (wei→ETH, gwei, relative ages, compact address objects) rather than raw Blockscout dumps. List endpoints forward pagination params and return Blockscout's `next_page_params`. Upstream failures return `{"error": ...}` (HTTP 502) — never tracebacks. Hot endpoints (stats, latest blocks/txs) have a 60s in-memory TTL cache.

## Etherscan feature mapping

| Etherscan | EthScope |
|---|---|
| Home + universal search | `#/` — search detects tx hash / address / block number / block hash / ENS; live stats cards; 10s-polling latest blocks & txs |
| Block page | `#/block/{n}` — details, gas bar, rewards, withdrawals + tx list |
| Transaction page | `#/tx/{h}` — status badge, fee breakdown, input hex + UTF-8 decode, token transfers, logs, internal txs |
| Address page | `#/address/{h}` — balance, ENS/contract badges, tabs: Transactions / Token Transfers / Internal / Tokens |
| Token tracker | `#/token/{h}` — ERC-20 overview + transfers |

## Not included (roadmap)

- Contract source verification / verified-contract badges beyond Blockscout's flag
- Charts (tx history, gas charts)
- Testnets / other chains (single-chain mainnet only)
- ENS name registration pages, NFT (ERC-721/1155) detail views
- Mempool/pending-tx stream (latest-txs list may include pending entries)
- Address transaction-count (Blockscout v2 address object doesn't expose it)
- CSV export, labels/cloud annotations, API keys for power users

## Notes

- Blockscout v2 has no `GET /blocks/{n}/transactions` on this instance; block txs use `GET /transactions?block_number=`.
- Sandbox quirk: the environment's `NO_PROXY` contains bracketed IPv6 literals (`[::1]`) that httpx's env-proxy parser rejects — `app.py` strips them at import. The egress proxy itself is still used for outbound calls.
