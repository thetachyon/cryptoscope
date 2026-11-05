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
