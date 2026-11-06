"""Solana data for the WalletScope dashboard — SOL + SPL holdings, keyless.

Sources (all probed 2026-11-05 from this sandbox):
- SOL balance: solana.publicnode.com getBalance (fast, ~0.5s). Indexed
  methods (getTokenAccountsByOwner, getProgramAccounts) are blocked there
  ("Indexed requests require a personal token") — mainnet-beta is used
  for those instead, with publicnode as the getBalance fallback.
- SPL token accounts: api.mainnet-beta.solana.com getProgramAccounts on
  the classic SPL Token program (jsonParsed, dataSize 165, memcmp owner
  at offset 32). Works (~1-2s). Token-2022 getProgramAccounts is
  rejected by the node ("WrongSize") — Token-2022 holdings are NOT
  covered; only classic SPL Token program accounts are listed.
- Jupiter APIs (api.jup.ag, token.jup.ag) are unreachable from this
  sandbox (connection fails) — NOT used.
- Prices: CoinGecko keyless (SOL via simple/price; SPL via
  simple/token_price/solana — ONE mint per request on the free tier,
  error 10012 otherwise) and DexScreener per mint (symbol/name/priceUsd,
  only for tokens with a DEX pair).
- Symbols: DexScreener first; CoinGecko contract endpoint as fallback
  for priced mints; otherwise the truncated mint address is shown.

Every helper degrades to None/empty on upstream failure — a Solana
pricing failure never breaks the dashboard.
"""
import asyncio
import time

import httpx

from common.clients import (
    TOKEN_PRICE_TTL,
    _cache_get,
    _cache_set,
    UpstreamError,
    coingecko,
    HEADERS,
    TIMEOUT,
)
from common.formatting import token_amount
from common.pricing import cg_token_price_full

SOL_BALANCE_RPC = "https://solana.publicnode.com"
SOL_ACCOUNTS_RPC = "https://api.mainnet-beta.solana.com"
SOL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
SOL_NATIVE_MINT = "So11111111111111111111111111111111111111112"
SOL_CG_ID = "solana"

# Caps keep the keyless upstreams happy: CoinGecko's free tier allows one
# contract per token_price request, and DexScreener is one call per mint.
SOL_MAX_DEXSCREENER = 30  # mints priced/symbolised via DexScreener
SOL_MAX_CG_PRICES = 5  # mints priced via CoinGecko (top by balance first)
SOL_MAX_CG_SYMBOLS = 3  # priced mints given a CoinGecko symbol lookup

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def is_solana_address(s):
    """True if `s` is a valid Solana address: base58, 32-44 chars, decodes
    to exactly 32 bytes."""
    if not isinstance(s, str) or not (32 <= len(s) <= 44):
        return False
    if any(ch not in _B58_ALPHABET for ch in s):
        return False
    try:
        n = 0
        for ch in s:
            n = n * 58 + _B58_ALPHABET.index(ch)
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
        # Leading '1's are leading zero bytes.
        pad = len(s) - len(s.lstrip("1"))
        raw = b"\x00" * pad + raw
        return len(raw) == 32
    except Exception:
        return False


async def _sol_rpc(url, method, params, timeout=None):
    """One Solana JSON-RPC call. Returns the `result` field; raises
    UpstreamError on transport, HTTP, or JSON-RPC errors."""
    t = TIMEOUT if timeout is None else httpx.Timeout(timeout)
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        async with httpx.AsyncClient(timeout=t, headers=HEADERS, follow_redirects=True) as c:
            r = await c.post(url, json=payload)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        raise UpstreamError(f"Solana RPC unreachable ({type(e).__name__})")
    if r.status_code == 429:
        raise UpstreamError("Solana RPC rate-limited (HTTP 429)")
    if r.status_code >= 400:
        raise UpstreamError(f"Solana RPC returned HTTP {r.status_code}")
    try:
        d = r.json()
    except Exception:
        raise UpstreamError("Solana RPC returned invalid JSON")
    if isinstance(d, dict) and d.get("error"):
        err = d["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise UpstreamError(f"Solana RPC error: {msg}", status=400)
    return d.get("result") if isinstance(d, dict) else None


async def sol_balance(address):
    """SOL balance in lamports (int). publicnode first (fast), mainnet-beta
    as fallback."""
    try:
        v = await _sol_rpc(SOL_BALANCE_RPC, "getBalance", [address])
    except UpstreamError:
        v = await _sol_rpc(SOL_ACCOUNTS_RPC, "getBalance", [address])
    if not isinstance(v, dict) or v.get("value") is None:
        raise UpstreamError("Solana RPC returned no balance")
    return int(v["value"])


async def sol_token_accounts(address):
    """Classic SPL Token program accounts owned by `address` (jsonParsed).
    Returns [{mint, amount, decimals, ui_amount}]. Zero-balance accounts
    are kept here — callers filter. One retry on rate-limit, then raise."""
    params = [
        SOL_TOKEN_PROGRAM,
        {
            "encoding": "jsonParsed",
            "filters": [
                {"dataSize": 165},
                {"memcmp": {"offset": 32, "bytes": address}},
            ],
        },
    ]
    try:
        res = await _sol_rpc(SOL_ACCOUNTS_RPC, "getProgramAccounts", params, timeout=25.0)
    except UpstreamError as e:
        if "429" not in str(e):
            raise
        await asyncio.sleep(1.0)
        res = await _sol_rpc(SOL_ACCOUNTS_RPC, "getProgramAccounts", params, timeout=25.0)
    out = []
    for a in res or []:
        try:
            info = a["account"]["data"]["parsed"]["info"]
            amt = info["tokenAmount"]
        except (KeyError, TypeError):
            continue
        out.append(
            {
                "mint": info.get("mint"),
                "amount": amt.get("amount"),
                "decimals": amt.get("decimals"),
                "ui_amount": amt.get("uiAmountString") or amt.get("uiAmount"),
            }
        )
    return out


async def sol_native_quote():
    """(SOL usd_price, usd_24h_change_pct) from CoinGecko, 5-min cache.
    (None, None) on any failure — never raises."""
    key = "solquote"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    val = (None, None)
    try:
        d = await coingecko(
            "/simple/price",
            params={"ids": SOL_CG_ID, "vs_currencies": "usd", "include_24hr_change": "true"},
        )
        if isinstance(d, dict):
            entry = d.get(SOL_CG_ID) or {}
            if entry.get("usd") is not None:
                val = (float(entry["usd"]), entry.get("usd_24h_change"))
    except Exception:
        val = (None, None)
    _cache_set(key, val, ttl=TOKEN_PRICE_TTL)
    return val


async def _dexscreener_token(mint):
    """DexScreener data for one mint: {symbol, name, usd_price,
    usd_change_24h_pct} of the most-liquid pair, or None (no pair /
    failure)."""
    key = f"dex:{mint.lower()}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    val = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
        if r.status_code == 200:
            d = r.json()
            best = None
            best_liq = -1.0
            for p in d.get("pairs") or []:
                # Only pairs where THIS mint is the base token: when the
                # mint is the quote token (e.g. USDC in a PUMP/USDC pool),
                # baseToken/priceUsd describe the OTHER token.
                bt = p.get("baseToken") or {}
                if bt.get("address") != mint:
                    continue
                try:
                    price = float(p.get("priceUsd")) if p.get("priceUsd") is not None else None
                except (TypeError, ValueError):
                    price = None
                if price is None:
                    continue
                try:
                    liq = float((p.get("liquidity") or {}).get("usd") or 0)
                except (TypeError, ValueError):
                    liq = 0.0
                if liq > best_liq:
                    best_liq = liq
                    try:
                        chg = float((p.get("priceChange") or {}).get("h24"))
                    except (TypeError, ValueError):
                        chg = None
                    best = {
                        "symbol": bt.get("symbol"),
                        "name": bt.get("name"),
                        "usd_price": price,
                        "usd_change_24h_pct": chg,
                    }
            val = best
    except Exception:
        val = None
    _cache_set(key, val, ttl=TOKEN_PRICE_TTL)
    return val


async def _cg_contract_symbol(mint):
    """(symbol, name) for one SPL mint from CoinGecko, or (None, None)."""
    key = f"cgsym:solana:{mint.lower()}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    val = (None, None)
    try:
        d = await coingecko(f"/coins/solana/contract/{mint}")
        if isinstance(d, dict):
            val = (d.get("symbol"), d.get("name"))
    except Exception:
        val = (None, None)
    _cache_set(key, val, ttl=TOKEN_PRICE_TTL)
    return val


async def sol_token_prices(holdings):
    """Price + symbol metadata for SPL holdings.

    `holdings` — [{mint, ui_amount}]. Returns {mint: {symbol, name,
    usd_price, usd_change_24h_pct}}. DexScreener first (one call per
    mint, most-liquid pair); CoinGecko for mints DexScreener did not
    price (max SOL_MAX_CG_PRICES, largest balances first — CoinGecko's
    free tier allows one mint per request). Symbols fall back to the
    CoinGecko contract endpoint for priced mints, then to None (the
    frontend shows the truncated mint)."""
    mints = []
    seen = set()
    for h in holdings or []:
        m = (h.get("mint") or "").strip()
        if m and m not in seen:
            seen.add(m)
            mints.append((m, h.get("ui_amount")))
    # DexScreener for all mints (bounded), most-liquid pair wins.
    order = sorted(mints, key=lambda x: _ui_num(x[1]), reverse=True)
    dex_mints = [m for m, _ in order[:SOL_MAX_DEXSCREENER]]
    sem = asyncio.Semaphore(8)

    async def _one(m):
        async with sem:
            return m, await _dexscreener_token(m)

    dex = {}
    if dex_mints:
        res = await asyncio.gather(*[_one(m) for m in dex_mints], return_exceptions=True)
        for r in res:
            if isinstance(r, tuple):
                m, v = r
                if v:
                    dex[m] = v
    out = {}
    for m, v in dex.items():
        out[m] = {
            "symbol": v.get("symbol"),
            "name": v.get("name"),
            "usd_price": v.get("usd_price"),
            "usd_change_24h_pct": v.get("usd_change_24h_pct"),
        }
    # CoinGecko for mints DexScreener did not price (bounded).
    missing = [m for m, _ in order if m not in out][:SOL_MAX_CG_PRICES]
    if missing:
        res = await asyncio.gather(
            *[cg_token_price_full("solana", m) for m in missing],
            return_exceptions=True,
        )
        for m, r in zip(missing, res):
            if isinstance(r, tuple) and r[0] is not None:
                out[m] = {
                    "symbol": None,
                    "name": None,
                    "usd_price": r[0],
                    "usd_change_24h_pct": r[1],
                }
    # Symbol fallback for priced mints that still lack one.
    need_sym = [m for m, v in out.items() if not v.get("symbol")][:SOL_MAX_CG_SYMBOLS]
    if need_sym:
        res = await asyncio.gather(
            *[_cg_contract_symbol(m) for m in need_sym], return_exceptions=True
        )
        for m, r in zip(need_sym, res):
            if isinstance(r, tuple):
                out[m]["symbol"] = (r[0] or "").upper() or None
                out[m]["name"] = r[1]
    return out


def _ui_num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


async def sol_token_holdings(address):
    """SPL holdings for one address, enriched like
    pricing.enrich_token_balances: returns {items, total_usd,
    tokens_count, tokens_priced_count, nft_count}.

    Items keep the enrich_token_balances shape (address=mint, name,
    symbol, decimals, type="SPL", balance=ui string, usd_*). Only
    non-zero balances are counted. NFT-like accounts (decimals 0,
    amount 1) are counted in nft_count and excluded from the fungible
    items — they have no fungible price and would otherwise read as
    dust holdings."""
    accounts = await sol_token_accounts(address)
    rows = []
    nft_count = 0
    for a in accounts:
        mint = a.get("mint")
        if not mint:
            continue
        try:
            raw = int(a.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        if raw <= 0:
            continue
        dec = a.get("decimals")
        if dec == 0 and raw == 1:
            nft_count += 1
            continue
        rows.append(
            {
                "mint": mint,
                "decimals": dec,
                "balance": token_amount(a.get("amount"), dec),
                "ui_amount": a.get("ui_amount"),
            }
        )
    prices = await sol_token_prices(rows) if rows else {}
    items = []
    total = 0.0
    priced = 0
    for r in rows:
        p = prices.get(r["mint"]) or {}
        price = p.get("usd_price")
        val = None
        if price is not None:
            try:
                val = float(r["balance"]) * float(price)
                total += val
                priced += 1
            except (TypeError, ValueError):
                val = None
        items.append(
            {
                "address": r["mint"],
                "name": p.get("name"),
                "symbol": p.get("symbol"),
                "decimals": r["decimals"],
                "type": "SPL",
                "icon_url": None,
                "balance": r["balance"],
                "usd_price": price,
                "usd_value": val,
                "usd_change_24h_pct": p.get("usd_change_24h_pct"),
            }
        )
    return {
        "items": items,
        "total_usd": total,
        "tokens_count": len(rows) + nft_count,
        "tokens_priced_count": priced,
        "nft_count": nft_count,
    }
