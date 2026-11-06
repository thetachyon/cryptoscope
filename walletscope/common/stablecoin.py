"""Stablecoin dashboards for the *Scope apps — peg, supply, holders,
transfers, depeg history, reserves.

Sources (all probed 2026-11-05 from this sandbox, all keyless):
- DeFiLlama stablecoins https://stablecoins.llama.fi/stablecoins?includePrices=true
  (list of peggedAssets: numeric id, symbol, gecko_id, pegType,
  pegMechanism, circulating.peggedUSD, chainCirculating).
- DeFiLlama https://stablecoins.llama.fi/stablecoin/{numeric-id}
  (description, address, auditLinks, price, currentChainBalances
  {chain: {peggedUSD}}, and — despite the name — `tokens`, a daily
  total-supply history [{date, circulating: {peggedUSD}, ...}]).
- DeFiLlama https://stablecoins.llama.fi/stablecoinprices (list of
  {date, prices: {gecko_id: price}}, ~2132 daily entries; tether in all
  of them, ethena-usde in ~1052). /stablecoincharts/* 404s — not used.
- CoinGecko keyless /coins/{gecko_id} (price / market cap / supply
  fallback + homepage/whitepaper links).
- Blockscout v2 (eth.blockscout.com) /tokens/{addr},
  /tokens/{addr}/holders, /tokens/{addr}/transfers for the Ethereum
  primary chain of USDT/USDC/DAI/USDe/FDUSD/PYUSD/TUSD.
- TronScan keyless https://apilist.tronscan.org/api/token_trc20
  (token info), /token_trc20/holders?contract_address=...,
  /token_trc20/transfers?contract_address=... for USDD (Tron-native;
  Blockscout does not cover Tron). NOTE: the holders/transfers
  endpoints need the `contract_address=` parameter name — `contract=`
  is silently ignored (transfers) or 400s (holders). The holders list
  is capped at 10,000 by TronScan.

USDD contract (verified): TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz — confirmed
a live contract via api.trongrid.io /wallet/getcontract AND TronScan
token info (symbol USDD, "Decentralized USD"). The address
TPYmHEhy5nXmKtc5f6KgQVPSMURkCPSoW9 is NOT a contract (empty
getcontract response) — do not use it. TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t
is USDT on Tron, not USDD.

Reserves: no keyless machine-readable API exposes live reserve /
collateral figures for any of these coins (probed: Ethena app API 307,
api.ethena.fi 403, daistats.com/oasis/makerdao API hosts unreachable or
404). The /reserves endpoint therefore returns issuer attestation /
transparency links plus market figures (market cap, supply) clearly
labelled as market figures — never invented reserve numbers.
"""
import asyncio
import time
from decimal import Decimal, InvalidOperation

import httpx

from common.clients import (
    HEADERS,
    UpstreamError,
    _cache_get,
    _cache_set,
    bs,
    coingecko,
)
from common.formatting import token_amount, token_transfer_short

LLAMA_BASE = "https://stablecoins.llama.fi"
TRONSCAN_BASE = "https://apilist.tronscan.org/api"

# Depeg threshold: |price - $1| > 50 bps (0.5%).
DEPEG_BPS = 50

# ---------------------------------------------------------------- config
# llama_id: numeric DeFiLlama stablecoin id (USDe = 146 — resolve by
# symbol, case-sensitive "USDe"; a filter for "USDE" misses it).
# contract: primary-chain token contract (Ethereum 0x... / Tron base58).
STABLECOINS = {
    "usdtscope": {
        "symbol": "USDT",
        "name": "Tether USD",
        "llama_id": 1,
        "gecko_id": "tether",
        "primary": "ethereum",
        "contract": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "decimals": 6,
        "issuer": "Tether",
        "links": {
            "website": "https://tether.to/",
            "attestations": [
                {"label": "Tether Transparency — attestation reports", "url": "https://tether.to/en/transparency/#reports"},
            ],
            "whitepaper": "https://tether.to/wp-content/uploads/2016/06/TetherWhitePaper.pdf",
        },
        "reserves_note": "Tether publishes quarterly attestation reports (BDO) on its transparency page. Reserve figures are not available via a keyless machine-readable API — the linked page is the source.",
    },
    "usdcscope": {
        "symbol": "USDC",
        "name": "USD Coin",
        "llama_id": 2,
        "gecko_id": "usd-coin",
        "primary": "ethereum",
        "contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "decimals": 6,
        "issuer": "Circle",
        "links": {
            "website": "https://www.circle.com/usdc/",
            "attestations": [
                {"label": "Circle USDC Transparency — attestations & reserve reports", "url": "https://www.centre.io/usdc-transparency"},
            ],
            "whitepaper": "https://www.circle.com/legal/mica-usdc-whitepaper",
        },
        "reserves_note": "Circle publishes monthly attestations and weekly reserve reports on its transparency page. Reserve figures are not available via a keyless machine-readable API — the linked page is the source.",
    },
    "daiscope": {
        "symbol": "DAI",
        "name": "Dai",
        "llama_id": 5,
        "gecko_id": "dai",
        "primary": "ethereum",
        "contract": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "decimals": 18,
        "issuer": "MakerDAO / Sky",
        "links": {
            "website": "https://app.sky.money/",
            "attestations": [
                {"label": "Sky (MakerDAO) app — vaults & collateral", "url": "https://app.sky.money/"},
                {"label": "DAI Stats — live collateral breakdown", "url": "https://daistats.com"},
                {"label": "MakerDAO", "url": "https://makerdao.com/"},
            ],
            "whitepaper": "https://makerdao.com/en/whitepaper/",
        },
        "reserves_note": "DAI is crypto-collateralised via MakerDAO/Sky vaults. No keyless machine-readable API exposes the live collateral composition (MakerDAO/oasis API hosts probed 2026-11-05 are unreachable or 404) — see DAI Stats for the current breakdown.",
    },
    "usdescope": {
        "symbol": "USDe",
        "name": "Ethena USDe",
        "llama_id": 146,
        "gecko_id": "ethena-usde",
        "primary": "ethereum",
        "contract": "0x4c9EDD5852cd905f086C759E8383e09bff1E68B3",
        "decimals": 18,
        "issuer": "Ethena Labs",
        "links": {
            "website": "https://www.ethena.fi",
            "attestations": [
                {"label": "Ethena audits (Quantstamp, v2 contracts)", "url": "https://ethena-labs.gitbook.io/ethena-labs/resources/audits#quantstamp-audit-on-v2-of-contracts"},
                {"label": "Ethena app — backing & yield", "url": "https://app.ethena.fi/"},
            ],
            "whitepaper": "https://ethena-labs.gitbook.io/",
        },
        "reserves_note": "USDe is backed by delta-neutral hedged positions (staked ETH plus perpetual shorts). No keyless machine-readable API exposes live backing figures (Ethena app API redirects; api.ethena.fi returns 403) — see the Ethena app for the current backing.",
    },
    "fdusdsscope": {
        "symbol": "FDUSD",
        "name": "First Digital USD",
        "llama_id": 119,
        "gecko_id": "first-digital-usd",
        "primary": "ethereum",
        "contract": "0xc5f0f7b66764f6ec8c8dff7ba683102295e16409",
        "decimals": 18,
        "issuer": "First Digital Labs",
        "links": {
            "website": "https://firstdigitallabs.com",
            "attestations": [
                {"label": "First Digital Labs — attestation reports", "url": "https://firstdigitallabs.com"},
            ],
            "whitepaper": "https://firstdigitallabs.com/workspace/uploads/FDUSD-Whitepaper-25216064ca0cc8.pdf",
        },
        "reserves_note": "First Digital publishes monthly attestation reports on its site. Reserve figures are not available via a keyless machine-readable API — the linked site is the source. Note: most FDUSD supply lives on BSC, not Ethereum.",
    },
    "pyusdsscope": {
        "symbol": "PYUSD",
        "name": "PayPal USD",
        "llama_id": 120,
        "gecko_id": "paypal-usd",
        "primary": "ethereum",
        "contract": "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8",
        "decimals": 6,
        "issuer": "PayPal (issued by Paxos)",
        "links": {
            "website": "https://www.paypal.com/pyusd",
            "attestations": [
                {"label": "PayPal PYUSD — issuer reports", "url": "https://www.paypal.com/pyusd"},
            ],
            "whitepaper": "https://developer.paypal.com/dev-center/pyusd/",
        },
        "reserves_note": "PYUSD is issued by Paxos; monthly attestation reports are published by the issuer. Reserve figures are not available via a keyless machine-readable API — the linked page is the source.",
    },
    "tusdscope": {
        "symbol": "TUSD",
        "name": "TrueUSD",
        "llama_id": 7,
        "gecko_id": "true-usd",
        "primary": "ethereum",
        "contract": "0x0000000000085d4780b73119b644ae5ecd22b376",
        "decimals": 18,
        "issuer": "TrueUSD (Techteryx)",
        "links": {
            "website": "https://trueusd.com/",
            "attestations": [
                {"label": "TrueUSD real-time attestation (TrustExplorer)", "url": "https://real-time-attest.trustexplorer.io/truecurrencies"},
            ],
            "whitepaper": "https://tusd.io/docs/trueusd-white-paper-202602.pdf",
        },
        "reserves_note": "TrueUSD publishes a real-time attestation feed (linked). Reserve figures are not available via a keyless machine-readable API — the linked feed is the source. Note: most TUSD supply lives on Tron, not Ethereum.",
    },
    "usddscope": {
        "symbol": "USDD",
        "name": "Decentralized USD",
        "llama_id": 14,
        "gecko_id": "usdd",
        "primary": "tron",
        "contract": "TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz",
        "decimals": 18,
        "issuer": "TRON DAO Reserve",
        "links": {
            "website": "https://usdd.io/",
            "attestations": [
                {"label": "USDD — collateral & transparency dashboard", "url": "https://usdd.io/"},
                {"label": "SlowMist audit report (USDD TRC20)", "url": "https://usdd.io/SlowMistAuditReport-USDDTRC20.pdf"},
            ],
            "whitepaper": "https://docs.usdd.io/",
        },
        "reserves_note": "USDD is over-collateralised by design (TRON DAO Reserve). The live collateral ratio is shown on usdd.io, not via a public API — no reserve figures are available keyless here. On-chain holders/transfers below are TronScan data (Tron-native coin; Blockscout does not cover Tron).",
    },
}


def _cfg(scope):
    c = STABLECOINS.get(scope)
    if c is None:
        raise UpstreamError(f"Unknown stablecoin scope: {scope}", status=404)
    return c


# ---------------------------------------------------------------- helpers
def _dev_bps(price):
    """Deviation from $1 in basis points (None if no price)."""
    try:
        return round((float(price) - 1.0) * 10000, 2)
    except (TypeError, ValueError):
        return None


def _peg_health(bps):
    if bps is None:
        return "unknown"
    a = abs(bps)
    if a <= 5:
        return "pegged"
    if a <= DEPEG_BPS:
        return "watch"
    return "depegged"


def _pct_change(new, old):
    try:
        if new is None or old in (None, 0):
            return None
        return round((float(new) - float(old)) / float(old) * 100.0, 2)
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _llama(path):
    """GET DeFiLlama stablecoins API. Returns parsed JSON; raises
    UpstreamError on transport/upstream failure."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(LLAMA_BASE + path)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        raise UpstreamError(f"DeFiLlama unreachable ({type(e).__name__})")
    if r.status_code >= 400:
        raise UpstreamError(f"DeFiLlama returned HTTP {r.status_code}")
    try:
        return r.json()
    except Exception:
        raise UpstreamError("DeFiLlama returned invalid JSON")


async def _coin_detail(cfg):
    """DeFiLlama /stablecoin/{id} (cached 120s)."""
    key = f"scDetail:{cfg['llama_id']}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await _llama(f"/stablecoin/{cfg['llama_id']}")
    if not isinstance(d, dict):
        raise UpstreamError("DeFiLlama returned unexpected stablecoin data")
    _cache_set(key, d, ttl=120)
    return d


async def _price_history(cfg):
    """DeFiLlama /stablecoinprices, filtered to this coin's gecko_id.
    Cached 600s (6MB+ download). Returns [{date, price}] ascending."""
    key = f"scPrices:{cfg['gecko_id']}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await _llama("/stablecoinprices")
    if not isinstance(d, list):
        raise UpstreamError("DeFiLlama returned unexpected price data")
    gid = cfg["gecko_id"]
    out = []
    for e in d:
        try:
            ts = int(e.get("date") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        p = (e.get("prices") or {}).get(gid)
        if p is None:
            continue
        out.append({"date": ts, "price": float(p)})
    out.sort(key=lambda x: x["date"])
    _cache_set(key, out, ttl=600)
    return out


async def _gecko(cfg):
    """CoinGecko /coins/{gecko_id} (cached 300s). None on any failure."""
    key = f"scGecko:{cfg['gecko_id']}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await coingecko(
        f"/coins/{cfg['gecko_id']}",
        {"localization": "false", "tickers": "false", "community_data": "false",
         "developer_data": "false", "sparkline": "false"},
    )
    if isinstance(d, dict):
        _cache_set(key, d, ttl=300)
        return d
    return None


def _supply_history(detail):
    """Daily total-supply history from /stablecoin/{id} `tokens`
    (despite the name, a daily [{date, circulating:{peggedUSD}}] series).
    Returns [{date, supply_usd}] ascending."""
    out = []
    for e in detail.get("tokens") or []:
        if not isinstance(e, dict):
            continue
        try:
            ts = int(e.get("date") or 0)
        except (TypeError, ValueError):
            continue
        v = (e.get("circulating") or {}).get("peggedUSD")
        if ts <= 0 or v is None:
            continue
        out.append({"date": ts, "supply_usd": float(v)})
    out.sort(key=lambda x: x["date"])
    return out


def _chain_breakdown(detail):
    """currentChainBalances -> [{chain, supply_usd, share_pct}] desc."""
    cb = detail.get("currentChainBalances") or {}
    rows = []
    total = 0.0
    for chain, v in cb.items():
        amt = _num((v or {}).get("peggedUSD")) or 0.0
        if amt > 0:
            rows.append({"chain": chain, "supply_usd": amt})
            total += amt
    rows.sort(key=lambda r: -r["supply_usd"])
    for r in rows:
        r["share_pct"] = round(r["supply_usd"] / total * 100.0, 2) if total else None
    return rows, total


def _depeg_events(hist):
    """Days where |price - $1| > 50 bps, newest first, with summary."""
    evs = []
    for p in hist:
        bps = _dev_bps(p["price"])
        if bps is not None and abs(bps) > DEPEG_BPS:
            evs.append({
                "date": p["date"],
                "price": p["price"],
                "deviation_bps": bps,
            })
    evs.sort(key=lambda e: -e["date"])
    worst = max(evs, key=lambda e: abs(e["deviation_bps"])) if evs else None
    return evs, worst


async def _current_price(cfg, detail):
    """Live price: DeFiLlama detail price first, CoinGecko fallback.
    Returns (price, source)."""
    p = _num(detail.get("price"))
    if p:
        return p, "defillama"
    g = await _gecko(cfg)
    md = (g or {}).get("market_data") or {}
    p = _num((md.get("current_price") or {}).get("usd"))
    if p:
        return p, "coingecko"
    return None, None


async def _token_overview(cfg):
    """Blockscout token overview for EVM coins (holders, on-chain
    supply, exchange rate). None for Tron / on failure."""
    if cfg["primary"] != "ethereum":
        return None
    try:
        d = await bs("ethereum", f"/tokens/{cfg['contract']}")
    except UpstreamError:
        return None
    if not isinstance(d, dict):
        return None
    return {
        "holders_count": d.get("holders_count"),
        "total_supply": token_amount(d.get("total_supply"), d.get("decimals")),
        "exchange_rate_usd": d.get("exchange_rate"),
        "name": d.get("name"),
        "symbol": d.get("symbol"),
        "decimals": d.get("decimals"),
    }


async def _tronscan(path, params):
    """GET TronScan keyless API. Returns parsed JSON or None on 4xx."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(TRONSCAN_BASE + path, params=params)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        raise UpstreamError(f"TronScan unreachable ({type(e).__name__})")
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise UpstreamError(f"TronScan returned HTTP {r.status_code}")
    try:
        return r.json()
    except Exception:
        raise UpstreamError("TronScan returned invalid JSON")


async def _tron_token_info(cfg):
    """TronScan token info for USDD (market cap, 24h volume)."""
    key = f"scTronTok:{cfg['contract']}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await _tronscan("/token_trc20", {"contract": cfg["contract"]})
    toks = (d or {}).get("trc20_tokens") or []
    out = None
    if toks and isinstance(toks[0], dict):
        t = toks[0]
        out = {
            "market_cap_usd": _num(t.get("market_cap_usd")),
            "volume_24h_usd": _num(t.get("volume24h")),
            "name": t.get("name"),
            "symbol": t.get("symbol"),
            "decimals": t.get("decimals"),
        }
    if out is not None:
        _cache_set(key, out, ttl=300)
    return out


def _holder_label(addr_obj):
    """Blockscout address object -> display label (first name tag)."""
    if not isinstance(addr_obj, dict):
        return None
    meta = addr_obj.get("metadata") or {}
    for tag in meta.get("tags") or []:
        if isinstance(tag, dict) and tag.get("name"):
            return tag["name"]
    return addr_obj.get("ens_domain_name")


def _amount(raw, decimals):
    """Raw base-unit string -> Decimal amount."""
    try:
        return Decimal(str(raw)) / (Decimal(10) ** int(decimals))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ---------------------------------------------------------------- stats
async def sc_stats(scope):
    cfg = _cfg(scope)
    detail, gecko = await asyncio.gather(
        _coin_detail(cfg), _gecko(cfg), return_exceptions=True
    )
    if isinstance(detail, Exception):
        raise detail if isinstance(detail, UpstreamError) else UpstreamError(str(detail))
    if isinstance(gecko, Exception):
        gecko = None
    price, price_source = await _current_price(cfg, detail)
    bps = _dev_bps(price)
    hist = _supply_history(detail)
    chains, chain_total = _chain_breakdown(detail)
    supply = chain_total or (hist[-1]["supply_usd"] if hist else None)
    # 7d / 30d supply change from daily history.
    chg7 = chg30 = None
    if len(hist) >= 2:
        last = hist[-1]["supply_usd"]
        for days, slot in ((7, "chg7"), (30, "chg30")):
            past = [h for h in hist if h["date"] <= hist[-1]["date"] - days * 86400]
            v = _pct_change(last, past[-1]["supply_usd"]) if past else None
            if slot == "chg7":
                chg7 = v
            else:
                chg30 = v
    md = (gecko or {}).get("market_data") or {}
    if cfg["primary"] == "ethereum":
        tok = await _token_overview(cfg)
        tron = None
    else:
        tok = None
        try:
            tron = await _tron_token_info(cfg)
        except UpstreamError:
            tron = None
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "peg_mechanism": detail.get("pegMechanism") or cfg.get("peg_mechanism"),
        "peg_type": detail.get("pegType"),
        "description": detail.get("description"),
        "price_usd": price,
        "price_source": price_source,
        "deviation_bps": bps,
        "peg_health": _peg_health(bps),
        "supply_usd": supply,
        "supply_change_7d_pct": chg7,
        "supply_change_30d_pct": chg30,
        "supply_history_points": len(hist),
        "supply_history_first": hist[0]["date"] if hist else None,
        "supply_history_last": hist[-1]["date"] if hist else None,
        "chains": chains[:10],
        "chain_count": len(chains),
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "decimals": cfg["decimals"],
        "market_cap_usd": _num((md.get("market_cap") or {}).get("usd")),
        "gecko_total_supply": _num(md.get("total_supply")),
        "gecko_circulating_supply": _num(md.get("circulating_supply")),
        "token_on_primary": tok,
        "tron_token": tron,
        "links": cfg["links"],
        "sources": ["DeFiLlama stablecoins API", "CoinGecko keyless API"]
        + (["Blockscout v2 (Ethereum)"] if cfg["primary"] == "ethereum" else ["TronScan keyless API", "TronGrid keyless API"]),
    }


# ---------------------------------------------------------------- peg
async def sc_peg(scope, days=30):
    cfg = _cfg(scope)
    detail, hist = await asyncio.gather(
        _coin_detail(cfg), _price_history(cfg), return_exceptions=True
    )
    if isinstance(detail, Exception):
        raise detail if isinstance(detail, UpstreamError) else UpstreamError(str(detail))
    if isinstance(hist, Exception):
        raise hist if isinstance(hist, UpstreamError) else UpstreamError(str(hist))
    price, price_source = await _current_price(cfg, detail)
    bps = _dev_bps(price)
    now = int(time.time())
    cutoff = now - max(1, days) * 86400
    window = [p for p in hist if p["date"] >= cutoff]
    evs, worst = _depeg_events(window)
    # Daily points for the chart (already daily; cap at 365 points).
    pts = window[-365:]
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "price_usd": price,
        "price_source": price_source,
        "deviation_bps": bps,
        "peg_health": _peg_health(bps),
        "days": days,
        "points": [{"date": p["date"], "price": p["price"],
                    "deviation_bps": _dev_bps(p["price"])} for p in pts],
        "points_count": len(pts),
        "history_first": hist[0]["date"] if hist else None,
        "history_last": hist[-1]["date"] if hist else None,
        "depeg_days_in_window": len(evs),
        "worst_depeg_in_window": worst,
        "depeg_threshold_bps": DEPEG_BPS,
    }


# ---------------------------------------------------------------- supply
async def sc_supply(scope, days=90):
    cfg = _cfg(scope)
    detail = await _coin_detail(cfg)
    hist = _supply_history(detail)
    chains, chain_total = _chain_breakdown(detail)
    now = int(time.time())
    cutoff = now - max(1, days) * 86400
    window = [h for h in hist if h["date"] >= cutoff]
    # Recent mint/burn: day-over-day deltas for the last 7 days.
    recent = []
    for i in range(max(1, len(hist) - 7), len(hist)):
        prev, cur = hist[i - 1], hist[i]
        recent.append({
            "date": cur["date"],
            "supply_usd": cur["supply_usd"],
            "delta_usd": round(cur["supply_usd"] - prev["supply_usd"], 2),
            "delta_pct": _pct_change(cur["supply_usd"], prev["supply_usd"]),
        })
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "days": days,
        "points": window[-365:],
        "points_count": len(window),
        "history_first": hist[0]["date"] if hist else None,
        "history_last": hist[-1]["date"] if hist else None,
        "supply_usd": chain_total or (hist[-1]["supply_usd"] if hist else None),
        "chains": chains,
        "chain_count": len(chains),
        "recent_daily_change": recent,
        "note": "Supply is DeFiLlama circulating peggedUSD across all chains; per-chain figures are currentChainBalances.",
    }


# ---------------------------------------------------------------- holders
async def sc_holders(scope, params):
    cfg = _cfg(scope)
    detail = await _coin_detail(cfg)
    price, _ = await _current_price(cfg, detail)
    if cfg["primary"] == "tron":
        start = 0
        try:
            start = max(0, int(params.get("start") or 0))
        except (TypeError, ValueError):
            start = 0
        d = await _tronscan("/token_trc20/holders", {
            "contract_address": cfg["contract"], "start": start, "limit": 50,
        })
        items = []
        for t in (d or {}).get("trc20_tokens") or []:
            amt = _amount(t.get("balance"), cfg["decimals"])
            items.append({
                "address": t.get("holder_address"),
                "label": t.get("addressTag") or None,
                "balance": float(amt) if amt is not None else None,
                "balance_usd": (float(amt) * price) if amt is not None and price else None,
                "balance_usd_tronscan": _num(t.get("balanceInUsd")),
            })
        total = (d or {}).get("total")
        return {
            "scope": scope,
            "symbol": cfg["symbol"],
            "primary_chain": "tron",
            "items": items,
            "start": start,
            "next_start": (start + len(items)) if len(items) == 50 else None,
            "holders_total": total,
            "holders_total_note": "TronScan caps the holder list at 10,000 — the total shown is the capped list size, not necessarily the true holder count.",
            "price_usd": price,
        }
    # Ethereum via Blockscout.
    d = await bs("ethereum", f"/tokens/{cfg['contract']}/holders", params=params)
    if d is None:
        raise UpstreamError("Token not found", status=404)
    tok = await _token_overview(cfg)
    items = []
    for it in d.get("items") or []:
        addr = it.get("address") or {}
        amt = _amount(it.get("value"), cfg["decimals"])
        items.append({
            "address": addr.get("hash"),
            "label": _holder_label(addr),
            "is_contract": addr.get("is_contract"),
            "balance": float(amt) if amt is not None else None,
            "balance_usd": (float(amt) * price) if amt is not None and price else None,
        })
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "primary_chain": "ethereum",
        "items": items,
        "next_page_params": d.get("next_page_params"),
        "holders_total": (tok or {}).get("holders_count"),
        "price_usd": price,
    }


# ---------------------------------------------------------------- transfers
async def sc_transfers(scope, params):
    cfg = _cfg(scope)
    detail = await _coin_detail(cfg)
    price, _ = await _current_price(cfg, detail)
    if cfg["primary"] == "tron":
        start = 0
        try:
            start = max(0, int(params.get("start") or 0))
        except (TypeError, ValueError):
            start = 0
        d = await _tronscan("/token_trc20/transfers", {
            "contract_address": cfg["contract"], "start": start, "limit": 50,
            "sort": "-timestamp",
        })
        items = []
        for t in (d or {}).get("token_transfers") or []:
            amt = _amount(t.get("quant"), cfg["decimals"])
            items.append({
                "tx_hash": t.get("transaction_id"),
                "block": t.get("block"),
                "timestamp": t.get("block_ts"),
                "from": t.get("from_address"),
                "to": t.get("to_address"),
                "amount": float(amt) if amt is not None else None,
                "amount_usd": (float(amt) * price) if amt is not None and price else None,
            })
        return {
            "scope": scope,
            "symbol": cfg["symbol"],
            "primary_chain": "tron",
            "items": items,
            "start": start,
            "next_start": (start + len(items)) if len(items) == 50 else None,
            "price_usd": price,
        }
    d = await bs("ethereum", f"/tokens/{cfg['contract']}/transfers", params=params)
    if d is None:
        raise UpstreamError("Token not found", status=404)
    items = []
    for tt in d.get("items") or []:
        row = token_transfer_short(tt)
        amt = _num(row.get("amount"))
        row["amount_usd"] = (amt * price) if amt is not None and price else None
        items.append(row)
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "primary_chain": "ethereum",
        "items": items,
        "next_page_params": d.get("next_page_params"),
        "price_usd": price,
    }


# ---------------------------------------------------------------- depegs
async def sc_depegs(scope):
    cfg = _cfg(scope)
    hist = await _price_history(cfg)
    evs, worst = _depeg_events(hist)
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "depeg_threshold_bps": DEPEG_BPS,
        "depeg_days_total": len(evs),
        "history_days": len(hist),
        "history_first": hist[0]["date"] if hist else None,
        "history_last": hist[-1]["date"] if hist else None,
        "worst_depeg": worst,
        "most_recent_depeg": evs[0] if evs else None,
        "events": evs,
    }


# ---------------------------------------------------------------- reserves
async def sc_reserves(scope):
    cfg = _cfg(scope)
    detail, gecko = await asyncio.gather(
        _coin_detail(cfg), _gecko(cfg), return_exceptions=True
    )
    if isinstance(detail, Exception):
        raise detail if isinstance(detail, UpstreamError) else UpstreamError(str(detail))
    if isinstance(gecko, Exception):
        gecko = None
    md = (gecko or {}).get("market_data") or {}
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "peg_mechanism": detail.get("pegMechanism"),
        "description": detail.get("description"),
        "mint_redeem": detail.get("mintRedeemDescription"),
        "attestations": cfg["links"]["attestations"],
        "website": cfg["links"]["website"],
        "whitepaper": cfg["links"].get("whitepaper"),
        "reserve_figures": None,
        "reserve_figures_note": "No keyless machine-readable API exposes live reserve / collateral figures for this coin (probed 2026-11-05). The figures below are market figures, not reserves.",
        "market_figures": {
            "market_cap_usd": _num((md.get("market_cap") or {}).get("usd")),
            "total_supply": _num(md.get("total_supply")),
            "circulating_supply": _num(md.get("circulating_supply")),
            "source": "CoinGecko keyless API",
        },
        "note": cfg["reserves_note"],
    }


# ---------------------------------------------------------------- chains
async def sc_chains(scope):
    cfg = _cfg(scope)
    detail = await _coin_detail(cfg)
    chains, total = _chain_breakdown(detail)
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "chains": chains,
        "chain_count": len(chains),
        "total_supply_usd": total,
    }


# ---------------------------------------------------------------- search
async def sc_search(scope, q):
    cfg = _cfg(scope)
    q = (q or "").strip()
    if not q:
        raise UpstreamError("Empty search", status=400)
    if cfg["primary"] == "tron":
        if q.startswith("T") and 33 <= len(q) <= 35:
            return {"type": "holder", "address": q, "chain": "tron"}
        if len(q) == 64 and all(c in "0123456789abcdefABCDEF" for c in q):
            return {"type": "transaction", "txid": q, "chain": "tron"}
        raise UpstreamError("No match: expected a Tron address (T...) or a transaction id", status=404)
    if q.startswith("0x") and len(q) == 42 and all(c in "0123456789abcdefABCDEF" for c in q[2:]):
        return {"type": "holder", "address": q, "chain": "ethereum"}
    if q.startswith("0x") and len(q) == 66 and all(c in "0123456789abcdefABCDEF" for c in q[2:]):
        return {"type": "transaction", "hash": q, "chain": "ethereum"}
    raise UpstreamError("No match: expected an Ethereum address (0x + 40 hex) or a transaction hash (0x + 64 hex)", status=404)


# ---------------------------------------------------------------- docs
SC_DOCS = [
    {"method": "GET", "suffix": "/stats", "description": "Coin overview: live price vs $1, deviation in bps, peg-health badge, total supply (DeFiLlama circulating), 7d/30d supply change, per-chain breakdown, holder count on the primary chain, market-cap figures.", "params": [], "example": "/stats"},
    {"method": "GET", "suffix": "/peg", "description": "Peg status + daily price history (?days=7|30|90, default 30): price points with deviation in bps, depeg days in window, worst depeg. Source: DeFiLlama /stablecoinprices.", "params": ["days"], "example": "/peg?days=90"},
    {"method": "GET", "suffix": "/supply", "description": "Supply history (?days=90, default 90): daily circulating supply (DeFiLlama), per-chain breakdown, recent daily mint/burn deltas (last 7 days).", "params": ["days"], "example": "/supply?days=90"},
    {"method": "GET", "suffix": "/holders", "description": "Top holders on the primary chain with balances + USD values (Ethereum: Blockscout; USDD: TronScan, capped at 10,000). Paginate with the returned next_page_params (EVM) or ?start= (Tron).", "params": ["start"], "example": "/holders"},
    {"method": "GET", "suffix": "/transfers", "description": "Recent transfers on the primary chain with amounts + USD values (Ethereum: Blockscout; USDD: TronScan, newest first). Paginate with next_page_params (EVM) or ?start= (Tron).", "params": ["start"], "example": "/transfers"},
    {"method": "GET", "suffix": "/depegs", "description": "Every day in the full price history where |price - $1| > 50 bps, newest first, with worst and most-recent depeg.", "params": [], "example": "/depegs"},
    {"method": "GET", "suffix": "/reserves", "description": "Reserves/attestations: issuer attestation + transparency links, peg mechanism, and market figures (market cap, supply) labelled as market figures. No reserve numbers are invented — no keyless API exposes them.", "params": [], "example": "/reserves"},
    {"method": "GET", "suffix": "/chains", "description": "Per-chain supply breakdown (DeFiLlama currentChainBalances).", "params": [], "example": "/chains"},
    {"method": "GET", "suffix": "/search", "description": "Search: an address (holder) or a transaction hash/id on the coin's primary chain.", "params": ["q"], "example": "/search?q=0x..."},
]


def sc_docs_payload(api_prefix, scope):
    cfg = _cfg(scope)
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "primary_chain": cfg["primary"],
        "endpoints": [
            {
                "method": e["method"],
                "path": api_prefix + e["suffix"],
                "description": e["description"],
                "params": e["params"],
                "example": api_prefix + e["suffix"].split("?")[0] + ("?days=90" if "days" in e["params"] else ""),
            }
            for e in SC_DOCS
        ]
        + [
            {"method": "GET", "path": api_prefix + "/chains", "description": "List the chains this app serves (primary chain only).", "params": [], "example": api_prefix + "/chains"},
            {"method": "GET", "path": api_prefix + "/docs", "description": "This endpoint list.", "params": [], "example": api_prefix + "/docs"},
        ],
    }
