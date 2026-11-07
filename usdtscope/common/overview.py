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
from common.freeze import sc_freeze
from common.issuer_risk import sc_issuer_risk
from common.payments_readiness import sc_payments_readiness
from common.stablecoin import STABLECOINS, sc_activity, sc_defi, sc_mint_burn, sc_peg_leaderboard, sc_stats, sc_supply_map, sc_whales, _bridge_label

WORMHOLE_BASE = "https://api.wormholescan.io/api/v1"

AGG_TTL = 120  # seconds, for the /overview aggregate
SECTION_TTL = 300  # seconds, for the slower sections (bridges, movers)


# ---------------------------------------------------------------- scope urls
# Expected Vercel domains, same pattern as every app's config.py SIBLINGS.
# Fly app names differ from scope names where the scope name is taken:
# "overview" on fly.dev belongs to an unrelated planner app, so the
# overview scope deploys as "cryptoscope-overview".
_FLY_APP_NAMES = {"overview": "cryptoscope-overview"}


def _scope_url(scope: str) -> str:
    return f"https://{_FLY_APP_NAMES.get(scope, scope)}.fly.dev"


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


# ---------------------------------------------------------------- mint/burn rollup
async def ov_mint_burn():
    """Net issuance across all 8 stablecoin scopes (feature 4 rollup):
    per-coin 7/30/90d net issuance + demand direction, summed totals,
    and the count of large on-chain mint/burn events per coin. Reuses
    sc_mint_burn per coin; a failed coin gets an error row and is
    excluded from the totals. Kept OUT of /overview: the per-coin
    on-chain scans are slow (Blockscout zero-address queries can take
    ~20s for a coin whose query times out), so this is a standalone
    section the overview page fetches separately, like the peg
    leaderboard. Net issuance is net supply change, not gross
    mint/burn — see the per-scope /mint-burn methodology."""
    key = "overview:mint-burn"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_mint_burn(s) for s in scopes), return_exceptions=True
    )
    rows = []
    for scope, res in zip(scopes, results):
        if isinstance(res, Exception):
            rows.append({
                "scope": scope, "scope_url": _scope_url(scope),
                "symbol": STABLECOINS[scope]["symbol"],
                "name": STABLECOINS[scope]["name"],
                "error": str(res),
            })
            continue
        ni = res.get("net_issuance") or {}
        le = res.get("large_events") or {}
        ds = res.get("demand_signal") or {}
        rows.append({
            "scope": scope,
            "scope_url": _scope_url(scope),
            "symbol": res.get("symbol"),
            "name": res.get("name"),
            "supply_usd": _num(res.get("supply_usd")),
            "net_7d_usd": _num((ni.get("window_7d") or {}).get("net_usd")),
            "net_30d_usd": _num((ni.get("window_30d") or {}).get("net_usd")),
            "net_90d_usd": _num((ni.get("window_90d") or {}).get("net_usd")),
            "net_30d_pct": _num((ni.get("window_30d") or {}).get("pct_of_supply")),
            "flat_supply": bool(ni.get("flat_supply")),
            "largest_mint_day": ni.get("largest_mint_day"),
            "largest_burn_day": ni.get("largest_burn_day"),
            "demand_direction": ds.get("direction"),
            "demand_summary": ds.get("summary"),
            "large_events_count": le.get("count"),
            "large_events_status": le.get("status"),
            "threshold_usd": _num(res.get("threshold_usd")),
            "error": None,
        })

    def _total(field):
        vals = [r.get(field) for r in rows
                if r.get(field) is not None and not r.get("error")]
        return round(sum(vals), 2) if vals else None

    out = {
        "rows": rows,
        "coins_included": sum(1 for r in rows if not r.get("error")),
        "total_supply_usd": _total("supply_usd"),
        "total_net_7d_usd": _total("net_7d_usd"),
        "total_net_30d_usd": _total("net_30d_usd"),
        "total_net_90d_usd": _total("net_90d_usd"),
        "total_large_events": sum(
            (r.get("large_events_count") or 0) for r in rows
            if not r.get("error")),
        "note": "Net issuance is the net change in DeFiLlama daily total circulating supply — net supply change, NOT gross mint/burn. Totals sum only the coins whose mint/burn data loaded.",
        "caveat": "Net issuance is a PROXY for demand, not a direct measure of it. Redemptions, chain migrations and issuer treasury moves also change total supply.",
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


# ---------------------------------------------------------------- whales rollup
async def ov_whales():
    """Whale & exchange exposure across all 8 stablecoin scopes
    (feature 6 rollup): per-coin top-1/10/50 holder share of
    primary-chain supply, identified exchange wallets + lower-bound
    exchange total (top 50, primary chain only — see the per-scope
    /whales methodology), and the largest recent transfer above that
    coin's threshold. Reuses sc_whales per coin; a failed coin gets an
    error row and is excluded from the totals. Kept OUT of /overview:
    the per-coin transfer scans are slow, so this is a standalone
    section the overview page fetches separately, like the mint/burn
    rollup."""
    key = "overview:whales"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_whales(s) for s in scopes), return_exceptions=True
    )
    rows = []
    for scope, res in zip(scopes, results):
        if isinstance(res, Exception):
            rows.append({
                "scope": scope, "scope_url": _scope_url(scope),
                "symbol": STABLECOINS[scope]["symbol"],
                "name": STABLECOINS[scope]["name"],
                "error": str(res),
            })
            continue
        conc = res.get("concentration") or {}
        ex = res.get("exchanges") or {}
        lt = res.get("large_transfers") or {}
        items = lt.get("items") or []
        largest = max(items, key=lambda x: x.get("amount_usd") or 0) if items else None
        rows.append({
            "scope": scope,
            "scope_url": _scope_url(scope),
            "symbol": res.get("symbol"),
            "name": res.get("name"),
            "primary_chain": res.get("primary_chain"),
            "supply_usd": _num(res.get("supply_usd")),
            "primary_chain_share_pct": _num(res.get("primary_chain_share_pct")),
            "holders_total": res.get("holders_total"),
            "top_1_share_pct": _num(conc.get("top_1_share_pct")),
            "top_10_share_pct": _num(conc.get("top_10_share_pct")),
            "top_50_share_pct": _num(conc.get("top_50_share_pct")),
            "holders_counted": conc.get("holders_counted"),
            "exchange_wallets_identified": ex.get("wallets_identified"),
            "exchange_total_usd": _num(ex.get("total_balance_usd")),
            "exchange_total_share_pct": _num(ex.get("total_share_pct")),
            "exchanges_identified": [r.get("exchange") for r in (ex.get("by_exchange") or [])],
            "large_transfers_count": lt.get("count"),
            "large_transfers_status": lt.get("status"),
            "threshold_usd": _num(lt.get("threshold_usd")),
            "largest_transfer": largest,
            "scanned_transfers": lt.get("scanned_transfers"),
            "scan_earliest": lt.get("scan_earliest"),
            "scan_latest": lt.get("scan_latest"),
            "error": None,
        })

    def _total(field):
        vals = [r.get(field) for r in rows
                if r.get(field) is not None and not r.get("error")]
        return round(sum(vals), 2) if vals else None

    out = {
        "rows": rows,
        "coins_included": sum(1 for r in rows if not r.get("error")),
        "total_supply_usd": _total("supply_usd"),
        "total_exchange_identified_usd": _total("exchange_total_usd"),
        "coins_with_exchange_identified": sum(
            1 for r in rows
            if not r.get("error") and (r.get("exchange_wallets_identified") or 0) > 0),
        "total_large_transfers": sum(
            (r.get("large_transfers_count") or 0) for r in rows
            if not r.get("error")),
        "note": "Concentration shares are of PRIMARY-CHAIN supply (Ethereum for 7 coins, Tron for USDD), not of all-chain supply. Exchange totals count only wallets identified by a source name tag among the top 50 holders on the primary chain — a lower bound on exchange-held supply, not a total. Large transfers are primary-chain only, above each coin's own threshold (max($1M, 0.01% of circulating supply)).",
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


# ---------------------------------------------------------------- activity rollup
async def ov_activity():
    """On-chain activity & velocity across all 8 stablecoin scopes
    (feature 7 rollup): per-coin 24h active addresses / tx count /
    transfer volume (with complete flags — incomplete windows are
    lower bounds, never totals), daily rates with their basis,
    annualised velocity on primary-chain supply, and 24h-vs-prior-24h
    trends where both halves are complete. Reuses sc_activity per
    coin; a failed coin gets an error row and is excluded from the
    totals. Kept OUT of /overview: the per-coin transfer scans are
    slow, so this is a standalone section the overview page fetches
    separately, like the mint/burn and whales rollups. Totals sum
    only complete-window figures where stated — the summed 24h volume
    mixes complete windows and extrapolated daily rates, so it is
    labelled an estimate and the count of complete windows is shown."""
    key = "overview:activity"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_activity(s) for s in scopes), return_exceptions=True
    )
    rows = []
    for scope, res in zip(scopes, results):
        if isinstance(res, Exception):
            rows.append({
                "scope": scope, "scope_url": _scope_url(scope),
                "symbol": STABLECOINS[scope]["symbol"],
                "name": STABLECOINS[scope]["name"],
                "error": str(res),
            })
            continue
        w = res.get("windows") or {}
        w24 = w.get("1d") or {}
        w7 = w.get("7d") or {}
        vel = res.get("velocity") or {}
        daily = res.get("daily") or {}
        tr = (res.get("trends") or {}).get("1d_vs_prior_1d") or {}
        cov = res.get("coverage") or {}
        rows.append({
            "scope": scope,
            "scope_url": _scope_url(scope),
            "symbol": res.get("symbol"),
            "name": res.get("name"),
            "primary_chain": res.get("primary_chain"),
            "supply_usd": _num(res.get("supply_usd")),
            "primary_chain_share_pct": _num(res.get("primary_chain_share_pct")),
            "holders_total": res.get("holders_total"),
            "active_24h": w24.get("unique_addresses"),
            "active_24h_complete": bool(w24.get("complete")),
            "active_share_of_holders_pct": w24.get("active_share_of_holders_pct"),
            "tx_24h": w24.get("tx_count"),
            "tx_24h_complete": bool(w24.get("complete")),
            "volume_24h_usd": w24.get("volume_usd"),
            "volume_24h_complete": bool(w24.get("complete")),
            "tx_7d": w7.get("tx_count"),
            "tx_7d_complete": bool(w7.get("complete")),
            "volume_7d_usd": w7.get("volume_usd"),
            "volume_7d_complete": bool(w7.get("complete")),
            "median_transfer_usd": (res.get("median_transfer") or {}).get("median_transfer_usd"),
            "median_transfer_basis": (res.get("median_transfer") or {}).get("basis"),
            "tx_per_day": daily.get("tx_per_day"),
            "volume_per_day_usd": daily.get("volume_per_day_usd"),
            "daily_basis": daily.get("basis"),
            "velocity_annualized": _num(vel.get("annualized")),
            "velocity_vs_total_supply": _num(vel.get("annualized_vs_total_supply")),
            "trend_tx_24h_pct": ((tr.get("tx_count") or {}).get("change_pct")),
            "trend_volume_24h_pct": ((tr.get("volume_usd") or {}).get("change_pct")),
            "trend_active_24h_pct": ((tr.get("active_addresses") or {}).get("change_pct")),
            "trends_comparable": bool(
                (tr.get("tx_count") or {}).get("comparable")),
            "scanned_transfers": cov.get("scanned_transfers"),
            "scan_window_label": cov.get("scan_window_label"),
            "scan_status": cov.get("status"),
            "trading_volume_24h_usd": ((res.get("market") or {}).get("trading_volume_24h_usd")),
            "error": None,
        })

    def _total(field, complete_field=None):
        vals = [r.get(field) for r in rows
                if r.get(field) is not None and not r.get("error")
                and (complete_field is None or r.get(complete_field))]
        return round(sum(vals), 2) if vals else None

    out = {
        "rows": rows,
        "coins_included": sum(1 for r in rows if not r.get("error")),
        "coins_with_complete_24h": sum(
            1 for r in rows
            if not r.get("error") and r.get("volume_24h_complete")),
        "total_supply_usd": _total("supply_usd"),
        "total_active_24h_complete_only": _total("active_24h", "active_24h_complete"),
        "total_tx_24h_complete_only": _total("tx_24h", "tx_24h_complete"),
        "total_volume_24h_complete_only": _total("volume_24h_usd", "volume_24h_complete"),
        "total_volume_per_day_usd_estimate": _total("volume_per_day_usd"),
        "total_tx_per_day_estimate": _total("tx_per_day"),
        "note": "Per-coin figures are PRIMARY-CHAIN only (Ethereum for 7 coins, Tron for USDD). A 24h window marked complete is the window total on the primary chain; an incomplete window is a LOWER BOUND. Totals with the _complete_only suffix sum only complete windows. The per-day estimate totals sum each coin's daily rate (observed where the window is complete, extrapolated from the scanned sample otherwise) — an estimate, not a measurement.",
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


# ---------------------------------------------------------------- payments-readiness rollup
async def ov_payments_readiness():
    """Payments readiness across all 8 stablecoin scopes (feature 8
    rollup): per-coin 0-100 score with band and the five input
    sub-scores, ranked by score descending (unknown scores last).
    Reuses sc_payments_readiness per coin; a failed coin gets an
    error row and is excluded from the ranking. Kept OUT of
    /overview: the per-coin activity scans are slow, so this is a
    standalone section the overview page fetches separately, like the
    mint/burn, whales and activity rollups."""
    key = "overview:payments-readiness"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_payments_readiness(s) for s in scopes), return_exceptions=True
    )
    rows = []
    for scope, res in zip(scopes, results):
        if isinstance(res, Exception):
            rows.append({
                "scope": scope, "scope_url": _scope_url(scope),
                "symbol": STABLECOINS[scope]["symbol"],
                "name": STABLECOINS[scope]["name"],
                "error": str(res),
            })
            continue
        inp = res.get("inputs") or {}

        def _sub(k):
            v = inp.get(k) or {}
            return v.get("score"), v.get("evidence") or {}

        peg_s, peg_ev = _sub("peg_stability")
        liq_s, liq_ev = _sub("liquidity")
        cov_s, cov_ev = _sub("chain_coverage")
        fee_s, fee_ev = _sub("fees_finality")
        frz_s, frz_ev = _sub("freeze_risk")
        rows.append({
            "scope": scope,
            "scope_url": _scope_url(scope),
            "symbol": res.get("symbol"),
            "name": res.get("name"),
            "issuer": res.get("issuer"),
            "primary_chain": res.get("primary_chain"),
            "score": res.get("score"),
            "band": res.get("band"),
            "weights_used": res.get("weights_used"),
            "inputs_unknown": res.get("inputs_unknown"),
            "peg_score": peg_s,
            "deviation_bps": peg_ev.get("current_deviation_bps"),
            "depeg_days_365d": peg_ev.get("depeg_days_365d"),
            "liquidity_score": liq_s,
            "near_par_liquidity_usd": liq_ev.get("near_par_liquidity_usd"),
            "daily_transfer_volume_usd": liq_ev.get("daily_transfer_volume_usd"),
            "daily_volume_basis": liq_ev.get("daily_volume_basis"),
            "chain_coverage_score": cov_s,
            "material_chains": cov_ev.get("material_chains"),
            "chain_count": cov_ev.get("chain_count"),
            "fees_score": fee_s,
            "typical_transfer_fee_usd": fee_ev.get("typical_transfer_fee_usd"),
            "finality_seconds": fee_ev.get("finality_seconds"),
            "freeze_score": frz_s,
            "freeze_functions_found": frz_ev.get("freeze_functions_found"),
            "freeze_events": frz_ev.get("freeze_events"),
            "error": None,
        })
    scored = [r for r in rows if not r.get("error") and r.get("score") is not None]
    scored.sort(key=lambda r: -r["score"])
    unscored = [r for r in rows if not r.get("error") and r.get("score") is None]
    errored = [r for r in rows if r.get("error")]
    ranked = scored + unscored + errored
    for i, r in enumerate(ranked, 1):
        r["rank"] = i if not r.get("error") and r.get("score") is not None else None
    out = {
        "rows": ranked,
        "coins_included": sum(1 for r in rows if not r.get("error")),
        "coins_scored": len(scored),
        "methodology": (
            "Payments readiness 0-100 (higher = more ready for a "
            "company to use for payments). Weights: peg stability 30%, "
            "liquidity 25%, chain coverage 20%, fees/finality 15%, "
            "freeze risk 10% (this implementation's stated choice — no "
            "Par spec exists). Fees/finality are primary-chain figures "
            "only (live gas/energy price x a typical transfer — 65,000 "
            "gas EVM / 14,000 energy Tron, both ESTIMATES). Freeze "
            "risk scores issuer CAPABILITY (live eth_call probes; "
            "TronScan method map for USDD) — observed freeze events "
            "are tracked in /freeze (feature 10); this rollup's "
            "freeze input still scores capability only. Unknown "
            "inputs are excluded and the remaining weights "
            "renormalised per coin."
        ),
        "note": "Per-coin figures are PRIMARY-CHAIN only (Ethereum for 7 coins, Tron for USDD). Bands: >= 75 ready, >= 50 usable, >= 25 limited, < 25 not ready.",
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


async def ov_freeze():
    """Freeze & blacklist monitor across all 8 stablecoin scopes
    (feature 10 rollup): per-coin freeze capability (feature-8 probe
    reused, cross-checked against the live contract ABI), frozen
    supply (LOWER-BOUND ESTIMATE — the blacklist is not enumerable
    on-chain; see the per-scope /freeze methodology), freeze/
    blacklist event counts in the scanned window, and sanctions
    matches. Reuses sc_freeze per coin; a failed coin gets an error
    row and is excluded from the totals. Kept OUT of /overview: the
    per-coin log scans + Multicall3 balance batches are slow, so
    this is a standalone section the overview page fetches
    separately, like the mint/burn, whales, activity and
    payments-readiness rollups. The total frozen supply sums only
    the coins with a measured estimate and is itself a lower bound,
    never a total."""
    key = "overview:freeze"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_freeze(s) for s in scopes), return_exceptions=True
    )
    rows = []
    for scope, res in zip(scopes, results):
        if isinstance(res, Exception):
            rows.append({
                "scope": scope, "scope_url": _scope_url(scope),
                "symbol": STABLECOINS[scope]["symbol"],
                "name": STABLECOINS[scope]["name"],
                "error": str(res),
            })
            continue
        cap = res.get("capability") or {}
        frz = res.get("frozen_supply") or {}
        evs = res.get("events") or {}
        dst = res.get("destroyed_black_funds") or {}
        san = res.get("sanctions") or {}
        rows.append({
            "scope": scope,
            "scope_url": _scope_url(scope),
            "symbol": res.get("symbol"),
            "name": res.get("name"),
            "issuer": res.get("issuer"),
            "primary_chain": res.get("primary_chain"),
            "can_freeze": cap.get("can_freeze"),
            "probe_functions_found": cap.get("probe_functions_found"),
            "pause_function_found": cap.get("pause_function_found"),
            "frozen_supply_status": frz.get("status"),
            "frozen_supply_amount": _num(frz.get("amount")),
            "frozen_supply_usd": _num(frz.get("amount_usd")),
            "frozen_addresses_count": frz.get("addresses_count"),
            "frozen_addresses_with_balance": frz.get("addresses_with_balance"),
            "frozen_share_of_primary_supply_pct": _num(
                frz.get("share_of_primary_supply_pct")),
            "events_status": evs.get("status"),
            "events_window_days": (evs.get("window") or {}).get("window_days"),
            "events_extended_lookback": (evs.get("window") or {}).get("extended_lookback"),
            "events_total": evs.get("total_events"),
            "events_counts_by_kind": evs.get("counts_by_kind"),
            "destroyed_events_count": dst.get("events_count"),
            "destroyed_total_usd": _num(dst.get("total_usd")),
            "sanctions_status": san.get("status"),
            "sanctions_matches_total": san.get("matches_total"),
            "sanctions_summary": san.get("summary"),
            "error": None,
        })
    measured = [r for r in rows if not r.get("error")
                and r.get("frozen_supply_usd") is not None]
    out = {
        "rows": rows,
        "coins_included": sum(1 for r in rows if not r.get("error")),
        "coins_with_freeze_capability": sum(
            1 for r in rows if not r.get("error") and r.get("can_freeze")),
        "coins_measured": len(measured),
        "total_frozen_supply_usd_lower_bound": round(
            sum(r["frozen_supply_usd"] for r in measured), 2) if measured else None,
        "total_frozen_addresses": sum(
            (r.get("frozen_addresses_count") or 0) for r in rows
            if not r.get("error")),
        "total_freeze_events": sum(
            (r.get("events_total") or 0) for r in rows if not r.get("error")),
        "total_destroyed_usd": round(sum(
            (r.get("destroyed_total_usd") or 0) for r in rows
            if not r.get("error") and r.get("destroyed_total_usd") is not None
        ), 2) if any(r.get("destroyed_total_usd") is not None for r in rows
                     if not r.get("error")) else None,
        "total_sanctions_matches": sum(
            (r.get("sanctions_matches_total") or 0) for r in rows
            if not r.get("error")),
        "note": (
            "Frozen supply per coin is a LOWER-BOUND ESTIMATE (the "
            "blacklist is not enumerable on-chain — it sums balances "
            "of addresses the scanned window's freeze events show as "
            "currently frozen; addresses frozen before the window are "
            "missed). The total sums only the coins with a measured "
            "estimate and is itself a lower bound, never a total. "
            "Coins with no freeze function (DAI, USDe) and USDD (no "
            "freeze function observed on Tron) carry no frozen "
            "supply. TUSD's capability rests on its implementation "
            "ABI (write-only blacklist), not feature 8's read probe."
        ),
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


def _issuer_risk_row(scope, res):
    """One rollup row from a sc_issuer_risk payload (or an
    exception). Shared by ov_issuer_risk and the phase-3 item-6
    detail endpoint (common/issuer_risk_api.py) so both build the
    ranking from the same payloads — a ranking row and its full
    breakdown can never disagree. A failed coin gets an error row."""
    if isinstance(res, Exception):
        return {
            "scope": scope, "scope_url": _scope_url(scope),
            "symbol": STABLECOINS[scope]["symbol"],
            "name": STABLECOINS[scope]["name"],
            "error": str(res),
        }
    inp = res.get("inputs") or {}

    def _sub(k):
        v = inp.get(k) or {}
        return v.get("score"), v.get("evidence") or {}

    dep_s, dep_ev = _sub("depeg_risk")
    res_s, res_ev = _sub("reserve_transparency")
    iss_s, iss_ev = _sub("issuance_direction")
    con_s, con_ev = _sub("concentration")
    frz_s, frz_ev = _sub("freeze_exposure")
    adp_s, adp_ev = _sub("adoption")
    return {
        "scope": scope,
        "scope_url": _scope_url(scope),
        "symbol": res.get("symbol"),
        "name": res.get("name"),
        "issuer": res.get("issuer"),
        "score": res.get("score"),
        "band": res.get("band"),
        "weights_used": res.get("weights_used"),
        "inputs_unknown": res.get("inputs_unknown"),
        "depeg_score": dep_s,
        "deviation_bps": dep_ev.get("current_deviation_bps"),
        "depeg_days_365d": dep_ev.get("depeg_days_365d"),
        "reserve_score": res_s,
        "attestation_status": res_ev.get("status"),
        "attestation_status_label": res_ev.get("status_label"),
        "auditor": res_ev.get("auditor"),
        "days_since_attestation": res_ev.get("days_since_attestation"),
        "reserve_to_outstanding_ratio": res_ev.get("reserve_to_outstanding_ratio"),
        "issuance_score": iss_s,
        "net_30d_pct_of_supply": iss_ev.get("net_30d_pct_of_supply"),
        "net_90d_pct_of_supply": iss_ev.get("net_90d_pct_of_supply"),
        "demand_direction": iss_ev.get("demand_direction"),
        "flat_supply": iss_ev.get("flat_supply"),
        "concentration_score": con_s,
        "top_10_share_pct": con_ev.get("top_10_share_pct"),
        "exchange_share_pct": con_ev.get("exchange_share_pct"),
        "freeze_score": frz_s,
        "can_freeze": frz_ev.get("can_freeze"),
        "freeze_events_total": frz_ev.get("events_total"),
        "frozen_supply_usd": frz_ev.get("frozen_supply_usd"),
        "frozen_share_of_primary_supply_pct": frz_ev.get("frozen_share_of_primary_supply_pct"),
        "sanctions_matches_total": frz_ev.get("sanctions_matches_total"),
        "adoption_score": adp_s,
        "material_chains": adp_ev.get("material_chains"),
        "typical_transfer_fee_usd": adp_ev.get("typical_transfer_fee_usd"),
        "error": None,
    }


def _rank_issuer_risk_rows(rows):
    """Rank issuer-risk rows by score ascending (rank 1 = least
    risky); unknown scores last, errored rows last of all and
    unranked. Same helper for the rollup and the item-6 detail
    endpoint."""
    scored = [r for r in rows if not r.get("error") and r.get("score") is not None]
    scored.sort(key=lambda r: r["score"])
    unscored = [r for r in rows if not r.get("error") and r.get("score") is None]
    errored = [r for r in rows if r.get("error")]
    ranked = scored + unscored + errored
    for i, r in enumerate(ranked, 1):
        r["rank"] = i if not r.get("error") and r.get("score") is not None else None
    return ranked


async def ov_issuer_risk():
    """Issuer risk across all 8 stablecoin scopes (feature 11 rollup):
    per-coin 0-100 issuer risk score (higher = riskier) with band and
    the six input sub-scores, ranked by score ascending (lowest risk
    first, rank 1 = least risky issuer; unknown scores last).
    Reuses sc_issuer_risk per coin; a failed coin gets an error row
    and is excluded from the ranking. Kept OUT of /overview: the
    per-coin freeze log scans + activity scans are slow, so this is a
    standalone section the overview page fetches separately, like the
    mint/burn, whales, activity, payments-readiness and freeze
    rollups."""
    key = "overview:issuer-risk"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_issuer_risk(s) for s in scopes), return_exceptions=True
    )
    rows = [_issuer_risk_row(scope, res) for scope, res in zip(scopes, results)]
    ranked = _rank_issuer_risk_rows(rows)
    scored = [r for r in ranked if not r.get("error") and r.get("score") is not None]
    out = {
        "rows": ranked,
        "coins_included": sum(1 for r in rows if not r.get("error")),
        "coins_scored": len(scored),
        "methodology": (
            "Issuer risk 0-100 (higher = riskier). Weights: depeg "
            "risk 25% (feature-1 score as-is), reserve transparency "
            "25% (feature-3 attestation tracker — transparency, NOT "
            "a live reserve measurement), issuance direction 15% "
            "(feature-4 net issuance, 30d primary / 90d "
            "confirmation; frozen supply scores high), concentration "
            "15% (feature-6 top-10 holder share of primary-chain "
            "supply + identified-exchange share, a lower bound), "
            "freeze exposure 15% (feature-10 capability + observed "
            "use: freeze events, frozen supply share — a LOWER-BOUND "
            "ESTIMATE — destroyed funds, sanctions matches), adoption "
            "5% (feature-8 chain-coverage + fees/finality legs only, "
            "inverted — the peg/liquidity/freeze legs are excluded "
            "to avoid double-counting). Unknown inputs are excluded "
            "and the remaining weights renormalised per coin. Rank 1 "
            "= lowest risk. Heuristic screening signal — not "
            "investment advice, not a rating, not a guarantee."
        ),
        "note": "Per-coin concentration, freeze and fee figures are PRIMARY-CHAIN only (Ethereum for 7 coins, Tron for USDD). Bands: < 25 low, < 50 moderate, < 75 elevated, >= 75 high.",
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


async def ov_defi():
    """DeFi deployment & yield across all 8 stablecoin scopes (feature 9
    rollup): per-coin best lending APY (protocol/chain), lending APY
    range across venues, lending market count, lending TVL, largest LP
    pair, LP TVL, total DeFi TVL and its share of supply, ranked by
    total DeFi TVL descending. Reuses sc_defi per coin; a failed coin
    gets an error row and is excluded from the ranking. Kept OUT of
    /overview: the yields snapshot is a ~11MB download, so this is a
    standalone section the overview page fetches separately, like the
    mint/burn, whales, activity and payments-readiness rollups. The
    yields snapshot is cached server-side and shared across coins, so
    one rollup call downloads it once."""
    key = "overview:defi"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_defi(s) for s in scopes), return_exceptions=True
    )
    rows = []
    for scope, res in zip(scopes, results):
        if isinstance(res, Exception):
            rows.append({
                "scope": scope, "scope_url": _scope_url(scope),
                "symbol": STABLECOINS[scope]["symbol"],
                "name": STABLECOINS[scope]["name"],
                "error": str(res),
            })
            continue
        lend = res.get("lending") or {}
        lp = res.get("lp") or {}
        tot = res.get("totals") or {}
        rng = lend.get("supply_apy_range_pct") or {}
        best = lend.get("best") or {}
        top_lp = lp.get("top_pair") or {}
        rows.append({
            "scope": scope,
            "scope_url": _scope_url(scope),
            "symbol": res.get("symbol"),
            "name": res.get("name"),
            "as_of": res.get("as_of"),
            "supply_usd": res.get("supply_usd"),
            "best_lending_apy_pct": best.get("supply_apy_pct"),
            "best_lending_protocol": best.get("protocol_label"),
            "best_lending_chain": best.get("chain"),
            "lending_apy_min_pct": rng.get("min_pct"),
            "lending_apy_max_pct": rng.get("max_pct"),
            "lending_apy_median_pct": rng.get("median_pct"),
            "lending_apy_tvl_weighted_pct": rng.get("tvl_weighted_pct"),
            "lending_markets_total": lend.get("markets_total"),
            "lending_tvl_usd": lend.get("tvl_usd"),
            "majors_absent": lend.get("majors_absent"),
            "lp_pairs_total": lp.get("pairs_total"),
            "lp_tvl_usd": lp.get("tvl_usd"),
            "top_lp_pair": top_lp.get("pair"),
            "top_lp_project": top_lp.get("project_label"),
            "top_lp_chain": top_lp.get("chain"),
            "top_lp_tvl_usd": top_lp.get("tvl_usd"),
            "top_lp_apy_pct": top_lp.get("apy_pct"),
            "defi_tvl_usd": tot.get("defi_tvl_usd"),
            "defi_tvl_share_of_supply_pct": tot.get("defi_tvl_share_of_supply_pct"),
            "error": None,
        })
    ok = [r for r in rows if not r.get("error")]
    ok.sort(key=lambda r: -((r.get("defi_tvl_usd") or 0)))
    errored = [r for r in rows if r.get("error")]
    ranked = ok + errored
    for i, r in enumerate(ranked, 1):
        r["rank"] = i if not r.get("error") else None
    out = {
        "rows": ranked,
        "coins_included": len(ok),
        "total_defi_tvl_usd": round(sum(r.get("defi_tvl_usd") or 0 for r in ok), 2),
        "total_lending_tvl_usd": round(sum(r.get("lending_tvl_usd") or 0 for r in ok), 2),
        "total_lp_tvl_usd": round(sum(r.get("lp_tvl_usd") or 0 for r in ok), 2),
        "as_of": max((r.get("as_of") or 0 for r in ok), default=None) or None,
        "methodology": (
            "Best lending APY is the highest single-asset lending-market "
            "supply APY per coin — shown alongside the min/max range "
            "across venues, never alone. Total DeFi TVL = lending TVL "
            "(lending markets only; vaults/aggregators excluded) + LP "
            "TVL (largest pool per DEX/chain/pair; pool TVL counts the "
            "whole pool, both sides, so it is an exposure measure, not "
            "the coin's own deployed balance). APYs are a snapshot at "
            "as_of and move constantly."
        ),
        "note": "Borrow APYs are unavailable keyless (DeFiLlama /poolsBorrow is paywalled, HTTP 402). Live per-market collateral eligibility, LTV and liquidation thresholds are not exposed by any keyless API.",
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


# ---------------------------------------------------------------- supply-map rollup
async def ov_supply_map():
    """Cross-chain supply distribution across all 8 stablecoin scopes
    (feature 5 rollup): per chain, total stablecoin supply summed
    across the coins, share of the 8-coin total, and the top coins on
    that chain; plus per-coin covered shares and the bridged-in stock
    by bridge across all coins. Reuses sc_supply_map per coin; a
    failed coin gets an error row and is excluded from the totals.
    Standalone (not in /overview): the per-coin maps are computed from
    DeFiLlama's large per-chain history payloads. Covered vs unknown
    is summed honestly — unknown is never folded into a chain."""
    key = "overview:supply-map"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_supply_map(s) for s in scopes), return_exceptions=True
    )
    rows = []
    per_chain = {}
    bridge_totals = {}
    for scope, res in zip(scopes, results):
        if isinstance(res, Exception):
            rows.append({
                "scope": scope, "scope_url": _scope_url(scope),
                "symbol": STABLECOINS[scope]["symbol"],
                "name": STABLECOINS[scope]["name"],
                "error": str(res),
            })
            continue
        rows.append({
            "scope": scope,
            "scope_url": _scope_url(scope),
            "symbol": res.get("symbol"),
            "name": res.get("name"),
            "total_supply_usd": _num(res.get("total_supply_usd")),
            "covered_supply_usd": _num(res.get("covered_supply_usd")),
            "covered_share_pct": _num(res.get("covered_share_pct")),
            "unknown_supply_usd": _num(res.get("unknown_supply_usd")),
            "chain_count": res.get("chain_count"),
            "bridged_total_usd": _num(res.get("bridged_total_usd")),
            "error": None,
        })
        for c in res.get("chains") or []:
            e = per_chain.setdefault(c["chain"], {"total_usd": 0.0, "coins": []})
            e["total_usd"] += c.get("supply_usd") or 0
            e["coins"].append({
                "symbol": res.get("symbol"),
                "supply_usd": c.get("supply_usd"),
            })
        for b in res.get("bridges_summary") or []:
            bt = bridge_totals.setdefault(b["bridge"], 0.0)
            bridge_totals[b["bridge"]] = bt + (b.get("total_usd") or 0)

    def _total(field):
        vals = [r.get(field) for r in rows
                if r.get(field) is not None and not r.get("error")]
        return round(sum(vals), 2) if vals else None

    grand_total = _total("total_supply_usd")
    chain_rows = []
    for chain, e in sorted(per_chain.items(), key=lambda kv: -kv[1]["total_usd"]):
        coins = sorted(e["coins"], key=lambda c: -(c.get("supply_usd") or 0))
        chain_rows.append({
            "chain": chain,
            "total_supply_usd": round(e["total_usd"], 2),
            "share_pct": round(e["total_usd"] / grand_total * 100.0, 2) if grand_total else None,
            "coins": coins[:3],
            "coin_count": len(coins),
        })
    bridged_grand = round(sum(bridge_totals.values()), 2)
    out = {
        "chains": chain_rows,
        "chain_count": len(chain_rows),
        "coins": rows,
        "coins_included": sum(1 for r in rows if not r.get("error")),
        "total_supply_usd": grand_total,
        "total_covered_usd": _total("covered_supply_usd"),
        "total_unknown_usd": _total("unknown_supply_usd"),
        "bridged_total_usd": bridged_grand,
        "bridges_summary": [
            {"bridge": name, "bridge_label": _bridge_label(name), "total_usd": round(v, 2),
             "share_of_bridged_pct": round(v / bridged_grand * 100.0, 2) if bridged_grand else None}
            for name, v in sorted(bridge_totals.items(), key=lambda kv: -kv[1])
        ],
        "note": "Per-chain totals sum the 8 coins' DeFiLlama chain-mapped supply. Unknown (unattributed) supply is summed separately and never assigned to a chain. Bridged totals are the current bridged-in stock per bridge, not flows.",
        "sources": ["DeFiLlama stablecoins API"],
    }
    _cache_set(key, out, ttl=SECTION_TTL)
    return out


# ---------------------------------------------------------------- peg leaderboard
async def ov_peg_leaderboard():
    """Stablecoin peg-stability leaderboard (same table as each scope's
    /peg-leaderboard, no coin highlighted). Degrades per-coin: a failed
    coin is an error row in sc_peg_leaderboard, never a failed table."""
    return await sc_peg_leaderboard()


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
    ("GET", "/stablecoin-leaderboard", "Peg-stability leaderboard across all 8 stablecoin scopes: current deviation, max |deviation| 30d/all-time, off-peg days 30/90/365d, depeg episodes in 365d with median/max recovery days, 30d volatility. Ranked by fewest off-peg days in 365d. Daily closes only — intraday depegs not counted.", [], "/stablecoin-leaderboard"),
    ("GET", "/mint-burn", "Net issuance rollup across all 8 stablecoin scopes (feature 4): per-coin 7/30/90d net issuance, demand direction, and large on-chain mint/burn event counts, plus summed totals. Standalone (not in /overview) — the per-coin on-chain scans are slow. Net issuance is net supply change, not gross mint/burn.", [], "/mint-burn"),
    ("GET", "/supply-feed", "Supply & flow feed (phase 3, item 8): daily net-issuance series + large on-chain mint/burn events across all 8 stablecoin scopes, paginated and filterable by coin (?coin=USDC, comma-separated, symbol or scope), record kind (?kind=net_change / mint / burn) and time window (?from= / ?to= unix epoch, ?days=N last N days, 1-365, default 30). Two record types, newest first: daily_net_change rows (one per coin per day — the SERIES form of net issuance, labelled as such, never presented as discrete mint/burn events) and on_chain_event rows (large individual mints/burns above max($1M, 0.1% of circulating supply), latest 20 per coin). Totals sum 7/30/90d net issuance across the coins that loaded (total 7d −$3.93B, 30d −$10.94B, 90d −$6.67B at the Nov 6, 2026 check — net supply change, NOT gross mint/burn). Per-coin coverage blocks carry 7/30/90d nets with partial flags, supply-history ranges and gaps (USDD's 83-day gap), the flat-supply flag (TUSD), on-chain event scan status (USDT unavailable) and top-5 chains with per-chain 7d/30d net change (bridges + local mint/burn combined — NOT a bridge-flow measurement). Built on common/stablecoin.py wholesale (feature-4 /mint-burn + feature-5 /supply-map); per-coin data cached 300s. Standalone (not in /overview) — the per-coin on-chain scans are slow. Rendered at #/supply-feed. The example_response below is the feed for USDC + DAI over 30d, 2 rows per page.", ['coin (optional, symbol or scope, comma-separated, default all 8 coins)', 'kind (optional, net_change / mint / burn)', 'from (optional, unix epoch seconds)', 'to (optional, unix epoch seconds)', 'days (optional, last N days, 1-365, default 30)', 'page (optional, default 1)', 'page_size (optional, default 50, max 100)'], "/supply-feed", {"paid_api": True, "example_response": {'items': [{'record_type': 'on_chain_event', 'kind': 'mint', 'timestamp': 1794005267, 'date': '2026-11-06T22:47:47Z', 'coin': 'DAI', 'symbol': 'DAI', 'scope': 'daiscope', 'issuer': 'MakerDAO / Sky', 'chain': 'ethereum', 'direction': 'mint', 'amount': 46816132.5247, 'amount_usd': 46826311.08, 'tx_hash': '0xc373dce99a6542761a16a49d1f3f5db337607701e75ef6f46439da6157714b91', 'block': 26358894.0, 'from': '0x0000000000000000000000000000000000000000', 'to': '0xf6e72db5454dd049d0788e411b06cfaf16853042'}, {'record_type': 'on_chain_event', 'kind': 'burn', 'timestamp': 1794005087, 'date': '2026-11-06T22:44:47Z', 'coin': 'DAI', 'symbol': 'DAI', 'scope': 'daiscope', 'issuer': 'MakerDAO / Sky', 'chain': 'ethereum', 'direction': 'burn', 'amount': 47002324.957173, 'amount_usd': 47012543.99, 'tx_hash': '0x5f95d683a2e5f3856b80e4cb7c747ed866b2d7e05adc6ebbb6c0e4ed1e747670', 'block': 26358879.0, 'from': '0x3225737a9bbb6473cb4a45b7244aca2befdb276a', 'to': '0x0000000000000000000000000000000000000000'}], 'page': 1, 'page_size': 2, 'total_pages': 37, 'total_filtered': 73, 'total_items': 73, 'counts_by_coin': {'DAI': 43, 'USDC': 30}, 'counts_by_kind': {'mint': 6, 'burn': 7, 'net_change': 60}, 'totals': {'total_supply_usd': 75207827483.74, 'total_net_7d_usd': -843872391.73, 'total_net_30d_usd': -3705357348.73, 'total_net_90d_usd': -1688292667.73, 'coins_included': 2, 'large_events_in_window': 13, 'note': 'Totals sum only the coins whose mint/burn data loaded. Net issuance is net supply change, NOT gross mint/burn.'}, 'filters': {'coins': ['USDC', 'DAI'], 'kind': None, 'from': 1791416020, 'to': None, 'days': 30}, 'coins': [{'scope': 'usdcscope', 'symbol': 'USDC', 'name': 'USD Coin', 'issuer': 'Circle', 'supply_usd': 70535581526.51599, 'price_usd': 0.9998335366500001, 'supply_history_first': 1536624000, 'supply_history_last': 1793923200, 'supply_history_points': 2979, 'supply_gap': None, 'flat_supply': False, 'net_7d_usd': -754093787.05, 'net_30d_usd': -3584450575.05, 'net_90d_usd': -1563149001.05, 'net_7d_partial': False, 'net_30d_partial': False, 'net_90d_partial': False, 'largest_mint_day': {'date': 1787270400, 'delta_usd': 859092516.0}, 'largest_burn_day': {'date': 1789516800, 'delta_usd': -766971210.0}, 'demand_direction': 'shrinking', 'daily_rows': 2978, 'on_chain_events': {'count': 0, 'cap': 20, 'status': 'ok', 'status_note': None, 'threshold_usd': 70535581.53, 'primary_chain': 'ethereum', 'primary_chain_share_pct': 60.75, 'scanned_zero_address_transfers': 500, 'scan_earliest': 1794002243}, 'chains': [{'chain': 'Ethereum', 'supply_usd': 42848407058.04029, 'share_pct': 60.75, 'change_7d': {'net_usd': -1119184992.12, 'pct': -2.53, 'start_date': 1793318400, 'end_date': 1793923200, 'window_days': 7, 'partial': False}, 'change_30d': {'net_usd': -2640689593.12, 'pct': -5.78, 'start_date': 1791331200, 'end_date': 1793923200, 'window_days': 30, 'partial': False}}, {'chain': 'Solana', 'supply_usd': 7003740135.108945, 'share_pct': 9.93, 'change_7d': {'net_usd': 214832328.25, 'pct': 3.15, 'start_date': 1793318400, 'end_date': 1793923200, 'window_days': 7, 'partial': False}, 'change_30d': {'net_usd': -175456867.75, 'pct': -2.43, 'start_date': 1791331200, 'end_date': 1793923200, 'window_days': 30, 'partial': False}}, {'chain': 'Hyperliquid L1', 'supply_usd': 6579215625.655549, 'share_pct': 9.33, 'change_7d': {'net_usd': -216514999.28, 'pct': -3.18, 'start_date': 1793318400, 'end_date': 1793923200, 'window_days': 7, 'partial': False}, 'change_30d': {'net_usd': -779856452.28, 'pct': -10.57, 'start_date': 1791331200, 'end_date': 1793923200, 'window_days': 30, 'partial': False}}, {'chain': 'Base', 'supply_usd': 4234887593.173839, 'share_pct': 6.0, 'change_7d': {'net_usd': -11678096.16, 'pct': -0.27, 'start_date': 1793318400, 'end_date': 1793923200, 'window_days': 7, 'partial': False}, 'change_30d': {'net_usd': -131961386.16, 'pct': -2.97, 'start_date': 1791331200, 'end_date': 1793923200, 'window_days': 30, 'partial': False}}, {'chain': 'Arbitrum', 'supply_usd': 2273550289.158309, 'share_pct': 3.22, 'change_7d': {'net_usd': 171350191.01, 'pct': 8.06, 'start_date': 1793318400, 'end_date': 1793923200, 'window_days': 7, 'partial': False}, 'change_30d': {'net_usd': -14418853.99, 'pct': -0.62, 'start_date': 1791331200, 'end_date': 1793923200, 'window_days': 30, 'partial': False}}], 'chain_note': 'Per-chain net change (7d/30d) combines bridges + local mint/burn — NOT a bridge-flow measurement. Covered 99.73% of total supply; $190198577.43 unattributed.', 'error': None}, {'scope': 'daiscope', 'symbol': 'DAI', 'name': 'Dai', 'issuer': 'MakerDAO / Sky', 'supply_usd': 4672245957.22608, 'price_usd': 1.0002174154772376, 'supply_history_first': 1574121600, 'supply_history_last': 1793923200, 'supply_history_points': 2545, 'supply_gap': None, 'flat_supply': False, 'net_7d_usd': -89778604.68, 'net_30d_usd': -120906773.68, 'net_90d_usd': -125143666.68, 'net_7d_partial': False, 'net_30d_partial': False, 'net_90d_partial': False, 'largest_mint_day': {'date': 1793232000, 'delta_usd': 28369074.0}, 'largest_burn_day': {'date': 1793404800, 'delta_usd': -51679946.0}, 'demand_direction': 'shrinking', 'daily_rows': 2544, 'on_chain_events': {'count': 13, 'cap': 20, 'status': 'ok', 'status_note': None, 'threshold_usd': 4672245.96, 'primary_chain': 'ethereum', 'primary_chain_share_pct': 87.86, 'scanned_zero_address_transfers': 500, 'scan_earliest': 1793988935}, 'chains': [{'chain': 'Ethereum', 'supply_usd': 4105143019.1201835, 'share_pct': 87.86, 'change_7d': {'net_usd': -86329309.6, 'pct': -2.06, 'start_date': 1793318400, 'end_date': 1793923200, 'window_days': 7, 'partial': False}, 'change_30d': {'net_usd': -98857693.6, 'pct': -2.35, 'start_date': 1791331200, 'end_date': 1793923200, 'window_days': 30, 'partial': False}}, {'chain': 'Polygon', 'supply_usd': 440908005.56848365, 'share_pct': 9.44, 'change_7d': {'net_usd': -433410.5, 'pct': -0.1, 'start_date': 1793318400, 'end_date': 1793923200, 'window_days': 7, 'partial': False}, 'change_30d': {'net_usd': -15636749.5, 'pct': -3.41, 'start_date': 1791331200, 'end_date': 1793923200, 'window_days': 30, 'partial': False}}, {'chain': 'Fantom', 'supply_usd': 35117405.238405965, 'share_pct': 0.75, 'change_7d': {'net_usd': 0.24, 'pct': 0.0, 'start_date': 1793318400, 'end_date': 1793923200, 'window_days': 7, 'partial': False}, 'change_30d': {'net_usd': 0.24, 'pct': 0.0, 'start_date': 1791331200, 'end_date': 1793923200, 'window_days': 30, 'partial': False}}, {'chain': 'BSC', 'supply_usd': 31071513.317453884, 'share_pct': 0.67, 'change_7d': {'net_usd': 0.32, 'pct': 0.0, 'start_date': 1793318400, 'end_date': 1793923200, 'window_days': 7, 'partial': False}, 'change_30d': {'net_usd': 1.32, 'pct': 0.0, 'start_date': 1791331200, 'end_date': 1793923200, 'window_days': 30, 'partial': False}}, {'chain': 'Arbitrum', 'supply_usd': 14463859.313355772, 'share_pct': 0.31, 'change_7d': {'net_usd': -2694979.69, 'pct': -15.71, 'start_date': 1793318400, 'end_date': 1793923200, 'window_days': 7, 'partial': False}, 'change_30d': {'net_usd': -2388918.69, 'pct': -14.18, 'start_date': 1791331200, 'end_date': 1793923200, 'window_days': 30, 'partial': False}}], 'chain_note': 'Per-chain net change (7d/30d) combines bridges + local mint/burn — NOT a bridge-flow measurement. Covered 99.83% of total supply; $7941034.09 unattributed.', 'error': None}], 'method': 'Supply & flow feed (phase 3, item 8). Mint/burn events and net issuance as a feed, built on common/stablecoin.py wholesale: the feature-4 mint/burn monitor (sc_mint_burn — 7/30/90d net issuance windows from the DeFiLlama daily supply series, plus large on-chain mint/burn events on the primary chain) and the feature-5 supply map (sc_supply_map — per-chain supply and per-chain 7d/30d net change, bridges + local mint/burn combined). Two record types, newest first: daily_net_change rows (one per coin per day — the SERIES form of net issuance, labelled as such, never presented as discrete mint/burn events) and on_chain_event rows (large individual mints/burns above max($1M, 0.1% of circulating supply), latest 20 per coin). Net issuance is net supply change, NOT gross mint/burn.', 'limits': ["Net issuance is the net change in DeFiLlama's daily total circulating supply — net supply change, NOT gross mint/burn. A day's net mint is new issuance minus redemptions; a small net figure can hide large offsetting gross flows. Daily rows are labelled 'daily net change', never presented as discrete mint/burn events.", 'On-chain mint/burn events cover the primary chain only (Ethereum for 7 coins, Tron for USDD). FDUSD/TUSD hold most of their supply off the primary chain (BSC / Tron); mint/burn events on other chains are not shown.', "USDT on-chain events are unavailable: Blockscout's zero-address transfer query times out for USDT. Tether's issue()/redeem() emit Issue/Redeem events, not zero-address Transfers, so a zero-address scan would undercount USDT mint/burn even if the query succeeded.", 'TUSD supply is frozen — no new mint/burn is expected, so an empty on-chain event list is consistent with the flat daily series shown instead.', "USDD's DeFiLlama daily supply series has an 83-day gap (2026-07-07 to 2026-09-28). Daily deltas spanning more than 2 days are excluded, so USDD's daily series has a hole over the gap and its 90d net-issuance window is flagged partial (it covers 122 days, not 90). The gap is reported in USDD's coverage block.", 'Per-chain flow figures (7d/30d net change per chain, from the supply map) combine bridges + local mint/burn — they are NOT a bridge-flow measurement. No keyless source reports per-coin bridge flows over a window (DeFiLlama bridges is paywalled; Wormholescan ignores the symbol filter).', 'Net issuance is a PROXY for demand, not a direct measure of it. Redemptions, chain migrations (supply moving between chains) and issuer treasury moves also change total supply.'], 'as_of': 1794008020}, "free_tier": "Free tier today: keyless, no auth, no rate limit of our own. One page per call (default 50 rows, max 100); per-coin series + event lists cached 300s server-side, so repeat calls inside the window are served from cache. A cold call runs 8 coins' mint/burn evaluations (Blockscout zero-address scans — USDT's times out after ~20s) and supply-map parses and can take ~40s. Upstream rate limits (DeFiLlama, Blockscout, TronScan) apply — expect occasional 502s on slow calls.", "paid_tier": 'Paid tier (planned): API key required; calls metered per feed call. Higher rate limits, longer windows than 365d, per-coin webhooks on large mint/burn events, gross mint/burn (not just net) from a stored event history, and batch / export (CSV / JSON, no pagination). No auth, metering, or billing is enforced yet — this endpoint is free and keyless today.'}),
    ("GET", "/whales", "Whale & exchange exposure rollup across all 8 stablecoin scopes (feature 6): per-coin top-1/10/50 holder share of primary-chain supply, identified exchange wallets + lower-bound exchange total (top 50, primary chain only), and the largest recent transfer above each coin's threshold. Standalone (not in /overview) — the per-coin transfer scans are slow.", [], "/whales"),
    ("GET", "/whale-feed", "Whale feed (phase 3, item 14): large-transfer events across all 8 stablecoin scopes, paginated and filterable by coin (?coin=USDC, comma-separated, symbol or scope), minimum USD value (?min_usd=) and time window (?from= / ?to= unix epoch, ?days=N last N days, 1-365, default 30). One record type, newest first: large_transfer rows — individual primary-chain transfers >= that coin's threshold (max($1,000,000, 0.01% of circulating supply), applied uniformly); zero-address counterparty labelled mint / burn, everything else transfer. A min_usd below a coin's threshold cannot surface smaller transfers (the scan never saw them) — the effective minimum per coin is in its coverage block. Totals at the Nov 7, 2026 check: 39 large transfers, $192.30M volume (USDC 1, DAI 4, USDe 10 at the per-coin cap, PYUSD 2, TUSD 5, USDD 7, FDUSD 10 at the cap; USDT 0 above its $17.72M threshold in the 500 transfers scanned). Per-coin coverage blocks carry threshold, scanned count, scan window, status (ok / partial / unavailable — never a silent empty feed) and the effective minimum. Built on common/stablecoin.py wholesale (feature-6 /whales); per-coin data cached 300s. Standalone (not in /overview) — the per-coin transfer scans are slow. Rendered at #/whale-feed. The example_response below is the feed with 2 rows per page.", ['coin (optional, symbol or scope, comma-separated, default all 8 coins)', 'min_usd (optional, minimum USD value per event)', 'from (optional, unix epoch seconds)', 'to (optional, unix epoch seconds)', 'days (optional, last N days, 1-365, default 30)', 'page (optional, default 1)', 'page_size (optional, default 50, max 100)'], "/whale-feed", {"paid_api": True, "example_response": {'items': [{'record_type': 'large_transfer', 'kind': 'transfer', 'direction': 'transfer', 'timestamp': 1794010619, 'date': '2026-11-07T00:16:59Z', 'coin': 'USDC', 'symbol': 'USDC', 'scope': 'usdcscope', 'issuer': 'Circle', 'chain': 'ethereum', 'amount': 14659415.0, 'amount_usd': 14656974.74, 'from': '0x2744dfD9898f0bAbbC570cC594Bbbc84b487a22b', 'from_label': 'USDC SingleOwnerMSCA', 'to': '0xbe84c0e15C37B1FD5b278496b1bD3f06198f8308', 'to_label': None, 'tx_hash': '0x676c455a4c26a273118d5e8260a7d265b7766fba22a244a06246cdfc91195ce2', 'block': 26359338, 'threshold_usd': 7053558.15}, {'record_type': 'large_transfer', 'kind': 'transfer', 'direction': 'transfer', 'timestamp': 1794010607, 'date': '2026-11-07T00:16:47Z', 'coin': 'DAI', 'symbol': 'DAI', 'scope': 'daiscope', 'issuer': 'MakerDAO / Sky', 'chain': 'ethereum', 'amount': 3041655.800869, 'amount_usd': 3042285.02, 'from': '0x5167069edb46BA7a1fb04aC24EE83f665A7A6E16', 'from_label': None, 'to': '0xf57F4213Bc77891Db5D6Dc81C55CC3fE342F01aB', 'to_label': None, 'tx_hash': '0x33b0ca11d17051fa1963be3145dea490d43b88edb01d694e3243e83eea78169a', 'block': 26359337, 'threshold_usd': 1000000.0}], 'page': 1, 'page_size': 2, 'total_pages': 20, 'total_filtered': 39, 'total_items': 39, 'counts_by_coin': {'USDC': 1, 'DAI': 4, 'USDe': 10, 'PYUSD': 2, 'USDD': 7, 'FDUSD': 10, 'TUSD': 5}, 'counts_by_kind': {'transfer': 28, 'burn': 5, 'mint': 6}, 'totals': {'total_volume_usd': 192299507.88, 'largest_transfer': {'record_type': 'large_transfer', 'kind': 'mint', 'direction': 'mint', 'timestamp': 1794008795, 'date': '2026-11-06T23:46:35Z', 'coin': 'PYUSD', 'symbol': 'PYUSD', 'scope': 'pyusdsscope', 'issuer': 'PayPal (issued by Paxos)', 'chain': 'ethereum', 'amount': 50000000.0, 'amount_usd': 49973952.38, 'from': '0x0000000000000000000000000000000000000000', 'from_label': 'Null: 0x000...000', 'to': '0x264bd8291fAE1D75DB2c5F573b07faA6715997B5', 'to_label': 'Exchange', 'tx_hash': '0x35dc0b9f71b8a2f90b291bd1c3fb67ac745cd97bf334eba7f96efd8a1061b6b9', 'block': 26359186, 'threshold_usd': 1000000.0}, 'coins_included': 8, 'note': "Totals sum only the coins whose whales data loaded, and only transfers above each coin's threshold in the scanned window (latest 10 per coin). Not a total of all large transfers in the window."}, 'filters': {'coins': ['USDT', 'USDC', 'DAI', 'USDe', 'FDUSD', 'PYUSD', 'TUSD', 'USDD'], 'min_usd': None, 'from': 1791419092, 'to': None, 'days': 30}, 'coins': [{'scope': 'usdtscope', 'symbol': 'USDT', 'name': 'Tether USD', 'issuer': 'Tether', 'primary_chain': 'ethereum', 'contract': '0xdac17f958d2ee523a2206206994597c13d831ec7', 'supply_usd': 177209934723.1279, 'price_usd': 0.9987, 'price_assumed_1_usd': False, 'primary_chain_share_pct': 38.19, 'threshold_usd': 17720993.47, 'threshold_rule': 'A transfer counts as large when its USD value is >= max($1,000,000, 0.01% of circulating supply), applied uniformly to every coin.', 'cap': 10, 'events_count': 0, 'scanned_transfers': 500, 'scan_earliest': 1794010691, 'scan_latest': 1794010979, 'status': 'ok', 'status_note': None, 'chain_scope_note': 'Transfers cover the primary chain only (Ethereum). Ethereum holds 38.19% of total supply; large transfers on other chains are not shown.', 'error': None, 'min_usd_requested': None, 'effective_min_usd': 17720993.47}, {'scope': 'usdcscope', 'symbol': 'USDC', 'name': 'USD Coin', 'issuer': 'Circle', 'primary_chain': 'ethereum', 'contract': '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48', 'supply_usd': 70535581526.51599, 'price_usd': 0.9998335366500001, 'price_assumed_1_usd': False, 'primary_chain_share_pct': 60.75, 'threshold_usd': 7053558.15, 'threshold_rule': 'A transfer counts as large when its USD value is >= max($1,000,000, 0.01% of circulating supply), applied uniformly to every coin.', 'cap': 10, 'events_count': 1, 'scanned_transfers': 500, 'scan_earliest': 1794010535, 'scan_latest': 1794010643, 'status': 'ok', 'status_note': None, 'chain_scope_note': 'Transfers cover the primary chain only (Ethereum). Ethereum holds 60.75% of total supply; large transfers on other chains are not shown.', 'error': None, 'min_usd_requested': None, 'effective_min_usd': 7053558.15}, {'scope': 'daiscope', 'symbol': 'DAI', 'name': 'Dai', 'issuer': 'MakerDAO / Sky', 'primary_chain': 'ethereum', 'contract': '0x6b175474e89094c44da98b954eedeac495271d0f', 'supply_usd': 4673869327.615542, 'price_usd': 1.000206867785192, 'price_assumed_1_usd': False, 'primary_chain_share_pct': 87.87, 'threshold_usd': 1000000.0, 'threshold_rule': 'A transfer counts as large when its USD value is >= max($1,000,000, 0.01% of circulating supply), applied uniformly to every coin.', 'cap': 10, 'events_count': 4, 'scanned_transfers': 500, 'scan_earliest': 1794007967, 'scan_latest': 1794010667, 'status': 'ok', 'status_note': None, 'chain_scope_note': 'Transfers cover the primary chain only (Ethereum). Ethereum holds 87.87% of total supply; large transfers on other chains are not shown.', 'error': None, 'min_usd_requested': None, 'effective_min_usd': 1000000.0}, {'scope': 'usdescope', 'symbol': 'USDe', 'name': 'Ethena USDe', 'issuer': 'Ethena Labs', 'primary_chain': 'ethereum', 'contract': '0x4c9EDD5852cd905f086C759E8383e09bff1E68B3', 'supply_usd': 4923416926.180688, 'price_usd': 0.9992793014999999, 'price_assumed_1_usd': False, 'primary_chain_share_pct': 57.8, 'threshold_usd': 1000000.0, 'threshold_rule': 'A transfer counts as large when its USD value is >= max($1,000,000, 0.01% of circulating supply), applied uniformly to every coin.', 'cap': 10, 'events_count': 10, 'scanned_transfers': 237, 'scan_earliest': 1794001163, 'scan_latest': 1794010655, 'status': 'ok', 'status_note': None, 'chain_scope_note': 'Transfers cover the primary chain only (Ethereum). Ethereum holds 57.8% of total supply; large transfers on other chains are not shown.', 'error': None, 'min_usd_requested': None, 'effective_min_usd': 1000000.0}, {'scope': 'fdusdsscope', 'symbol': 'FDUSD', 'name': 'First Digital USD', 'issuer': 'First Digital Labs', 'primary_chain': 'ethereum', 'contract': '0xc5f0f7b66764f6ec8c8dff7ba683102295e16409', 'supply_usd': 283781504.1705849, 'price_usd': 0.996532794, 'price_assumed_1_usd': False, 'primary_chain_share_pct': 61.91, 'threshold_usd': 1000000.0, 'threshold_rule': 'A transfer counts as large when its USD value is >= max($1,000,000, 0.01% of circulating supply), applied uniformly to every coin.', 'cap': 10, 'events_count': 10, 'scanned_transfers': 129, 'scan_earliest': 1793779331, 'scan_latest': 1793998955, 'status': 'ok', 'status_note': None, 'chain_scope_note': 'Transfers cover the primary chain only (Ethereum). Ethereum holds 61.91% of total supply; large transfers on other chains are not shown.', 'error': None, 'min_usd_requested': None, 'effective_min_usd': 1000000.0}, {'scope': 'pyusdsscope', 'symbol': 'PYUSD', 'name': 'PayPal USD', 'issuer': 'PayPal (issued by Paxos)', 'primary_chain': 'ethereum', 'contract': '0x6c3ea9036406852006290770BEdFcAbA0e23A0e8', 'supply_usd': 2652481510.983866, 'price_usd': 0.9994790474999999, 'price_assumed_1_usd': False, 'primary_chain_share_pct': 65.33, 'threshold_usd': 1000000.0, 'threshold_rule': 'A transfer counts as large when its USD value is >= max($1,000,000, 0.01% of circulating supply), applied uniformly to every coin.', 'cap': 10, 'events_count': 2, 'scanned_transfers': 500, 'scan_earliest': 1794006923, 'scan_latest': 1794010667, 'status': 'ok', 'status_note': None, 'chain_scope_note': 'Transfers cover the primary chain only (Ethereum). Ethereum holds 65.33% of total supply; large transfers on other chains are not shown.', 'error': None, 'min_usd_requested': None, 'effective_min_usd': 1000000.0}, {'scope': 'tusdscope', 'symbol': 'TUSD', 'name': 'TrueUSD', 'issuer': 'TrueUSD (Techteryx)', 'primary_chain': 'ethereum', 'contract': '0x0000000000085d4780b73119b644ae5ecd22b376', 'supply_usd': 484484718.87, 'price_usd': 0.9986188328685921, 'price_assumed_1_usd': False, 'primary_chain_share_pct': 64.7, 'threshold_usd': 1000000.0, 'threshold_rule': 'A transfer counts as large when its USD value is >= max($1,000,000, 0.01% of circulating supply), applied uniformly to every coin.', 'cap': 10, 'events_count': 5, 'scanned_transfers': 500, 'scan_earliest': 1793674619, 'scan_latest': 1794008651, 'status': 'ok', 'status_note': None, 'chain_scope_note': 'Transfers cover the primary chain only (Ethereum). Ethereum holds 64.7% of total supply; large transfers on other chains are not shown.', 'error': None, 'min_usd_requested': None, 'effective_min_usd': 1000000.0}, {'scope': 'usddscope', 'symbol': 'USDD', 'name': 'Decentralized USD', 'issuer': 'TRON DAO Reserve', 'primary_chain': 'tron', 'contract': 'TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz', 'supply_usd': 1524283375.5844207, 'price_usd': 0.9974299416732509, 'price_assumed_1_usd': False, 'primary_chain_share_pct': 76.52, 'threshold_usd': 1000000.0, 'threshold_rule': 'A transfer counts as large when its USD value is >= max($1,000,000, 0.01% of circulating supply), applied uniformly to every coin.', 'cap': 10, 'events_count': 7, 'scanned_transfers': 500, 'scan_earliest': 1793979612, 'scan_latest': 1794010929, 'status': 'ok', 'status_note': None, 'chain_scope_note': 'Transfers cover the primary chain only (Tron). Tron holds 76.52% of total supply; large transfers on other chains are not shown.', 'error': None, 'min_usd_requested': None, 'effective_min_usd': 1000000.0}], 'method': 'Whale feed (phase 3, item 14). Large-transfer events across all stablecoin scopes as a feed, built on common/stablecoin.py wholesale: the feature-6 whale & exchange exposure evaluation (sc_whales — large recent transfers above max($1,000,000, 0.01% of circulating supply), latest 10 per coin, found by paging the recent-transfers feed, up to 10 pages of 50). One record type, newest first: large_transfer rows (individual primary-chain transfers; zero-address counterparty labelled mint / burn, everything else transfer). No large-transfer detection, holder/concentration logic, or exchange attribution is re-implemented here.', 'limits': ["Large transfers are defined as >= max($1,000,000, 0.01% of circulating supply) per coin; each coin's threshold is reported in its coverage block. For the largest coins the 0.01% leg dominates (USDT ~$18M, USDC ~$7M), so their lists are short by design.", 'The scan covers the recent-transfers feed only (up to 10 pages of 50 transfers per coin; at most the latest 10 large transfers per coin are kept). For high-volume coins 500 transfers cover only a minute or two of activity, so the window is whatever the feed covers — the scanned count and the scan window (earliest/latest timestamp) are reported per coin. An empty list means no transfer above the threshold in the scanned window, not that no large transfer happened.', 'Transfers cover the primary chain only (Ethereum for 7 coins, Tron for USDD). FDUSD/TUSD hold most of their supply off the primary chain (BSC / Tron); large transfers on other chains are not shown.', 'Concentration denominators are primary-chain supply only (Blockscout total_supply on Ethereum; DeFiLlama Tron chain balance for USDD) — shares are never presented as shares of all-chain supply. The feed itself carries no concentration figures; this is the denominator rule of the /whales evaluation it is built on.', "Exchange attribution is a lower bound from keyless source name tags only (Blockscout address tags / TronScan addressTag): an address counts as an exchange wallet only when the source's own tag names a known exchange. From/to labels on feed rows are those source-provided tags, shown verbatim; an untagged wallet stays unlabeled even if its size suggests an exchange.", "If a coin's large transfers cannot be observed keyless in a window (its transfer feed fails, or the scan stops early), its coverage block reports status unavailable / partial with the reason — the feed never returns a silent empty list for it.", "A ?min_usd below a coin's own threshold cannot surface smaller transfers: the scan only ever saw transfers above the threshold. The effective minimum per coin (max(min_usd, threshold_usd)) is reported in its coverage block."], 'as_of': 1794011092}, "free_tier": "Free tier today: keyless, no auth, no rate limit of our own. One page per call (default 50 rows, max 100); per-coin large-transfer lists cached 300s server-side, so repeat calls inside the window are served from cache. A cold call runs 8 coins' whales evaluations (Blockscout transfer scans, up to 10 pages each) and can take ~40s. Upstream rate limits (DeFiLlama, Blockscout, TronScan) apply — expect occasional 502s on slow calls.", "paid_tier": 'Paid tier (planned): API key required; calls metered per feed call. Higher rate limits, longer windows than 365d, per-coin webhooks on large transfers, a stored event history (no 10-per-coin cap, no recent-feed-only window), and batch / export (CSV / JSON, no pagination). No auth, metering, or billing is enforced yet — this endpoint is free and keyless today.'}),
    ("GET", "/activity", "On-chain activity & velocity rollup across all 8 stablecoin scopes (feature 7): per-coin 24h active addresses / tx count / transfer volume with complete flags (incomplete windows are lower bounds), daily rates with their basis, annualised velocity on primary-chain supply, and 24h-vs-prior-24h trends where both halves are complete. Standalone (not in /overview) — the per-coin transfer scans are slow.", [], "/activity"),
    ("GET", "/payments-readiness", "Payments readiness rollup across all 8 stablecoin scopes (feature 8): per-coin 0-100 score with band and the five input sub-scores (peg stability, liquidity, chain coverage, fees/finality, freeze risk), ranked by score descending. Standalone (not in /overview) — the per-coin activity scans are slow. Fees/finality are primary-chain figures only; freeze risk scores issuer capability — observed freeze events are tracked in the /freeze rollup (feature 10).", [], "/payments-readiness"),
    ("GET", "/freeze", "Freeze & blacklist rollup across all 8 stablecoin scopes (feature 10): per-coin freeze capability, frozen supply (LOWER-BOUND ESTIMATE — the blacklist is not enumerable on-chain), freeze/blacklist event counts in the scanned window, destroyed black funds, and sanctions matches. Standalone (not in /overview) — the per-coin log scans and Multicall3 balance batches are slow. The total frozen supply sums only coins with a measured estimate and is itself a lower bound.", [], "/freeze"),
    ("GET", "/issuer-risk", "Issuer risk rollup across all 8 stablecoin scopes (feature 11): per-coin 0-100 issuer risk score (higher = riskier) with band and the six input sub-scores (depeg risk, reserve transparency, issuance direction, concentration, freeze exposure, adoption), ranked by score ascending — rank 1 is the least risky issuer. Standalone (not in /overview) — the per-coin freeze log scans and activity scans are slow. Reserve transparency scores attestation recency/credibility, not live reserves (no keyless API exposes them); frozen supply is a lower-bound estimate; concentration and freeze figures are primary-chain only.", [], "/issuer-risk"),
    ("GET", "/issuer-risk/detail", "Issuer risk API (phase 3, item 6): the 8-issuer ranking PLUS the full per-issuer payload for every coin — all six input sub-scores with their evidence (depeg risk, reserve transparency, issuance direction, concentration, freeze exposure, adoption) — in one call, ranked by score ascending (rank 1 = least risky issuer). Ranking and breakdowns are derived from a single evaluation of the 8 composite scores, so a coin's ranking score and its breakdown score always agree. Weights (depeg 25%, reserve transparency 25%, issuance 15%, concentration 15%, freeze exposure 15%, adoption 5%), direction (higher = riskier) and bands (low <25, moderate 25–<50, elevated 50–<75, high >=75) are stated in every response, with methodology and honest limits. Reserve transparency scores attestation recency/credibility, not live reserves (no keyless API exposes them); frozen supply is a lower-bound estimate; concentration and freeze figures are primary-chain only; weights are this implementation's stated choice (no Par spec, no backtest against issuer failures). The per-coin score alone is GET /api/<scope>/issuer-risk; the ranking alone (sub-scores, truncated evidence) is GET /api/overview/issuer-risk. Standalone (not in /overview) — the per-coin freeze log scans and activity scans are slow. Rendered at #/issuer-risk. The example_response below shows the full ranking plus the full payloads for 2 of the 8 issuers (USDC, FDUSD); the live response carries all 8 in `issuers` in rank order.", [], "/issuer-risk/detail", {"paid_api": True, "example_response": {'ranking': [{'scope': 'daiscope',
              'scope_url': 'https://daiscope.fly.dev',
              'symbol': 'DAI',
              'name': 'Dai',
              'issuer': 'MakerDAO / Sky',
              'score': 27.9,
              'band': 'moderate',
              'weights_used': {'depeg_risk': 0.25,
                               'reserve_transparency': 0.25,
                               'issuance_direction': 0.15,
                               'concentration': 0.15,
                               'freeze_exposure': 0.15,
                               'adoption': 0.05},
              'inputs_unknown': [],
              'depeg_score': 17.9,
              'deviation_bps': 2.17,
              'depeg_days_365d': 0,
              'reserve_score': 25.0,
              'attestation_status': 'on_chain',
              'attestation_status_label': 'On-chain collateral — no dated attestation',
              'auditor': None,
              'days_since_attestation': None,
              'reserve_to_outstanding_ratio': None,
              'issuance_score': 45.0,
              'net_30d_pct_of_supply': -2.59,
              'net_90d_pct_of_supply': -2.68,
              'demand_direction': 'shrinking',
              'flat_supply': False,
              'concentration_score': 45.0,
              'top_10_share_pct': 37.23,
              'exchange_share_pct': 0,
              'freeze_score': 10.0,
              'can_freeze': False,
              'freeze_events_total': 0,
              'frozen_supply_usd': None,
              'frozen_share_of_primary_supply_pct': None,
              'sanctions_matches_total': 0,
              'adoption_score': 42.5,
              'material_chains': 2,
              'typical_transfer_fee_usd': None,
              'error': None,
              'rank': 1},
             {'scope': 'usddscope',
              'scope_url': 'https://usddscope.fly.dev',
              'symbol': 'USDD',
              'name': 'Decentralized USD',
              'issuer': 'TRON DAO Reserve',
              'score': 33.5,
              'band': 'moderate',
              'weights_used': {'depeg_risk': 0.25,
                               'reserve_transparency': 0.25,
                               'issuance_direction': 0.15,
                               'concentration': 0.15,
                               'freeze_exposure': 0.15,
                               'adoption': 0.05},
              'inputs_unknown': [],
              'depeg_score': 25.8,
              'deviation_bps': -26.1,
              'depeg_days_365d': 0,
              'reserve_score': 25.0,
              'attestation_status': 'on_chain',
              'attestation_status_label': 'On-chain collateral — no dated attestation',
              'auditor': 'SlowMist',
              'days_since_attestation': None,
              'reserve_to_outstanding_ratio': None,
              'issuance_score': 37.0,
              'net_30d_pct_of_supply': -4.34,
              'net_90d_pct_of_supply': 1.87,
              'demand_direction': 'mixed',
              'flat_supply': False,
              'concentration_score': 85.0,
              'top_10_share_pct': 76.96,
              'exchange_share_pct': 0,
              'freeze_score': 10.0,
              'can_freeze': False,
              'freeze_events_total': 0,
              'frozen_supply_usd': None,
              'frozen_share_of_primary_supply_pct': None,
              'sanctions_matches_total': 0,
              'adoption_score': 20.0,
              'material_chains': 3,
              'typical_transfer_fee_usd': None,
              'error': None,
              'rank': 2},
             {'scope': 'usdtscope',
              'scope_url': 'https://usdtscope.fly.dev',
              'symbol': 'USDT',
              'name': 'Tether USD',
              'issuer': 'Tether',
              'score': 42.1,
              'band': 'moderate',
              'weights_used': {'depeg_risk': 0.25,
                               'reserve_transparency': 0.25,
                               'issuance_direction': 0.15,
                               'concentration': 0.15,
                               'freeze_exposure': 0.15,
                               'adoption': 0.05},
              'inputs_unknown': [],
              'depeg_score': 20.9,
              'deviation_bps': -13.0,
              'depeg_days_365d': 0,
              'reserve_score': 10.0,
              'attestation_status': 'current',
              'attestation_status_label': 'Current',
              'auditor': 'BDO',
              'days_since_attestation': 37,
              'reserve_to_outstanding_ratio': 1.0435,
              'issuance_score': 45.0,
              'net_30d_pct_of_supply': -3.66,
              'net_90d_pct_of_supply': -3.1,
              'demand_direction': 'shrinking',
              'flat_supply': False,
              'concentration_score': 75.0,
              'top_10_share_pct': 45.646,
              'exchange_share_pct': 45.261,
              'freeze_score': 100.0,
              'can_freeze': True,
              'freeze_events_total': 932,
              'frozen_supply_usd': 1597581070.42,
              'frozen_share_of_primary_supply_pct': 1.8975,
              'sanctions_matches_total': 40,
              'adoption_score': 27.5,
              'material_chains': 4,
              'typical_transfer_fee_usd': None,
              'error': None,
              'rank': 3},
             {'scope': 'tusdscope',
              'scope_url': 'https://tusdscope.fly.dev',
              'symbol': 'TUSD',
              'name': 'TrueUSD',
              'issuer': 'TrueUSD (Techteryx)',
              'score': 44.9,
              'band': 'moderate',
              'weights_used': {'depeg_risk': 0.25,
                               'reserve_transparency': 0.25,
                               'issuance_direction': 0.15,
                               'concentration': 0.15,
                               'freeze_exposure': 0.15,
                               'adoption': 0.05},
              'inputs_unknown': [],
              'depeg_score': 25.9,
              'deviation_bps': -15.32,
              'depeg_days_365d': 4,
              'reserve_score': 10.0,
              'attestation_status': 'live',
              'attestation_status_label': 'Live — real-time attestation feed',
              'auditor': 'The Network Firm',
              'days_since_attestation': None,
              'reserve_to_outstanding_ratio': None,
              'issuance_score': 70.0,
              'net_30d_pct_of_supply': -0.0,
              'net_90d_pct_of_supply': -0.0,
              'demand_direction': 'flat',
              'flat_supply': True,
              'concentration_score': 85.0,
              'top_10_share_pct': 91.937,
              'exchange_share_pct': 3.794,
              'freeze_score': 70.0,
              'can_freeze': True,
              'freeze_events_total': 8,
              'frozen_supply_usd': 578930.92,
              'frozen_share_of_primary_supply_pct': 0.1838,
              'sanctions_matches_total': 0,
              'adoption_score': 42.5,
              'material_chains': 2,
              'typical_transfer_fee_usd': None,
              'error': None,
              'rank': 4},
             {'scope': 'usdcscope',
              'scope_url': 'https://usdcscope.fly.dev',
              'symbol': 'USDC',
              'name': 'USD Coin',
              'issuer': 'Circle',
              'score': 45.2,
              'band': 'moderate',
              'weights_used': {'depeg_risk': 0.25,
                               'reserve_transparency': 0.25,
                               'issuance_direction': 0.15,
                               'concentration': 0.15,
                               'freeze_exposure': 0.15,
                               'adoption': 0.05},
              'inputs_unknown': [],
              'depeg_score': 20.8,
              'deviation_bps': -1.71,
              'depeg_days_365d': 0,
              'reserve_score': 50.7,
              'attestation_status': 'overdue',
              'attestation_status_label': 'Overdue by 7 days',
              'auditor': 'Deloitte',
              'days_since_attestation': 37,
              'reserve_to_outstanding_ratio': 1.0011,
              'issuance_score': 57.0,
              'net_30d_pct_of_supply': -5.08,
              'net_90d_pct_of_supply': -2.22,
              'demand_direction': 'shrinking',
              'flat_supply': False,
              'concentration_score': 25.0,
              'top_10_share_pct': 23.616,
              'exchange_share_pct': 5.541,
              'freeze_score': 95.0,
              'can_freeze': True,
              'freeze_events_total': 579,
              'frozen_supply_usd': 851595657.45,
              'frozen_share_of_primary_supply_pct': 1.84,
              'sanctions_matches_total': 41,
              'adoption_score': 15.0,
              'material_chains': 8,
              'typical_transfer_fee_usd': None,
              'error': None,
              'rank': 5},
             {'scope': 'pyusdsscope',
              'scope_url': 'https://pyusdsscope.fly.dev',
              'symbol': 'PYUSD',
              'name': 'PayPal USD',
              'issuer': 'PayPal (issued by Paxos)',
              'score': 50.9,
              'band': 'elevated',
              'weights_used': {'depeg_risk': 0.25,
                               'reserve_transparency': 0.25,
                               'issuance_direction': 0.15,
                               'concentration': 0.15,
                               'freeze_exposure': 0.15,
                               'adoption': 0.05},
              'inputs_unknown': [],
              'depeg_score': 11.2,
              'deviation_bps': -3.96,
              'depeg_days_365d': 0,
              'reserve_score': 50.7,
              'attestation_status': 'overdue',
              'attestation_status_label': 'Overdue by 7 days',
              'auditor': 'KPMG LLP',
              'days_since_attestation': 37,
              'reserve_to_outstanding_ratio': 1.0009,
              'issuance_score': 57.0,
              'net_30d_pct_of_supply': -8.84,
              'net_90d_pct_of_supply': -3.96,
              'demand_direction': 'shrinking',
              'flat_supply': False,
              'concentration_score': 85.0,
              'top_10_share_pct': 91.029,
              'exchange_share_pct': 0.264,
              'freeze_score': 85.0,
              'can_freeze': True,
              'freeze_events_total': 101,
              'frozen_supply_usd': 154432.62,
              'frozen_share_of_primary_supply_pct': 0.0087,
              'sanctions_matches_total': 50,
              'adoption_score': 27.5,
              'material_chains': 4,
              'typical_transfer_fee_usd': None,
              'error': None,
              'rank': 6},
             {'scope': 'usdescope',
              'scope_url': 'https://usdescope.fly.dev',
              'symbol': 'USDe',
              'name': 'Ethena USDe',
              'issuer': 'Ethena Labs',
              'score': 58.0,
              'band': 'elevated',
              'weights_used': {'depeg_risk': 0.25,
                               'reserve_transparency': 0.25,
                               'issuance_direction': 0.15,
                               'concentration': 0.15,
                               'freeze_exposure': 0.15,
                               'adoption': 0.05},
              'inputs_unknown': [],
              'depeg_score': 34.7,
              'deviation_bps': -7.71,
              'depeg_days_365d': 2,
              'reserve_score': 100.0,
              'attestation_status': 'no_attestation',
              'attestation_status_label': 'No reserve attestation published',
              'auditor': 'Quantstamp',
              'days_since_attestation': None,
              'reserve_to_outstanding_ratio': None,
              'issuance_score': 57.0,
              'net_30d_pct_of_supply': -8.44,
              'net_90d_pct_of_supply': 13.4,
              'demand_direction': 'mixed',
              'flat_supply': False,
              'concentration_score': 90.0,
              'top_10_share_pct': 92.785,
              'exchange_share_pct': 11.794,
              'freeze_score': 10.0,
              'can_freeze': False,
              'freeze_events_total': 0,
              'frozen_supply_usd': None,
              'frozen_share_of_primary_supply_pct': None,
              'sanctions_matches_total': 0,
              'adoption_score': 15.0,
              'material_chains': 10,
              'typical_transfer_fee_usd': None,
              'error': None,
              'rank': 7},
             {'scope': 'fdusdsscope',
              'scope_url': 'https://fdusdsscope.fly.dev',
              'symbol': 'FDUSD',
              'name': 'First Digital USD',
              'issuer': 'First Digital Labs',
              'score': 69.2,
              'band': 'elevated',
              'weights_used': {'depeg_risk': 0.25,
                               'reserve_transparency': 0.25,
                               'issuance_direction': 0.15,
                               'concentration': 0.15,
                               'freeze_exposure': 0.15,
                               'adoption': 0.05},
              'inputs_unknown': [],
              'depeg_score': 37.2,
              'deviation_bps': -34.67,
              'depeg_days_365d': 1,
              'reserve_score': 87.3,
              'attestation_status': 'overdue',
              'attestation_status_label': 'Overdue by 223 days',
              'auditor': 'AOGB CPA Limited',
              'days_since_attestation': 251,
              'reserve_to_outstanding_ratio': None,
              'issuance_score': 85.0,
              'net_30d_pct_of_supply': -12.43,
              'net_90d_pct_of_supply': -21.84,
              'demand_direction': 'shrinking',
              'flat_supply': False,
              'concentration_score': 95.0,
              'top_10_share_pct': 93.371,
              'exchange_share_pct': 82.784,
              'freeze_score': 65.0,
              'can_freeze': True,
              'freeze_events_total': 6,
              'frozen_supply_usd': 3442916.09,
              'frozen_share_of_primary_supply_pct': 1.9665,
              'sanctions_matches_total': 0,
              'adoption_score': 27.5,
              'material_chains': 4,
              'typical_transfer_fee_usd': None,
              'error': None,
              'rank': 8}],
 'issuers': [{'scope': 'usdcscope',
              'symbol': 'USDC',
              'name': 'USD Coin',
              'issuer': 'Circle',
              'score': 45.2,
              'band': 'moderate',
              'direction': 'higher = riskier',
              'weights': {'depeg_risk': 0.25,
                          'reserve_transparency': 0.25,
                          'issuance_direction': 0.15,
                          'concentration': 0.15,
                          'freeze_exposure': 0.15,
                          'adoption': 0.05},
              'weights_used': {'depeg_risk': 0.25,
                               'reserve_transparency': 0.25,
                               'issuance_direction': 0.15,
                               'concentration': 0.15,
                               'freeze_exposure': 0.15,
                               'adoption': 0.05},
              'inputs': {'depeg_risk': {'weight': 0.25,
                                        'score': 20.8,
                                        'evidence': {'depeg_risk_score': 20.8,
                                                     'current_deviation_bps': -1.71,
                                                     'price_usd': 0.9998286030000001,
                                                     'depeg_days_365d': 0,
                                                     'worst_depeg_365d_bps': None,
                                                     'note': 'Feature-1 depeg early-warning score '
                                                             'reused as-is (current deviation + '
                                                             '365d depeg history + reserve '
                                                             'staleness proxy + DexScreener depth '
                                                             '+ disclosure/incident history). See '
                                                             '/depeg-score for its own '
                                                             'breakdown.'}},
                         'reserve_transparency': {'weight': 0.25,
                                                  'score': 50.7,
                                                  'evidence': {'status': 'overdue',
                                                               'status_label': 'Overdue by 7 days',
                                                               'cadence': 'monthly',
                                                               'auditor': 'Deloitte',
                                                               'auditor_tier': 'big4',
                                                               'last_attestation': '2026-09-30',
                                                               'days_since_attestation': 37,
                                                               'next_expected': '2026-10-30',
                                                               'overdue_by_days': 7,
                                                               'reserve_to_outstanding_ratio': 1.0011,
                                                               'reserve_figures_as_of': '2026-09-30',
                                                               'reserve_total_usd': 73956687701,
                                                               'reserve_total_approx': False,
                                                               'status_component': 60.7,
                                                               'auditor_adjustment': -5.0,
                                                               'reserve_ratio_adjustment': -5.0,
                                                               'note': 'Attestation overdue by 7d '
                                                                       'vs stated cadence. Big-4 '
                                                                       'attestor (-5). Dated '
                                                                       'reserves covered '
                                                                       'outstanding (1.0011x, '
                                                                       '2026-09-30) (-5) '
                                                                       'TRANSPARENCY score, not a '
                                                                       'reserve measurement: no '
                                                                       'keyless API exposes live '
                                                                       'reserve composition.'}},
                         'issuance_direction': {'weight': 0.15,
                                                'score': 57.0,
                                                'evidence': {'net_30d_usd': -3584450575.05,
                                                             'net_30d_pct_of_supply': -5.08,
                                                             'net_90d_usd': -1563149001.05,
                                                             'net_90d_pct_of_supply': -2.22,
                                                             'window_30d_partial': False,
                                                             'window_90d_partial': False,
                                                             'largest_mint_day': {'date': 1787270400,
                                                                                  'delta_usd': 859092516.0},
                                                             'largest_burn_day': {'date': 1789516800,
                                                                                  'delta_usd': -766971210.0},
                                                             'trend_label': 'Net issuance lower '
                                                                            'than the prior 30 '
                                                                            'days',
                                                             'demand_direction': 'shrinking',
                                                             'flat_supply': False,
                                                             'note': 'Net issuance is a PROXY for '
                                                                     'demand (feature 4): '
                                                                     'redemptions, chain '
                                                                     'migrations and issuer '
                                                                     'treasury moves also move '
                                                                     'supply. Sustained net '
                                                                     'redemptions score high; '
                                                                     'rapid growth scores '
                                                                     'moderately; near-flat scores '
                                                                     'low.'}},
                         'concentration': {'weight': 0.15,
                                           'score': 25.0,
                                           'evidence': {'top_1_share_pct': 6.193,
                                                        'top_10_share_pct': 23.616,
                                                        'top_50_share_pct': 42.63,
                                                        'holders_counted': 50,
                                                        'primary_chain': 'ethereum',
                                                        'primary_chain_supply_usd': 46282942790.96306,
                                                        'primary_chain_share_pct': 60.85,
                                                        'exchange_share_pct': 5.541,
                                                        'exchange_wallets_identified': 5,
                                                        'top_10_component': 25.0,
                                                        'exchange_adjustment': 0.0,
                                                        'note': 'Shares are of PRIMARY-CHAIN '
                                                                'supply, not all-chain supply. The '
                                                                'exchange share counts ONLY '
                                                                'wallets in the top 50 carrying a '
                                                                'source name tag — a LOWER BOUND '
                                                                'on exchange-held supply, not a '
                                                                'total.'}},
                         'freeze_exposure': {'weight': 0.15,
                                             'score': 95.0,
                                             'evidence': {'can_freeze': True,
                                                          'probe_functions_found': ['isBlacklisted'],
                                                          'pause_function_found': True,
                                                          'events_status': 'ok',
                                                          'events_total': 579,
                                                          'events_window_days': 365,
                                                          'events_counts_by_kind': {'blacklisted': 488,
                                                                                    'unblacklisted': 91},
                                                          'frozen_supply_status': 'estimate_lower_bound',
                                                          'frozen_supply_usd': 851595657.45,
                                                          'frozen_addresses_count': 394,
                                                          'frozen_share_of_primary_supply_pct': 1.84,
                                                          'destroyed_events_count': None,
                                                          'destroyed_total_usd': None,
                                                          'sanctions_matches_total': 41,
                                                          'sanctions_status': 'ok',
                                                          'note': 'Frozen supply is a LOWER-BOUND '
                                                                  'ESTIMATE (the blacklist is not '
                                                                  'enumerable on-chain — it sums '
                                                                  'balances of addresses the '
                                                                  "scanned window's freeze events "
                                                                  'show as currently frozen; '
                                                                  'addresses frozen before the '
                                                                  'window are missed). Events and '
                                                                  'frozen supply are primary-chain '
                                                                  'only. Issuer CAN '
                                                                  'freeze/blacklist holder '
                                                                  'balances (base 40); 579 '
                                                                  'freeze/blacklist event(s) in '
                                                                  'the scanned window (365d) '
                                                                  '(+10); frequent use (1.59 '
                                                                  'events/day) (+10); frozen '
                                                                  'supply 1.8400% of primary-chain '
                                                                  'supply (>1%) (+15); 41 OFAC SDN '
                                                                  'match(es) in the checked sets '
                                                                  '(+20).'}},
                         'adoption': {'weight': 0.05,
                                      'score': 15.0,
                                      'evidence': {'chain_coverage_score': 100.0,
                                                   'material_chains': 8,
                                                   'chain_count': 125,
                                                   'fees_finality_score': 70.0,
                                                   'typical_transfer_fee_usd': None,
                                                   'finality_seconds': 768,
                                                   'primary_chain': 'ethereum',
                                                   'note': "100 minus the mean of feature 8's "
                                                           'chain-coverage and fees/finality legs '
                                                           'ONLY — its peg, liquidity and freeze '
                                                           'legs are excluded here to avoid '
                                                           'double-counting the depeg-risk and '
                                                           'freeze-exposure inputs. Fees are '
                                                           'primary-chain figures (live gas/energy '
                                                           'price x a typical transfer — 65,000 '
                                                           'gas EVM / 14,000 energy Tron, both '
                                                           'ESTIMATES).'}}},
              'inputs_unknown': [],
              'supply_usd': 70546894760.157,
              'price_usd': 0.9998286030000001,
              'deviation_bps': -1.71,
              'methodology': 'Issuer risk score (0-100, higher = riskier). Weights (this '
                             "implementation's stated choice — no Par spec exists for this score): "
                             'depeg risk 25% (feature-1 score as-is), reserve transparency 25% '
                             '(feature-3 attestation tracker: status, days since last verified '
                             'attestation, auditor tier, dated reserve-to-outstanding ratio where '
                             'verifiable — transparency, NOT a live reserve measurement; no '
                             'keyless API exposes live reserve composition), issuance direction '
                             '15% (feature-4 net issuance, 30d primary / 90d confirmation; frozen '
                             'supply scores high), concentration 15% (feature-6 top-10 holder '
                             'share of primary-chain supply + identified-exchange share, a lower '
                             'bound), freeze exposure 15% (feature-10 capability + observed use: '
                             'freeze events, frozen supply share — a LOWER-BOUND ESTIMATE — '
                             'destroyed funds, sanctions matches), adoption 5% (feature-8 '
                             'chain-coverage + fees/finality legs only, inverted — the '
                             'peg/liquidity/freeze legs are excluded to avoid double-counting '
                             'inputs 1 and 5). Unknown inputs are excluded and the remaining '
                             'weights renormalised. Heuristic screening signal — not investment '
                             'advice, not a rating, not a guarantee.',
              'as_of': 1794006946,
              'sources': ['Feature 1 depeg early-warning score (DeFiLlama price/supply/depeg '
                          'history + DexScreener depth + issuer attestation config)',
                          'Feature 3 attestation tracker (issuer transparency pages, as verified — '
                          'see attestation block)',
                          'Feature 4 mint/burn monitor (DeFiLlama daily supply series)',
                          'Feature 6 whale & exchange exposure (Blockscout v2 holders / TronScan '
                          'holders, primary chain)',
                          'Feature 10 freeze & blacklist monitor (live eth_call probes + on-chain '
                          'freeze events + Multicall3 balances + OFAC SDN set)',
                          'Feature 8 payments readiness, chain-coverage + fees/finality legs only '
                          '(feature-5 supply map, public RPC gas/energy prices, CoinGecko '
                          'native-coin prices)']},
             {'scope': 'fdusdsscope',
              'symbol': 'FDUSD',
              'name': 'First Digital USD',
              'issuer': 'First Digital Labs',
              'score': 69.2,
              'band': 'elevated',
              'direction': 'higher = riskier',
              'weights': {'depeg_risk': 0.25,
                          'reserve_transparency': 0.25,
                          'issuance_direction': 0.15,
                          'concentration': 0.15,
                          'freeze_exposure': 0.15,
                          'adoption': 0.05},
              'weights_used': {'depeg_risk': 0.25,
                               'reserve_transparency': 0.25,
                               'issuance_direction': 0.15,
                               'concentration': 0.15,
                               'freeze_exposure': 0.15,
                               'adoption': 0.05},
              'inputs': {'depeg_risk': {'weight': 0.25,
                                        'score': 37.2,
                                        'evidence': {'depeg_risk_score': 37.2,
                                                     'current_deviation_bps': -34.67,
                                                     'price_usd': 0.996532794,
                                                     'depeg_days_365d': 1,
                                                     'worst_depeg_365d_bps': -51.64,
                                                     'note': 'Feature-1 depeg early-warning score '
                                                             'reused as-is (current deviation + '
                                                             '365d depeg history + reserve '
                                                             'staleness proxy + DexScreener depth '
                                                             '+ disclosure/incident history). See '
                                                             '/depeg-score for its own '
                                                             'breakdown.'}},
                         'reserve_transparency': {'weight': 0.25,
                                                  'score': 87.3,
                                                  'evidence': {'status': 'overdue',
                                                               'status_label': 'Overdue by 223 '
                                                                               'days',
                                                               'cadence': 'monthly',
                                                               'auditor': 'AOGB CPA Limited',
                                                               'auditor_tier': 'other',
                                                               'last_attestation': '2026-02-28',
                                                               'days_since_attestation': 251,
                                                               'next_expected': '2026-03-28',
                                                               'overdue_by_days': 223,
                                                               'reserve_to_outstanding_ratio': None,
                                                               'reserve_figures_as_of': '2026-02-28',
                                                               'reserve_total_usd': 381700000,
                                                               'reserve_total_approx': False,
                                                               'status_component': 82.3,
                                                               'auditor_adjustment': 5.0,
                                                               'reserve_ratio_adjustment': 0.0,
                                                               'note': 'Attestation overdue by '
                                                                       '223d vs stated cadence. '
                                                                       'Non-Big-4 attestor (+5) '
                                                                       'TRANSPARENCY score, not a '
                                                                       'reserve measurement: no '
                                                                       'keyless API exposes live '
                                                                       'reserve composition.'}},
                         'issuance_direction': {'weight': 0.15,
                                                'score': 85.0,
                                                'evidence': {'net_30d_usd': -35261090.65,
                                                             'net_30d_pct_of_supply': -12.43,
                                                             'net_90d_usd': -61966214.65,
                                                             'net_90d_pct_of_supply': -21.84,
                                                             'window_30d_partial': False,
                                                             'window_90d_partial': False,
                                                             'largest_mint_day': {'date': 1788220800,
                                                                                  'delta_usd': 3180000.0},
                                                             'largest_burn_day': {'date': 1787875200,
                                                                                  'delta_usd': -11903203.0},
                                                             'trend_label': 'Net issuance lower '
                                                                            'than the prior 30 '
                                                                            'days',
                                                             'demand_direction': 'shrinking',
                                                             'flat_supply': False,
                                                             'note': 'Net issuance is a PROXY for '
                                                                     'demand (feature 4): '
                                                                     'redemptions, chain '
                                                                     'migrations and issuer '
                                                                     'treasury moves also move '
                                                                     'supply. Sustained net '
                                                                     'redemptions score high; '
                                                                     'rapid growth scores '
                                                                     'moderately; near-flat scores '
                                                                     'low.'}},
                         'concentration': {'weight': 0.15,
                                           'score': 95.0,
                                           'evidence': {'top_1_share_pct': 56.918,
                                                        'top_10_share_pct': 93.371,
                                                        'top_50_share_pct': 99.44,
                                                        'holders_counted': 50,
                                                        'primary_chain': 'ethereum',
                                                        'primary_chain_supply_usd': 175112874.56311825,
                                                        'primary_chain_share_pct': 61.91,
                                                        'exchange_share_pct': 82.784,
                                                        'exchange_wallets_identified': 7,
                                                        'top_10_component': 85.0,
                                                        'exchange_adjustment': 10.0,
                                                        'note': 'Shares are of PRIMARY-CHAIN '
                                                                'supply, not all-chain supply. The '
                                                                'exchange share counts ONLY '
                                                                'wallets in the top 50 carrying a '
                                                                'source name tag — a LOWER BOUND '
                                                                'on exchange-held supply, not a '
                                                                'total.'}},
                         'freeze_exposure': {'weight': 0.15,
                                             'score': 65.0,
                                             'evidence': {'can_freeze': True,
                                                          'probe_functions_found': ['frozen'],
                                                          'pause_function_found': True,
                                                          'events_status': 'ok',
                                                          'events_total': 6,
                                                          'events_window_days': 365,
                                                          'events_counts_by_kind': {'frozen': 6},
                                                          'frozen_supply_status': 'estimate_lower_bound',
                                                          'frozen_supply_usd': 3442916.09,
                                                          'frozen_addresses_count': 6,
                                                          'frozen_share_of_primary_supply_pct': 1.9665,
                                                          'destroyed_events_count': None,
                                                          'destroyed_total_usd': None,
                                                          'sanctions_matches_total': 0,
                                                          'sanctions_status': 'ok',
                                                          'note': 'Frozen supply is a LOWER-BOUND '
                                                                  'ESTIMATE (the blacklist is not '
                                                                  'enumerable on-chain — it sums '
                                                                  'balances of addresses the '
                                                                  "scanned window's freeze events "
                                                                  'show as currently frozen; '
                                                                  'addresses frozen before the '
                                                                  'window are missed). Events and '
                                                                  'frozen supply are primary-chain '
                                                                  'only. Issuer CAN '
                                                                  'freeze/blacklist holder '
                                                                  'balances (base 40); 6 '
                                                                  'freeze/blacklist event(s) in '
                                                                  'the scanned window (365d) '
                                                                  '(+10); frozen supply 1.9665% of '
                                                                  'primary-chain supply (>1%) '
                                                                  '(+15).'}},
                         'adoption': {'weight': 0.05,
                                      'score': 27.5,
                                      'evidence': {'chain_coverage_score': 75.0,
                                                   'material_chains': 4,
                                                   'chain_count': 6,
                                                   'fees_finality_score': 70.0,
                                                   'typical_transfer_fee_usd': None,
                                                   'finality_seconds': 768,
                                                   'primary_chain': 'ethereum',
                                                   'note': "100 minus the mean of feature 8's "
                                                           'chain-coverage and fees/finality legs '
                                                           'ONLY — its peg, liquidity and freeze '
                                                           'legs are excluded here to avoid '
                                                           'double-counting the depeg-risk and '
                                                           'freeze-exposure inputs. Fees are '
                                                           'primary-chain figures (live gas/energy '
                                                           'price x a typical transfer — 65,000 '
                                                           'gas EVM / 14,000 energy Tron, both '
                                                           'ESTIMATES).'}}},
              'inputs_unknown': [],
              'supply_usd': 283781504.1705849,
              'price_usd': 0.996532794,
              'deviation_bps': -34.67,
              'methodology': 'Issuer risk score (0-100, higher = riskier). Weights (this '
                             "implementation's stated choice — no Par spec exists for this score): "
                             'depeg risk 25% (feature-1 score as-is), reserve transparency 25% '
                             '(feature-3 attestation tracker: status, days since last verified '
                             'attestation, auditor tier, dated reserve-to-outstanding ratio where '
                             'verifiable — transparency, NOT a live reserve measurement; no '
                             'keyless API exposes live reserve composition), issuance direction '
                             '15% (feature-4 net issuance, 30d primary / 90d confirmation; frozen '
                             'supply scores high), concentration 15% (feature-6 top-10 holder '
                             'share of primary-chain supply + identified-exchange share, a lower '
                             'bound), freeze exposure 15% (feature-10 capability + observed use: '
                             'freeze events, frozen supply share — a LOWER-BOUND ESTIMATE — '
                             'destroyed funds, sanctions matches), adoption 5% (feature-8 '
                             'chain-coverage + fees/finality legs only, inverted — the '
                             'peg/liquidity/freeze legs are excluded to avoid double-counting '
                             'inputs 1 and 5). Unknown inputs are excluded and the remaining '
                             'weights renormalised. Heuristic screening signal — not investment '
                             'advice, not a rating, not a guarantee.',
              'as_of': 1794006941,
              'sources': ['Feature 1 depeg early-warning score (DeFiLlama price/supply/depeg '
                          'history + DexScreener depth + issuer attestation config)',
                          'Feature 3 attestation tracker (issuer transparency pages, as verified — '
                          'see attestation block)',
                          'Feature 4 mint/burn monitor (DeFiLlama daily supply series)',
                          'Feature 6 whale & exchange exposure (Blockscout v2 holders / TronScan '
                          'holders, primary chain)',
                          'Feature 10 freeze & blacklist monitor (live eth_call probes + on-chain '
                          'freeze events + Multicall3 balances + OFAC SDN set)',
                          'Feature 8 payments readiness, chain-coverage + fees/finality legs only '
                          '(feature-5 supply map, public RPC gas/energy prices, CoinGecko '
                          'native-coin prices)']}],
 'issuers_missing': [],
 'coins_included': 8,
 'coins_scored': 8,
 'weights': {'depeg_risk': 0.25,
             'reserve_transparency': 0.25,
             'issuance_direction': 0.15,
             'concentration': 0.15,
             'freeze_exposure': 0.15,
             'adoption': 0.05},
 'bands': {'low': 'score < 25',
           'moderate': '25 <= score < 50',
           'elevated': '50 <= score < 75',
           'high': 'score >= 75'},
 'direction': 'higher = riskier',
 'methodology': "Issuer risk score (0-100, higher = riskier). Weights (this implementation's "
                'stated choice — no Par spec exists for this score): depeg risk 25% (feature-1 '
                'score as-is), reserve transparency 25% (feature-3 attestation tracker: status, '
                'days since last verified attestation, auditor tier, dated reserve-to-outstanding '
                'ratio where verifiable — transparency, NOT a live reserve measurement; no keyless '
                'API exposes live reserve composition), issuance direction 15% (feature-4 net '
                'issuance, 30d primary / 90d confirmation; frozen supply scores high), '
                'concentration 15% (feature-6 top-10 holder share of primary-chain supply + '
                'identified-exchange share, a lower bound), freeze exposure 15% (feature-10 '
                'capability + observed use: freeze events, frozen supply share — a LOWER-BOUND '
                'ESTIMATE — destroyed funds, sanctions matches), adoption 5% (feature-8 '
                'chain-coverage + fees/finality legs only, inverted — the peg/liquidity/freeze '
                'legs are excluded to avoid double-counting inputs 1 and 5). Unknown inputs are '
                'excluded and the remaining weights renormalised. Heuristic screening signal — not '
                'investment advice, not a rating, not a guarantee.',
 'limits': ['Reserve transparency scores how recently and how credibly the issuer proved its '
            'backing, NOT reserve adequacy: no keyless API exposes live reserve composition, so a '
            'low transparency sub-score does not mean reserves are sufficient and a high one does '
            'not mean they are insufficient.',
            "Weights are this implementation's stated choice (depeg risk 25%, reserve transparency "
            '25%, issuance direction 15%, concentration 15%, freeze exposure 15%, adoption 5%) — '
            'no Par spec exists for this score, and no backtest against issuer failures has been '
            'run. The weights are uncalibrated.',
            'Frozen supply is a LOWER-BOUND ESTIMATE: the issuer blacklist is not enumerable '
            'on-chain, so the currently-frozen set is rebuilt from the freeze events inside the '
            'scanned window only; addresses frozen before the window are missed, and frozen supply '
            'sums only those addresses.',
            'Concentration, freeze and fee figures are PRIMARY-CHAIN only (Ethereum for 7 coins, '
            'Tron for USDD). FDUSD/TUSD hold most of their supply off the primary chain; '
            'concentration and freezes on other chains are not covered.',
            'Net issuance is a PROXY for demand (feature 4): redemptions, chain migrations and '
            'issuer treasury moves also move supply, so growing supply can reflect issuer '
            'inventory moves rather than end-user demand.',
            'DAI, USDe and USDD have no freeze/blacklist function and score low on freeze exposure '
            '— that is a statement about issuer power (the issuer cannot freeze holder balances), '
            'not an endorsement of the issuer.',
            'Coins whose backing is on-chain (DAI, USDD) score middling on reserve transparency: '
            'the collateral is verifiable in principle, but this feature does not measure its '
            'composition.',
            'Heuristic screening signal for comparing issuers — not investment advice, not a '
            'credit rating, not a guarantee.'],
 'note': 'Ranking and per-issuer breakdowns are derived from a single evaluation of the 8 '
         "composite scores, so a coin's ranking score and its breakdown score always agree (rank 1 "
         '= least risky issuer; unknown scores last). Per-coin concentration, freeze and fee '
         'figures are PRIMARY-CHAIN only (Ethereum for 7 coins, Tron for USDD). Bands: < 25 low, < '
         '50 moderate, < 75 elevated, >= 75 high.',
 'as_of': 1794006952}, "free_tier": 'Free tier today: keyless, no auth, no rate limit of our own. One full 8-issuer evaluation per call; the response is cached 300s server-side, so repeat calls inside the window are served from cache. A cold call runs the per-coin freeze log scans and activity scans for all 8 coins and can take ~4 minutes. Upstream rate limits (Blockscout, DeFiLlama, DexScreener) apply — expect occasional 502s on slow calls.', "paid_tier": 'Paid tier (planned): API key required; calls metered per detail call. Higher rate limits, a stored score history (so the composite is available historically instead of live-only), per-coin webhooks on band changes, and batch / export (CSV / JSON). No auth, metering, or billing is enforced yet — this endpoint is free and keyless today.'}),
    ("GET", "/attestations", "Attestation monitor API (phase 3, item 7): all 8 stablecoin scopes' attestation status in one call — the feature-3 tracker (GET /api/<scope>/reserves -> attestation_tracker) reused wholesale per coin, embedded verbatim, with headline fields copied from it (auditor, cadence, last verified attestation + source, days since, status, next expected reporting date, overdue flag + overdue-by days, reserve-to-supply and reserve-to-outstanding ratios where a dated figure was verified, source links, alerts). Coins are ordered overdue first. Current-status-only: no auditor-change history exists (the tracker holds current status only; past auditors appear solely as prose in a coin's note) and none is invented — a stored history is a planned paid-tier feature. 'Overdue' means past the next reporting date per cadence, not past a publication deadline. Reserve figures are dated published figures from the named attestation only, never live or estimated. Standalone (not in /overview) — the per-coin reserves evaluations are slow. Rendered at #/attestations. The example_response below shows 2 of the 8 coins in full (FDUSD and USDC); the live response carries all 8 in `coins`, overdue first.", [], "/attestations", {"paid_api": True, "example_response": {'coins': [{'scope': 'fdusdsscope',
                'scope_url': 'https://fdusdsscope.fly.dev',
                'symbol': 'FDUSD',
                'name': 'First Digital USD',
                'issuer': 'First Digital Labs',
                'attestations': [{'label': 'First Digital Labs — attestation reports',
                                  'url': 'https://firstdigitallabs.com'}],
                'auditor': 'AOGB CPA Limited',
                'cadence': 'monthly',
                'last_attestation': '2026-02-28',
                'last_attestation_source': 'https://firstdigitallabs.com',
                'last_attestation_note': 'Monthly attestations; auditor changed 3x in ~1 yr '
                                         '(Prescient, Prism, AOGB from Jan 2026). Latest report '
                                         'retrievable Oct 2026 was Feb 2026 — issuer site blocks '
                                         'automated access; re-checked Nov 6, 2026, still nothing '
                                         'newer retrievable.',
                'days_since': 251,
                'status': 'overdue',
                'status_label': 'Overdue by 223 days',
                'next_expected': '2026-03-28',
                'overdue_by_days': 223,
                'reserve_total_usd': 381700000,
                'reserve_total_approx': False,
                'reserve_figures_as_of': '2026-02-28',
                'reserve_figures_source': 'First Digital FDUSD reserve attestation as of Feb 28, 2026 '
                                          '— AOGB CPA Limited (ISAE 3000, limited assurance), signed '
                                          'Mar 13, 2026; figures per secondary summary',
                'reserve_figures_source_url': 'https://www.firstdigitallabs.com/transparency',
                'reserve_to_supply_ratio': 1.345,
                'ratio_supply_usd': 283781504.1705849,
                'ratio_supply_as_of': '2026-11-06',
                'reserve_to_outstanding_ratio': None,
                'alerts': ['Attestation overdue by 223 days vs monthly cadence — latest verified '
                           'report 2026-02-28; next reporting date was 2026-03-28. Issuers typically '
                           'publish several weeks after the reporting date, so this means the next '
                           'report has not been verified yet, not necessarily that it is late.'],
                'tracker': {'auditor': 'AOGB CPA Limited',
                            'cadence': 'monthly',
                            'last_attestation': '2026-02-28',
                            'last_attestation_source': 'https://firstdigitallabs.com',
                            'last_attestation_note': 'Monthly attestations; auditor changed 3x in ~1 '
                                                     'yr (Prescient, Prism, AOGB from Jan 2026). '
                                                     'Latest report retrievable Oct 2026 was Feb 2026 '
                                                     '— issuer site blocks automated access; '
                                                     're-checked Nov 6, 2026, still nothing newer '
                                                     'retrievable.',
                            'as_of': '2026-11-06',
                            'days_since': 251,
                            'status': 'overdue',
                            'status_label': 'Overdue by 223 days',
                            'next_expected': '2026-03-28',
                            'overdue_by_days': 223,
                            'reserve_figures': {'as_of': '2026-02-28',
                                                'source': 'First Digital FDUSD reserve attestation as '
                                                          'of Feb 28, 2026 — AOGB CPA Limited (ISAE '
                                                          '3000, limited assurance), signed Mar 13, '
                                                          '2026; figures per secondary summary',
                                                'source_url': 'https://www.firstdigitallabs.com/transparency',
                                                'reserve_total_usd': 381700000,
                                                'reserve_total_approx': False,
                                                'tokens_outstanding': None,
                                                'excess_usd': None,
                                                'reserve_mix': [{'label': 'US Treasury bills (maturing '
                                                                          'Mar–Apr 2026)',
                                                                 'amount_usd': 334700000,
                                                                 'share_pct': 87.7},
                                                                {'label': 'US dollar cash in custody '
                                                                          'accounts',
                                                                 'amount_usd': 39500000,
                                                                 'share_pct': 10.3},
                                                                {'label': 'Fixed deposits',
                                                                 'amount_usd': 7500000,
                                                                 'share_pct': 2.0}],
                                                'mix_note': 'Excess over circulation ~$1.3m (~0.35%). '
                                                            'Amounts rounded to $0.1m in the secondary '
                                                            'summary. Latest report retrievable as of '
                                                            'Oct 2026; later reports may exist but '
                                                            'could not be verified.',
                                                'reserve_to_supply_ratio': 1.345,
                                                'ratio_supply_usd': 283781504.1705849,
                                                'ratio_supply_as_of': '2026-11-06',
                                                'reserve_to_outstanding_ratio': None},
                            'alerts': ['Attestation overdue by 223 days vs monthly cadence — latest '
                                       'verified report 2026-02-28; next reporting date was '
                                       '2026-03-28. Issuers typically publish several weeks after the '
                                       'reporting date, so this means the next report has not been '
                                       'verified yet, not necessarily that it is late.'],
                            'methodology': 'Staleness = days since the last verified attestation '
                                           "reporting date vs the coin's cadence; next expected = last "
                                           'attestation + one cadence interval (monthly +1 month, '
                                           'quarterly +3 months). Current: next reporting date more '
                                           'than 7 days away. Due soon: within 7 days. Overdue: past '
                                           'the next reporting date. Issuers typically publish a '
                                           'report several weeks after its reporting date, so '
                                           "'overdue' means past the next reporting date, not past a "
                                           'publication deadline. Coins with no dated attestation '
                                           '(real-time feed, on-chain collateral, or none published) '
                                           'are not scored. Reserve figures are dated published '
                                           'figures from the named attestation only — never live or '
                                           'estimated. Reserve-to-supply ratio = dated reserve total / '
                                           'current DeFiLlama circulating supply, with both dates '
                                           'stated; reserve-to-outstanding ratio uses the tokens '
                                           'outstanding stated in the same attestation.'},
                'error': None},
               {'scope': 'usdcscope',
                'scope_url': 'https://usdcscope.fly.dev',
                'symbol': 'USDC',
                'name': 'USD Coin',
                'issuer': 'Circle',
                'attestations': [{'label': 'Circle USDC Transparency — attestations & reserve reports',
                                  'url': 'https://www.centre.io/usdc-transparency'}],
                'auditor': 'Deloitte',
                'cadence': 'monthly',
                'last_attestation': '2026-09-30',
                'last_attestation_source': 'https://6778953.fs1.hubspotusercontent-na1.net/hubfs/6778953/USDCAttestationReports/2026/2026%20USDC_Examination%20Report%20September%2026.pdf',
                'last_attestation_note': 'Monthly Deloitte examinations; latest verified report Sep '
                                         '2026 (reporting date 2026-09-30, signed Oct 26, 2026) — '
                                         "re-verified Nov 6, 2026 from Circle's transparency page.",
                'days_since': 37,
                'status': 'overdue',
                'status_label': 'Overdue by 7 days',
                'next_expected': '2026-10-30',
                'overdue_by_days': 7,
                'reserve_total_usd': 73956687701,
                'reserve_total_approx': False,
                'reserve_figures_as_of': '2026-09-30',
                'reserve_figures_source': 'Circle USDC Reserve Report as of Sep 30, 2026 — Deloitte & '
                                          'Touche LLP examination, signed Oct 26, 2026',
                'reserve_figures_source_url': 'https://6778953.fs1.hubspotusercontent-na1.net/hubfs/6778953/USDCAttestationReports/2026/2026%20USDC_Examination%20Report%20September%2026.pdf',
                'reserve_to_supply_ratio': 1.0485,
                'ratio_supply_usd': 70535581526.51599,
                'ratio_supply_as_of': '2026-11-06',
                'reserve_to_outstanding_ratio': 1.0011,
                'alerts': ['Attestation overdue by 7 days vs monthly cadence — latest verified report '
                           '2026-09-30; next reporting date was 2026-10-30. Issuers typically publish '
                           'several weeks after the reporting date, so this means the next report has '
                           'not been verified yet, not necessarily that it is late.'],
                'tracker': {'auditor': 'Deloitte',
                            'cadence': 'monthly',
                            'last_attestation': '2026-09-30',
                            'last_attestation_source': 'https://6778953.fs1.hubspotusercontent-na1.net/hubfs/6778953/USDCAttestationReports/2026/2026%20USDC_Examination%20Report%20September%2026.pdf',
                            'last_attestation_note': 'Monthly Deloitte examinations; latest verified '
                                                     'report Sep 2026 (reporting date 2026-09-30, '
                                                     'signed Oct 26, 2026) — re-verified Nov 6, 2026 '
                                                     "from Circle's transparency page.",
                            'as_of': '2026-11-06',
                            'days_since': 37,
                            'status': 'overdue',
                            'status_label': 'Overdue by 7 days',
                            'next_expected': '2026-10-30',
                            'overdue_by_days': 7,
                            'reserve_figures': {'as_of': '2026-09-30',
                                                'source': 'Circle USDC Reserve Report as of Sep 30, '
                                                          '2026 — Deloitte & Touche LLP examination, '
                                                          'signed Oct 26, 2026',
                                                'source_url': 'https://6778953.fs1.hubspotusercontent-na1.net/hubfs/6778953/USDCAttestationReports/2026/2026%20USDC_Examination%20Report%20September%2026.pdf',
                                                'reserve_total_usd': 73956687701,
                                                'reserve_total_approx': False,
                                                'tokens_outstanding': 73878861314,
                                                'excess_usd': None,
                                                'reserve_mix': [{'label': 'Circle Reserve Fund — U.S. '
                                                                          'Treasury repo',
                                                                 'amount_usd': 36654000000,
                                                                 'share_pct': 49.6},
                                                                {'label': 'Circle Reserve Fund — U.S. '
                                                                          'Treasury securities',
                                                                 'amount_usd': 25361578416,
                                                                 'share_pct': 34.3},
                                                                {'label': 'Cash at regulated financial '
                                                                          'institutions',
                                                                 'amount_usd': 9502194346,
                                                                 'share_pct': 12.8},
                                                                {'label': 'U.S. Treasury securities '
                                                                          'held directly',
                                                                 'amount_usd': 1757678272,
                                                                 'share_pct': 2.4},
                                                                {'label': 'Circle Reserve Fund — cash',
                                                                 'amount_usd': 1003847741,
                                                                 'share_pct': 1.4},
                                                                {'label': 'Settlement differences '
                                                                          '(net)',
                                                                 'amount_usd': -322611074,
                                                                 'share_pct': -0.4,
                                                                 'detail': 'Circle Reserve Fund '
                                                                           '−$552,000,382 + other '
                                                                           'reserves +$229,389,308'}],
                                                'mix_note': 'Exact figures from the Deloitte-examined '
                                                            'report; the mix sums to the '
                                                            '$73,956,687,701 total. $62.47bn of the '
                                                            'reserve sits in the BlackRock-managed '
                                                            'Circle Reserve Fund.',
                                                'reserve_to_supply_ratio': 1.0485,
                                                'ratio_supply_usd': 70535581526.51599,
                                                'ratio_supply_as_of': '2026-11-06',
                                                'reserve_to_outstanding_ratio': 1.0011},
                            'alerts': ['Attestation overdue by 7 days vs monthly cadence — latest '
                                       'verified report 2026-09-30; next reporting date was '
                                       '2026-10-30. Issuers typically publish several weeks after the '
                                       'reporting date, so this means the next report has not been '
                                       'verified yet, not necessarily that it is late.'],
                            'methodology': 'Staleness = days since the last verified attestation '
                                           "reporting date vs the coin's cadence; next expected = last "
                                           'attestation + one cadence interval (monthly +1 month, '
                                           'quarterly +3 months). Current: next reporting date more '
                                           'than 7 days away. Due soon: within 7 days. Overdue: past '
                                           'the next reporting date. Issuers typically publish a '
                                           'report several weeks after its reporting date, so '
                                           "'overdue' means past the next reporting date, not past a "
                                           'publication deadline. Coins with no dated attestation '
                                           '(real-time feed, on-chain collateral, or none published) '
                                           'are not scored. Reserve figures are dated published '
                                           'figures from the named attestation only — never live or '
                                           'estimated. Reserve-to-supply ratio = dated reserve total / '
                                           'current DeFiLlama circulating supply, with both dates '
                                           'stated; reserve-to-outstanding ratio uses the tokens '
                                           'outstanding stated in the same attestation.'},
                'error': None}],
     'coins_included': 8,
     'coins_missing': [],
     'summary': {'counts': {'overdue': 3, 'current': 1, 'live': 1, 'on_chain': 2, 'no_attestation': 1},
                 'by_status': {'overdue': ['USDC', 'FDUSD', 'PYUSD'],
                               'current': ['USDT'],
                               'live': ['TUSD'],
                               'on_chain': ['DAI', 'USDD'],
                               'no_attestation': ['USDe']},
                 'overdue': ['USDC', 'FDUSD', 'PYUSD'],
                 'due_soon': []},
     'history': {'available': False,
                 'coverage': 'current-status-only',
                 'note': 'No auditor-change history exists in this repo: the feature-3 tracker holds '
                         'current status only (auditor, cadence, last verified attestation). Past '
                         "auditors appear solely as prose in a coin's note where the config records "
                         'them (FDUSD: Prescient, then Prism, then AOGB from Jan 2026; PYUSD: Withum '
                         'before KPMG from Feb 28, 2025). No history is invented; a stored attestation '
                         'history is a planned paid-tier feature.'},
     'methodology': "Staleness = days since the last verified attestation reporting date vs the coin's "
                    'cadence; next expected = last attestation + one cadence interval (monthly +1 '
                    'month, quarterly +3 months). Current: next reporting date more than 7 days away. '
                    'Due soon: within 7 days. Overdue: past the next reporting date. Issuers typically '
                    "publish a report several weeks after its reporting date, so 'overdue' means past "
                    'the next reporting date, not past a publication deadline. Coins with no dated '
                    'attestation (real-time feed, on-chain collateral, or none published) are not '
                    'scored. Reserve figures are dated published figures from the named attestation '
                    'only — never live or estimated. Reserve-to-supply ratio = dated reserve total / '
                    'current DeFiLlama circulating supply, with both dates stated; '
                    'reserve-to-outstanding ratio uses the tokens outstanding stated in the same '
                    'attestation.',
     'limits': ['Current-status-only: no auditor-change history exists in this repo (the feature-3 '
                "tracker holds current status only; past auditors appear solely as prose in a coin's "
                "note, e.g. FDUSD's Prescient / Prism / AOGB sequence and PYUSD's Withum before KPMG). "
                'No history is invented here; a stored history is a planned paid-tier feature.',
                'Attestation dates are the latest VERIFIED reporting dates from the config (feature 3, '
                'verified Nov 6, 2026). Later reports may exist but could not be verified keyless '
                "(FDUSD's issuer site blocks automated access) — do not assume one exists.",
                "'Overdue' means past the next reporting date per the coin's cadence, not past a "
                'publication deadline: issuers typically publish a report several weeks after its '
                'reporting date, so overdue means the next report has not been verified yet, not '
                'necessarily that it is late.',
                'Reserve figures are dated published figures from the named attestation only — never '
                'live or estimated. Reserve-to-supply ratio = dated reserve total / current DeFiLlama '
                'circulating supply, with both dates stated; reserve-to-outstanding ratio uses the '
                'tokens outstanding stated in the same attestation.',
                'Coins with no dated attestation (real-time feed, on-chain collateral, or none '
                'published) carry null reserve ratios and are reported with their status, never as '
                'zero.',
                'No keyless machine-readable API exposes live reserve / collateral figures for any of '
                'these coins (probed 2026-11-05) — the linked attestation pages are the source.',
                'Screening signal — not investment advice, not a credit rating, not a guarantee.'],
     'note': "All 8 stablecoin scopes' attestation status in one call, from the feature-3 tracker "
             'reused wholesale per coin (GET /api/<scope>/reserves -> attestation_tracker). Coins are '
             "ordered overdue first. 'Overdue' means past the next reporting date per cadence, not "
             'past a publication deadline. Reserve ratios exist only where a dated published figure '
             'could be verified; coins with no dated attestation carry null ratios.',
     'as_of': 1794007432}, "free_tier": "Free tier today: keyless, no auth, no rate limit of our own. One call returns all 8 coins; the response is cached 300s server-side, so repeat calls inside the window are served from cache. A cold call runs 8 per-coin reserves evaluations (DeFiLlama + CoinGecko per coin) and takes ~9s (observed 2026-11-06). Upstream rate limits (DeFiLlama, CoinGecko) apply — expect occasional 502s on slow calls.", "paid_tier": "Paid tier (planned): API key required; calls metered per monitor call. Higher rate limits, a stored attestation history (auditor changes and per-coin attestation dates over time, so overdue / due-soon transitions are visible historically instead of live-only), per-coin webhooks on status changes (current -> due soon -> overdue) and on new attestations, and batch / export (CSV / JSON). No auth, metering, or billing is enforced yet — this endpoint is free and keyless today."}),
    ("GET", "/defi", "DeFi deployment & yield rollup across all 8 stablecoin scopes (feature 9): per-coin best lending APY (protocol/chain) with the lending APY range across venues, lending market count and TVL, largest LP pair, LP TVL, total DeFi TVL and its share of supply, ranked by total DeFi TVL descending. Standalone (not in /overview) — the yields snapshot is a ~11MB download (cached server-side, shared across coins). LP TVL counts whole pools (both sides). Borrow APYs unavailable keyless (DeFiLlama /poolsBorrow paywalled).", [], "/defi"),
    ("GET", "/supply-map", "Cross-chain supply map rollup across all 8 stablecoin scopes (feature 5): per-chain total stablecoin supply summed across coins with top coins per chain, per-coin covered shares, and bridged-in stock by bridge. Standalone (not in /overview). Unknown supply is summed separately, never assigned to a chain.", [], "/supply-map"),
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
                # Paid-API model extension (phase 3): an entry tuple
                # may carry a 6th element (dict of extra fields —
                # paid_api, example_response, free_tier, paid_tier).
                **(extra[0] if extra else {}),
            }
            for (m, s, d, p, e, *extra) in _OV_DOCS
        ],
    }
