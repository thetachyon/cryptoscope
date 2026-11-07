"""Attestation monitor API (phase 3, item 7) — keyless.

  GET /api/overview/attestations

All 8 stablecoins' attestation status in one endpoint. The
per-coin attestation tracker already exists — feature 3, in the
/reserves responses (GET /api/<scope>/reserves ->
`attestation_tracker`, built in common/stablecoin.py from each
coin's attestation config block) — and this endpoint reuses it
wholesale: one gather of sc_reserves per coin, the tracker
embedded verbatim per coin, headline fields copied from it
(never recomputed, never re-weighted). Nothing is re-verified
and the tracker is not rebuilt.

Endpoint home — decided, not defaulted. The per-scope
equivalent already exists (GET /api/<scope>/reserves carries
the full tracker for ONE coin), so no per-scope endpoint is
added — there is nothing new to serve there. The rollup lives
in the overview app, alongside the item-6 issuer-risk detail
endpoint, which made the same call for the same reason. The
per-scope /reserves endpoints are left untouched.

Coins are ordered overdue first, then due soon, then current,
then live / on-chain / no-attestation, ties in scope order —
a monitor surfaces the stale coins first.

What is NOT in this endpoint: auditor-change history. The
tracker holds current status only, and no history source
exists in the repo (no data file, no recorded series — the
config notes mention past auditors only as prose, e.g. FDUSD's
Prescient / Prism / AOGB sequence and PYUSD's Withum before
KPMG, carried through in each coin's note). So the response
serves current status + cadence and labels itself
current-status-only in `history` — no history is invented.
A paid-tier stored history is the planned way to get one
(see the docs entry's paid_tier).

Honest limits (stated in the response as `limits` and in the
docs entry):

  * Attestation dates are the latest VERIFIED reporting dates
    from the config (feature 3, verified Nov 6, 2026). Later
    reports may exist but could not be verified keyless
    (FDUSD's issuer site blocks automated access) — do not
    assume one exists.
  * "Overdue" means past the next reporting date per the
    coin's cadence, not past a publication deadline: issuers
    typically publish a report several weeks after its
    reporting date, so overdue means the next report has not
    been verified yet, not necessarily that it is late.
  * Reserve figures are dated published figures from the
    named attestation only — never live or estimated. The
    reserve-to-supply ratio divides that dated total by the
    current DeFiLlama circulating supply, with both dates
    stated; the reserve-to-outstanding ratio uses the tokens
    outstanding stated in the same attestation.
  * Coins with no dated attestation (real-time feed, on-chain
    collateral, or none published) carry null reserve ratios
    and are reported with their status, never as zero.
  * No keyless machine-readable API exposes live reserve /
    collateral figures for any of these coins (probed
    2026-11-05) — the linked attestation pages are the source.
  * Screening signal — not investment advice, not a credit
    rating, not a guarantee.

Rendered at #/attestations (overview app), with a summary card
on the overview home page.
"""
import asyncio
import time

from common.clients import _cache_get, _cache_set
from common.overview import _scope_url
from common.stablecoin import STABLECOINS, TRACKER_METHODOLOGY, sc_reserves

CACHE_TTL = 300

# Monitor order: the stale coins first. Anything not in this map
# (should not happen — the tracker only emits these statuses)
# sorts after no_attestation, in scope order.
_STATUS_ORDER = {
    "overdue": 0,
    "due_soon": 1,
    "current": 2,
    "live": 3,
    "on_chain": 4,
    "no_attestation": 5,
}

LIMITS = [
    "Current-status-only: no auditor-change history exists in "
    "this repo (the feature-3 tracker holds current status "
    "only; past auditors appear solely as prose in a coin's "
    "note, e.g. FDUSD's Prescient / Prism / AOGB sequence and "
    "PYUSD's Withum before KPMG). No history is invented here; "
    "a stored history is a planned paid-tier feature.",
    "Attestation dates are the latest VERIFIED reporting dates "
    "from the config (feature 3, verified Nov 6, 2026). Later "
    "reports may exist but could not be verified keyless "
    "(FDUSD's issuer site blocks automated access) — do not "
    "assume one exists.",
    "'Overdue' means past the next reporting date per the "
    "coin's cadence, not past a publication deadline: issuers "
    "typically publish a report several weeks after its "
    "reporting date, so overdue means the next report has not "
    "been verified yet, not necessarily that it is late.",
    "Reserve figures are dated published figures from the "
    "named attestation only — never live or estimated. "
    "Reserve-to-supply ratio = dated reserve total / current "
    "DeFiLlama circulating supply, with both dates stated; "
    "reserve-to-outstanding ratio uses the tokens outstanding "
    "stated in the same attestation.",
    "Coins with no dated attestation (real-time feed, on-chain "
    "collateral, or none published) carry null reserve ratios "
    "and are reported with their status, never as zero.",
    "No keyless machine-readable API exposes live reserve / "
    "collateral figures for any of these coins (probed "
    "2026-11-05) — the linked attestation pages are the source.",
    "Screening signal — not investment advice, not a credit "
    "rating, not a guarantee.",
]


def _coin_row(scope, res):
    """One monitor row from a sc_reserves payload (or an
    exception). Headline fields are copied from the embedded
    tracker — never recomputed — and the tracker itself is
    embedded verbatim under `tracker`, so the row and the
    tracker can never disagree. A failed coin gets an error
    row and is listed in `coins_missing`, never silently
    dropped."""
    cfg = STABLECOINS[scope]
    if isinstance(res, Exception):
        return {
            "scope": scope,
            "scope_url": _scope_url(scope),
            "symbol": cfg["symbol"],
            "name": cfg["name"],
            "issuer": cfg["issuer"],
            "error": str(res),
        }
    t = res.get("attestation_tracker") or {}
    rf = t.get("reserve_figures") or {}
    return {
        "scope": scope,
        "scope_url": _scope_url(scope),
        "symbol": res.get("symbol"),
        "name": res.get("name"),
        "issuer": res.get("issuer"),
        "attestations": res.get("attestations"),
        "auditor": t.get("auditor"),
        "cadence": t.get("cadence"),
        "last_attestation": t.get("last_attestation"),
        "last_attestation_source": t.get("last_attestation_source"),
        "last_attestation_note": t.get("last_attestation_note"),
        "days_since": t.get("days_since"),
        "status": t.get("status"),
        "status_label": t.get("status_label"),
        "next_expected": t.get("next_expected"),
        "overdue_by_days": t.get("overdue_by_days"),
        "reserve_total_usd": rf.get("reserve_total_usd"),
        "reserve_total_approx": rf.get("reserve_total_approx"),
        "reserve_figures_as_of": rf.get("as_of"),
        "reserve_figures_source": rf.get("source"),
        "reserve_figures_source_url": rf.get("source_url"),
        "reserve_to_supply_ratio": rf.get("reserve_to_supply_ratio"),
        "ratio_supply_usd": rf.get("ratio_supply_usd"),
        "ratio_supply_as_of": rf.get("ratio_supply_as_of"),
        "reserve_to_outstanding_ratio": rf.get("reserve_to_outstanding_ratio"),
        "alerts": t.get("alerts") or [],
        "tracker": t,
        "error": None,
    }


async def ov_attestation_monitor():
    """GET /api/overview/attestations — all 8 coins' attestation
    status in one call: the feature-3 tracker reused wholesale
    per coin (embedded verbatim), headline fields copied from
    it, coins ordered overdue first. Cached 300s.

    Single-computation guarantee: each coin's row is built from
    ONE gather of sc_reserves, so a coin's headline fields and
    its embedded tracker are the same evaluation and can never
    disagree."""
    cache_key = "overview:attestations"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    scopes = list(STABLECOINS.keys())
    results = await asyncio.gather(
        *(sc_reserves(s) for s in scopes), return_exceptions=True
    )
    rows = [_coin_row(scope, res) for scope, res in zip(scopes, results)]
    missing = [r["scope"] for r in rows if r.get("error")]
    included = [r for r in rows if not r.get("error")]
    # Overdue first, then due soon, current, live, on-chain,
    # no-attestation; ties in scope order.
    order = {s: i for i, s in enumerate(scopes)}
    included.sort(
        key=lambda r: (
            _STATUS_ORDER.get(r.get("status"), 99),
            order.get(r["scope"], 99),
        )
    )
    counts = {}
    for r in included:
        counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
    by_status = {}
    for r in included:
        by_status.setdefault(r.get("status"), []).append(r.get("symbol"))
    out = {
        "coins": included,
        "coins_included": len(included),
        "coins_missing": missing,
        "summary": {
            "counts": counts,
            "by_status": by_status,
            "overdue": by_status.get("overdue", []),
            "due_soon": by_status.get("due_soon", []),
        },
        "history": {
            "available": False,
            "coverage": "current-status-only",
            "note": (
                "No auditor-change history exists in this repo: "
                "the feature-3 tracker holds current status only "
                "(auditor, cadence, last verified attestation). "
                "Past auditors appear solely as prose in a coin's "
                "note where the config records them (FDUSD: "
                "Prescient, then Prism, then AOGB from Jan 2026; "
                "PYUSD: Withum before KPMG from Feb 28, 2025). "
                "No history is invented; a stored attestation "
                "history is a planned paid-tier feature."
            ),
        },
        "methodology": TRACKER_METHODOLOGY,
        "limits": list(LIMITS),
        "note": (
            "All 8 stablecoin scopes' attestation status in one "
            "call, from the feature-3 tracker reused wholesale "
            "per coin (GET /api/<scope>/reserves -> "
            "attestation_tracker). Coins are ordered overdue "
            "first. 'Overdue' means past the next reporting date "
            "per cadence, not past a publication deadline. "
            "Reserve ratios exist only where a dated published "
            "figure could be verified; coins with no dated "
            "attestation carry null ratios."
        ),
        "as_of": int(time.time()),
    }
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out
