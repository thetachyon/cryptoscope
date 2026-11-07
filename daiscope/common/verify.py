"""Settlement verification API (phase 3, item 9) — keyless.

One transaction hash in, a verification verdict out: confirmation
status, finality stated explicitly against the per-chain
economically-final threshold, USD value at execution with its price
basis, the bridge leg (Circle CCTP attestation where applicable),
and the shareable receipt — in a single call.

Nothing is rebuilt here — the building blocks are reused wholesale:

- Settlement data (timeline, confirmations, amount, USD at
  execution, fee, sender/recipient, bridge leg) comes from
  common/settlement.py ``track_settlement`` (phase 2, Q1).
- The receipt comes from common/settlement.py ``receipt`` (phase 2,
  Q1), embedded verbatim under ``receipt``.

Endpoint home: ``GET /api/walletscope/verify?chain=&hash=`` — a new
dedicated endpoint, not a rename of ``/settle``. The Q1 tracker
(``/settle``) and receipt (``/receipt``) stay exactly as they are:
they are the tracker's UI backing and the shareable receipt, while
``/verify`` is the productized paid-API contract — one call that
answers "did this settle, is it final, what was it worth, and
where is the receipt?". Chains: ethereum / sepolia / optimism /
gnosis / solana / bitcoin (the same SETTLE_CHAINS as the tracker).

Verdict
-------
  - verified-final: confirmed and confirmations/slot-depth at or
    above the economically-final threshold.
  - confirmed-not-final: confirmed (in a block/slot) but below the
    threshold — settled, not yet final by the convention.
  - pending: submitted but not yet mined/confirmed.
  - failed: the transaction executed with an error status.

"Economically final" is a convention (EVM 64 confirmations, Sepolia
12, Solana 32 slots, Bitcoin 6 confirmations), not protocol
finality — a reorg deeper than the threshold is still possible, just
increasingly unlikely. The response states the threshold, the
observed confirmations, and the verdict together, so a caller never
has to infer finality from a raw count.

CCTP: when the transaction is a Circle burn, the bridge leg carries
the live Circle iris-api attestation (``complete`` means the mint is
claimable). A "complete" attestation does NOT link the destination
mint transaction — no keyless source maps a Circle message hash to
it — and the response says so instead of guessing a hash.

Honest limits (also stated in every response):
  - USD at execution uses the execution-day price where available;
    when it is not, the live price is used and labelled
    ``estimate-current``; when no price exists the USD fields are
    null, never invented.
  - Bitcoin amounts are the largest single output; change outputs
    cannot be identified from chain data alone. Total moved is
    reported alongside.
  - Solana recipients are the account with the largest positive
    balance change; SPL token USD values are not priced (no keyless
    execution-day price for arbitrary SPL mints).
  - Wormhole legs are exact-matched client-side on the origin hash;
    a Mayan Swift transfer from a very active sender can be missed
    and is reported as "no bridge transfer detected", never as a
    borrowed leg.
"""
from common.settlement import HONEST_NOTES, receipt, track_settlement

METHOD = (
    "Settlement verification (phase 3, item 9). Settlement data from "
    "common/settlement.py track_settlement (phase 2, Q1) and the "
    "receipt from common/settlement.py receipt, both reused wholesale. "
    "This module only adds the verdict: confirmed/not-final/final "
    "stated explicitly against the per-chain threshold, the USD value "
    "at execution with its price basis, and the receipt embedded in "
    "the same response."
)

LIMITS = HONEST_NOTES + [
    "A \"verified-final\" verdict is a statement about the observed confirmation count against a stated convention threshold, not protocol finality and not proof the payment was the intended one — check the amount, recipient and token before treating a transaction as settled.",
    "The receipt embedded here is generated at call time (generated_at); a later call re-checks the transaction live — confirmations move, and a bridge leg can still progress after the receipt was first issued.",
]


def _verdict(d):
    """(verdict, reason) from a track_settlement payload."""
    status = (d.get("status") or "").lower()
    if status in ("failed", "error"):
        return "failed", "The transaction executed with an error status."
    confirmed = d.get("block") is not None
    conf = d.get("confirmations")
    threshold = d.get("finality_threshold")
    unit = d.get("confirmations_unit", "confirmations")
    if not confirmed:
        return (
            "pending",
            "Submitted but not yet mined — no block, no confirmations yet.",
        )
    if conf is not None and threshold is not None and conf >= threshold:
        return (
            "verified-final",
            f"Confirmed with {conf} {unit} — at or above the "
            f"economically-final threshold of {threshold} {unit}.",
        )
    return (
        "confirmed-not-final",
        f"Confirmed in a block but with {conf} {unit} — below the "
        f"economically-final threshold of {threshold} {unit}.",
    )


async def verify_settlement(chain, h):
    """Verify one transaction: tracker fields + verdict + receipt.

    The response carries every field of ``track_settlement`` (so the
    #/track page renders it unchanged) plus ``verdict``,
    ``verdict_reason``, an explicit ``finality`` block, top-level
    USD-at-execution fields, and the ``receipt`` payload verbatim.
    ``track_settlement`` validates chain/hash and raises
    UpstreamError (400/404) for unknown chains, malformed hashes and
    transactions no upstream knows.
    """
    d = await track_settlement(chain, h)
    r = await receipt(chain, h)
    verdict, reason = _verdict(d)
    amount = d.get("amount") or {}
    conf = d.get("confirmations")
    threshold = d.get("finality_threshold")
    unit = d.get("confirmations_unit", "confirmations")
    out = dict(d)
    out.update(
        {
            "verify": True,
            "verdict": verdict,
            "verdict_reason": reason,
            "finality": {
                "confirmations": conf,
                "confirmations_unit": unit,
                "threshold": threshold,
                "economically_final": bool(
                    conf is not None
                    and threshold is not None
                    and conf >= threshold
                ),
                "verdict": verdict,
                "note": (
                    "\"Economically final\" is a convention "
                    f"({unit} threshold {threshold} on "
                    f"{d.get('chain_label') or chain}), not protocol "
                    "finality — a deeper reorg is still possible, "
                    "just increasingly unlikely."
                ),
            },
            "value_usd_at_execution": amount.get("usd_value"),
            "usd_price_at_execution": amount.get("usd_price"),
            "price_basis": amount.get("price_basis"),
            "receipt": r,
            "verify_url": (
                f"/api/walletscope/verify?chain={d['chain']}&hash={d['hash']}"
            ),
            "method": METHOD,
            "limits": LIMITS,
        }
    )
    return out
