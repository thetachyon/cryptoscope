"""Cryptoscope overview — cross-chain analytics landing data.

Aggregates the existing `common/` clients; no new data plumbing except
two public keyless sources the other apps do not use:

- CoinGecko keyless `/global` (total market cap, BTC/ETH dominance,
  24h change) and `/coins/markets` (top movers).
- Wormholescan keyless `/api/v1/scorecards` (Wormhole bridge volume,
  messages, TVL) and `/api/v1/last-txs` (hourly tx counts).
  NOTE: DeFiLlama's bridges API (`bridges.llama.fi`, `api.llama.fi/
  bridges`) is PAYWALLED as of 2026-11-06 — it answers "Upgrade to the
  paid API plan". Bridge figures here are Wormhole-only and are
  labelled as such; they are not all-bridge totals.

Every section degrades independently: a failed upstream blanks its
section with a note and never fails the whole page. All keyless.
"""
import asyncio
import re
import time

import httpx

from common.bitcoin import btc_stats
from common.chains import CHAINS
from common.clients import (
    HEADERS,
    TIMEOUT,
    UpstreamError,
    _cache_get,
    _cache_set,
    coingecko,
)
from common.explorer import _stats
from common.solana import is_solana_address
from common.solana_explorer import sol_stats
from common.stablecoin import STABLECOINS, sc_stats

WORMHOLE_BASE = "https://api.wormholescan.io/api/v1"

AGG_TTL = 120  # seconds, for the /overview aggregate
SECTION_TTL = 300  # seconds, for the slower sections (bridges, movers)


# ---------------------------------------------------------------- scope urls
# Expected Vercel domains, same pattern as every app's config.py SIBLINGS.
def _scope_url(scope: str) -> str:
    return f"https://{scope}.vercel.app"


CHAIN_SCOPE = {
    "ethereum": "ethscope",
    "sepolia": "sepoliascope",
    "optimism": "optimismscope",
    "gnosis": "gnosisscope",
    "solana": "solscope",
    "bitcoin": "bitscope",
}


# ---------------------------------------------------------------- helpers
async def _wh(path):
    """GET Wormholescan. Returns parsed JSON or None on any failure."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            r = await c.get(WORMHOLE_BASE + path)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError):
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- global market
async def ov_global():
    """CoinGecko global market + total stablecoin supply (sum of the 8
    scopes' supplies). Never raises: returns an error field instead."""
    key = "overview:global"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    g = await coingecko("/global")
    if not isinstance(g, dict):
        out = {"error": "CoinGecko global data unavailable"}
        _cache_set(key, out, ttl=60)
        return out
    d = g.get("data") or {}
    tmc = d.get("total_market_cap") or {}
    dom = d.get("market_cap_percentage") or {}
    # Total stablecoin supply from the 8 scopes (cheap: sc_stats is
    # cached per coin by the stablecoin module).
    try:
        coins = await ov_stablecoins()
        st_total = sum(
            (c.get("supply_usd") or 0)
            for c in (coins.get("rows") or [])
            if isinstance(c, dict)
        ) or None
    except Exception:
        st_total = None
    out = {
        "total_market_cap_usd": _num(tmc.get("usd")),
        "market_cap_change_24h_pct": _num(d.get("market_cap_change_percentage_24h_usd")),
        "btc_dominance_pct": _num(dom.get("btc")),
        "eth_dominance_pct": _num(dom.get("eth")),
        "active_cryptocurrencies": d.get("active_cryptocurrencies"),
        "markets": d.get("markets"),
        "total_stablecoin_supply_usd": st_total,
        "sources": ["CoinGecko keyless API", "DeFiLlama stablecoins API"],
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


# ---------------------------------------------------------------- chains
async def _evm_row(chain):
    s = await _stats(chain)
    gp = (s.get("gas_price_gwei") or {}).get("average")
    return {
        "id": chain,
        "label": s.get("chain") and CHAINS[chain]["label"],
        "scope": CHAIN_SCOPE[chain],
        "scope_url": _scope_url(CHAIN_SCOPE[chain]),
        "native": s.get("native"),
        "price_usd": _num(s.get("eth_price_usd")),
        "change_24h_pct": _num(s.get("eth_price_change_24h_pct")),
        "latest": s.get("latest_block"),
        "latest_label": "block",
        "tps": None,
        "avg_block_time_s": s.get("avg_block_time_s"),
        "fee": f"{gp} gwei" if gp is not None else None,
        "total_transactions": s.get("total_transactions"),
        "error": None,
    }


async def _sol_row():
    s = await sol_stats()
    return {
        "id": "solana",
        "label": "Solana",
        "scope": "solscope",
        "scope_url": _scope_url("solscope"),
        "native": "SOL",
        "price_usd": _num(s.get("sol_price_usd")),
        "change_24h_pct": _num(s.get("sol_change_24h_pct")),
        "latest": s.get("slot"),
        "latest_label": "slot",
        "tps": s.get("tps"),
        "avg_block_time_s": None,
        "fee": None,
        "total_transactions": s.get("transaction_count"),
        "error": None,
    }


async def _btc_row():
    s = await btc_stats()
    fees = s.get("fees") or {}
    fast = fees.get("fastest")
    return {
        "id": "bitcoin",
        "label": "Bitcoin",
        "scope": "bitscope",
        "scope_url": _scope_url("bitscope"),
        "native": "BTC",
        "price_usd": _num(s.get("btc_price_usd")),
        "change_24h_pct": _num(s.get("btc_change_24h_pct")),
        "latest": s.get("height"),
        "latest_label": "block",
        "tps": None,
        "avg_block_time_s": None,
        "fee": f"{fast} sat/vB" if fast is not None else None,
        "total_transactions": None,
        "mempool_count": s.get("mempool_count"),
        "error": None,
    }


async def ov_chains():
    """One row per chain (EVM + Solana + Bitcoin). A failed chain gets
    an error row, never a failed response."""
    key = "overview:chains"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    jobs = (
        [_evm_row(c) for c in ("ethereum", "optimism", "gnosis", "sepolia")]
        + [_sol_row(), _btc_row()]
    )
    results = await asyncio.gather(*jobs, return_exceptions=True)
    ids = ["ethereum", "optimism", "gnosis", "sepolia", "solana", "bitcoin"]
    rows = []
    for cid, res in zip(ids, results):
        if isinstance(res, Exception):
            rows.append({
                "id": cid,
                "label": (CHAINS.get(cid) or {}).get("label") or cid.title(),
                "scope": CHAIN_SCOPE.get(cid),
                "scope_url": _scope_url(CHAIN_SCOPE[cid]) if cid in CHAIN_SCOPE else None,
                "native": (CHAINS.get(cid) or {}).get("native"),
                "price_usd": None, "change_24h_pct": None, "latest": None,
                "latest_label": "block", "tps": None, "avg_block_time_s": None,
                "fee": None, "total_transactions": None,
                "error": str(res),
            })
        else:
            rows.append(res)
    # Optimism's native token is ETH: carry Ethereum's CoinGecko 24h change
    # (Blockscout stats for Optimism carries no change field).
    eth_change = next((r.get("change_24h_pct") for r in rows if r["id"] == "ethereum"), None)
    for r in rows:
        if r["id"] == "optimism" and r.get("change_24h_pct") is None:
            r["change_24h_pct"] = eth_change
    out = {"rows": rows}
    _cache_set(key, out, ttl=60)
    return out


# ---------------------------------------------------------------- stablecoins
async def ov_stablecoins():
    """One row per stablecoin scope (reuse sc_stats). A failed coin gets
    an error row."""
    key = "overview:stablecoins"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_stats(s) for s in scopes), return_exceptions=True
    )
    rows = []
    for scope, res in zip(scopes, results):
        if isinstance(res, Exception):
            sym = STABLECOINS[scope]["symbol"]
            rows.append({
                "scope": scope, "scope_url": _scope_url(scope),
                "symbol": sym, "name": STABLECOINS[scope]["name"],
                "price_usd": None, "deviation_bps": None, "peg_health": None,
                "supply_usd": None, "market_cap_usd": None, "error": str(res),
            })
        else:
            rows.append({
                "scope": scope,
                "scope_url": _scope_url(scope),
                "symbol": res.get("symbol"),
                "name": res.get("name"),
                "issuer": res.get("issuer"),
                "price_usd": _num(res.get("price_usd")),
                "deviation_bps": _num(res.get("deviation_bps")),
                "peg_health": res.get("peg_health"),
                "supply_usd": _num(res.get("supply_usd")),
                "market_cap_usd": _num(res.get("market_cap_usd")),
                "supply_change_7d_pct": _num(res.get("supply_change_7d_pct")),
                "error": None,
            })
    total = sum((r.get("supply_usd") or 0) for r in rows) or None
    out = {"rows": rows, "total_supply_usd": total}
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


# ---------------------------------------------------------------- bridges (Wormhole)
async def ov_bridges():
    """Wormhole bridge flows via Wormholescan (keyless). DeFiLlama's
    bridges API is paywalled (probed 2026-11-06) — the section says so
    and reports Wormhole only, never all-bridge totals."""
    key = "overview:bridges"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    sc, hourly = await asyncio.gather(_wh("/scorecards"), _wh("/last-txs?limit=24"))
    if not isinstance(sc, dict):
        out = {
            "error": "Bridge data unavailable (Wormholescan unreachable; "
                     "DeFiLlama bridges API requires a paid plan)",
            "provider": "Wormholescan (Wormhole)",
        }
        _cache_set(key, out, ttl=60)
        return out
    out = {
        "provider": "Wormholescan (Wormhole)",
        "note": "Wormhole bridge only. DeFiLlama's bridges API is paywalled "
                "(probed 2026-11-06), so no all-bridge total is available keyless.",
        "volume_24h_usd": _num(sc.get("24h_volume")),
        "volume_7d_usd": _num(sc.get("7d_volume")),
        "volume_30d_usd": _num(sc.get("30d_volume")),
        "volume_total_usd": _num(sc.get("total_volume")),
        "messages_24h": _num(sc.get("24h_messages")),
        "messages_7d": _num(sc.get("7d_messages")),
        "tvl_usd": _num(sc.get("tvl")),
        "hourly_tx_counts": [
            {"time": h.get("time"), "count": h.get("count")}
            for h in (hourly or []) if isinstance(h, dict)
        ],
        "sources": ["Wormholescan keyless API"],
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


# ---------------------------------------------------------------- movers
async def ov_movers():
    """Top gainers / losers among the top 50 coins by market cap
    (CoinGecko keyless markets endpoint)."""
    key = "overview:movers"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await coingecko("/coins/markets", params={
        "vs_currency": "usd", "order": "market_cap_desc",
        "per_page": 50, "page": 1, "price_change_percentage": "24h",
    })
    if not isinstance(d, list) or not d:
        out = {"error": "CoinGecko markets data unavailable"}
        _cache_set(key, out, ttl=60)
        return out

    def row(c):
        return {
            "id": c.get("id"),
            "symbol": (c.get("symbol") or "").upper(),
            "name": c.get("name"),
            "price_usd": _num(c.get("current_price")),
            "market_cap_usd": _num(c.get("market_cap")),
            "change_24h_pct": _num(c.get("price_change_percentage_24h")),
        }

    ranked = sorted(
        (row(c) for c in d if c.get("price_change_percentage_24h") is not None),
        key=lambda r: r["change_24h_pct"],
    )
    out = {
        "gainers": list(reversed(ranked[-10:])),
        "losers": ranked[:10],
        "universe": "Top 50 coins by market cap (CoinGecko)",
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


# ---------------------------------------------------------------- aggregate
async def ov_overview():
    """The landing-page aggregate: every section in one call, each
    degrading independently."""
    key = "overview:aggregate"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    results = await asyncio.gather(
        ov_global(), ov_chains(), ov_stablecoins(), ov_bridges(), ov_movers(),
        return_exceptions=True,
    )
    names = ["global", "chains", "stablecoins", "bridges", "movers"]
    out = {"generated_at": int(time.time())}
    for name, res in zip(names, results):
        out[name] = {"error": str(res)} if isinstance(res, Exception) else res
    _cache_set(key, out, ttl=AGG_TTL)
    return out


# ---------------------------------------------------------------- search routing
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_TX_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX64_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_BTC_ADDR_RE = re.compile(
    r"^(1[1-9A-HJ-NP-Za-km-z]{25,34}|3[1-9A-HJ-NP-Za-km-z]{25,34}"
    r"|bc1[ac-hj-np-z02-9]{11,71})$"
)
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Stablecoin lookup by symbol or name (case-insensitive).
_STABLE_BY_NAME = {}
for _scope, _cfg in STABLECOINS.items():
    _STABLE_BY_NAME[_cfg["symbol"].lower()] = _scope
    _STABLE_BY_NAME[_cfg["name"].lower()] = _scope
_STABLE_BY_NAME.update({
    "tether": "usdtscope", "usd coin": "usdcscope", "dai stablecoin": "daiscope",
    "ethena": "usdescope", "first digital usd": "fdusdsscope",
    "paypal usd": "pyusdsscope", "trueusd": "tusdscope",
})


def _is_b58(s):
    return bool(s) and all(ch in _B58_ALPHABET for ch in s)


def ov_search(q):
    """Route a query to the right Scope. Returns a typed result with an
    absolute URL, or an `ambiguous` result with candidates when the
    query could be more than one thing (plain number = block/slot/
    height; bare 64-hex = EVM tx hash or BTC txid). Raises ValueError
    (400) / LookupError (404) for bad / unmatched input."""
    q = (q or "").strip()
    if not q:
        raise ValueError("Empty search query")
    low = q.lower()

    # Stablecoin by symbol or name.
    if low in _STABLE_BY_NAME:
        scope = _STABLE_BY_NAME[low]
        return {
            "type": "stablecoin", "scope": scope,
            "url": _scope_url(scope) + "/#/",
            "label": f"{STABLECOINS[scope]['name']} ({STABLECOINS[scope]['symbol']}) dashboard",
        }
    # ENS name -> Ethereum address page.
    if low.endswith(".eth"):
        return {
            "type": "address", "chain": "ethereum",
            "url": _scope_url("ethscope") + "/#/address/" + q,
            "label": f"ENS name {q} on EthScope",
        }
    # EVM address / tx hash (0x-prefixed is decisive).
    if _ADDR_RE.match(q):
        return {
            "type": "address", "chain": "ethereum",
            "url": _scope_url("ethscope") + "/#/address/" + q,
            "label": f"Ethereum address {q[:10]}…",
        }
    if _TX_RE.match(q):
        return {
            "type": "transaction", "chain": "ethereum",
            "url": _scope_url("ethscope") + "/#/tx/" + q,
            "label": f"Ethereum transaction {q[:10]}…",
        }
    # Bitcoin address (checked before Solana: bech32 can look base58-ish).
    if _BTC_ADDR_RE.match(q):
        return {
            "type": "address", "chain": "bitcoin",
            "url": _scope_url("bitscope") + "/#/address/" + q,
            "label": f"Bitcoin address {q[:12]}…",
        }
    # Plain number: Ethereum block, Solana slot, or Bitcoin height.
    if q.isdigit():
        return {
            "type": "ambiguous",
            "label": f"Block / slot / height {q}",
            "candidates": [
                {"type": "block", "chain": "ethereum",
                 "url": _scope_url("ethscope") + "/#/block/" + q,
                 "label": f"Ethereum block {q}"},
                {"type": "slot", "chain": "solana",
                 "url": _scope_url("solscope") + "/#/slot/" + q,
                 "label": f"Solana slot {q}"},
                {"type": "block", "chain": "bitcoin",
                 "url": _scope_url("bitscope") + "/#/block/" + q,
                 "label": f"Bitcoin block {q}"},
            ],
        }
    # Bare 64-hex: EVM tx hash or BTC txid.
    if _HEX64_RE.match(q):
        return {
            "type": "ambiguous",
            "label": f"Transaction {q[:10]}…",
            "candidates": [
                {"type": "transaction", "chain": "ethereum",
                 "url": _scope_url("ethscope") + "/#/tx/" + q,
                 "label": "Ethereum transaction"},
                {"type": "transaction", "chain": "bitcoin",
                 "url": _scope_url("bitscope") + "/#/tx/" + q,
                 "label": "Bitcoin transaction"},
            ],
        }
    # Solana signature (87-88 base58 chars) then address (32-44).
    if 87 <= len(q) <= 88 and _is_b58(q):
        return {
            "type": "transaction", "chain": "solana",
            "url": _scope_url("solscope") + "/#/tx/" + q,
            "label": f"Solana transaction {q[:8]}…",
        }
    if is_solana_address(q):
        return {
            "type": "address", "chain": "solana",
            "url": _scope_url("solscope") + "/#/address/" + q,
            "label": f"Solana address {q[:8]}…",
        }
    raise LookupError(
        f"No match for “{q}”. Try an address, transaction hash, block "
        "number, slot, or a stablecoin name (e.g. USDC)."
    )


# ---------------------------------------------------------------- docs
_OV_DOCS = [
    ("GET", "/overview", "Full landing-page aggregate: global market, chains, stablecoins, bridges, movers. Each section degrades independently.", [], "/overview"),
    ("GET", "/stats", "Global market: total market cap, BTC/ETH dominance, 24h change, total stablecoin supply.", [], "/stats"),
    ("GET", "/chains", "One row per chain (Ethereum, Optimism, Gnosis, Sepolia, Solana, Bitcoin): price, latest block/slot, fee.", [], "/chains"),
    ("GET", "/stablecoins", "One row per stablecoin scope: price, peg deviation, health, supply, market cap.", [], "/stablecoins"),
    ("GET", "/bridges", "Wormhole bridge flows (24h/7d/30d volume, messages, TVL) via Wormholescan. Wormhole only — DeFiLlama bridges is paywalled.", [], "/bridges"),
    ("GET", "/movers", "Top gainers and losers among the top 50 coins by market cap (CoinGecko).", [], "/movers"),
    ("GET", "/search", "Route a query to the right Scope: address, tx, block/slot, ENS, stablecoin name.", ["q"], "/search?q=vitalik.eth"),
    ("GET", "/docs", "This endpoint list.", [], "/docs"),
]


def ov_docs_payload(api_prefix):
    return {
        "chains": ["ethereum", "sepolia", "optimism", "gnosis", "solana", "bitcoin", "stablecoins"],
        "endpoints": [
            {
                "method": m, "path": api_prefix + s, "description": d,
                "params": p, "example": api_prefix + e,
            }
            for (m, s, d, p, e) in _OV_DOCS
        ],
    }
