"""Depeg early-warning score for the *Scope stablecoin apps — keyless.

Feature 1. One live risk score per coin (0-100, higher = higher depeg
risk), from Par's documented v0 weights (proposal v0.2, section 6
"Scoring methodology v0" — weights are PROVISIONAL UNCONFIRMED):

  1. Redemption quality        40%
  2. Reserve liquidity          25%
  3. Secondary-market depth     20%
  4. Issuer / operational risk  15%

Every input is a proxy where a direct measurement is not available
keyless, and each proxy is labelled as such in the response:

  1. Redemption quality — direct-ish: current peg deviation plus
     depeg frequency / worst depeg in the last 365 days (DeFiLlama
     daily closes). If redemption were gated or halted, the price
     deviates — that is the proxy's mechanism, stated here, not a
     claim that redemption status itself was observed.
  2. Reserve liquidity — NOT measurable live keyless (no keyless API
     exposes live reserve composition for any of these coins, probed
     2026-11-05). Proxy: attestation staleness (days since the last
     verified attestation in the coin's `attestation` config block)
     + disclosure cadence. When no attestation date could be
     verified, this input is `unknown` and its weight is
     redistributed across the known inputs — never an invented date.
  3. Secondary-market depth — DexScreener tracked liquidity in
     near-par pairs (pair price within 50 bps of $1.00) on the
     coin's primary chain, as a share of circulating supply, plus a
     venue-concentration penalty (top pair's share of that
     liquidity). DexScreener-tracked pairs only; CEX depth and
     off-DexScreener venues are not covered.
  4. Issuer / operational risk — attestation cadence + auditor
     quality (from the config block) + incident history (depeg days
     and worst depeg over the full DeFiLlama price history).

An input that cannot be measured scores `null` ("unknown"); the
overall score is the weight-renormalised mean of the known inputs,
and the response lists which inputs were unknown and the weights
actually used. The four sub-scores and their evidence are ALWAYS
returned alongside the headline score — never a bare number.

This is a heuristic screening signal, not investment advice, not a
credit rating, and not a guarantee. Weights are Par v0 provisional
(UNCONFIRMED) and uncalibrated — no backtest against historical
depeg episodes has been run for this implementation.
"""
import asyncio
import math
import time

import httpx

from common.clients import HEADERS, TIMEOUT, _cache_get, _cache_set
from common.stablecoin import (
    DEPEG_BPS,
    STABLECOINS,
    _cfg,
    _chain_breakdown,
    _coin_detail,
    _current_price,
    _depeg_events,
    _dev_bps,
    _num,
    _price_history,
)

# Par v0 provisional weights (UNCONFIRMED — proposal v0.2 section 6).
WEIGHTS = {
    "redemption_quality": 0.40,
    "reserve_liquidity": 0.25,
    "secondary_market_depth": 0.20,
    "issuer_operational": 0.15,
}

METHODOLOGY = (
    "Depeg early-warning score (Par v0, provisional UNCONFIRMED weights: "
    "redemption quality 40%, reserve liquidity 25%, secondary-market "
    "depth 20%, issuer/operational risk 15%). Redemption quality is "
    "proxied by current peg deviation plus depeg frequency and worst "
    "depeg in the last 365 days — if redemption were gated or halted, "
    "the price deviates; redemption status itself is not observed "
    "directly. Reserve liquidity is NOT measurable live keyless, so it "
    "is proxied by attestation staleness plus disclosure cadence, and "
    "is unknown where no attestation date could be verified. "
    "Secondary-market depth is DexScreener-tracked liquidity in "
    "near-par pairs (within 50 bps of $1.00) on the primary chain as a "
    "share of circulating supply, with a venue-concentration penalty. "
    "The DexScreener endpoint returns at most 30 pairs per token, so "
    "the figure is a lower bound on tracked DEX liquidity, not total "
    "market depth; CEX depth is not covered. "
    "Issuer/operational risk combines attestation cadence and auditor "
    "quality with incident history (depeg days over the full price "
    "history). Unknown inputs are excluded and the remaining weights "
    "renormalised. Heuristic screening signal — not investment advice, "
    "not a rating, not a guarantee."
)

# DexScreener chain ids for the primary chains used here.
_DEXSCREENER_CHAIN = {"ethereum": "ethereum", "tron": "tron"}

# Auditor tiers for the issuer/operational input. "audit-only" means
# the named firm audited the smart contracts, not the reserves — it
# does not count as reserve-attestation quality.
_AUDITOR_TIER = {
    "Deloitte": "big4",
    "KPMG LLP": "big4",
    "KPMG": "big4",
    "BDO": "top5",
    "WithumSmith+Brown, PC": "other",
    "Prescient Assurance": "other",
    "AOGB CPA Limited": "other",
    "Quantstamp": "audit-only",
    "SlowMist": "audit-only",
    "The Network Firm": "other",
}


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _band(score):
    if score is None:
        return "unknown"
    if score < 25:
        return "low"
    if score < 50:
        return "moderate"
    if score < 75:
        return "elevated"
    return "high"


def _redemption_score(price, bps, hist):
    """0-100 (higher = riskier). 60% current deviation, 40% 365d history."""
    if bps is None:
        return None, {}
    now = int(time.time())
    window = [p for p in hist if p["date"] >= now - 365 * 86400]
    evs, worst = _depeg_events(window)
    current = _clamp(abs(bps))  # 50 bps -> 50, 100+ bps -> 100
    worst_bps = abs(worst["deviation_bps"]) if worst else 0
    history = _clamp(
        len(evs) * 5 + max(0.0, worst_bps - DEPEG_BPS) * 0.25
    )
    score = round(0.6 * current + 0.4 * history, 1)
    return score, {
        "price_usd": price,
        "current_deviation_bps": bps,
        "depeg_days_365d": len(evs),
        "worst_depeg_365d_bps": worst["deviation_bps"] if worst else None,
        "worst_depeg_365d_date": worst["date"] if worst else None,
        "current_component": round(current, 1),
        "history_component": round(history, 1),
        "note": (
            "Proxy: current deviation + 365d depeg frequency/worst "
            "depeg. Redemption gates or halts are not observed "
            "directly; the proxy assumes a gated/halted redemption "
            "shows up as a price deviation."
        ),
    }


def _reserve_score(att):
    """0-100 proxy from attestation staleness + disclosure cadence.
    None (unknown) when no attestation date could be verified and the
    cadence is not real-time/on-chain."""
    cadence = (att or {}).get("cadence")
    auditor = (att or {}).get("auditor")
    last = (att or {}).get("last_attestation")
    evidence = {
        "cadence": cadence,
        "auditor": auditor,
        "last_attestation": last,
        "source_url": (att or {}).get("source_url"),
        "note": (
            "PROXY: no keyless API exposes live reserve composition, "
            "so reserve liquidity is proxied by how stale the last "
            "verified attestation is and how often the issuer "
            "publishes. It is not a measurement of reserves."
        ),
    }
    if cadence in ("real-time", "on-chain"):
        # Collateral is in principle verifiable on-chain / live, but
        # this feature does not measure its composition — middling
        # score, not a pass.
        evidence["days_since_attestation"] = None
        evidence["staleness_note"] = (
            "No dated attestation: backing is published live/on-chain "
            "rather than in dated reports, so staleness does not "
            "apply; composition is still not measured here."
        )
        return 20.0, evidence
    if not last:
        evidence["days_since_attestation"] = None
        evidence["staleness_note"] = (
            "No attestation date could be verified from the issuer's "
            "public transparency page — scored as unknown, not "
            "assumed current."
        )
        return None, evidence
    try:
        att_ts = int(time.mktime(time.strptime(last, "%Y-%m-%d")))
    except (ValueError, TypeError):
        return None, evidence
    days = max(0, (int(time.time()) - att_ts) // 86400)
    evidence["days_since_attestation"] = days
    if days <= 45:
        staleness = 10.0
    elif days <= 90:
        staleness = 30.0
    elif days <= 180:
        staleness = 55.0
    else:
        staleness = 85.0
    cadence_score = {"monthly": 15.0, "quarterly": 35.0}.get(cadence, 70.0)
    score = round(0.5 * cadence_score + 0.5 * staleness, 1)
    evidence["cadence_component"] = cadence_score
    evidence["staleness_component"] = staleness
    return score, evidence


async def _dexscreener_near_par(cfg):
    """DexScreener tracked pairs for the coin's primary-chain contract,
    filtered to near-par pairs (|price - $1| <= 50 bps). Uses the
    chain-scoped ``/token-pairs/v1/{chain}/{addr}`` endpoint: the
    all-chain ``/latest/dex/tokens/{addr}`` endpoint returns only 30
    pairs across ALL chains (PulseChain junk dominated the list for
    USDT/USDC, leaving 1-2 Ethereum pairs), which understated tracked
    liquidity by orders of magnitude. The v1 endpoint still returns
    at most 30 pairs per token and its ordering is not by liquidity,
    so the figure is a LOWER BOUND on tracked DEX liquidity, not
    total market depth — stated in the evidence note. Returns
    (near_par_liquidity_usd, total_liquidity_usd, near_pairs, error,
    meta)."""
    cid = _DEXSCREENER_CHAIN.get(cfg["primary"])
    if cid is None:
        return None, None, [], f"DexScreener does not index {cfg['primary']}", {}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            r = await c.get(
                f"https://api.dexscreener.com/token-pairs/v1/{cid}/{cfg['contract']}"
            )
    except Exception:
        return None, None, [], "DexScreener unreachable", {}
    if r.status_code != 200:
        return None, None, [], f"DexScreener returned HTTP {r.status_code}", {}
    try:
        d = r.json()
    except Exception:
        return None, None, [], "DexScreener returned invalid JSON", {}
    pairs = d if isinstance(d, list) else (d.get("pairs") or [])
    near, total = [], 0.0
    off_par = 0
    for p in pairs:
        if p.get("chainId") != cid:
            continue
        liq = (p.get("liquidity") or {}).get("usd")
        liq = float(liq) if liq is not None else 0.0
        total += liq
        price = _num(p.get("priceUsd"))
        if price is None or abs(price - 1.0) > 0.005:
            off_par += 1
            continue
        near.append({
            "pairAddress": p.get("pairAddress"),
            "dexId": p.get("dexId"),
            "liquidity_usd": liq,
            "price_usd": price,
        })
    near.sort(key=lambda p: -p["liquidity_usd"])
    near_total = sum(p["liquidity_usd"] for p in near)
    meta = {"pairs_returned": len(pairs), "off_par_pairs": off_par}
    return near_total, total, near, None, meta


def _depth_score(near_liq, pairs, supply_usd, meta=None):
    """0-100 from near-par tracked liquidity / circulating supply,
    plus a venue-concentration penalty. None when unmeasurable."""
    meta = meta or {}
    if not pairs:
        detail = (
            "No near-par DexScreener pairs found on the primary "
            "chain — depth is unknown, not zero."
        )
        if meta.get("pairs_returned"):
            detail += (
                f" DexScreener returned {meta['pairs_returned']} pair(s) "
                f"for this token, {meta.get('off_par_pairs', 0)} of them "
                "priced off-par (outside 50 bps of $1.00), so no "
                "credible near-par pair exists in the tracked set."
            )
        return None, {"note": detail}
    if not supply_usd:
        return None, {"note": "Circulating supply unavailable — no ratio can be computed."}
    ratio = near_liq / supply_usd if supply_usd else 0.0
    if ratio <= 0:
        return None, {"note": "Tracked near-par liquidity is zero — depth is unknown, not zero."}
    # ratio 0.1 -> 0, 0.01 -> 25, 0.001 -> 50, 1e-4 -> 75, 1e-5 -> 100
    base = _clamp(50 - 25 * math.log10(ratio / 0.001))
    top_share = pairs[0]["liquidity_usd"] / near_liq if near_liq else 0.0
    if top_share > 0.8:
        penalty = 15.0
    elif top_share > 0.6:
        penalty = 8.0
    else:
        penalty = 0.0
    score = round(_clamp(base + penalty), 1)
    return score, {
        "near_par_liquidity_usd": round(near_liq, 2),
        "tracked_pairs_near_par": len(pairs),
        "pairs_returned_by_dexscreener": meta.get("pairs_returned"),
        "top_pair": {
            "dex": pairs[0].get("dexId"),
            "pair_address": pairs[0].get("pairAddress"),
            "liquidity_usd": round(pairs[0]["liquidity_usd"], 2),
            "share_of_near_par_pct": round(top_share * 100, 1),
        },
        "supply_usd": supply_usd,
        "liquidity_to_supply_ratio": ratio,
        "liquidity_to_supply_pct": round(ratio * 100, 4),
        "concentration_penalty": penalty,
        "note": (
            "DexScreener-tracked pairs on the primary chain only, via "
            "the chain-scoped token-pairs endpoint, which returns at "
            "most 30 pairs per token (ordering not by liquidity) — "
            "this is a lower bound on tracked DEX liquidity, not "
            "total market depth. CEX depth and untracked venues are "
            "not covered."
        ),
    }


def _issuer_score(att, hist):
    """0-100: 50% disclosure (cadence + auditor tier), 50% incident
    history (full-history depeg days + worst depeg)."""
    cadence = (att or {}).get("cadence")
    auditor = (att or {}).get("auditor")
    tier = _AUDITOR_TIER.get(auditor or "", "unknown")
    if cadence in ("real-time",):
        disclosure = 15.0
    elif cadence == "on-chain":
        disclosure = 25.0
    elif cadence == "monthly":
        disclosure = 15.0 if tier == "big4" else 30.0
    elif cadence == "quarterly":
        disclosure = 40.0 if tier in ("big4", "top5") else 50.0
    else:  # "none" or unknown cadence
        disclosure = 80.0
    if tier in ("audit-only", "unknown") and cadence not in ("real-time", "on-chain"):
        disclosure = _clamp(disclosure + 10.0)
    evs, worst = _depeg_events(hist)
    worst_bps = abs(worst["deviation_bps"]) if worst else 0
    n = len(evs)
    if n == 0:
        incident_base = 0.0
    elif n <= 5:
        incident_base = 20.0
    elif n <= 20:
        incident_base = 40.0
    elif n <= 100:
        incident_base = 60.0
    else:
        incident_base = 80.0
    incident = _clamp(incident_base + max(0.0, worst_bps - DEPEG_BPS) * 0.1)
    score = round(0.5 * disclosure + 0.5 * incident, 1)
    return score, {
        "cadence": cadence,
        "auditor": auditor,
        "auditor_tier": tier,
        "disclosure_component": round(disclosure, 1),
        "depeg_days_total": n,
        "worst_depeg_bps": worst["deviation_bps"] if worst else None,
        "worst_depeg_date": worst["date"] if worst else None,
        "incident_component": round(incident, 1),
        "note": (
            "Disclosure quality is from the issuer's stated "
            "attestation cadence and auditor; incident history is "
            "depeg days in the full DeFiLlama daily-close history "
            "(intraday depegs larger than the daily close are not "
            "counted)."
        ),
    }


async def sc_depeg_score(scope):
    """GET /api/<scope>/depeg-score — the Par v0 depeg early-warning
    score for one coin, with all four input sub-scores and evidence."""
    cfg = _cfg(scope)
    cache_key = f"scDepegScore:{scope}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    detail, hist = await asyncio.gather(
        _coin_detail(cfg), _price_history(cfg), return_exceptions=True
    )
    if isinstance(detail, Exception):
        raise detail
    if isinstance(hist, Exception):
        raise hist
    price, price_source = await _current_price(cfg, detail)
    bps = _dev_bps(price)
    _chains, supply_usd = _chain_breakdown(detail)
    att = cfg.get("attestation") or {}

    red_score, red_ev = _redemption_score(price, bps, hist)
    res_score, res_ev = _reserve_score(att)
    near_liq, total_liq, near_pairs, ds_err, ds_meta = await _dexscreener_near_par(cfg)
    if ds_err:
        depth_score, depth_ev = None, {
            "note": f"Secondary-market depth unknown: {ds_err}.",
            "dexscreener_error": ds_err,
        }
    else:
        depth_score, depth_ev = _depth_score(near_liq, near_pairs, supply_usd, ds_meta)
        depth_ev["total_tracked_liquidity_usd"] = round(total_liq, 2)
    iss_score, iss_ev = _issuer_score(att, hist)

    inputs = {
        "redemption_quality": {
            "weight": WEIGHTS["redemption_quality"],
            "score": red_score,
            "evidence": red_ev,
        },
        "reserve_liquidity": {
            "weight": WEIGHTS["reserve_liquidity"],
            "score": res_score,
            "evidence": res_ev,
        },
        "secondary_market_depth": {
            "weight": WEIGHTS["secondary_market_depth"],
            "score": depth_score,
            "evidence": depth_ev,
        },
        "issuer_operational": {
            "weight": WEIGHTS["issuer_operational"],
            "score": iss_score,
            "evidence": iss_ev,
        },
    }
    known = {k: v for k, v in inputs.items() if v["score"] is not None}
    unknown = [k for k, v in inputs.items() if v["score"] is None]
    if known:
        wsum = sum(v["weight"] for v in known.values())
        score = round(
            sum(v["score"] * v["weight"] for v in known.values()) / wsum, 1
        )
        weights_used = {k: round(v["weight"] / wsum, 3) for k, v in known.items()}
    else:
        score = None
        weights_used = {}
    out = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "score": score,
        "band": _band(score),
        "weights": dict(WEIGHTS),
        "weights_used": weights_used,
        "inputs": inputs,
        "inputs_unknown": unknown,
        "price_usd": price,
        "price_source": price_source,
        "deviation_bps": bps,
        "supply_usd": supply_usd,
        "methodology": METHODOLOGY,
        "attestation": {
            "cadence": att.get("cadence"),
            "auditor": att.get("auditor"),
            "last_attestation": att.get("last_attestation"),
            "source_url": att.get("source_url"),
            "note": att.get("note"),
        },
        "as_of": int(time.time()),
        "sources": [
            "DeFiLlama stablecoins API (price, supply, depeg history)",
            "DexScreener keyless API (secondary-market depth)",
            "Issuer transparency pages (attestation cadence/auditor/date, as verified — see attestation block)",
        ],
    }
    _cache_set(cache_key, out, ttl=300)
    return out
