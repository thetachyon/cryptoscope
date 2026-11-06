# BitScope

Bitcoin explorer — blocks, transactions, addresses, mempool — in the
mempool.space style, with a FastAPI backend and a single-page frontend.

**Data source:** [mempool.space](https://mempool.space/api) (primary),
[Blockstream Esplora](https://blockstream.info/api) (fallback for the
core paths). BTC price from mempool.space's hourly history, CoinGecko
fallback.
**Keys:** none. No API keys anywhere.

## Run locally

```bash
cd bitscope
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
./venv/bin/uvicorn app:app --port 8042
# open http://127.0.0.1:8042/
```

`requirements.txt` pins `fastapi==0.162.0`, `uvicorn==0.54.0`, `httpx==0.28.1`.

## Deploy (Vercel)

Serverless Python, same pattern as the other Scope apps:

- `vercel.json` runs `python3 ../scripts/sync-common.py sync` as its
  `buildCommand` (re-vendors `common/` + `static/` at build time) and
  rewrites every path to `/api/index`
- `api/index.py` re-exports the FastAPI `app` (ASGI)
- The app serves `static/` itself, so the whole site (API + frontend)
  runs in the one Python function
- `vercel deploy` installs `requirements.txt` automatically

## Layout

```
config.py           App metadata (single source of truth)
app.py              Thin entrypoint — builds the app via common/factory.py
api/index.py        Vercel serverless entrypoint (re-exports app)
frontend/app.js     Bitcoin SPA (#/, #/block/{n}, #/tx/{txid},
                    #/address/{addr}, #/mempool, #/api-docs)
frontend/index.html App shell (overlay — replaces the canonical one)
vercel.json         Vercel buildCommand + rewrites
requirements.txt    Pinned deps
```

All data logic lives in `common/bitcoin.py` (mempool.space/Esplora
client) and the factory in `common/factory.py` (`create_bitcoin_app`).

## Backend endpoints (`/api/bitscope/*`)

| Endpoint | Source |
|---|---|
| `GET /stats` | tip height, difficulty, hashrate, mempool, fees, BTC price |
| `GET /blocks`, `GET /blocks?before=<height>` | latest blocks (10, Load-more via start height) |
| `GET /block/{height\|hash\|latest}` | block detail + pool/miner + fees (v1 extras) |
| `GET /block/{n}/txs?start=` | block transactions, 25/page |
| `GET /tx/{txid}` | full tx: fee + sat/vB rate, inputs w/ prevouts, outputs, totals, confirmations, RBF when unconfirmed |
| `GET /address/{addr}` | balance (confirmed/unconfirmed), tx count |
| `GET /address/{addr}/txs?after=` | address tx history, 50/page |
| `GET /address/{addr}/utxo` | UTXOs (first 100; huge sets refused upstream, reported as a note) |
| `GET /mempool` | unconfirmed count/size, fee-rate bands, recent txs, recommended fees |
| `GET /search?q=` | block height, txid, block hash, or address |

## Notes

- mempool.space/Esplora tx objects carry `weight`, not `vsize` —
  vsize = ceil(weight/4); fee rate = fee / vsize (sat/vB).
- mempool.space is the primary for everything; Esplora is the fallback
  for the shared core paths (blocks, block, tx, address, mempool). The
  v1-only endpoints (fees, difficulty adjustment, hashrate, pool extras,
  RBF, historical price) are mempool.space-only and degrade to nulls if
  mempool.space is down.
- Block/tx detail responses are cached 10 minutes (immutable once
  confirmed); stats/mempool 15–60s.
- Address validation is shape + base58check checksum (legacy/P2SH) and
  bech32 charset/length (bc1q/bc1p); the upstream reports a bad checksum.
- Miner names come from mempool.space v1 block extras (`pool.name`).
  When the v1 call fails, the miner shows as unknown rather than guessed.
