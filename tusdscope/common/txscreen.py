"""Transaction screening API (phase 3, item 2) — keyless.

Score one transaction before/after settlement: taint, sanctioned
counterparties, mixer interaction. Nothing is rebuilt here — the
building blocks are reused wholesale:

- Settlement data (value, USD at execution, confirmations, status,
  sender/recipient, token transfers) comes from
  common/settlement.py ``track_settlement`` (phase 2, Q1).
- Sanctions classification reuses common/risk.py: the vendored OFAC
  SDN set (514 addresses, 2026-11-05) via ``lookup`` / ``is_mixer`` /
  the hop-score tables, and the bounded funder walk via
  ``evm_risk`` / ``bitcoin_risk`` / ``solana_risk`` — run once per
  walked counterparty, not re-implemented.

How a transaction is scored
----------------------------
Hop 1 (direct): every distinct counterparty of the transaction —
sender, recipient, token-transfer parties (EVM), input/output
addresses (Bitcoin), transaction accounts + token owners (Solana) —
is checked directly against the OFAC SDN list. A direct
counterparty on the list scores 100 / critical (the transaction
itself involves a sanctioned address).

Hop 2-3 (indirect): each walked counterparty's own risk walk is
reused, and its hits are shifted by one hop: a sanctioned address
1 hop from a counterparty is 2 hops from this transaction, 2 hops
from a counterparty is 3 hops from this transaction. The same
score tables as the address walk apply at the shifted hop
(sanctioned 80/60/40, mixer 70/50/40). Hits 4+ hops from the
transaction are counted as beyond-scope, never scored.

Verdict (mirrors address screening, item 1):
  - block: a direct counterparty is on the SDN list (score 100),
    or score >= 75.
  - review: score 40-74 (sanctioned counterparty or mixer 2-3 hops out).
  - clear: nothing found in the counterparties checked (score 0-39).

Honest limits (also stated in every response):
  - A clear verdict is NOT proof the transaction is clean: only the
    listed counterparties are checked directly, only a capped
    number of them get the full funder walk, and each walk covers
    only the transactions its page loads.
  - EVM: at most 4 counterparties walked (sender/recipient first);
    each walk is the address walk (first tx page + 1 page each for
    up to 10 first-hop and 5 second-hop counterparties, 3 hops max
    from the counterparty — so scored exposure stops 3 hops from
    this transaction).
  - Solana: at most 4 counterparties walked, and each Solana walk
    is 1 hop only — so scored exposure stops 2 hops from this
    transaction. The SDN list holds only 2 Solana addresses.
  - Bitcoin: at most 4 counterparties walked (25 most recent txs
    per counterparty page); all output addresses are checked
    directly, but change outputs cannot be distinguished from
    payment outputs, so every output counts as a counterparty.
  - Internal EVM transfers (calls inside a contract that move
    value without a token-transfer record) are not counterparties
    here — only the tx's sender/recipient and its recorded token
    transfers are checked.
  - Tornado Cash is NOT in the vendored set: OFAC removed it from
    the SDN list in March 2025.
  - Scores are a heuristic screening signal, not a legal
    determination.
"""
from common.clients import UpstreamError, _cache_get, _cache_set
from common.risk import (
    HOP_SCORES,
    MIXER_HOP_SCORES,
    OFAC_SDN_COUNT,
    OFAC_SDN_DATE,
    OFAC_SDN_SOURCE,
    is_mixer,
    level_for,
    lookup,
)
from common.settlement import SETTLE_CHAINS, _validate, track_settlement

METHOD = (
    "Transaction screening (phase 3, item 2). Settlement data from "
    "common/settlement.py (phase 2, Q1); sanctions classification "
    "from common/risk.py (vendored OFAC SDN list, 514 addresses, "
    "2026-11-05): every counterparty checked directly, a capped "
    "number walked with the address funder walk, hits shifted by "
    "one hop. Heuristic screening signal, not a legal determination."
)
LIMITS = [
    "A clear verdict is not proof the transaction is clean: only "
    "the listed counterparties are checked directly, only a capped "
    "number of them get the full funder walk, and each walk covers "
    "only the transactions its page loads.",
    "EVM: at most 4 counterparties walked (sender/recipient first); "
    "each walk is the address walk (first tx page plus one page "
    "each for up to 10 first-hop and 5 second-hop counterparties) — "
    "scored exposure stops 3 hops from this transaction.",
    "Solana: at most 4 counterparties walked, and each Solana walk "
    "is 1 hop only — scored exposure stops 2 hops from this "
    "transaction. The SDN list holds only 2 Solana addresses.",
    "Bitcoin: at most 4 counterparties walked (25 most recent txs "
    "per counterparty page); all output addresses are checked "
    "directly, but change outputs cannot be distinguished from "
    "payment outputs, so every output counts as a counterparty.",
    "Internal EVM transfers (value moved inside a contract call "
    "with no token-transfer record) are not counterparties here — "
    "only the sender/recipient and the recorded token transfers "
    "are checked.",
    "Tornado Cash is not in the vendored set — OFAC removed it from "
    "the SDN list in March 2025.",
    "Scores are a heuristic screening signal, not a legal determination.",
]

# Caps on the expensive part (one full funder walk per counterparty).
MAX_WALKED = 4
# Caps on the cheap part (direct SDN lookup per counterparty).
MAX_DIRECT = {"evm": 10, "solana": 12, "bitcoin": 12}

# Scored exposure stops at this many hops from the transaction.
# EVM/Bitcoin: the counterparty walk reaches 3 hops from the
# counterparty, so tx hops 2-3 are scored. Solana: the walk is
# 1 hop only, so only tx hop 2 is scored.
MAX_TX_HOP = {"evm": 3, "bitcoin": 3, "solana": 2}


def _short(addr):
    a = str(addr)
    return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


def _verdict(score, direct_hit):
    if direct_hit is not None or score >= 75:
        return (
            "block",
            "Do not accept or rely on this transaction — a counterparty "
            "is on the OFAC SDN list, or a sanctioned address is 1 hop "
            "from a counterparty (2 hops from this transaction).",
        )
    if score >= 40:
        return (
            "review",
            "Review before accepting this transaction — a sanctioned "
            "address or mixer is 2-3 hops from it.",
        )
    return (
        "clear",
        "Nothing found in the counterparties checked — no OFAC SDN "
        "match for this transaction's counterparties or within the "
        "walked hops. This is not proof the transaction is clean "
        "(see limits).",
    )


def _ordered_cps(pairs, cap):
    """Deduplicate (address, role) pairs preserving order; first role
    wins. Returns [(address, role)] capped at `cap`."""
    seen = {}
    for addr, role in pairs:
        if not addr:
            continue
        key = addr.lower() if str(addr).startswith("0x") else str(addr)
        if key not in seen:
            seen[key] = (addr, role)
    return list(seen.values())[:cap]


async def _counterparties(chain, kind, h, settle):
    """Ordered (address, role) pairs for a transaction, from the
    settlement payload plus the chain's own tx detail where the
    settlement payload is lossy (Bitcoin outputs, Solana accounts)."""
    if kind == "evm":
        pairs = []
        if settle.get("from"):
            pairs.append((settle["from"], "sender"))
        if settle.get("to"):
            pairs.append((settle["to"], "recipient"))
        for tt in settle.get("token_transfers") or []:
            if tt.get("from"):
                pairs.append((tt["from"], "token-transfer sender"))
            if tt.get("to"):
                pairs.append((tt["to"], "token-transfer recipient"))
        return _ordered_cps(pairs, MAX_DIRECT["evm"])
    if kind == "bitcoin":
        from common.bitcoin import btc_tx

        pairs = []
        try:
            detail = await btc_tx(h)
        except Exception:
            detail = None
        if detail is not None:
            outputs = sorted(
                (o for o in detail.get("outputs") or [] if o.get("address")),
                key=lambda o: o.get("value_sats") or 0,
                reverse=True,
            )
            for o in outputs:
                pairs.append((o["address"], "output"))
            for i in detail.get("inputs") or []:
                if i.get("address"):
                    pairs.append((i["address"], "input"))
        else:
            # Fall back to what the settlement payload carries.
            for s in settle.get("senders") or []:
                pairs.append((s, "input"))
            if settle.get("to"):
                pairs.append((settle["to"], "output"))
        return _ordered_cps(pairs, MAX_DIRECT["bitcoin"])
    # solana
    from common.risk import KNOWN_PROGRAMS
    from common.solana_explorer import sol_tx

    pairs = []
    if settle.get("from"):
        pairs.append((settle["from"], "sender"))
    if settle.get("to"):
        pairs.append((settle["to"], "recipient"))
    for tt in settle.get("token_transfers") or []:
        if tt.get("to"):
            pairs.append((tt["to"], "token owner"))
    try:
        detail = await sol_tx(h)
        for a in detail.get("accounts") or []:
            addr = a.get("address") if isinstance(a, dict) else None
            if addr and addr not in KNOWN_PROGRAMS:
                pairs.append((addr, "account"))
    except Exception:
        pass
    return _ordered_cps(pairs, MAX_DIRECT["solana"])


async def _walk_counterparty(chain, kind, addr):
    """One counterparty's risk walk (reused wholesale). Returns the
    risk dict, or raises the risk function's UpstreamError."""
    if kind == "evm":
        from common.risk import evm_risk

        return await evm_risk(chain, addr)
    if kind == "bitcoin":
        from common.risk import bitcoin_risk

        return await bitcoin_risk(addr)
    from common.risk import solana_risk

    return await solana_risk(addr)


async def screen_transaction(chain, h):
    """Screen one transaction. Raises UpstreamError(400) for bad
    input; a tx the settlement tracker cannot find surfaces as its
    own 404. A counterparty walk that fails upstream is recorded as
    unchecked (walk_partial), never as a clean score for that
    counterparty."""
    h = _validate(chain, h)
    kind = "evm" if chain in SETTLE_CHAINS and chain != "solana" and chain != "bitcoin" else chain
    # _validate already rejects unknown chains; kind is one of
    # evm / solana / bitcoin by construction of SETTLE_CHAINS.
    cache_key = f"txscreen:{chain}:{h}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    settle = await track_settlement(chain, h)
    cps = await _counterparties(chain, kind, h, settle)

    direct_hits = []
    cp_entries = []
    hits = []
    beyond_scope = 0
    txs_scanned = 0
    walk_partial = False
    walked = 0
    max_tx_hop = MAX_TX_HOP[kind]

    for addr, role in cps:
        entry = {
            "address": addr,
            "role": role,
            "direct_hit": None,
            "walked": False,
            "walk_error": None,
            "risk_score": None,
            "risk_level": None,
            "nearest_hop_from_counterparty": None,
            "hits_total": None,
            "txs_scanned": None,
        }
        direct = lookup(addr)
        if direct is not None:
            entry["direct_hit"] = direct
            direct_hits.append({"address": addr, "role": role, **direct})
            cp_entries.append(entry)
            continue
        if walked >= MAX_WALKED:
            cp_entries.append(entry)
            continue
        try:
            risk = await _walk_counterparty(chain, kind, addr)
        except Exception as e:
            walk_partial = True
            entry["walk_error"] = str(e)[:120]
            cp_entries.append(entry)
            continue
        walked += 1
        entry["walked"] = True
        entry["risk_score"] = risk.get("score")
        entry["risk_level"] = risk.get("level")
        entry["nearest_hop_from_counterparty"] = risk.get("nearest_hop")
        entry["hits_total"] = risk.get("hits_total")
        cov = risk.get("coverage") or {}
        entry["txs_scanned"] = cov.get("txs_scanned")
        txs_scanned += cov.get("txs_scanned") or 0
        if cov.get("walk_partial"):
            walk_partial = True
        for hit in risk.get("hits") or []:
            tx_hop = (hit.get("hop") or 0) + 1
            if tx_hop > max_tx_hop:
                beyond_scope += 1
                continue
            table = MIXER_HOP_SCORES if hit.get("mixer") else HOP_SCORES
            hits.append(
                {
                    "tx_hop": tx_hop,
                    "via_counterparty": addr,
                    "via_role": role,
                    "counterparty_hop": hit.get("hop"),
                    "address": hit.get("address"),
                    "currency": hit.get("currency"),
                    "name": hit.get("name"),
                    "program": hit.get("program"),
                    "mixer": bool(hit.get("mixer")),
                    "score": table[tx_hop],
                }
            )
        cp_entries.append(entry)

    if direct_hits:
        score = 100
    elif hits:
        score = max(hit["score"] for hit in hits)
    else:
        score = 0
    level = level_for(score)
    direct_hit = direct_hits[0] if direct_hits else None
    verdict, verdict_reason = _verdict(score, direct_hit)

    reasons = []
    for dh in direct_hits:
        reasons.append(
            f"{dh['role'].capitalize()} {_short(dh['address'])} is on the "
            f"OFAC SDN list as {dh['name']} ({dh['program']}; "
            f"{dh['currency']} address) — this transaction directly "
            "involves a sanctioned address."
        )
    for hit in sorted(hits, key=lambda x: -x["score"])[:5]:
        if hit["mixer"]:
            reasons.append(
                f"{hit['tx_hop']}-hop counterparty {_short(hit['address'])} "
                f"is a sanctioned mixer ({hit['name']}, {hit['program']}) "
                f"— funds touched a listed mixer (via {hit['via_role']} "
                f"{_short(hit['via_counterparty'])})."
            )
        else:
            reasons.append(
                f"{hit['tx_hop']}-hop counterparty {_short(hit['address'])} "
                f"is on the OFAC SDN list as {hit['name']} "
                f"({hit['program']}) — via {hit['via_role']} "
                f"{_short(hit['via_counterparty'])}."
            )
    if len(hits) > 5:
        reasons.append(
            f"Plus {len(hits) - 5} more sanctioned counterparties in the "
            "walk (showing 5)."
        )
    if not reasons:
        reasons.append(
            f"No OFAC SDN match among the {len(cps)} counterparties "
            "checked directly."
        )
        reasons.append(
            f"No sanctioned counterparties within {max_tx_hop} hops of "
            f"the {walked} walked counterpart{'y' if walked == 1 else 'ies'} "
            f"({txs_scanned} transactions scanned in their walks)."
        )
    if beyond_scope:
        reasons.append(
            f"Note: {beyond_scope} further sanctioned counterparties "
            f"were found {max_tx_hop + 1} hops from this transaction — "
            "beyond the scored scope, so they do not raise the score."
        )
    if walk_partial:
        reasons.append(
            "Note: part of the counterparty walk failed or timed out "
            "upstream, so the hop check is partial — a low score here "
            "is weaker evidence than a complete walk."
        )
    if not settle.get("block"):
        reasons.append(
            "Transaction is not yet confirmed — screening covers the "
            "counterparties visible now; token transfers are not yet "
            "known for a pending transaction."
        )

    hits_by_hop = {}
    for hit in hits:
        k = str(hit["tx_hop"])
        hits_by_hop[k] = hits_by_hop.get(k, 0) + 1
    mixer_hits = [hit for hit in hits if hit.get("mixer")]
    amount = settle.get("amount") or {}
    confirmations = settle.get("confirmations")
    threshold = settle.get("finality_threshold")
    bridge = settle.get("bridge") or {}
    out = {
        "tx_hash": h,
        "hash": h,
        "chain": chain,
        "chain_label": settle.get("chain_label"),
        "kind": kind,
        "score": score,
        "level": level,
        "verdict": verdict,
        "verdict_reason": verdict_reason,
        "reasons": reasons,
        "direct_hits": direct_hits,
        "direct_hit": direct_hits[0] if direct_hits else None,
        "nearest_hop": min((hit["tx_hop"] for hit in hits), default=None),
        "hits": hits,
        "hits_total": len(hits),
        "hits_beyond_scope": beyond_scope,
        "mixer_hits": mixer_hits,
        "counterparties": cp_entries,
        "exposure": {
            "nearest_sanctioned_hop": min((hit["tx_hop"] for hit in hits), default=None),
            "sanctioned_hits_by_hop": hits_by_hop,
            "hits_shown": len(hits),
            "counterparties_checked": len(cps),
            "counterparties_walked": walked,
            "txs_scanned": txs_scanned,
            "max_hop": max_tx_hop,
            "walk_partial": walk_partial,
            "note": (
                f"Direct SDN check on all {len(cps)} counterparties; "
                f"full funder walk on {walked} (cap {MAX_WALKED}, "
                "sender/recipient first). Scored exposure stops "
                f"{max_tx_hop} hops from this transaction."
            ),
        },
        "coverage": {
            "counterparties_checked": len(cps),
            "counterparties_walked": walked,
            "counterparties_total_listed": len(cps),
            "txs_scanned": txs_scanned,
            "max_hop": max_tx_hop,
            "walk_partial": walk_partial,
            "note": (
                f"Direct SDN check on all {len(cps)} counterparties; "
                f"full funder walk on {walked} (cap {MAX_WALKED})."
            ),
        },
        "settlement": {
            "status": settle.get("status"),
            "confirmations": confirmations,
            "confirmations_unit": settle.get("confirmations_unit", "confirmations"),
            "finality_threshold": threshold,
            "economically_final": bool(
                confirmations is not None
                and threshold is not None
                and confirmations >= threshold
            ),
            "block": settle.get("block"),
            "timestamp": settle.get("timestamp"),
            "age": settle.get("age"),
            "from": settle.get("from"),
            "to": settle.get("to"),
            "amount": amount,
            "value": amount.get("value"),
            "symbol": amount.get("symbol"),
            "usd_value": amount.get("usd_value"),
            "usd_price": amount.get("usd_price"),
            "price_basis": amount.get("price_basis"),
            "fee": settle.get("fee"),
            "explorer_url": settle.get("explorer_url"),
            "bridge": (
                {
                    "kind": bridge.get("kind"),
                    "bridge": bridge.get("bridge"),
                    "to_chain": bridge.get("to_chain"),
                    "attestation_status": bridge.get("attestation_status"),
                    "destination_status": bridge.get("destination_status"),
                }
                if bridge
                else None
            ),
        },
        "sdn": {
            "source": OFAC_SDN_SOURCE,
            "date": OFAC_SDN_DATE,
            "labels_total": OFAC_SDN_COUNT,
        },
        "method": METHOD,
        "limits": LIMITS,
    }
    _cache_set(cache_key, out, ttl=60)
    return out
