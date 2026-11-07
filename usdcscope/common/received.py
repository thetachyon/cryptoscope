"""Received-funds monitor (phase 2, Q4).

Post-receipt aftermath for one receipt (the Q1 tracker in
common/settlement.py): given a tx hash + chain, answer three
questions about what happened AFTER the receipt:

  1. Did the sender (counterparty) move funds onward after the
     receipt? — the sender's outgoing transfers with a timestamp
     after the receipt, newest first.
  2. Did the received funds later touch a sanctioned address? —
     every onward-transfer destination is checked directly against
     the vendored OFAC SDN set (common/risk.py — REUSED, never
     rebuilt), and the counterparty's full risk walk
     (evm_risk / bitcoin_risk / solana_risk) is included so
     proximity beyond the onward destinations is visible too.
  3. Has the counterparty been frozen since the receipt? — the
     issuer freeze/blacklist read probe for the receipt token
     (common/preflight.py — REUSED), plus, for the freezable
     stablecoins with indexed freeze events (USDC / PYUSD / FDUSD
     on Ethereum; USDT's events carry the address in `data`),
     a targeted getLogs scan from the receipt block to the head
     for freeze/unfreeze events naming the counterparty
     (common/freeze.py — REUSED event config + getLogs).

Chains (Tejas's directive: ALL coins and chains, wherever it
applies): Ethereum / Sepolia / Optimism / Gnosis (Blockscout),
Solana (public RPC), Bitcoin (mempool.space primary, Esplora
fallback). All keyless, zero new dependencies.

Honest limits (also carried in the response `notes` and rendered
on the receipt page):
- Forward-looking from receipt time only: only onward transfers
  that happened after the receipt AND that the page loads are
  visible — EVM first page (50 native txs + 50 token transfers),
  Solana latest 25 signatures (10 txs parsed), Bitcoin latest 25
  txs. A clean result is not proof of clean.
- "Onward" means the sender's later transfers. It is NOT proven
  to be the same funds: no keyless taint tracking exists, so the
  monitor reports the sender's later movements, never "the
  received 15 USDC went to X".
- The counterparty risk walk covers the counterparty's most
  recent transactions (same sampled-window limits as the risk
  walk itself), which include onward transfers after the receipt
  plus possibly older ones — the walk is not itself scoped to
  post-receipt time; only the onward list and the direct
  destination checks are.
- The freeze read probe reports the counterparty's CURRENT frozen
  state; it cannot date when a freeze happened. Only the targeted
  freeze-event scan (freezable stablecoins on Ethereum) can say
  "frozen since the receipt" — for other coins/tokens the answer
  is current-state only, and that is stated, not smoothed over.
- The issuer blacklist is not enumerable on-chain; the event scan
  only sees events from the receipt block onward, never earlier.
- Scores are a heuristic screening signal, not a legal
  determination.
"""
import asyncio
from datetime import datetime, timezone

from common.bitcoin import _api as _btc_api, is_bitcoin_address
from common.chains import CHAINS
from common.clients import UpstreamError, _cache_get, _cache_set
from common.explorer import _address_token_transfers, _address_txs
from common.freeze import (
    FREEZE_EVENT_CONFIG,
    _getlogs,
    _latest_block,
    _parse_log,
    _topic,
)
from common.preflight import FREEZE_READ_FN, _frozen_check_solana, _probe_token_frozen
from common.risk import (
    OFAC_SDN_COUNT,
    OFAC_SDN_DATE,
    OFAC_SDN_SOURCE,
    bitcoin_risk,
    evm_risk,
    lookup,
    solana_risk,
)
from common.settlement import SETTLE_CHAINS, _plain, _validate, track_settlement
from common.solana import is_solana_address
from common.solana_explorer import _signatures, sol_tx
from common.stablecoin import STABLECOINS

CACHE_TTL = 300

# Sampled-window caps (same as the risk walk / address pages).
EVM_PAGE = 50
SOL_SIGS = 25
SOL_TX_PARSE_CAP = 10
BTC_PAGE = 25
ONWARD_SHOWN = 10

HONEST_NOTES = [
    "Forward-looking from receipt time only: only onward transfers after the receipt that the page loads are visible (EVM first page: 50 native txs + 50 token transfers; Solana latest 25 signatures, 10 parsed; Bitcoin first page of txs). A clean result is not proof of clean.",
    "\"Onward\" means the sender's later transfers — it is not proven to be the same funds. No keyless taint tracking exists, so the monitor reports the sender's later movements, never that the received funds went to a specific address.",
    "The counterparty risk walk covers the counterparty's most recent transactions (same sampled-window limits as the risk walk), which include onward transfers plus possibly older ones — only the onward list and the direct destination checks are scoped to post-receipt time.",
    "The freeze read probe reports the counterparty's CURRENT frozen state and cannot date when a freeze happened. Only the targeted freeze-event scan (freezable stablecoins on Ethereum) can say \"frozen since the receipt\"; for other coins the answer is current-state only.",
    "The issuer blacklist is not enumerable on-chain; the freeze-event scan only sees events from the receipt block onward, never earlier.",
    "Scores are a heuristic screening signal, not a legal determination.",
]


def _to_unix(ts):
    """Receipt timestamp (ISO string, unix int, or None) -> unix int or None."""
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return int(ts)
        return int(datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _parse_ts(ts):
    """A Blockscout ISO timestamp -> unix int or None."""
    return _to_unix(ts)


def _addr_of(side):
    if isinstance(side, dict):
        return side.get("hash")
    return side if isinstance(side, str) else None


def _short(addr):
    a = str(addr or "")
    return f"{a[:6]}…{a[-4:]}" if len(a) > 12 else a


# ---------------------------------------------------------------- EVM onward
async def _onward_evm(chain, sender, receipt_unix):
    """Sender's outgoing native txs + token transfers after the
    receipt (first page each, newest first). Returns
    (onward_list, scanned, partial_note)."""
    sender_l = sender.lower()
    onward = {}
    scanned = 0
    note = None
    try:
        page = await _address_txs(chain, sender, {})
        items = page.get("items", []) if isinstance(page, dict) else []
    except Exception:
        items = []
        note = "Native transaction feed failed — native onward transfers not checked."
    for t in items:
        scanned += 1
        ts = _parse_ts(t.get("timestamp"))
        if ts is None or ts <= receipt_unix:
            continue
        if (_addr_of(t.get("from")) or "").lower() != sender_l:
            continue
        h = t.get("hash")
        if h and h not in onward:
            onward[h] = {
                "hash": h,
                "timestamp": ts,
                "kind": "native",
                "to": _addr_of(t.get("to")),
                "value": t.get("value_eth"),
                "symbol": CHAINS[chain]["native"],
            }
    try:
        page = await _address_token_transfers(chain, sender, {})
        tts = page.get("items", []) if isinstance(page, dict) else []
    except Exception:
        tts = []
        note = (note + " " if note else "") + "Token-transfer feed failed — token onward transfers not checked."
    for tt in tts:
        scanned += 1
        ts = _parse_ts(tt.get("timestamp"))
        if ts is None or ts <= receipt_unix:
            continue
        if (_addr_of(tt.get("from")) or "").lower() != sender_l:
            continue
        h = tt.get("tx_hash")
        tok = tt.get("token") or {}
        row = {
            "hash": h,
            "timestamp": ts,
            "kind": "token",
            "to": _addr_of(tt.get("to")),
            "value": tt.get("amount"),
            "symbol": tok.get("symbol"),
            "token_address": tok.get("address"),
        }
        if h and h in onward:
            # Same parent tx as a native row: keep both legs visible.
            onward[h + ":token"] = row
        elif h:
            onward[h] = row
    rows = sorted(onward.values(), key=lambda r: -r["timestamp"])
    return rows, scanned, note


# ---------------------------------------------------------------- Solana onward
async def _onward_solana(sender, receipt_unix, receipt_mint=None):
    """Sender's later signatures: onward when the sender signed and
    lost SOL or SPL value in the tx. Parses at most
    SOL_TX_PARSE_CAP txs (same cap family as the risk walk)."""
    sigs = await _signatures(sender, limit=SOL_SIGS)
    items = sigs.get("items", []) if isinstance(sigs, dict) else []
    scanned = len(items)
    later = [s for s in items
             if s.get("block_time") is not None and s["block_time"] > receipt_unix]
    rows = []
    note = None

    async def one(sig):
        try:
            return await sol_tx(sig)
        except Exception:
            return None

    try:
        txs = await asyncio.wait_for(
            asyncio.gather(*[one(s["signature"]) for s in later[:SOL_TX_PARSE_CAP]]),
            timeout=25.0,
        )
    except (asyncio.TimeoutError, Exception):
        txs, note = [None] * len(later[:SOL_TX_PARSE_CAP]), (
            "Solana tx parse timed out — the onward list is partial."
        )
    for s, tx in zip(later[:SOL_TX_PARSE_CAP], txs):
        if not tx:
            continue
        accounts = tx.get("accounts") or []
        mine = [a for a in accounts if a.get("address") == sender]
        if not mine:
            continue
        if not any(a.get("signer") for a in mine):
            continue
        # Net SOL out, AFTER the transaction fee: the sender's SOL
        # balance drops by the fee on every tx, so a fee-only tx
        # (no value moved) must not read as an onward transfer.
        fee_sol = tx.get("fee_sol") or 0
        sol_out = -(sum(a.get("change_sol") or 0 for a in mine)) - fee_sol
        # SPL out: negative token changes owned by the sender.
        tok_out = []
        for c in tx.get("token_changes") or []:
            if c.get("owner") == sender and (c.get("change") or 0) < 0:
                tok_out.append(c)
        if sol_out <= 0.000000001 and not tok_out:
            continue
        # Destination: largest positive SOL balance change, else the
        # largest token-change owner that is not the sender.
        dest = None
        pos = [(a.get("change_sol") or 0, a.get("address")) for a in accounts
               if (a.get("change_sol") or 0) > 0]
        if pos:
            dest = max(pos, key=lambda c: c[0])[1]
        elif tok_out:
            owners = [(abs(c.get("change") or 0), c.get("owner"))
                      for c in tx.get("token_changes") or []
                      if c.get("owner") and c.get("owner") != sender
                      and (c.get("change") or 0) > 0]
            if owners:
                dest = max(owners, key=lambda c: c[0])[1]
        if tok_out:
            c0 = tok_out[0]
            rows.append({
                "hash": s["signature"], "timestamp": s["block_time"],
                "kind": "token", "to": dest,
                "value": str(abs(c0.get("change") or 0)),
                "symbol": None, "token_address": c0.get("mint"),
            })
        else:
            rows.append({
                "hash": s["signature"], "timestamp": s["block_time"],
                "kind": "native", "to": dest,
                "value": _plain(sol_out), "symbol": "SOL",
            })
    if len(later) > SOL_TX_PARSE_CAP:
        note = (note + " " if note else "") + (
            f"Only the {SOL_TX_PARSE_CAP} most recent post-receipt "
            f"signatures were parsed ({len(later)} found)."
        )
    rows.sort(key=lambda r: -r["timestamp"])
    return rows, scanned, note


# ---------------------------------------------------------------- Bitcoin onward
async def _onward_bitcoin(sender, receipt_unix):
    """Sender's later txs (first page, newest first): onward when the
    sender is an input. Uses the raw mempool.space page (the compact
    rows carry no vin/vout addresses)."""
    try:
        d = await _btc_api(f"/address/{sender}/txs")
    except UpstreamError:
        return [], 0, "Bitcoin address feed failed — onward transfers not checked."
    if not isinstance(d, list):
        return [], 0, "Bitcoin address feed returned no transactions."
    rows = []
    for t in d:
        st = t.get("status") or {}
        ts = st.get("block_time")
        if not st.get("confirmed") or ts is None or ts <= receipt_unix:
            continue
        vins = t.get("vin") or []
        spent = sum(
            (v.get("prevout") or {}).get("value") or 0
            for v in vins
            if (v.get("prevout") or {}).get("scriptpubkey_address") == sender
        )
        if spent <= 0:
            continue
        dests = sorted({
            o.get("scriptpubkey_address")
            for o in t.get("vout") or []
            if o.get("scriptpubkey_address") and o.get("scriptpubkey_address") != sender
        })
        rows.append({
            "hash": t.get("txid"),
            "timestamp": ts,
            "kind": "bitcoin",
            "to": dests[0] if dests else None,
            "destinations": dests,
            "value": str(round(spent / 100_000_000, 8)),
            "symbol": "BTC",
        })
    rows.sort(key=lambda r: -r["timestamp"])
    return rows, len(d), None


# ---------------------------------------------------------------- freeze
def _stablecoin_scope_for(chain, contract):
    """Stablecoin scope whose primary chain is `chain` and whose
    contract matches, or None."""
    if not contract or chain != "ethereum":
        return None
    low = contract.lower()
    for scope, cfg in STABLECOINS.items():
        if cfg.get("primary") == "ethereum" and (cfg.get("contract") or "").lower() == low:
            return scope
    return None


async def _freeze_events_since(chain, token_address, sender, receipt_block):
    """Freeze/unfreeze events naming the counterparty, from the
    receipt block to the head. Only for freezable stablecoins on
    Ethereum with event config in common/freeze.py. Returns
    (events, status, note)."""
    scope = _stablecoin_scope_for(chain, token_address)
    specs = FREEZE_EVENT_CONFIG.get(scope, []) if scope else []
    holder_specs = [s for s in specs if s.get("state") is not None]
    if not holder_specs:
        return [], "not_applicable", (
            "No targeted freeze-event scan: the receipt token has no "
            "indexed freeze/blacklist events to scan (not a freezable "
            "stablecoin on Ethereum, or its events carry no holder "
            "address to filter on)."
        )
    try:
        head = await _latest_block()
    except UpstreamError:
        return [], "unavailable", "Latest block unavailable — freeze-event scan skipped."
    events = []
    statuses = []
    for spec in holder_specs:
        logs, st, _note = await _getlogs(
            token_address, _topic(spec["sig"]), receipt_block, head
        )
        statuses.append(st)
        for log in logs:
            ev = _parse_log(log, spec)
            if ev is not None and (ev.get("address") or "").lower() == sender.lower():
                events.append({
                    "timestamp": ev["timestamp"],
                    "kind": ev["kind"],
                    "event": ev["event"],
                    "tx_hash": ev["tx_hash"],
                    "block": ev["block"],
                })
    events.sort(key=lambda e: (e["timestamp"] or 0, e["block"] or 0))
    status = "partial" if "partial" in statuses or "unavailable" in statuses else "ok"
    note = None
    if status == "partial":
        note = "The freeze-event scan stopped early upstream — events in the window may be incomplete."
    return events, status, note


async def _frozen_state(chain, kind, counterparty, amount):
    """Current frozen state of the counterparty for the receipt
    token (+ USDC/USDT majors on Ethereum), the targeted
    freeze-event scan since the receipt, and the Solana SPL check.
    Returns the frozen block."""
    token_address = (amount or {}).get("token_address")
    symbol = (amount or {}).get("symbol")
    if kind == "bitcoin":
        return {
            "status": "not_applicable",
            "summary": "Bitcoin has no issuer freeze: no one can freeze a Bitcoin holder's balance.",
            "current": [],
            "events_since_receipt": [],
            "events_status": "not_applicable",
            "events_note": None,
        }
    if kind == "solana":
        if not token_address:
            return {
                "status": "not_applicable",
                "summary": "Native SOL cannot be frozen by an issuer. A token receipt carries its mint, which is checked instead.",
                "current": [],
                "events_since_receipt": [],
                "events_status": "not_applicable",
                "events_note": None,
            }
        chk = await _frozen_check_solana(
            counterparty, {"kind": "spl", "mint": token_address}
        )
        frozen = chk.get("status") == "block"
        return {
            "status": "frozen" if frozen else ("unknown" if chk.get("status") == "unknown" else "not_frozen"),
            "summary": chk.get("detail") or "",
            "current": [{
                "symbol": symbol or token_address[:8],
                "contract": token_address,
                "frozen": frozen if chk.get("status") in ("block", "pass") else None,
                "note": chk.get("detail") or "",
            }],
            "events_since_receipt": [],
            "events_status": "not_applicable",
            "events_note": "Solana has no freeze-event scan in this monitor — the check above is the token account's current state only.",
            "freeze_authority": chk.get("freeze_authority"),
        }
    # EVM.
    probes = []
    if token_address:
        probes.append({"symbol": symbol or token_address[:10],
                       "contract": token_address,
                       "read_fn": "probe_all", "scope": None})
    if chain == "ethereum":
        for scope in ("usdcscope", "usdtscope"):
            cfg = STABLECOINS[scope]
            if not token_address or cfg["contract"].lower() != token_address.lower():
                probes.append({"symbol": cfg["symbol"], "contract": cfg["contract"],
                               "read_fn": FREEZE_READ_FN[scope], "scope": scope})
    current = []
    any_frozen = False
    any_unknown = False
    for token in probes:
        frozen, note = await _probe_token_frozen(chain, token, counterparty)
        current.append({"symbol": token["symbol"], "contract": token["contract"],
                        "frozen": frozen, "note": note})
        if frozen is True:
            any_frozen = True
        elif frozen is None:
            any_unknown = True
    return {
        "status": "frozen" if any_frozen else ("unknown" if any_unknown else "not_frozen"),
        "summary": (
            "Counterparty is currently FROZEN/blacklisted for "
            + ", ".join(c["symbol"] for c in current if c["frozen"] is True)
            + "."
            if any_frozen else
            "Counterparty is not currently frozen/blacklisted for any token probed."
            + (" Some tokens could not be verified — see the per-token notes." if any_unknown else "")
        ),
        "current": current,
        # Event scan is filled in by the caller (needs receipt block).
        "events_since_receipt": [],
        "events_status": "pending",
        "events_note": None,
    }


# ---------------------------------------------------------------- entry
async def monitor_aftermath(chain, h):
    """GET /api/walletscope/aftermath?chain=&hash= — Q4 received-funds
    monitor. Raises UpstreamError(400/404) for bad input or a receipt
    that cannot be loaded. Cached 300s."""
    h = _validate(chain, h)
    key = f"aftermath:{chain}:{h}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await track_settlement(chain, h)
    receipt_unix = _to_unix(d.get("timestamp"))
    counterparty = d.get("from")
    # For a token transfer the tracker's `to` is the token contract;
    # the real recipient is the token transfer's `to`.
    recipient = d.get("to")
    if (d.get("amount") or {}).get("kind") == "token" and d.get("token_transfers"):
        recipient = d["token_transfers"][0].get("to") or recipient
    if receipt_unix is None or not counterparty:
        out = {
            "chain": d["chain"], "chain_label": d["chain_label"],
            "hash": d["hash"], "status": "unavailable",
            "summary": (
                "Aftermath cannot be checked: the receipt has no "
                "timestamp or no sender on-chain yet (unconfirmed or "
                "unindexed). Check again once it is confirmed."
            ),
            "receipt": {"timestamp": d.get("timestamp"), "from": counterparty,
                        "to": recipient, "amount": d.get("amount"),
                        "block": d.get("block")},
            "onward_transfers": [], "onward_count": 0,
            "sanctioned_destinations": [],
            "counterparty_risk": None,
            "frozen": None,
            "notes": HONEST_NOTES,
        }
        _cache_set(key, out, ttl=60)
        return out

    kind = d["kind"]
    # 1. Onward transfers after the receipt.
    if kind == "evm":
        onward, scanned, onward_note = await _onward_evm(chain, counterparty, receipt_unix)
        window_note = f"EVM: first page scanned ({scanned} txs/token-transfers checked, newest first)."
    elif kind == "solana":
        onward, scanned, onward_note = await _onward_solana(
            counterparty, receipt_unix,
            receipt_mint=(d.get("amount") or {}).get("token_address"))
        window_note = f"Solana: latest {scanned} signatures scanned, up to {SOL_TX_PARSE_CAP} parsed."
    else:
        onward, scanned, onward_note = await _onward_bitcoin(counterparty, receipt_unix)
        window_note = f"Bitcoin: latest {scanned} txs scanned (one page)."
    # 2. Direct SDN check on every onward destination (+ the
    # counterparty itself), then the full risk walk on the counterparty.
    dests = set()
    for r in onward:
        for a in (r.get("destinations") or [r.get("to")]):
            if a:
                dests.add(a)
    sanctioned = []
    for a in sorted(dests):
        e = lookup(a)
        if e is not None:
            sanctioned.append({"address": a, "name": e["name"],
                               "program": e["program"], "currency": e["currency"]})
    direct_cp = lookup(counterparty)
    if direct_cp is not None:
        sanctioned.append({"address": counterparty, "name": direct_cp["name"],
                           "program": direct_cp["program"],
                           "currency": direct_cp["currency"],
                           "context": "counterparty itself is on the OFAC SDN list"})
    try:
        if kind == "evm":
            cp_risk = await evm_risk(chain, counterparty)
        elif kind == "solana":
            cp_risk = await solana_risk(counterparty)
        else:
            cp_risk = await bitcoin_risk(counterparty)
    except UpstreamError as e:
        cp_risk = {"error": e.msg, "score": None, "level": "unknown",
                   "reasons": [f"Counterparty risk walk failed: {e.msg}"]}
    except Exception as e:
        cp_risk = {"error": str(e), "score": None, "level": "unknown",
                   "reasons": [f"Counterparty risk walk failed: {e}"]}
    # 3. Frozen state (current probe + targeted event scan since receipt).
    frozen = await _frozen_state(chain, kind, counterparty, d.get("amount"))
    if kind == "evm" and d.get("block") is not None:
        token_address = (d.get("amount") or {}).get("token_address")
        events, ev_status, ev_note = await _freeze_events_since(
            chain, token_address, counterparty, d["block"]
        )
        frozen["events_since_receipt"] = events
        frozen["events_status"] = ev_status
        frozen["events_note"] = ev_note
        if events and any(e["kind"] in ("blacklisted", "frozen", "blacklist_set")
                          for e in events):
            frozen["status"] = "frozen"
            frozen["summary"] = (
                "Counterparty was frozen/blacklisted by the token "
                "issuer in the window since the receipt — see the "
                "freeze events below."
            )
    elif kind == "evm":
        frozen["events_status"] = "unavailable"
        frozen["events_note"] = "Receipt block unknown — freeze-event scan skipped."
    frozen_since = any(
        e["kind"] in ("blacklisted", "frozen", "blacklist_set")
        for e in (frozen.get("events_since_receipt") or [])
    )
    if sanctioned:
        summary = (
            f"ATTENTION: {len(sanctioned)} sanctioned address(es) touch "
            f"this receipt's aftermath — {len(onward)} onward transfer(s) "
            "by the sender after the receipt were found in the sampled window."
        )
    elif frozen.get("status") == "frozen":
        summary = (
            "ATTENTION: the counterparty is frozen/blacklisted "
            + ("(freeze event since the receipt) " if frozen_since else "(current state) ")
            + f"— {len(onward)} onward transfer(s) by the sender after the receipt were found in the sampled window."
        )
    elif onward:
        summary = (
            f"The sender moved funds onward after the receipt: "
            f"{len(onward)} transfer(s) found in the sampled window, "
            "none to a sanctioned address, counterparty not frozen."
        )
    else:
        summary = (
            "No onward transfer by the sender after the receipt was "
            "found in the sampled window, no sanctioned touch, "
            "counterparty not frozen — a clean result is not proof of "
            "clean (see the limits below)."
        )
    out = {
        "chain": d["chain"],
        "chain_label": d["chain_label"],
        "hash": d["hash"],
        "status": ("attention" if (sanctioned or frozen.get("status") == "frozen")
                   else ("moved_on" if onward else "no_onward_seen")),
        "summary": summary,
        "receipt": {
            "timestamp": d.get("timestamp"),
            "receipt_unix": receipt_unix,
            "from": counterparty,
            "to": recipient,
            "amount": d.get("amount"),
            "block": d.get("block"),
            "explorer_url": d.get("explorer_url"),
        },
        "onward_transfers": onward[:ONWARD_SHOWN],
        "onward_count": len(onward),
        "onward_shown": min(len(onward), ONWARD_SHOWN),
        "onward_window": window_note + (f" {onward_note}" if onward_note else ""),
        "sanctioned_destinations": sanctioned,
        "counterparty_risk": cp_risk,
        "frozen": frozen,
        "sdn": {
            "source": OFAC_SDN_SOURCE,
            "date": OFAC_SDN_DATE,
            "labels_total": OFAC_SDN_COUNT,
        },
        "notes": HONEST_NOTES,
    }
    _cache_set(key, out, ttl=CACHE_TTL)
    return out
