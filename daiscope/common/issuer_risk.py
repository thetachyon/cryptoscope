"""Issuer risk score for the *Scope stablecoin apps — keyless.

Feature 11 of the stablecoin analytics set (the last one). One
composite score per coin (0-100, higher = riskier — same direction as
the feature-1 depeg score), built ONLY by reusing the features
already built. No new upstream data is fetched here: every input is
one of the existing feature endpoints, called wholesale.

Weights (this implementation's stated choice — no Par spec exists
for this score, unlike feature 1's Par v0 weights):

  1. Depeg risk              25%  feature 1 score, as-is
  2. Reserve transparency   25%  feature 3 attestation tracker
  3. Issuance direction     15%  feature 4 net issuance
  4. Concentration          15%  feature 6 holder concentration
  5. Freeze exposure        15%  feature 10 capability + observed use
  6. Adoption                5%  feature 8, two legs only (see below)

Each input scores 0-100 (higher = riskier) and carries its measured
value alongside the sub-score — the headline is never a bare number.
An input that cannot be measured scores `null` ("unknown"); the
overall score is the weight-renormalised mean of the known inputs,
and the response lists which inputs were unknown and the weights
actually used — the same rule as features 1 and 8.

Why these inputs, and what is deliberately NOT double-counted:

  1. Depeg risk — the feature-1 score is the single best live read
     on whether the issuer is keeping the peg. Reused as-is; its own
     four sub-inputs are not re-weighted here.
  2. Reserve transparency — the feature-3 tracker: attestation
     status (current / due soon / overdue / live / on-chain / no
     attestation), days since the last verified attestation,
     auditor tier, and the dated reserve-to-outstanding ratio where
     one could be verified. Live reserves are NOT measurable keyless
     (probed 2026-11-05), so this input scores TRANSPARENCY (how
     recently and how credibly the issuer proved its backing), not
     reserve adequacy — stated in the evidence, not hidden. A coin
     whose backing is on-chain (DAI, USDD) scores middling: the
     collateral is verifiable in principle, but this feature does
     not measure its composition.
  3. Issuance direction — feature-4 net issuance over 30d (primary)
     and 90d (confirmation): sustained net redemptions score high
     (holders leaving faster than they arrive), rapid net growth
     scores moderately (fast growth can be demand, but also
     inventory build), near-flat scores low. A frozen supply (no
     net mint/burn at all in 90d — TUSD) scores high: the issuer is
     not issuing or redeeming. Net issuance is a proxy (redemptions,
     chain migrations and treasury moves also move supply — the
     feature-4 caveat applies here too).
  4. Concentration — feature-6 top-10 holder share of PRIMARY-CHAIN
     supply plus the identified-exchange share (a LOWER BOUND — only
     wallets in the top 50 carrying a source name tag). A coin held
     mostly by a few wallets is more exposed to a single holder's
     exit or a single custodian's failure. Primary-chain only,
     labelled as such.
  5. Freeze exposure — feature 10: capability (can the issuer
     freeze/blacklist holder balances?) plus OBSERVED use — freeze/
     blacklist events in the scanned window, frozen supply as a
     share of primary-chain supply (LOWER-BOUND ESTIMATE — the
     blacklist is not enumerable on-chain), destroyed black funds,
     and OFAC SDN matches in the checked sets. A coin the issuer
     cannot freeze scores low; a coin whose issuer can freeze AND
     has frozen funds, destroyed funds, or sanctioned holders in
     the window scores high. DAI/USDe/USDD have no freeze function
     (not applicable, with ABI evidence) and score low on this
     input — that is a statement about issuer power, not an
     endorsement.
  6. Adoption — feature 8, but ONLY its two non-overlapping legs
     (chain coverage + fees/finality), inverted (100 - score). The
     other three feature-8 legs (peg stability, liquidity, freeze
     risk) are already inputs 1, 4-adjacent and 5 here, so including
     the whole feature-8 score would double-count them. Adoption is
     weighted only 5%: a widely-used, cheap-to-move coin is a
     slightly safer issuer bet, but adoption is not solvency.

This is a heuristic screening signal for comparing issuers, not
investment advice, not a credit rating, and not a guarantee. The
weights are uncalibrated — no backtest against issuer failures has
been run.
"""
import asyncio
import time

from common.clients import _cache_get, _cache_set
from common.freeze import sc_freeze
from common.payments_readiness import sc_payments_readiness
from common.stablecoin import (
    _cfg,
    sc_mint_burn,
    sc_reserves,
    sc_whales,
)
from common.stablecoin_score import _AUDITOR_TIER, sc_depeg_score

WEIGHTS = {
    "depeg_risk": 0.25,
    "reserve_transparency": 0.25,
    "issuance_direction": 0.15,
    "concentration": 0.15,
    "freeze_exposure": 0.15,
    "adoption": 0.05,
}

METHODOLOGY = (
    "Issuer risk score (0-100, higher = riskier). Weights (this "
    "implementation's stated choice — no Par spec exists for this "
    "score): depeg risk 25% (feature-1 score as-is), reserve "
    "transparency 25% (feature-3 attestation tracker: status, days "
    "since last verified attestation, auditor tier, dated "
    "reserve-to-outstanding ratio where verifiable — transparency, "
    "NOT a live reserve measurement; no keyless API exposes live "
    "reserve composition), issuance direction 15% (feature-4 net "
    "issuance, 30d primary / 90d confirmation; frozen supply scores "
    "high), concentration 15% (feature-6 top-10 holder share of "
    "primary-chain supply + identified-exchange share, a lower "
    "bound), freeze exposure 15% (feature-10 capability + observed "
    "use: freeze events, frozen supply share — a LOWER-BOUND "
    "ESTIMATE — destroyed funds, sanctions matches), adoption 5% "
    "(feature-8 chain-coverage + fees/finality legs only, inverted — "
    "the peg/liquidity/freeze legs are excluded to avoid "
    "double-counting inputs 1 and 5). Unknown inputs are excluded "
    "and the remaining weights renormalised. Heuristic screening "
    "signal — not investment advice, not a rating, not a guarantee."
)

CACHE_TTL = 300


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


# ---------------------------------------------------------------- input 1: depeg risk
def _depeg_input(depeg):
    """Feature-1 score reused as-is (higher = riskier already)."""
    if depeg is None:
        return None, {"note": "Depeg score unavailable — depeg risk unknown."}
    risk = depeg.get("score")
    if risk is None:
        return None, {
            "note": "Depeg risk score is unknown — depeg risk unknown.",
            "depeg_inputs_unknown": depeg.get("inputs_unknown"),
        }
    red = (depeg.get("inputs") or {}).get("redemption_quality") or {}
    ev = red.get("evidence") or {}
    return round(float(risk), 1), {
        "depeg_risk_score": risk,
        "current_deviation_bps": depeg.get("deviation_bps"),
        "price_usd": depeg.get("price_usd"),
        "depeg_days_365d": ev.get("depeg_days_365d"),
        "worst_depeg_365d_bps": ev.get("worst_depeg_365d_bps"),
        "note": (
            "Feature-1 depeg early-warning score reused as-is "
            "(current deviation + 365d depeg history + reserve "
            "staleness proxy + DexScreener depth + disclosure/incident "
            "history). See /depeg-score for its own breakdown."
        ),
    }


# ---------------------------------------------------------------- input 2: reserve transparency
def _reserve_input(reserves):
    """Feature-3 attestation tracker -> 0-100 (higher = riskier).
    Scores TRANSPARENCY, not reserve adequacy."""
    if reserves is None:
        return None, {"note": "Reserves/attestation data unavailable."}
    t = reserves.get("attestation_tracker") or {}
    status = t.get("status")
    if not status:
        return None, {"note": "Attestation tracker returned no status."}
    auditor = t.get("auditor")
    tier = _AUDITOR_TIER.get(auditor or "", "unknown")
    days = t.get("days_since")
    overdue = t.get("overdue_by_days")
    rf = t.get("reserve_figures") or {}
    ratio = rf.get("reserve_to_outstanding_ratio")
    # Base by tracker status.
    if status == "live":
        base, why = 10.0, "Real-time attestation feed is live"
    elif status == "on_chain":
        base, why = 25.0, (
            "Backing is on-chain collateral rather than dated "
            "reports — verifiable in principle, but its composition "
            "is not measured here"
        )
    elif status == "current":
        if days is None:
            base, why = 20.0, "Attestation current (no staleness figure)"
        elif days <= 45:
            base, why = 15.0, f"Attestation current ({days}d old)"
        elif days <= 90:
            base, why = 25.0, f"Attestation current but {days}d old"
        else:
            base, why = 30.0, f"Attestation current but {days}d old"
    elif status == "due_soon":
        base, why = 40.0, "Next attestation due soon"
    elif status == "overdue":
        extra = min(25.0, (overdue or 0) / 10.0)
        base, why = 60.0 + extra, (
            f"Attestation overdue by {overdue}d vs stated cadence"
        )
    else:  # "no_attestation"
        base, why = 90.0, "No reserve attestation is published"
    # Auditor tier adjustment (not applied to live/on-chain, where
    # the auditor is a contract auditor or none).
    adj = 0.0
    adj_note = None
    if status not in ("live", "on_chain"):
        if tier == "big4":
            adj, adj_note = -5.0, "Big-4 attestor (-5)"
        elif tier == "top5":
            adj, adj_note = 0.0, "Top-5 attestor (no adjustment)"
        elif tier == "audit-only":
            adj, adj_note = 10.0, (
                "Named firm audited the smart contracts, not the "
                "reserves (+10)"
            )
        elif tier == "other":
            adj, adj_note = 5.0, "Non-Big-4 attestor (+5)"
        else:
            adj, adj_note = 5.0, "No recognised attestor (+5)"
    # Dated reserve-to-outstanding ratio, where one could be
    # verified from a dated attestation: < 1.0 means the attested
    # reserves did not cover the tokens outstanding at that date.
    ratio_adj = 0.0
    ratio_note = None
    if ratio is not None:
        if ratio >= 1.0:
            ratio_adj, ratio_note = -5.0, (
                f"Dated reserves covered outstanding "
                f"({ratio:.4f}x, {rf.get('as_of')}) (-5)"
            )
        elif ratio >= 0.99:
            ratio_adj, ratio_note = 5.0, (
                f"Dated reserves slightly below outstanding "
                f"({ratio:.4f}x, {rf.get('as_of')}) (+5)"
            )
        else:
            ratio_adj, ratio_note = 25.0, (
                f"Dated reserves BELOW outstanding "
                f"({ratio:.4f}x, {rf.get('as_of')}) (+25)"
            )
    score = round(_clamp(base + adj + ratio_adj), 1)
    ev = {
        "status": status,
        "status_label": t.get("status_label"),
        "cadence": t.get("cadence"),
        "auditor": auditor,
        "auditor_tier": tier,
        "last_attestation": t.get("last_attestation"),
        "days_since_attestation": days,
        "next_expected": t.get("next_expected"),
        "overdue_by_days": overdue,
        "reserve_to_outstanding_ratio": ratio,
        "reserve_figures_as_of": rf.get("as_of"),
        "reserve_total_usd": rf.get("reserve_total_usd"),
        "reserve_total_approx": rf.get("reserve_total_approx"),
        "status_component": round(base, 1),
        "auditor_adjustment": adj,
        "reserve_ratio_adjustment": ratio_adj,
        "note": (
            why + (f". {adj_note}" if adj_note else "")
            + (f". {ratio_note}" if ratio_note else "")
            + (". No dated reserve figures could be verified — no "
               "ratio adjustment." if ratio is None and rf.get("reserve_total_usd") is None else "")
            + " TRANSPARENCY score, not a reserve measurement: no "
            "keyless API exposes live reserve composition."
        ),
    }
    return score, ev


# ---------------------------------------------------------------- input 3: issuance direction
def _issuance_bucket(pct):
    """Net issuance % of supply over a window -> 0-100 risk bucket."""
    if pct is None:
        return None
    if pct < -10:
        return 85.0
    if pct < -5:
        return 65.0
    if pct < -1:
        return 45.0
    if pct <= 1:
        return 20.0
    if pct <= 10:
        return 25.0
    if pct <= 25:
        return 45.0
    return 60.0


def _issuance_input(mint_burn):
    """Feature-4 net issuance: 30d primary, 90d confirmation."""
    if mint_burn is None:
        return None, {"note": "Mint/burn data unavailable."}
    ni = mint_burn.get("net_issuance") or {}
    w30 = ni.get("window_30d") or {}
    w90 = ni.get("window_90d") or {}
    flat = bool(ni.get("flat_supply"))
    trend = ni.get("trend_30d_vs_prior_30d") or {}
    demand = mint_burn.get("demand_signal") or {}
    ev = {
        "net_30d_usd": w30.get("net_usd"),
        "net_30d_pct_of_supply": w30.get("pct_of_supply"),
        "net_90d_usd": w90.get("net_usd"),
        "net_90d_pct_of_supply": w90.get("pct_of_supply"),
        "window_30d_partial": w30.get("partial"),
        "window_90d_partial": w90.get("partial"),
        "largest_mint_day": ni.get("largest_mint_day"),
        "largest_burn_day": ni.get("largest_burn_day"),
        "trend_label": trend.get("label"),
        "demand_direction": demand.get("direction"),
        "flat_supply": flat,
        "note": (
            "Net issuance is a PROXY for demand (feature 4): "
            "redemptions, chain migrations and issuer treasury moves "
            "also move supply. Sustained net redemptions score high; "
            "rapid growth scores moderately; near-flat scores low."
        ),
    }
    if flat:
        return 70.0, {
            **ev,
            "note": ev["note"]
            + " Supply is FROZEN — no net mint/burn at all in the "
            "90d window, so the issuer is neither issuing nor "
            "redeeming. Scored 70.",
        }
    b30 = _issuance_bucket(w30.get("pct_of_supply"))
    b90 = _issuance_bucket(w90.get("pct_of_supply"))
    if b30 is None and b90 is None:
        return None, {
            **ev,
            "note": "Net issuance windows unavailable — issuance unknown.",
        }
    if b30 is None:
        score = b90
    elif b90 is None:
        score = b30
    else:
        score = round(0.6 * b30 + 0.4 * b90, 1)
    return score, ev


# ---------------------------------------------------------------- input 4: concentration
def _concentration_input(whales):
    """Feature-6 top-10 holder share of primary-chain supply plus
    the identified-exchange share (lower bound)."""
    if whales is None:
        return None, {"note": "Whale/concentration data unavailable."}
    c = whales.get("concentration") or {}
    ex = whales.get("exchanges") or {}
    top10 = c.get("top_10_share_pct")
    if top10 is None:
        return None, {
            **{"note": "Top-10 holder share unavailable — concentration unknown."},
            "top_1_share_pct": c.get("top_1_share_pct"),
            "top_50_share_pct": c.get("top_50_share_pct"),
        }
    if top10 < 10:
        base = 10.0
    elif top10 < 25:
        base = 25.0
    elif top10 < 40:
        base = 45.0
    elif top10 < 60:
        base = 65.0
    else:
        base = 85.0
    ex_share = ex.get("total_share_pct")
    if ex_share is None:
        ex_adj = 0.0
    elif ex_share > 20:
        ex_adj = 10.0
    elif ex_share >= 10:
        ex_adj = 5.0
    else:
        ex_adj = 0.0
    score = round(_clamp(base + ex_adj), 1)
    return score, {
        "top_1_share_pct": c.get("top_1_share_pct"),
        "top_10_share_pct": top10,
        "top_50_share_pct": c.get("top_50_share_pct"),
        "holders_counted": c.get("holders_counted"),
        "primary_chain": whales.get("primary_chain"),
        "primary_chain_supply_usd": c.get("primary_chain_supply_usd"),
        "primary_chain_share_pct": whales.get("primary_chain_share_pct"),
        "exchange_share_pct": ex_share,
        "exchange_wallets_identified": ex.get("wallets_identified"),
        "top_10_component": round(base, 1),
        "exchange_adjustment": ex_adj,
        "note": (
            "Shares are of PRIMARY-CHAIN supply, not all-chain "
            "supply. The exchange share counts ONLY wallets in the "
            "top 50 carrying a source name tag — a LOWER BOUND on "
            "exchange-held supply, not a total."
        ),
    }


# ---------------------------------------------------------------- input 5: freeze exposure
def _freeze_input(freeze):
    """Feature 10: capability + observed use (events, frozen supply
    share, destroyed funds, sanctions matches)."""
    if freeze is None:
        return None, {"note": "Freeze/blacklist data unavailable."}
    cap = freeze.get("capability") or {}
    frz = freeze.get("frozen_supply") or {}
    evs = freeze.get("events") or {}
    win = evs.get("window") or {}
    dst = freeze.get("destroyed_black_funds") or {}
    san = freeze.get("sanctions") or {}
    total_events = evs.get("total_events") or 0
    window_days = win.get("window_days")
    frozen_share = frz.get("share_of_primary_supply_pct")
    destroyed_events = dst.get("events_count") or 0
    sanctions_matches = san.get("matches_total") or 0
    ev = {
        "can_freeze": cap.get("can_freeze"),
        "probe_functions_found": cap.get("probe_functions_found"),
        "pause_function_found": cap.get("pause_function_found"),
        "events_status": evs.get("status"),
        "events_total": total_events,
        "events_window_days": window_days,
        "events_counts_by_kind": evs.get("counts_by_kind"),
        "frozen_supply_status": frz.get("status"),
        "frozen_supply_usd": frz.get("amount_usd"),
        "frozen_addresses_count": frz.get("addresses_count"),
        "frozen_share_of_primary_supply_pct": frozen_share,
        "destroyed_events_count": dst.get("events_count"),
        "destroyed_total_usd": dst.get("total_usd"),
        "sanctions_matches_total": sanctions_matches,
        "sanctions_status": san.get("status"),
        "note": (
            "Frozen supply is a LOWER-BOUND ESTIMATE (the blacklist "
            "is not enumerable on-chain — it sums balances of "
            "addresses the scanned window's freeze events show as "
            "currently frozen; addresses frozen before the window "
            "are missed). Events and frozen supply are primary-chain "
            "only."
        ),
    }
    if not cap.get("can_freeze"):
        return 10.0, {
            **ev,
            "note": ev["note"]
            + " The issuer has no freeze/blacklist function on the "
            "primary-chain contract (ABI evidence in /freeze) — it "
            "cannot freeze holder balances. Scored 10.",
        }
    score = 40.0
    parts = ["Issuer CAN freeze/blacklist holder balances (base 40)"]
    if total_events > 0:
        score += 10.0
        parts.append(
            f"{total_events} freeze/blacklist event(s) in the "
            f"scanned window ({window_days}d) (+10)"
        )
        rate = total_events / window_days if window_days else 0
        if rate > 0.1:
            score += 10.0
            parts.append(
                f"frequent use ({rate:.2f} events/day) (+10)"
            )
    if frozen_share is not None and frozen_share > 0:
        if frozen_share > 1:
            score += 15.0
            parts.append(
                f"frozen supply {frozen_share:.4f}% of primary-chain "
                "supply (>1%) (+15)"
            )
        elif frozen_share >= 0.1:
            score += 10.0
            parts.append(
                f"frozen supply {frozen_share:.4f}% of primary-chain "
                "supply (0.1–1%) (+10)"
            )
        else:
            score += 5.0
            parts.append(
                f"frozen supply {frozen_share:.4f}% of primary-chain "
                "supply (<0.1%) (+5)"
            )
    if destroyed_events:
        score += 10.0
        parts.append(
            f"{destroyed_events} destroyed-black-funds event(s) — "
            "issuer seized and burned frozen balances (+10)"
        )
    if sanctions_matches:
        score += 20.0
        parts.append(
            f"{sanctions_matches} OFAC SDN match(es) in the checked "
            "sets (+20)"
        )
    if evs.get("status") == "unavailable":
        parts.append(
            "Note: the freeze-event scan was unavailable, so observed "
            "use may be understated — the score rests on capability."
        )
    return round(_clamp(score), 1), {**ev, "note": ev["note"] + " " + "; ".join(parts) + "."}


# ---------------------------------------------------------------- input 6: adoption (feature 8, two legs only)
def _adoption_input(payments):
    """100 minus the mean of feature 8's chain-coverage and
    fees/finality legs. The peg/liquidity/freeze legs are
    deliberately excluded — they are inputs 1 and 5 here."""
    if payments is None:
        return None, {"note": "Payments-readiness data unavailable."}
    inp = payments.get("inputs") or {}
    cov = (inp.get("chain_coverage") or {}).get("score")
    fee = (inp.get("fees_finality") or {}).get("score")
    cov_ev = (inp.get("chain_coverage") or {}).get("evidence") or {}
    fee_ev = (inp.get("fees_finality") or {}).get("evidence") or {}
    legs = [s for s in (cov, fee) if s is not None]
    if not legs:
        return None, {
            "note": (
                "Neither the chain-coverage nor the fees/finality leg "
                "could be measured — adoption unknown."
            ),
            "chain_coverage_score": cov,
            "fees_finality_score": fee,
        }
    mean = sum(legs) / len(legs)
    score = round(100.0 - mean, 1)
    return score, {
        "chain_coverage_score": cov,
        "material_chains": cov_ev.get("material_chains"),
        "chain_count": cov_ev.get("chain_count"),
        "fees_finality_score": fee,
        "typical_transfer_fee_usd": fee_ev.get("typical_transfer_fee_usd"),
        "finality_seconds": fee_ev.get("finality_seconds"),
        "primary_chain": payments.get("primary_chain"),
        "note": (
            "100 minus the mean of feature 8's chain-coverage and "
            "fees/finality legs ONLY — its peg, liquidity and freeze "
            "legs are excluded here to avoid double-counting the "
            "depeg-risk and freeze-exposure inputs. Fees are "
            "primary-chain figures (live gas/energy price x a "
            "typical transfer — 65,000 gas EVM / 14,000 energy Tron, "
            "both ESTIMATES)."
        ),
    }


# ---------------------------------------------------------------- entry
async def sc_issuer_risk(scope):
    """GET /api/<scope>/issuer-risk — feature 11: one 0-100 issuer
    risk score per coin (higher = riskier), composed of six documented
    inputs reused wholesale from features 1, 3, 4, 6, 8 and 10, each
    with its measured value alongside its sub-score. Cached 300s."""
    cfg = _cfg(scope)
    cache_key = f"scIssuerRisk:{scope}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    depeg, reserves, mint_burn, whales, freeze, payments = await asyncio.gather(
        sc_depeg_score(scope),
        sc_reserves(scope),
        sc_mint_burn(scope),
        sc_whales(scope),
        sc_freeze(scope),
        sc_payments_readiness(scope),
        return_exceptions=True,
    )
    if isinstance(depeg, Exception):
        depeg = None
    if isinstance(reserves, Exception):
        reserves = None
    if isinstance(mint_burn, Exception):
        mint_burn = None
    if isinstance(whales, Exception):
        whales = None
    if isinstance(freeze, Exception):
        freeze = None
    if isinstance(payments, Exception):
        payments = None

    dep_score, dep_ev = _depeg_input(depeg)
    res_score, res_ev = _reserve_input(reserves)
    iss_score, iss_ev = _issuance_input(mint_burn)
    con_score, con_ev = _concentration_input(whales)
    frz_score, frz_ev = _freeze_input(freeze)
    adp_score, adp_ev = _adoption_input(payments)

    inputs = {
        "depeg_risk": {
            "weight": WEIGHTS["depeg_risk"],
            "score": dep_score,
            "evidence": dep_ev,
        },
        "reserve_transparency": {
            "weight": WEIGHTS["reserve_transparency"],
            "score": res_score,
            "evidence": res_ev,
        },
        "issuance_direction": {
            "weight": WEIGHTS["issuance_direction"],
            "score": iss_score,
            "evidence": iss_ev,
        },
        "concentration": {
            "weight": WEIGHTS["concentration"],
            "score": con_score,
            "evidence": con_ev,
        },
        "freeze_exposure": {
            "weight": WEIGHTS["freeze_exposure"],
            "score": frz_score,
            "evidence": frz_ev,
        },
        "adoption": {
            "weight": WEIGHTS["adoption"],
            "score": adp_score,
            "evidence": adp_ev,
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
        "direction": "higher = riskier",
        "weights": dict(WEIGHTS),
        "weights_used": weights_used,
        "inputs": inputs,
        "inputs_unknown": unknown,
        "supply_usd": (depeg or {}).get("supply_usd")
        if depeg else None,
        "price_usd": (depeg or {}).get("price_usd") if depeg else None,
        "deviation_bps": (depeg or {}).get("deviation_bps")
        if depeg else None,
        "methodology": METHODOLOGY,
        "as_of": int(time.time()),
        "sources": [
            "Feature 1 depeg early-warning score (DeFiLlama price/supply/depeg history + DexScreener depth + issuer attestation config)",
            "Feature 3 attestation tracker (issuer transparency pages, as verified — see attestation block)",
            "Feature 4 mint/burn monitor (DeFiLlama daily supply series)",
            "Feature 6 whale & exchange exposure (Blockscout v2 holders / TronScan holders, primary chain)",
            "Feature 10 freeze & blacklist monitor (live eth_call probes + on-chain freeze events + Multicall3 balances + OFAC SDN set)",
            "Feature 8 payments readiness, chain-coverage + fees/finality legs only (feature-5 supply map, public RPC gas/energy prices, CoinGecko native-coin prices)",
        ],
    }
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out
