# Cryptoscope

Home for Tejas's crypto tooling — explorers, forensics, and analytics, each in its own directory, each independently deployable.

## Apps

| App | What it is | Vercel root directory |
|---|---|---|
| [ethscope](./ethscope) | Etherscan-style Ethereum mainnet explorer (search, blocks, transactions, addresses, tokens, contracts, NFTs, charts, gas). | `ethscope` |
| [sepoliascope](./sepoliascope) | Same explorer for Sepolia. | `sepoliascope` |
| [optimismscope](./optimismscope) | Same explorer for Optimism. | `optimismscope` |
| [gnosisscope](./gnosisscope) | Same explorer for Gnosis. | `gnosisscope` |
| [walletscope](./walletscope) | Cross-chain wallet dashboard (portfolio, history, PnL estimate, NFTs, activity) across all four chains. | `walletscope` |

Every app is keyless — data via the public Blockscout v2 API, public JSON-RPC endpoints, and CoinGecko's keyless tier. No API keys anywhere.

## Layout

```
common/             Shared Python package — the single source of truth.
                    Chain registry, per-chain Blockscout clients, USD
                    pricing, formatting, Keccak/ABI codec, explorer core,
                    wallet aggregation, thin-app factories.
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
walletscope/        Cross-chain wallet dashboard (thin)
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

Solana holdings into the wallet dashboard ✓ (SOL + SPL, keyless — see `walletscope/README.md`) → **SolScope** (full Solana explorer, own directory) → **BitScope** (Bitcoin explorer via keyless mempool.space / Blockstream Esplora) → stablecoin Scopes (Top 8: USDT, USDC, DAI, USDe, FDUSD, PYUSD, TUSD, USDD — peg status, supply, holders, transfers, depeg history, reserves).

## Deploy status

Vercel deploy is blocked: `create_git_project` returns 403 "Not authorized under scope thetachyon" — the Vercel GitHub App has no access to the private `cryptoscope` repo (or the MCP connection lost team scope). Fix: add `cryptoscope` to the Vercel GitHub App's repository access, or import each app in the Vercel dashboard with its root directory. Each app is independently deployable once that is fixed.
