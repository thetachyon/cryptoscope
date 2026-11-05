# Cryptoscope

Home for Tejas's crypto tooling — explorers, forensics, and analytics, each in its own directory, each independently deployable.

## Projects

| Project | What it is | Deploy |
|---|---|---|
| [ethscope](./ethscope) | Etherscan-style Ethereum mainnet explorer (search, blocks, transactions, addresses, tokens). Keyless — data via the public Blockscout v2 API. | Vercel, root directory `ethscope` |

## Layout

Each project is self-contained: its own app, dependencies, and deployment config. Shared nothing yet — if two projects grow a common helper, it goes in `common/` and gets imported, not copied.
