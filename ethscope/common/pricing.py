"""USD pricing for the *Scope apps — per-token prices, token-balance enrichment,
native-coin quotes, and daily price series (all CoinGecko keyless API).

Blockscout `exchange_rate` wins wherever present; CoinGecko
`simple/token_price` is the fallback (keyless tier: ONE contract address
per request, error 10012 otherwise). Every helper degrades to None/empty
on upstream failure — a pricing failure never breaks a caller.
Historical prices are immutable, so daily series use the long chart cache.
"""
import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from common.chains import CHAINS
from common.clients import CHART_TTL, TOKEN_PRICE_TTL, _cache_get, _cache_set, bs, coingecko


async def cg_token_price_full(platform, contract_address):
    """(usd_price, usd_24h_change_pct) for one token contract, or (None, None).
    5-min cache, shared with cg_token_price (same cache key stores the tuple)."""
    key = f"cgprice:{platform}:{contract_address.lower()}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await coingecko(
        f"/simple/token_price/{platform}",
        params={
            "contract_addresses": contract_address,
            "vs_currencies": "usd",
            "include_24hr_change": "true",
        },
    )
    price = change = None
    if isinstance(d, dict):
        entry = d.get(contract_address.lower()) or d.get(contract_address)
        if isinstance(entry, dict):
            if entry.get("usd") is not None:
                try:
                    price = float(entry["usd"])
                except (TypeError, ValueError):
                    price = None
            if entry.get("usd_24h_change") is not None:
                try:
                    change = float(entry["usd_24h_change"])
                except (TypeError, ValueError):
                    change = None
    val = (price, change)
    _cache_set(key, val, ttl=TOKEN_PRICE_TTL)
    return val


async def cg_token_price(platform, contract_address):
    """USD price for one token contract, or None."""
    price, _ = await cg_token_price_full(platform, contract_address)
    return price


async def enrich_token_balances(chain, raw_items, max_cg_lookups=5):
    """Enrich raw Blockscout token-balance entries with USD price/value and
    24h change. `raw_items` are the entries of
    /addresses/{h}/token-balances (a list, or the `items` of a paged dict).

    Returns (items, total_usd). Items keep the shape _address_tokens used
    to return, plus `usd_change_24h_pct` (None when unknown — Blockscout
    gives no 24h change, so it is only known for CoinGecko-priced tokens).
    ERC-721/1155 items get null USD fields.

    CoinGecko budget per call is `max_cg_lookups` concurrent lookups:
    first for ERC-20 items with no Blockscout price at all, then — with
    whatever budget remains — for the 24h change of the largest
    Blockscout-priced items. A CoinGecko failure never raises."""
    from common.formatting import token_amount

    items = []
    for tb in raw_items or []:
        tok = tb.get("token") or {}
        items.append(
            {
                "address": tok.get("address_hash") or tok.get("address"),
                "name": tok.get("name"),
                "symbol": tok.get("symbol"),
                "decimals": tok.get("decimals"),
                "type": tok.get("type"),
                "icon_url": tok.get("icon_url"),
                "balance": token_amount(tb.get("value"), tok.get("decimals")),
                "token_id": tb.get("token_id"),
                "usd_price": None,
                "usd_value": None,
                "usd_change_24h_pct": None,
                "_exchange_rate": tok.get("exchange_rate"),
            }
        )
    # CoinGecko budget per call is `max_cg_lookups` concurrent lookups,
    # spent in this order:
    #   Phase A — 24h change for the largest Blockscout-priced items
    #     (these are the holdings that dominate the total, and Blockscout
    #     gives no 24h change, so without this the portfolio change
    #     estimate would cover almost nothing).
    #   Phase B — price (+change) for items with no Blockscout rate at all.
    # Phase A runs first on purpose: unpriced items are mostly dust/spam
    # with no CoinGecko listing, so pricing them first would burn the
    # whole budget and leave the big holdings without a change figure.
    # A CoinGecko failure never raises.
    platform = CHAINS[chain]["coingecko_platform"]

    def _value_of(it):
        """USD value of an item from its Blockscout rate, or None."""
        if it["type"] != "ERC-20" or it.get("_exchange_rate") is None:
            return None
        try:
            return Decimal(it["balance"]) * Decimal(str(float(it["_exchange_rate"])))
        except (InvalidOperation, ValueError, TypeError):
            return None

    cg_results = {}
    budget = max_cg_lookups
    # Phase A: 24h change for the largest Blockscout-priced items.
    priced = [it for it in items if _value_of(it) is not None]
    priced.sort(key=lambda it: _value_of(it), reverse=True)
    need_change = [it for it in priced if it.get("address")][:3]
    if need_change and budget > 0:
        take = need_change[:budget]
        res = await asyncio.gather(
            *[cg_token_price_full(platform, it["address"]) for it in take],
            return_exceptions=True,
        )
        for it, r in zip(take, res):
            if isinstance(r, tuple):
                cg_results[it["address"].lower()] = r
        budget -= len(take)
    # Phase B: price (+change) for items with no Blockscout rate.
    need_price = [
        it for it in items
        if it["type"] == "ERC-20" and it.get("address") and it.get("_exchange_rate") is None
    ][:budget]
    if need_price:
        res = await asyncio.gather(
            *[cg_token_price_full(platform, it["address"]) for it in need_price],
            return_exceptions=True,
        )
        for it, r in zip(need_price, res):
            if isinstance(r, tuple):
                cg_results.setdefault(it["address"].lower(), r)

    total_usd = Decimal(0)
    for it in items:
        if it["type"] != "ERC-20" or not it["address"]:
            it.pop("_exchange_rate", None)
            continue
        price = change = None
        if it.get("_exchange_rate") is not None:
            try:
                price = float(it["_exchange_rate"])
            except (TypeError, ValueError):
                price = None
        cg = cg_results.get(it["address"].lower())
        if cg:
            if price is None:
                price = cg[0]
            if change is None:
                change = cg[1]
        it.pop("_exchange_rate", None)
        it["usd_change_24h_pct"] = change
        if price is not None:
            try:
                bal = Decimal(it["balance"])
                val = bal * Decimal(str(price))
                it["usd_price"] = price
                it["usd_value"] = float(val)
                total_usd += val
            except (InvalidOperation, ValueError):
                pass
    return items, float(total_usd)


async def native_quote(chain):
    """(usd_price, change_24h_pct) for a chain's native coin, from Blockscout
    /stats (60s cache). Returns (None, None) on failure."""
    key = f"nativequote:{chain}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    val = (None, None)
    try:
        d = await bs(chain, "/stats")
    except Exception:
        d = None
    if d:
        price = change = None
        try:
            price = float(d["coin_price"]) if d.get("coin_price") is not None else None
        except (TypeError, ValueError):
            price = None
        try:
            change = (
                float(d["coin_price_change_percentage"])
                if d.get("coin_price_change_percentage") is not None
                else None
            )
        except (TypeError, ValueError):
            change = None
        val = (price, change)
    _cache_set(key, val)
    return val


def _daily_series(prices):
    """CoinGecko market_chart [[ms, price], ...] -> {YYYY-MM-DD: price},
    one entry per UTC day (last point of the day wins)."""
    out = {}
    for ms, p in prices or []:
        try:
            day = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date().isoformat()
            out[day] = float(p)
        except (TypeError, ValueError, OSError):
            continue
    return out


async def _daily_native_prices(chain, days):
    """{date: usd_price} for a chain's native coin over the last `days` days
    (CoinGecko market_chart, 10-min cache). Empty dict on failure."""
    key = f"{chain}:dailyprice:{days}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    coin = CHAINS[chain]["coingecko_coin"]
    d = await coingecko(
        f"/coins/{coin}/market_chart", params={"vs_currency": "usd", "days": days}
    )
    out = _daily_series(d.get("prices", []) if isinstance(d, dict) else [])
    _cache_set(key, out, ttl=CHART_TTL)
    return out


async def _cg_contract_daily_prices(platform, contract, days):
    """{date: usd_price} for one token contract over the last `days` days
    (CoinGecko contract market_chart, 10-min cache). Empty dict on failure."""
    key = f"cgchart:{platform}:{contract.lower()}:{days}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await coingecko(
        f"/coins/{platform}/contract/{contract}/market_chart",
        params={"vs_currency": "usd", "days": days},
    )
    out = _daily_series(d.get("prices", []) if isinstance(d, dict) else [])
    _cache_set(key, out, ttl=CHART_TTL)
    return out
