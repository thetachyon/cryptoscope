"""Depeg early-warning score history (phase 3, item 5) — keyless.

  GET /api/<scope>/depeg-score/history?days=30

Productizes the phase-1 depeg score (common/stablecoin_score.py,
Par v0 provisional weights) as a dedicated endpoint with a score
history and threshold-crossing events. The CURRENT full score is
reused wholesale — sc_depeg_score(scope) is called as-is and
embedded as `current`, never recomputed.

A fully honest daily history of the FULL score is NOT
reconstructable keyless, and this endpoint does not pretend it is.
Of the four live inputs, only two can be rebuilt for a past day
from real data:

  * redemption_quality (40%) — FULLY reconstructable: every input
    is a function of the DeFiLlama daily-close price history
    (current deviation that day + depeg frequency / worst depeg in
    the trailing 365 days ending that day). Computed with the exact
    live function (_redemption_score, anchored to that day via its
    as_of parameter) on the history truncated to that day.
  * issuer/operational incident half (7.5% of live weight) —
    reconstructable: depeg days + worst depeg over the history up
    to that day, via the exact live function (_incident_score,
    extracted from _issuer_score).

  The other 52.5% of the live score's weight CANNOT be
  reconstructed historically and is reported unknown for every
  history day — never backfilled:

  * reserve_liquidity (25%) — proxied live by attestation
    staleness, which needs the attestation date in force on that
    day. Only the latest verified attestation date is known (the
    config block); past attestation dates were not recorded, so
    past staleness is unknowable.
  * secondary_market_depth (20%) — proxied live by DexScreener
    near-par liquidity, which is a live snapshot; DexScreener
    exposes no historical liquidity series keyless.
  * issuer/operational disclosure half (7.5%) — attestation
    cadence / auditor as verified TODAY; the cadence/auditor in
    force on a past day is not recorded.

Each history day therefore carries a `partial_score`: the
weight-renormalised mean of the two historically known inputs
(redemption 0.40 + incident 0.075, renormalised over 0.475) —
the same unknown-input rule the live score itself applies
(unknown inputs excluded, remaining weights renormalised), so a
partial score is the live score recomputed with only the
historically measurable inputs. It is labelled `partial_score`
everywhere it appears, with the coverage (47.5% of live weight)
stated — it is NOT the full score, and day-to-day moves in it
reflect price history only, not reserve or depth changes.

Threshold crossings are band changes of the PARTIAL score between
consecutive days, using the live score's bands (low <25,
moderate 25–<50, elevated 50–<75, high >=75). A crossing on the
first window day is detected against the day before the window
when that close exists. Crossings are score-band crossings, not
depeg days — depeg days themselves are already listed by
GET /api/<scope>/depegs.
"""
import time

from common.clients import _cache_get, _cache_set
from common.stablecoin import (
    DEPEG_BPS,
    _cfg,
    _dev_bps,
    _price_history,
)
from common.stablecoin_score import (
    _band,
    _incident_score,
    _redemption_score,
    sc_depeg_score,
)

CACHE_TTL = 300

# Live-score weight of the inputs that ARE reconstructable daily:
# redemption_quality 0.40 + the incident half of
# issuer_operational (0.15 x 0.5 = 0.075). The other 0.525 of live
# weight (reserve liquidity 0.25, secondary-market depth 0.20,
# issuer disclosure half 0.075) is unknown per history day.
PARTIAL_WEIGHTS = {
    "redemption_quality": 0.40,
    "issuer_incident": 0.075,
}
PARTIAL_WEIGHT_TOTAL = sum(PARTIAL_WEIGHTS.values())  # 0.475

INPUTS_UNKNOWN_PER_DAY = [
    "reserve_liquidity",
    "secondary_market_depth",
    "issuer_operational_disclosure",
]

BANDS = {
    "low": "score < 25",
    "moderate": "25 <= score < 50",
    "elevated": "50 <= score < 75",
    "high": "score >= 75",
}

METHOD = (
    "Depeg early-warning score history (phase 3, item 5). The "
    "current full score (Par v0, provisional UNCONFIRMED weights) "
    "is the live /depeg-score evaluation reused wholesale and "
    "embedded as `current`. Each history day carries a "
    "partial_score — the weight-renormalised mean of the two "
    "inputs reconstructable from the DeFiLlama daily-close price "
    "history (redemption quality, 40% of live weight, computed "
    "with the exact live function anchored to that day; and the "
    "issuer incident half, 7.5% of live weight, same function on "
    "the history up to that day). The other 52.5% of live weight "
    "(reserve liquidity 25%, secondary-market depth 20%, issuer "
    "disclosure half 7.5%) cannot be reconstructed historically "
    "— past attestation dates were not recorded and DexScreener "
    "has no historical liquidity series keyless — so those inputs "
    "are unknown for every history day and excluded with weights "
    "renormalised, exactly as the live score does for unknown "
    "inputs. A partial score therefore covers 47.5% of the live "
    "score's weight and reflects price history only. Threshold "
    "crossings are band changes of the partial score between "
    "consecutive days (bands low <25, moderate 25–<50, elevated "
    "50–<75, high >=75, same as the live score). Heuristic "
    "screening signal — not investment advice, not a rating, not "
    "a guarantee."
)

LIMITS = [
    "History is a PARTIAL score, not the full score: each day "
    "covers 47.5% of the live score's weight (redemption quality "
    "40% + issuer incident half 7.5%). Reserve liquidity (25%), "
    "secondary-market depth (20%) and the issuer disclosure half "
    "(7.5%) are unknown for every history day — past attestation "
    "dates were not recorded, so past attestation staleness is "
    "unknowable, and DexScreener exposes no historical liquidity "
    "series keyless. Day-to-day moves in the partial score reflect "
    "price history only, not reserve or depth changes.",
    "A partial score is not directly comparable to the full live "
    "score: the same day can sit in a different band under the "
    "partial score than under the full score, because the unknown "
    "inputs are excluded rather than assumed. The response states "
    "both bands for the latest day.",
    "All history inputs are DeFiLlama daily closes — intraday "
    "depegs larger than the daily close are not counted, and a "
    "depeg that recovered intraday never appears.",
    "The price history starts when DeFiLlama's series starts for "
    "the coin (e.g. USDe's series is much shorter than USDT's). "
    "A window longer than the available history returns fewer "
    "days, stated in history_coverage — days are never invented.",
    "Threshold crossings are band changes of the partial score, "
    "not depeg days. A coin can cross bands without a depeg day "
    "(a slow drift in the trailing-365d history component), and "
    "can have depeg days without crossing a band.",
]


def _date_iso(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _day_partial(day, hist_up_to_day):
    """Partial score for one daily close. `hist_up_to_day` is the
    full price history truncated to days <= this day. Returns the
    history-day dict (without the unknown-inputs label, added by
    the caller)."""
    price = day["price"]
    bps = _dev_bps(price)
    red_score, red_ev = _redemption_score(price, bps, hist_up_to_day, as_of=day["date"])
    incident, inc_ev = _incident_score(hist_up_to_day)
    if red_score is None:
        partial = None
    else:
        partial = round(
            (PARTIAL_WEIGHTS["redemption_quality"] * red_score
             + PARTIAL_WEIGHTS["issuer_incident"] * incident)
            / PARTIAL_WEIGHT_TOTAL,
            1,
        )
    return {
        "date": day["date"],
        "date_iso": _date_iso(day["date"]),
        "price_usd": price,
        "deviation_bps": bps,
        "depeg": bool(bps is not None and abs(bps) > DEPEG_BPS),
        "redemption_score": red_score,
        "redemption_evidence": {
            "depeg_days_365d": red_ev.get("depeg_days_365d"),
            "worst_depeg_365d_bps": red_ev.get("worst_depeg_365d_bps"),
            "worst_depeg_365d_date": red_ev.get("worst_depeg_365d_date"),
        },
        "issuer_incident_score": round(incident, 1),
        "issuer_incident_evidence": {
            "depeg_days_total": inc_ev["depeg_days_total"],
            "worst_depeg_bps": inc_ev["worst_depeg_bps"],
            "worst_depeg_date": inc_ev["worst_depeg_date"],
        },
        "partial_score": partial,
        "band": _band(partial),
        "inputs_unknown": list(INPUTS_UNKNOWN_PER_DAY),
    }


async def sc_depeg_score_history(scope, days=30):
    """GET /api/<scope>/depeg-score/history — current full depeg
    score (reused wholesale) + daily partial-score history over
    the last `days` days + threshold-crossing events."""
    cache_key = f"scDepegScoreHistory:{scope}:{days}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    cfg = _cfg(scope)
    current = await sc_depeg_score(scope)
    hist = await _price_history(cfg)
    now = int(time.time())
    # Window: the last `days` daily closes at or before now, by
    # position in the ascending history (not by assuming the window
    # is the tail — a stray future-dated close must not shift it).
    past_idx = [i for i, p in enumerate(hist) if p["date"] <= now]
    win_idx = past_idx[-days:] if days else []
    window = [hist[i] for i in win_idx]
    # The close immediately before the window, for detecting a
    # crossing on the first window day (not emitted in history).
    prev_day = None
    prev_pos = None
    if win_idx and win_idx[0] > 0:
        prev_pos = win_idx[0] - 1
        prev_day = hist[prev_pos]

    history = []
    for pos in win_idx:
        # History up to this day: all entries with date <= day.
        history.append(_day_partial(hist[pos], hist[: pos + 1]))

    prev_partial = None
    if prev_day is not None:
        prev_partial = _day_partial(prev_day, hist[: prev_pos + 1])

    crossings = []
    prev = prev_partial
    for day in history:
        if prev is not None and day["band"] != prev["band"]:
            crossings.append({
                "date": day["date"],
                "date_iso": day["date_iso"],
                "from_band": prev["band"],
                "to_band": day["band"],
                "direction": (
                    "up" if (day["partial_score"] or 0) > (prev["partial_score"] or 0)
                    else "down"
                ),
                "partial_score": day["partial_score"],
                "previous_partial_score": prev["partial_score"],
                "price_usd": day["price_usd"],
                "deviation_bps": day["deviation_bps"],
                "depeg_day": day["depeg"],
            })
        prev = day

    last = history[-1] if history else None
    out = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "current": current,
        "window": {
            "days_requested": days,
            "days_returned": len(history),
            "start": history[0]["date"] if history else None,
            "start_date": history[0]["date_iso"] if history else None,
            "end": history[-1]["date"] if history else None,
            "end_date": history[-1]["date_iso"] if history else None,
        },
        "history": history,
        "crossings": crossings,
        "crossing_summary": {
            "total": len(crossings),
            "up": sum(1 for c in crossings if c["direction"] == "up"),
            "down": sum(1 for c in crossings if c["direction"] == "down"),
            "current_partial_band": last["band"] if last else None,
            "current_partial_score": last["partial_score"] if last else None,
            "current_full_band": current.get("band"),
            "current_full_score": current.get("score"),
            "bands_agree": (
                last["band"] == current.get("band") if last else None
            ),
            "bands_agree_note": (
                "The latest day's partial band and the live full-score "
                "band agree." if last and last["band"] == current.get("band")
                else "The latest day's partial band and the live "
                "full-score band DIFFER — expected: the partial score "
                "excludes 52.5% of the live score's weight (reserve "
                "liquidity, secondary-market depth, issuer "
                "disclosure), so the two are not directly comparable."
            ) if last else None,
        },
        "bands": dict(BANDS),
        "partial_score_coverage": {
            "weight_covered": PARTIAL_WEIGHT_TOTAL,
            "weight_covered_pct": round(PARTIAL_WEIGHT_TOTAL * 100, 1),
            "inputs_known": [
                {"input": "redemption_quality", "live_weight": 0.40},
                {"input": "issuer_incident (half of issuer_operational)",
                 "live_weight": 0.075},
            ],
            "inputs_unknown": [
                {"input": "reserve_liquidity", "live_weight": 0.25,
                 "reason": "Past attestation dates were not recorded; "
                 "only the latest verified attestation date is known, "
                 "so past attestation staleness is unknowable."},
                {"input": "secondary_market_depth", "live_weight": 0.20,
                 "reason": "DexScreener exposes no historical "
                 "liquidity series keyless — only a live snapshot."},
                {"input": "issuer_operational_disclosure",
                 "live_weight": 0.075,
                 "reason": "Attestation cadence / auditor as verified "
                 "today; the cadence/auditor in force on a past day "
                 "is not recorded."},
            ],
        },
        "history_coverage": {
            "history_first": hist[0]["date"] if hist else None,
            "history_first_date": _date_iso(hist[0]["date"]) if hist else None,
            "history_last": hist[-1]["date"] if hist else None,
            "history_last_date": _date_iso(hist[-1]["date"]) if hist else None,
            "history_days_total": len(hist),
            "note": (
                "History is DeFiLlama daily closes only. A window "
                "longer than the available history returns fewer "
                "days; days are never invented."
            ),
        },
        "method": METHOD,
        "limits": list(LIMITS),
        "as_of": now,
    }
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out
