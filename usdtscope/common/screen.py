"""Address screening API (phase 3, item 1) — keyless.

Productizes common/risk.py (vendored OFAC SDN set, 514 addresses,
2026-11-05; bounded funder walk) as a dedicated screen/address
endpoint: a single address in, score + band + explicit reasons + hop
exposure out. Nothing is rebuilt here — classification reuses
preflight._classify (EVM hex / Bitcoin / Solana / ENS) and the walk
reuses evm_risk / bitcoin_risk / solana_risk wholesale.

Verdict (mirrors the sanctions check in preflight, P2):
  - block: direct OFAC SDN match (score 100) or a sanctioned
    counterparty 1 hop away (score >= 75).
  - review: a sanctioned counterparty or mixer 2-3 hops out
    (score 40-74).
  - clear: nothing found in the transactions checked (score 0-39).

Honest limits (also stated in every response):
  - A clear verdict is NOT proof the address is clean: the walk only
    covers the transactions the address page loads (first page), and
    the SDN list is a dated vendored snapshot (2026-11-05).
  - Solana: the SDN list holds only 2 Solana addresses and the walk
    is 1 hop only (counterparties from the 10 most recent
    transactions); deeper hops are not available via the public RPC
    in this version.
  - Tornado Cash is NOT in the vendored set: OFAC removed it from
    the SDN list in March 2025.
  - Scores are a heuristic screening signal, not a legal
    determination.
"""
from common.chains import CHAINS
from common.clients import UpstreamError, _cache_get, _cache_set
from common.preflight import _classify
from common.risk import bitcoin_risk, evm_risk, solana_risk

METHOD = (
    "Address screening (phase 3, item 1). OFAC SDN direct match + "
    "bounded funder walk from common/risk.py (vendored OFAC SDN list, "
    "514 addresses, 2026-11-05): EVM/Bitcoin 3 hops max, Solana 1 hop. "
    "Heuristic screening signal, not a legal determination."
)
LIMITS = [
    "A clear verdict is not proof the address is clean: the walk "
    "covers only the transactions the address page loads (first page), "
    "and the SDN list is a dated vendored snapshot (2026-11-05).",
    "Solana: the SDN list holds only 2 Solana addresses and the walk "
    "is 1 hop only (counterparties from the 10 most recent "
    "transactions); deeper hops are not available via the public RPC "
    "in this version.",
    "Tornado Cash is not in the vendored set — OFAC removed it from "
    "the SDN list in March 2025.",
    "Scores are a heuristic screening signal, not a legal determination.",
]


def _verdict(score, direct_hit):
    if direct_hit is not None or score >= 75:
        return (
            "block",
            "Do not transact with this address — it is on the OFAC SDN "
            "list, or a sanctioned address is 1 hop away.",
        )
    if score >= 40:
        return (
            "review",
            "Review before transacting — a sanctioned address or mixer "
            "is 2-3 hops away.",
        )
    return (
        "clear",
        "Nothing found in the transactions checked — no OFAC SDN match "
        "for this address or its checked counterparties. This is not "
        "proof the address is clean (see limits).",
    )


async def screen_address(address, chain=None):
    """Screen one address. Raises UpstreamError(400) for bad input;
    upstream failures inside the risk walk surface as the risk
    functions' own UpstreamError (honest 502), never as a clean score."""
    family, addr, resolved_from = await _classify(address)
    if family == "evm":
        chain = chain or "ethereum"
        if chain not in CHAINS:
            raise UpstreamError(
                f"Unknown chain '{chain}' (EVM chains: "
                f"{', '.join(CHAINS)})", status=400)
        chain_label = CHAINS[chain]["label"]
    elif family == "solana":
        if chain is not None and chain != "solana":
            raise UpstreamError("chain must be 'solana' for a Solana address",
                                status=400)
        chain = "solana"
        chain_label = "Solana"
    else:
        if chain is not None and chain != "bitcoin":
            raise UpstreamError("chain must be 'bitcoin' for a Bitcoin address",
                                status=400)
        chain = "bitcoin"
        chain_label = "Bitcoin"

    cache_key = f"screen:{family}:{chain}:{addr.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if family == "evm":
        risk = await evm_risk(chain, addr)
    elif family == "bitcoin":
        risk = await bitcoin_risk(addr)
    else:
        risk = await solana_risk(addr)

    score = risk.get("score", 0)
    direct_hit = risk.get("direct_hit")
    verdict, verdict_reason = _verdict(score, direct_hit)
    hits = risk.get("hits") or []
    hits_by_hop = {}
    for h in hits:
        hop = str(h.get("hop"))
        hits_by_hop[hop] = hits_by_hop.get(hop, 0) + 1
    # hits is capped at 5 in the risk response; hits_total is the true
    # count — the per-hop breakdown below is over the shown hits only.
    coverage = risk.get("coverage") or {}
    out = {
        "address": addr,
        "resolved_from": resolved_from,
        "family": family,
        "chain": chain,
        "chain_label": chain_label,
        "score": score,
        "level": risk.get("level"),
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "reasons": risk.get("reasons"),
        "direct_hit": direct_hit,
        "nearest_hop": risk.get("nearest_hop"),
        "hits": hits,
        "hits_total": risk.get("hits_total"),
        "exposure": {
            "nearest_sanctioned_hop": risk.get("nearest_hop"),
            "sanctioned_hits_by_hop": hits_by_hop,
            "hits_shown": len(hits),
            "counterparties_checked": coverage.get("counterparties_checked"),
            "txs_scanned": coverage.get("txs_scanned"),
            "max_hop": coverage.get("max_hop"),
            "walk_partial": coverage.get("walk_partial"),
            "note": coverage.get("note"),
        },
        "coverage": coverage,
        "family_note": risk.get("family_note"),
        "sdn": risk.get("sdn"),
        "method": METHOD,
        "limits": LIMITS,
        "risk": risk,
    }
    _cache_set(cache_key, out, ttl=60)
    return out
