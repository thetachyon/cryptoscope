"""Issuer risk API (phase 3, item 6) — keyless.

  GET /api/overview/issuer-risk/detail

Productizes the phase-1 composite issuer risk score
(common/issuer_risk.py, feature 11) as a dedicated endpoint:
the 8-issuer ranking PLUS the full per-issuer payload for every
coin, with all six input sub-scores, their evidence, the weights,
the direction (higher = riskier) and the bands stated in every
response.

Endpoint home — decided, not defaulted. The live score already
exists in two places, and neither one does this job:

  * GET /api/<scope>/issuer-risk — the full per-issuer score with
    all inputs/evidence and bands, for ONE coin. It cannot rank
    issuers, and it is left untouched.
  * GET /api/overview/issuer-risk — the 8-issuer rollup, ranked
    rank 1 = least risky, but with TRUNCATED evidence (sub-scores
    plus a handful of headline fields per input). It is left
    untouched — the overview home page keeps fetching it.

So the dedicated endpoint lives in the overview app, alongside
the rollup: one gather of sc_issuer_risk per coin (embedded as
`issuers`, in rank order — never recomputed, never re-weighted),
from which the ranking rows are derived with the same helpers
the rollup uses (_issuer_risk_row / _rank_issuer_risk_rows in
common/overview.py). One upstream pass, and ranking and
breakdowns are the same evaluation by construction — they can
never disagree (see ov_issuer_risk_detail's docstring for the
live-observed failure this prevents). The per-scope
/issuer-risk and the rollup above are left untouched.

What is NOT in this endpoint: no history (a daily full-score
history is not reconstructable keyless — the same reason item 5
ships partial scores only — and no history is faked here), no
per-coin page (that is /api/<scope>/issuer-risk), no new inputs
(the six feature-11 inputs are reused as-is).

Honest limits (carried from the feature-11 work, stated in the
response as `limits` and in the docs entry):

  * Reserve transparency is transparency, not adequacy: no
    keyless API exposes live reserve composition.
  * Weights are this implementation's stated choice — no Par spec
    exists for this score, and no backtest against issuer
    failures has been run. The weights are uncalibrated.
  * Frozen supply is a LOWER-BOUND ESTIMATE (the blacklist is
    not enumerable on-chain); addresses frozen before the
    scanned window are missed.
  * Concentration, freeze and fee figures are PRIMARY-CHAIN
    only (Ethereum for 7 coins, Tron for USDD).
  * Net issuance is a PROXY for demand (redemptions, chain
    migrations and issuer treasury moves also move supply).
  * DAI/USDe/USDD have no freeze function and score low on
    freeze exposure — a statement about issuer power, not an
    endorsement.
  * Coins whose backing is on-chain (DAI, USDD) score middling
    on reserve transparency: the collateral is verifiable in
    principle, but this feature does not measure its
    composition.
  * Heuristic screening signal — not investment advice, not a
    credit rating, not a guarantee.

Rendered at #/issuer-risk (overview app).
"""
import asyncio
import time

from common.clients import _cache_get, _cache_set
from common.issuer_risk import METHODOLOGY, WEIGHTS, sc_issuer_risk
from common.overview import (
    _issuer_risk_row,
    _rank_issuer_risk_rows,
)
from common.stablecoin import STABLECOINS

CACHE_TTL = 300

BANDS = {
    "low": "score < 25",
    "moderate": "25 <= score < 50",
    "elevated": "50 <= score < 75",
    "high": "score >= 75",
}

LIMITS = [
    "Reserve transparency scores how recently and how credibly "
    "the issuer proved its backing, NOT reserve adequacy: no "
    "keyless API exposes live reserve composition, so a low "
    "transparency sub-score does not mean reserves are sufficient "
    "and a high one does not mean they are insufficient.",
    "Weights are this implementation's stated choice (depeg risk "
    "25%, reserve transparency 25%, issuance direction 15%, "
    "concentration 15%, freeze exposure 15%, adoption 5%) — no "
    "Par spec exists for this score, and no backtest against "
    "issuer failures has been run. The weights are uncalibrated.",
    "Frozen supply is a LOWER-BOUND ESTIMATE: the issuer "
    "blacklist is not enumerable on-chain, so the currently-"
    "frozen set is rebuilt from the freeze events inside the "
    "scanned window only; addresses frozen before the window "
    "are missed, and frozen supply sums only those addresses.",
    "Concentration, freeze and fee figures are PRIMARY-CHAIN "
    "only (Ethereum for 7 coins, Tron for USDD). FDUSD/TUSD "
    "hold most of their supply off the primary chain; "
    "concentration and freezes on other chains are not covered.",
    "Net issuance is a PROXY for demand (feature 4): "
    "redemptions, chain migrations and issuer treasury moves "
    "also move supply, so growing supply can reflect issuer "
    "inventory moves rather than end-user demand.",
    "DAI, USDe and USDD have no freeze/blacklist function and "
    "score low on freeze exposure — that is a statement about "
    "issuer power (the issuer cannot freeze holder balances), "
    "not an endorsement of the issuer.",
    "Coins whose backing is on-chain (DAI, USDD) score middling "
    "on reserve transparency: the collateral is verifiable in "
    "principle, but this feature does not measure its "
    "composition.",
    "Heuristic screening signal for comparing issuers — not "
    "investment advice, not a credit rating, not a guarantee.",
]


async def ov_issuer_risk_detail():
    """GET /api/overview/issuer-risk/detail — ranking + the full
    per-issuer payload for every coin (reused wholesale from
    sc_issuer_risk), in rank order, with weights, bands,
    direction, methodology and limits stated once at the top
    level. Cached 300s.

    Single-computation guarantee: the ranking rows AND the
    `issuers` payloads are derived from ONE gather of
    sc_issuer_risk, via the shared _issuer_risk_row /
    _rank_issuer_risk_rows helpers (common/overview.py) — the
    ranking row for a coin and its full breakdown are the same
    evaluation, so their scores can never disagree. This matters
    because the naive shape (rollup first, then a second gather
    for the payloads) DOES disagree live: the shared cache is
    bounded at 512 entries and clears wholesale past that bound,
    so a cold 8-coin computation can evict the per-coin entries
    between the two reads and the second gather recomputes
    against moved upstream data (observed 2026-11-06: FDUSD
    64.6 in the ranking vs 68.7 in the payloads, USDC adoption
    15.0 vs 4.6 — the first pass had FDUSD freeze/concentration
    unavailable). One gather, one truth."""
    cache_key = "overview:issuer-risk-detail"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_issuer_risk(s) for s in scopes), return_exceptions=True
    )
    rows = [_issuer_risk_row(scope, res) for scope, res in zip(scopes, results)]
    ranked = _rank_issuer_risk_rows(rows)
    rank_of = {
        r["scope"]: r["rank"] for r in ranked if r.get("rank") is not None
    }
    issuers = []
    missing = []
    for scope, res in zip(scopes, results):
        if isinstance(res, Exception):
            missing.append(scope)
            continue
        issuers.append(res)
    # Rank order (rank 1 = least risky first); an unscored coin
    # (score None) has no rank and sorts after the scored coins,
    # in scope order; a failed coin is absent from `issuers` and
    # listed in `issuers_missing` instead — never silently dropped.
    issuers.sort(
        key=lambda r: (
            rank_of.get(r["scope"], 999),
            r["scope"],
        )
    )
    scored = [r for r in ranked if not r.get("error") and r.get("score") is not None]
    out = {
        "ranking": ranked,
        "issuers": issuers,
        "issuers_missing": missing,
        "coins_included": sum(1 for r in rows if not r.get("error")),
        "coins_scored": len(scored),
        "weights": dict(WEIGHTS),
        "bands": dict(BANDS),
        "direction": "higher = riskier",
        "methodology": METHODOLOGY,
        "limits": list(LIMITS),
        "note": (
            "Ranking and per-issuer breakdowns are derived from a "
            "single evaluation of the 8 composite scores, so a "
            "coin's ranking score and its breakdown score always "
            "agree (rank 1 = least risky issuer; unknown scores "
            "last). Per-coin concentration, freeze and fee figures "
            "are PRIMARY-CHAIN only (Ethereum for 7 coins, Tron "
            "for USDD). Bands: < 25 low, < 50 moderate, < 75 "
            "elevated, >= 75 high."
        ),
        "as_of": int(time.time()),
    }
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out
