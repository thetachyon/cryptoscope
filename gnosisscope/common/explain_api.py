"""Plain-English transaction API (phase 3, item 13) — keyless.

One transaction hash in, a plain-English explanation out: one
sentence ("What happened") plus the token-flow summary (token,
amount, from -> to) — the same explanation the "What happened" card
on every transaction page shows.

Nothing is rebuilt here — the explainer is reused wholesale:

- EVM: common/explain.py ``evm_tx_explain`` (feature 3).
- Solana: common/explain.py ``sol_tx_explain`` (feature 3).
- Bitcoin: common/explain.py ``btc_tx_explain`` (feature 3).

Endpoint home: ``GET /api/walletscope/explain?chain=&hash=`` — a new
dedicated endpoint, not a rename of the per-scope explainer.
``GET /api/<scope>/tx/{h}/explain`` stays exactly as it is: it is
the tx-page card's backing, while ``/explain`` is the productized
paid-API contract — the same explanation payload plus the contract
fields (``explain``, ``hash``, ``chain_label``, ``api_url``,
``method``, ``limits``) and the cross-chain dispatch on ``?chain=``.
One rename: the explainer's ``method`` (the decoded text signature)
moves to ``method_signature`` in this contract, because ``method``
carries the method description here, as in every other phase-3
wrapper. The per-scope ``/tx/{h}/explain`` payload is untouched
and keeps ``method`` as the signature.
Chains: ethereum / sepolia / optimism / gnosis / solana / bitcoin
(the same SETTLE_CHAINS as the settlement tracker).

Honest labels (also stated in every response, inherited from the
explainer):
  - A method the signature database does not know is reported
    honestly ("method 0x... is not in the signature database, so
    what it does cannot be described here") — never an invented
    description. The 4byte.directory fallback accepts only
    function names in the curated KNOWN_NAMES set; colliding junk
    signatures are never trusted.
  - Multicall inner calls are not decoded — the sentence says so
    and the individual actions are only visible in the token flows.
    ``execute`` (proxy / meta-tx forwarding) shows the forwarded-to
    address and the wei attached, not the inner calldata.
  - Swap amounts come from the transaction's token transfers (the
    actual movements), never from the call's minimum-out arguments.
    A token -> native swap cannot show the exact native amount —
    it is an internal transfer, not a token transfer — and the
    sentence says so.
  - Only the first page of token transfers is shown when a
    transaction has more; the note says so.
  - Solana: token symbols are not resolved — SPL mints are shown
    shortened. SOL balance changes include the transaction fee paid
    by the fee payer.
  - Bitcoin: change outputs cannot be identified from chain data
    alone — every output is listed, including any change back to
    the sender.
  - A failed (reverted) transaction's sentence is prefixed with
    what it attempted to do — the attempt is described, never
    presented as if it succeeded.
"""
from common.chains import CHAINS
from common.clients import _cache_get, _cache_set
from common.explain import btc_tx_explain, evm_tx_explain, sol_tx_explain
from common.settlement import SETTLE_CHAINS, _validate

METHOD = (
    "Plain-English transaction API (phase 3, item 13). The tx "
    "explainer from common/explain.py (feature 3) reused wholesale — "
    "one sentence plus the token-flow summary, method names from the "
    "local canonical signature map then the keyless 4byte.directory "
    "API (only curated names accepted), swap amounts from the "
    "transaction's token transfers. This module only adds the "
    "cross-chain dispatch on ?chain= and the paid-API contract "
    "fields (explain, hash, chain_label, api_url, method, limits) — "
    "no method decoding or sentence building is re-implemented."
)

LIMITS = [
    "A method the signature database does not know is reported "
    "honestly ('method 0x... is not in the signature database, so "
    "what it does cannot be described here') — never an invented "
    "description. The 4byte.directory fallback accepts only "
    "function names in the curated KNOWN_NAMES set; colliding junk "
    "signatures are never trusted.",
    "Multicall inner calls are not decoded — the sentence says so "
    "and the individual actions are only visible in the token flows "
    "below. execute (proxy / meta-tx forwarding) shows the "
    "forwarded-to address and the wei attached, not the inner "
    "calldata.",
    "Swap amounts come from the transaction's token transfers (the "
    "actual movements), never from the call's minimum-out arguments. "
    "A token -> native swap cannot show the exact native amount — "
    "it is an internal transfer, not a token transfer — and the "
    "sentence says so.",
    "Only the first page of token transfers is shown when a "
    "transaction has more; the response's note says so.",
    "Solana: token symbols are not resolved — SPL mints are shown "
    "shortened. SOL balance changes include the transaction fee paid "
    "by the fee payer.",
    "Bitcoin: change outputs cannot be identified from chain data "
    "alone — every output is listed, including any change back to "
    "the sender.",
    "A failed (reverted) transaction's sentence describes what it "
    "attempted to do, prefixed as an attempt — never presented as "
    "if it succeeded.",
    "Results are cached 60s server-side; a repeat call inside the "
    "window returns the same explanation.",
]


async def explain_transaction(chain, h):
    """Explain one transaction: the wholesale explainer payload plus
    the paid-API contract fields. Raises UpstreamError (400/404) for
    unknown chains, malformed hashes and transactions no upstream
    knows; a per-scope explain 404 passes through unchanged."""
    h = _validate(chain, h)
    key = f"explain:{chain}:{h}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    if chain in CHAINS:
        out = await evm_tx_explain(chain, h)
    elif chain == "solana":
        out = await sol_tx_explain(h)
    else:
        out = await btc_tx_explain(h)
    if not isinstance(out, dict):
        return out  # JSONResponse (404) passthrough, as in explain.py
    canonical = out.get("tx_hash") or out.get("signature") or out.get("txid") or h
    chain_label = SETTLE_CHAINS[chain]["label"]
    out = dict(out)
    # The explainer's `method` is the decoded text signature (e.g.
    # "transfer(address,uint256)"); the paid-API contract reserves
    # `method` for the method description, as in every other phase-3
    # wrapper — so the signature moves to `method_signature` here.
    # The per-scope /tx/{h}/explain payload is untouched and keeps
    # `method` as the signature.
    if out.get("method") is not None:
        out["method_signature"] = out.pop("method")
    out.update(
        {
            "explain": True,
            "hash": canonical,
            "chain_label": chain_label,
            "api_url": f"/api/walletscope/explain?chain={chain}&hash={h}",
            "method": METHOD,
            "limits": LIMITS,
        }
    )
    _cache_set(key, out, ttl=60)
    return out
