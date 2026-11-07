"""Quote & route API (phase 3, item 11) — keyless.

The phase 2, P1 quote engine as a documented, productized API: one
transfer in, the ranked routes AND the peg/volatility guard verdict
for both legs out, in a single call.

Nothing is rebuilt here — the building blocks are reused wholesale:

- ``common/quotes.py`` ``get_quote`` (phase 2, P1): ranked LI.FI /
  Circle CCTP routes, fees, times, rate vs spot. The quote payload
  is embedded verbatim (``from`` / ``to`` / ``quoted_at`` /
  ``cache_ttl_seconds`` / ``routes`` / ``sources`` / ``notes`` /
  ``disclaimer``).
- ``common/guard.py`` ``quote_guard`` (phase 2, P3): the peg guard
  (stablecoins) / volatility guard (everything else), called once
  per distinct leg. The best route's rate-vs-spot % is passed as
  the guard's ``slippage_pct`` input — exactly as the #/quote page
  does — never invented here. A leg whose guard call fails does not
  fail the response: that leg is reported ``unknown`` with the
  error, and the routes are still shown.

Endpoint home: ``GET /api/walletscope/quote/with-guard`` — a new
dedicated endpoint, not a rename of ``/quote``. The P1 engine
(``/quote``) and the guard (``/quote/guard``) stay exactly as they
are: they are the quote page's original backing and the standalone
guard check, while ``/quote/with-guard`` is the productized paid-API
contract — one call that answers "what are the routes, and is
either coin safe to quote right now?". The existing endpoints are
not marked paid_api; only this combined contract is.

Guard summary
-------------
``guard_summary`` carries the two leg verdicts and the worst of
them (``block`` > ``warn`` > ``unknown`` > ``ok``) plus a
``blocked`` flag (True when either leg is ``block``). A ``block``
verdict is a screening signal — do not pay in that coin — not a
transaction block: nothing here creates, signs, or blocks a
transaction.

Honest limits (also stated in every response):
  - Quotes are estimates, not executable quotes: amounts, fees and
    times move with the market and the quote expires quickly
    (cached 30s server-side). No transaction is created or signed.
  - LI.FI (EVM chains only) is keyless with a 75 requests / 2h
    limit per shared egress IP — a rate limit surfaces in
    ``sources`` as an error and the CCTP estimate route (if any)
    is still shown; a quote is never invented to fill the gap.
  - Swap API (swapapi.dev) returns 404 on every path probed and
    OpenOcean is Cloudflare-blocked from this server, so neither
    contributes routes — do not re-add without re-probing.
  - Solana and Bitcoin have no keyless quote source reachable
    from this server (LI.FI lists EVM chains only; Jupiter is
    unreachable from the sandbox) — requests involving them
    return zero routes, honestly, never a fabricated quote.
  - CCTP routes are computed estimates (1:1 burn/mint, no bridge
    fee, gas from the live gas price x a typical gas budget,
    ~13-19 min for the attestation), labelled as such in every
    route — no API returns CCTP pricing.
  - A guard verdict is a screening signal at quote time, not
    investment advice and not a guarantee: the peg score is the
    Par v0 provisional heuristic (UNCONFIRMED weights),
    volatility is backward-looking (daily closes), and the
    slippage figure is the quote's own estimate.
"""
import asyncio

from common.guard import quote_guard
from common.quotes import get_quote

METHOD = (
    "Quote & route API (phase 3, item 11). The phase 2, P1 quote "
    "engine (common/quotes.py get_quote: ranked LI.FI / Circle "
    "CCTP routes, fees, times, rate vs DeFiLlama spot) reused "
    "wholesale, plus the phase 2, P3 peg/volatility guard "
    "(common/guard.py quote_guard) for both legs in the same "
    "response — the best route's rate-vs-spot % is passed as the "
    "guard's slippage_pct input, exactly as the #/quote page does. "
    "This module only adds the guard block, the guard summary "
    "(worst leg verdict + blocked flag), and the contract fields "
    "(method, limits, api/quote/guard URLs)."
)

LIMITS = [
    "Quotes are estimates, not executable quotes: amounts, fees and times move with the market and the quote expires quickly (cached 30s server-side). No transaction is created or signed here.",
    "LI.FI covers EVM chains only, keyless, 75 requests / 2h per shared egress IP — a rate limit surfaces in `sources` as an error and the CCTP estimate route (if any) is still shown; a quote is never invented to fill the gap.",
    "Swap API (swapapi.dev) returns 404 on every path probed and OpenOcean is Cloudflare-blocked from this server, so neither contributes routes — do not re-add without re-probing.",
    "Solana and Bitcoin have no keyless quote source reachable from this server (LI.FI lists EVM chains only; Jupiter is unreachable from the sandbox) — requests involving them return zero routes, honestly, never a fabricated quote.",
    "CCTP routes are computed estimates (1:1 burn/mint, no bridge fee, gas from the live gas price x a typical gas budget, ~13-19 min for the attestation), labelled as such in every route — no API returns CCTP pricing.",
    "A guard verdict is a screening signal at quote time, not investment advice and not a guarantee: the peg score is the Par v0 provisional heuristic (UNCONFIRMED weights), volatility is backward-looking (daily closes), and the slippage figure is the quote's own estimate.",
    "A leg whose guard check fails is reported `unknown` with the error — the routes are still shown and the response never claims a verdict it did not measure.",
]

_RANK = {"block": 3, "warn": 2, "unknown": 1, "ok": 0}


async def _guard_leg(coin, chain, amount, slippage):
    """One guard leg, never raising: failure becomes verdict unknown."""
    try:
        g = await quote_guard(
            coin, chain=chain, amount=amount, slippage_pct=slippage
        )
        return g
    except Exception as e:
        return {
            "coin": str(coin).upper(),
            "symbol": str(coin).upper(),
            "chain": chain,
            "mode": None,
            "verdict": "unknown",
            "verdict_reasons": [f"Guard check failed: {e}"],
            "error": str(e),
        }


async def quote_api(from_chain, from_token, to_chain, to_token, amount,
                    from_address=None):
    """Ranked routes + guard verdicts for both legs, in one response.

    The ``get_quote`` payload is embedded verbatim (``error`` payloads
    pass through untouched — the route turns them into a 400), plus
    ``quote_api``, ``guard`` (``from_leg`` / ``to_leg``, each a full
    ``quote_guard`` response or an ``unknown`` leg on failure),
    ``guard_summary``, ``method``, ``limits`` and the api / quote /
    guard URLs. Identical legs (same coin, same chain) share one
    guard call.
    """
    payload = await get_quote(
        from_chain, from_token, to_chain, to_token, amount, from_address
    )
    if payload.get("error"):
        return payload

    routes = payload.get("routes") or []
    best = routes[0] if routes else None
    slippage = best.get("vs_spot_pct") if best else None

    from urllib.parse import urlencode

    qd = {
        "from_chain": from_chain,
        "from_token": from_token,
        "to_chain": to_chain,
        "to_token": to_token,
        "amount": amount,
    }
    if from_address:
        qd["from_address"] = from_address
    qs = urlencode(qd)
    base_url = f"/api/walletscope/quote/with-guard?{qs}"
    quote_url = f"/api/walletscope/quote?{qs}"

    same_leg = (
        str(from_token).lower() == str(to_token).lower()
        and from_chain == to_chain
    )
    if same_leg:
        from_leg = await _guard_leg(from_token, from_chain, amount, slippage)
        to_leg = from_leg
    else:
        from_leg, to_leg = await asyncio.gather(
            _guard_leg(from_token, from_chain, amount, slippage),
            _guard_leg(to_token, to_chain, amount, slippage),
        )

    def _guard_url(coin, chain):
        gqs = urlencode({"coin": coin, "chain": chain, "amount": amount})
        if slippage is not None:
            gqs += "&" + urlencode({"slippage_pct": slippage})
        return f"/api/walletscope/quote/guard?{gqs}"

    fv = str((from_leg or {}).get("verdict") or "unknown").lower()
    tv = str((to_leg or {}).get("verdict") or "unknown").lower()
    worst = fv if _RANK.get(fv, 1) >= _RANK.get(tv, 1) else tv

    out = dict(payload)
    out.update(
        {
            "quote_api": True,
            "guard": {"from_leg": from_leg, "to_leg": to_leg},
            "guard_summary": {
                "from_verdict": fv,
                "to_verdict": tv,
                "worst_verdict": worst,
                "blocked": worst == "block",
                "note": (
                    "Worst of the two leg verdicts "
                    "(block > warn > unknown > ok). A block verdict "
                    "is a screening signal — do not pay in that "
                    "coin — not a transaction block."
                ),
            },
            "method": METHOD,
            "limits": LIMITS,
            "api_url": base_url,
            "quote_url": quote_url,
            "guard_urls": {
                "from_leg": _guard_url(from_token, from_chain),
                "to_leg": _guard_url(to_token, to_chain),
            },
        }
    )
    return out
