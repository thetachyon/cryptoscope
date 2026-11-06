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

        dep_s, dep_ev = _sub("depeg_risk")
        res_s, res_ev = _sub("reserve_transparency")
        iss_s, iss_ev = _sub("issuance_direction")
        con_s, con_ev = _sub("concentration")
        frz_s, frz_ev = _sub("freeze_exposure")
        adp_s, adp_ev = _sub("adoption")
        rows.append({
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
        })
    scored = [r for r in rows if not r.get("error") and r.get("score") is not None]
    scored.sort(key=lambda r: r["score"])
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
    ("GET", "/whales", "Whale & exchange exposure rollup across all 8 stablecoin scopes (feature 6): per-coin top-1/10/50 holder share of primary-chain supply, identified exchange wallets + lower-bound exchange total (top 50, primary chain only), and the largest recent transfer above each coin's threshold. Standalone (not in /overview) — the per-coin transfer scans are slow.", [], "/whales"),
    ("GET", "/activity", "On-chain activity & velocity rollup across all 8 stablecoin scopes (feature 7): per-coin 24h active addresses / tx count / transfer volume with complete flags (incomplete windows are lower bounds), daily rates with their basis, annualised velocity on primary-chain supply, and 24h-vs-prior-24h trends where both halves are complete. Standalone (not in /overview) — the per-coin transfer scans are slow.", [], "/activity"),
    ("GET", "/payments-readiness", "Payments readiness rollup across all 8 stablecoin scopes (feature 8): per-coin 0-100 score with band and the five input sub-scores (peg stability, liquidity, chain coverage, fees/finality, freeze risk), ranked by score descending. Standalone (not in /overview) — the per-coin activity scans are slow. Fees/finality are primary-chain figures only; freeze risk scores issuer capability — observed freeze events are tracked in the /freeze rollup (feature 10).", [], "/payments-readiness"),
    ("GET", "/freeze", "Freeze & blacklist rollup across all 8 stablecoin scopes (feature 10): per-coin freeze capability, frozen supply (LOWER-BOUND ESTIMATE — the blacklist is not enumerable on-chain), freeze/blacklist event counts in the scanned window, destroyed black funds, and sanctions matches. Standalone (not in /overview) — the per-coin log scans and Multicall3 balance batches are slow. The total frozen supply sums only coins with a measured estimate and is itself a lower bound.", [], "/freeze"),
    ("GET", "/issuer-risk", "Issuer risk rollup across all 8 stablecoin scopes (feature 11): per-coin 0-100 issuer risk score (higher = riskier) with band and the six input sub-scores (depeg risk, reserve transparency, issuance direction, concentration, freeze exposure, adoption), ranked by score ascending — rank 1 is the least risky issuer. Standalone (not in /overview) — the per-coin freeze log scans and activity scans are slow. Reserve transparency scores attestation recency/credibility, not live reserves (no keyless API exposes them); frozen supply is a lower-bound estimate; concentration and freeze figures are primary-chain only.", [], "/issuer-risk"),
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
            }
            for (m, s, d, p, e) in _OV_DOCS
        ],
    }
