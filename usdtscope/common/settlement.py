"""Settlement tracker & shareable receipt (phase 2, Q1).

One transaction, one timeline: submitted -> confirmed (with
confirmation count) -> economically final -> bridged leg minted on
the destination (where applicable). A receipt page (`#/receipt/…`)
renders the same payload as a shareable, JSON-exportable receipt.

Chains (Tejas's directive: ALL coins and chains, wherever it
applies): Ethereum / Sepolia / Optimism / Gnosis (Blockscout),
Solana (public RPC), Bitcoin (mempool.space primary, Esplora
fallback). All keyless, zero new dependencies.

Sources (all probed live from this sandbox 2026-11-06):
- EVM tx detail: Blockscout v2 `/transactions/{hash}` (via
  common.explorer._tx) — confirmations, block, fee, token transfers,
  historic native price at execution (`historic_exchange_rate`).
  A hash Blockscout does not know is checked against the public RPC
  `eth_getTransactionByHash`: a result with no block number is a
  submitted-but-unmined (pending) transaction; no result at all is
  "not found". Nothing is ever reported confirmed without a block.
- Solana: `getTransaction` (via common.solana_explorer.sol_tx) +
  live head slot for slot depth. Solana has no confirmation count,
  so the timeline reports slot depth; "economically final" is slot
  depth >= 32 (~13s of slots), labelled as a convention.
- Bitcoin: mempool.space / Esplora (via common.bitcoin.btc_tx) —
  confirmations from the tip height. "Economically final" is the
  standard 6-confirmation convention, labelled as such.
- Amount / USD at execution: the native price Blockscout reports
  for the execution block wins for EVM; otherwise the CoinGecko
  daily close for the transaction's UTC date (token contracts via
  _cg_contract_daily_prices, native coins via _daily_native_prices,
  BTC via market_chart). When no execution-day price exists the
  receipt falls back to the live price and says so
  (price_basis "estimate-current"); when no price exists at all the
  USD fields are null, never invented.
- CCTP (USDC burn-and-mint): detected from the MessageTransmitter
  `MessageSent` log (topic0 0x8c526166… verified live on a real
  2026-11-06 depositForBurn). The message bytes are decoded from
  the log, the message hash is keccak256(message) (common.abi),
  and the attestation is read from Circle's public/keyless
  iris-api (`GET https://iris-api.circle.com/v1/attestations/{hash}`,
  probed live 2026-11-06: 200 + {"status":"complete"} for a real
  attested burn, 404 "Message hash not found" for a bogus hash).
  A 404 from iris-api means "attestation not yet published (or the
  message hash is not recognised by Circle)" — never "complete".
- Wormhole: Wormholescan keyless `/api/v1/transactions` (probed live
  2026-11-06; the path form /transactions/{hash} 404s — do not use
  it, and the `?txHash=` parameter is effectively ignored, so every
  candidate is exact-matched client-side on its own origin hash).
  Second attempt: `?address={sender}` (sender read from the tx
  itself), up to 3 pages. Mayan Swift transfers standardize no
  sender, so a Mayan transfer from a very active sender can still
  be missed — reported as "no bridge transfer detected", never a
  borrowed leg (a borrowed leg from an unrelated transfer was
  caught live during verification and fixed).

Honest limits (also carried in the response `notes` and rendered
under the timeline):
- "Economically final" is a convention (EVM 64 / 12 on Sepolia,
  Solana 32 slots, Bitcoin 6 confirmations), not protocol
  finality — a reorg deeper than the threshold is still possible,
  just increasingly unlikely.
- A CCTP attestation of "complete" means the mint is claimable; the
  actual mint transaction on the destination chain is NOT linked —
  no keyless source maps a Circle message hash to the destination
  mint tx. The receipt says so instead of guessing a hash.
- Bitcoin amounts are the largest single output; change outputs
  cannot be identified from chain data alone, so the receipt also
  reports total moved and never claims the largest output is the
  payment.
- Solana recipients are the account with the largest positive
  balance change; for a token transfer that is the token owner,
  for a SOL transfer the SOL account. The fee payer's SOL change
  includes the fee.
"""
import asyncio
import re
from datetime import datetime, timezone

import httpx

from common.abi import keccak_256
from common.bitcoin import _is_hex64, btc_tx
from common.chains import CHAINS, WALLET_CHAINS
from common.clients import (
    HEADERS,
    TIMEOUT,
    UpstreamError,
    _cache_get,
    _cache_set,
    coingecko,
    rpc,
)
from common.crosschain import _fmt_amount, _wh_chain
from common.explorer import _chain_head, _tx, _tx_logs
from common.formatting import age, token_amount, wei_to_eth
from common.pricing import (
    _cg_contract_daily_prices,
    _daily_native_prices,
    _daily_series,
    cg_token_price,
    native_quote,
)
from common.solana import sol_native_quote
from common.solana_explorer import _is_sig, _rpc as _sol_rpc, sol_tx

# Chains the tracker serves: every wallet chain plus Bitcoin (which
# WALLET_CHAINS does not carry — the wallet dashboard is EVM+Solana,
# the tracker is not).
SETTLE_CHAINS = {**WALLET_CHAINS, "bitcoin": {
    "label": "Bitcoin",
    "blockscout": None,
    "rpc": None,
    "coingecko_platform": "bitcoin",
    "coingecko_coin": "bitcoin",
    "native": "BTC",
}}

# Confirmations at which a tx is called "economically final" — a
# convention, not protocol finality (see module docstring).
FINALITY_CONFIRMATIONS = {
    "ethereum": 64,
    "sepolia": 12,
    "optimism": 64,
    "gnosis": 32,
}
BTC_FINALITY_CONFIRMATIONS = 6
SOL_FINALITY_SLOTS = 32

# Blockscout UI base per chain (API base minus /api/v2), for receipt
# explorer links.
_EXPLORER_UI = {
    c: CHAINS[c]["blockscout"].removesuffix("/api/v2") for c in CHAINS
}

# Circle CCTP ------------------------------------------------------------
IRIS_API = "https://iris-api.circle.com/v1/attestations"
# MessageSent(bytes message) on the MessageTransmitter — verified live
# 2026-11-06 on eth tx 0x805869ec… (depositForBurn, dest domain 5).
MESSAGE_SENT_TOPIC = "0x8c5261668696ce22758910d05bab8f186d6eb247ceac2af2e82c7dc17669b036"
# DepositForBurn on the TokenMessenger — corroborating signal only.
DEPOSIT_FOR_BURN_TOPIC = "0x2fa9ca894982930190727e75500a97d8dc500233a5065e0f3126c48fbe0343c0"
# Circle CCTP domain ids -> chain label (only chains this repo serves
# get a name; anything else renders as "CCTP domain N").
CCTP_DOMAIN_CHAINS = {
    0: "ethereum",
    1: "avalanche",
    2: "optimism",
    3: "arbitrum",
    5: "solana",
    6: "base",
    7: "polygon",
}

# Wormhole ---------------------------------------------------------------
WORMHOLE_TX_URL = "https://api.wormholescan.io/api/v1/transactions"

_EVM_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

HONEST_NOTES = [
    "\"Economically final\" is a convention (Ethereum/Optimism/Gnosis 64 confirmations, Sepolia 12, Solana 32 slots, Bitcoin 6 confirmations), not protocol finality — a deeper reorg is still possible, just increasingly unlikely.",
    "A CCTP attestation of \"complete\" means the mint is claimable; the actual mint transaction on the destination chain is not linked — no keyless source maps a Circle message hash to the destination mint tx.",
    "Bitcoin amounts are the largest single output; change outputs cannot be identified from chain data alone. Total moved is reported alongside.",
    "USD values at execution use the execution-day price where available; when it is not, the live price is used and labelled estimate-current, or the USD fields are null — never invented.",
]


# ---------------------------------------------------------------- helpers
def _days_since(iso_or_unix):
    """Days between a tx timestamp (ISO string or unix int) and today,
    +2 days of headroom for the daily-price lookup. None when the
    timestamp is unusable. Capped at 365 (CoinGecko daily history)."""
    try:
        if isinstance(iso_or_unix, (int, float)):
            dt = datetime.fromtimestamp(int(iso_or_unix), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(iso_or_unix).replace("Z", "+00:00"))
        days = (datetime.now(timezone.utc).date() - dt.date()).days + 2
        return max(2, min(365, days))
    except Exception:
        return None


def _price_on_day(series, iso_or_unix):
    """Daily-close price for the tx's UTC date from a {YYYY-MM-DD:
    price} series, or None."""
    if not series or not iso_or_unix:
        return None
    try:
        if isinstance(iso_or_unix, (int, float)):
            day = datetime.fromtimestamp(int(iso_or_unix), tz=timezone.utc).date().isoformat()
        else:
            day = datetime.fromisoformat(str(iso_or_unix).replace("Z", "+00:00")).date().isoformat()
        return series.get(day)
    except Exception:
        return None


async def _native_price_at(chain, iso_or_unix):
    """(usd_price, basis) for a chain's native coin at execution time.
    Daily close on the tx date wins; live price is the estimate
    fallback; (None, "unavailable") when neither answers."""
    days = _days_since(iso_or_unix)
    if days is not None:
        try:
            series = await _daily_native_prices(chain, days)
            p = _price_on_day(series, iso_or_unix)
            if p is not None:
                return float(p), "at-execution"
        except Exception:
            pass
    try:
        price, _ = await native_quote(chain)
        if price is not None:
            return float(price), "estimate-current"
    except Exception:
        pass
    return None, "unavailable"


async def _btc_price_at(block_time):
    """(usd_price, basis) for BTC at execution time, same ladder as
    _native_price_at (mempool.space has no per-tx price; CoinGecko
    daily close is the source)."""
    days = _days_since(block_time)
    if days is not None:
        d = await coingecko(
            "/coins/bitcoin/market_chart",
            params={"vs_currency": "usd", "days": days},
        )
        p = _price_on_day(
            _daily_series(d.get("prices", []) if isinstance(d, dict) else []),
            block_time,
        )
        if p is not None:
            return float(p), "at-execution"
    from common.bitcoin import btc_price

    price, _ = await btc_price()
    if price is not None:
        return float(price), "estimate-current"
    return None, "unavailable"


async def _token_price_at(chain, token_address, iso_or_unix):
    """(usd_price, basis) for one ERC-20 at execution time: CoinGecko
    daily close on the tx date, else the live token price as an
    estimate, else (None, "unavailable")."""
    platform = CHAINS[chain]["coingecko_platform"]
    days = _days_since(iso_or_unix)
    if days is not None:
        try:
            series = await _cg_contract_daily_prices(platform, token_address, days)
            p = _price_on_day(series, iso_or_unix)
            if p is not None:
                return float(p), "at-execution"
        except Exception:
            pass
    try:
        p = await cg_token_price(platform, token_address)
        if p is not None:
            return float(p), "estimate-current"
    except Exception:
        pass
    return None, "unavailable"


def _usd(amount, price):
    """amount (str/float) x price -> rounded USD float, or None."""
    if amount is None or price is None:
        return None
    try:
        return round(float(amount) * float(price), 2)
    except (TypeError, ValueError):
        return None


def _timeline(confirmed, confirmations, final_threshold, bridge=None):
    """Timeline steps. `confirmed` False means submitted-but-unmined.
    `bridge` is the bridge-leg dict (or None) appended as a step."""
    steps = [
        {
            "id": "submitted",
            "label": "Submitted",
            "status": "done",
            "detail": "Seen by the network" if confirmed else "In the mempool — not yet mined",
        },
        {
            "id": "confirmed",
            "label": "Confirmed",
            "status": "done" if confirmed else "pending",
            "detail": (
                f"{confirmations} confirmation(s)"
                if confirmed and confirmations is not None
                else ("Confirmed in a block" if confirmed else "Waiting for a block")
            ),
        },
        {
            "id": "final",
            "label": "Economically final",
            "status": (
                "done"
                if confirmed and confirmations is not None and confirmations >= final_threshold
                else ("current" if confirmed else "pending")
            ),
            "detail": f"Threshold: {final_threshold} confirmation(s) — see notes",
        },
    ]
    if bridge is not None:
        b = bridge
        dest_done = (b.get("destination_status") or "").lower() in ("completed", "complete", "success")
        if b.get("kind") == "cctp":
            att = (b.get("attestation_status") or "").lower()
            status = "done" if att == "complete" else ("current" if att == "pending" else "pending")
            detail = f"Circle attestation: {b.get('attestation_status') or 'unknown'}"
            label = f"Bridged leg minted ({b.get('to_chain') or 'destination'})"
        elif b.get("destination_status") is None and not b.get("to_chain"):
            # A generic Wormhole message (not a token transfer):
            # Wormholescan tracks no destination mint for it, so the
            # step is the published message itself — never claim a
            # mint is pending when none is tracked.
            status = "current"
            detail = "Wormholescan reports no destination transaction for this message"
            label = "Bridge message published (no token mint tracked)"
        else:
            status = "done" if dest_done else "current"
            detail = f"Destination tx: {b.get('destination_status') or 'not yet minted'}"
            label = f"Bridged leg minted ({b.get('to_chain') or 'destination'})"
        steps.append(
            {
                "id": "bridged",
                "label": label,
                "status": status,
                "detail": detail,
            }
        )
    return steps


# ---------------------------------------------------------------- CCTP
def _decode_cctp_message(data_hex):
    """ABI-encoded MessageSent log data -> (message bytes, dest domain,
    nonce, recipient hex) or None. The message is a `bytes` arg: 32-byte
    offset, 32-byte length, then the message. Header: version(4) +
    sourceDomain(4) + destDomain(4) + nonce(8) + sender(32) +
    recipient(32) + destinationCaller(32)."""
    try:
        raw = bytes.fromhex((data_hex or "0x")[2:])
        if len(raw) < 64:
            return None
        ln = int.from_bytes(raw[32:64], "big")
        msg = raw[64:64 + ln]
        if len(msg) < 84 or ln == 0:
            return None
        dest_domain = int.from_bytes(msg[8:12], "big")
        nonce = int.from_bytes(msg[12:20], "big")
        recipient = "0x" + msg[52:84].hex()
        return msg, dest_domain, nonce, recipient
    except Exception:
        return None


async def _cctp_leg(chain, tx_hash):
    """CCTP bridge leg for an EVM tx, or None when the tx is not a
    CCTP burn. Detection: a MessageSent log on any contract (the
    MessageTransmitter emits it for every burn). The attestation is
    read live from Circle iris-api; a 404 there is "not yet
    published", never "complete". Never raises."""
    try:
        logs = await _tx_logs(chain, tx_hash)
    except Exception:
        return None
    if not isinstance(logs, dict):
        return None
    decoded = None
    has_burn = False
    for lg in logs.get("items") or []:
        topics = lg.get("topics") or []
        if not topics:
            continue
        if topics[0].lower() == MESSAGE_SENT_TOPIC.lower():
            decoded = _decode_cctp_message(lg.get("data"))
            if decoded is not None:
                break
        if topics[0].lower() == DEPOSIT_FOR_BURN_TOPIC.lower():
            has_burn = True
    if decoded is None:
        return None
    msg, dest_domain, nonce, recipient = decoded
    msg_hash = "0x" + keccak_256(msg).hex()
    dest_chain = CCTP_DOMAIN_CHAINS.get(dest_domain, f"CCTP domain {dest_domain}")
    leg = {
        "kind": "cctp",
        "bridge": "Circle CCTP (native burn-and-mint)",
        "from_chain": chain,
        "to_chain": dest_chain,
        "destination_domain": dest_domain,
        "nonce": nonce,
        "recipient": recipient,
        "message_hash": msg_hash,
        "attestation_status": "unverifiable",
        "attestation": None,
        "destination_tx_hash": None,
        "note": (
            "Attestation read live from Circle iris-api. A \"complete\" "
            "attestation means the mint is claimable; the destination "
            "mint transaction is not linked — no keyless source maps a "
            "Circle message hash to it."
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            r = await c.get(IRIS_API + "/" + msg_hash)
        if r.status_code == 200:
            d = r.json()
            leg["attestation_status"] = (d.get("status") or "unknown").lower()
            leg["attestation"] = d.get("attestation")
        elif r.status_code == 404:
            leg["attestation_status"] = "pending"
            leg["note"] += " Circle reports no attestation yet for this message hash."
        else:
            leg["attestation_status"] = "unverifiable"
            leg["note"] += f" Circle iris-api returned HTTP {r.status_code}."
    except Exception as e:
        leg["attestation_status"] = "unverifiable"
        leg["note"] += f" Circle iris-api unreachable ({type(e).__name__})."
    return leg


# ---------------------------------------------------------------- Wormhole
async def _wormhole_leg(tx_hash, sender=None):
    """Wormhole bridge leg for an EVM hash or Solana signature, or None
    when Wormholescan knows no transfer for it. Never raises.

    Lookup is two attempts, both exact-matched client-side:
    1. `?txHash=` — probed 2026-11-06: the parameter is effectively
       ignored (it returns recent transfers regardless), so a hit only
       counts when the returned transfer's own origin hash equals the
       queried hash.
    2. `?address={sender}` (sender from the tx itself), up to 3 pages
       — Wormholescan indexes transfers by standardized from/to
       address, so user-originated transfers are found this way.
       Probed 2026-11-06: the `page` parameter appears ignored
       (pages 1–4 return the same 50 newest transfers), so the sender
       lookup effectively covers only the sender's 50 most recent
       transfers. Mayan Swift transfers standardize no sender, so a
       Mayan transfer from a very active sender can still be missed;
       that is reported as "no bridge transfer detected", never as a
       borrowed leg (a borrowed leg from an unrelated transfer was
       caught live during verification and fixed).
    """
    low = tx_hash.lower()
    async def _get(params):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
                r = await c.get(WORMHOLE_TX_URL, params=params)
            if r.status_code != 200:
                return []
            return (r.json() or {}).get("transactions") or []
        except Exception:
            return []

    def _match(txs):
        for cand in txs:
            gtx_c = cand.get("globalTx") or {}
            origin_c = gtx_c.get("originTx") or {}
            for h in (cand.get("txHash"), origin_c.get("txHash")):
                if h and str(h).lower() == low:
                    return cand
        return None

    t = _match(await _get({"txHash": tx_hash}))
    if t is None and sender:
        for page in range(3):
            txs = await _get({"address": sender, "page": page, "size": 50})
            if not txs:
                break
            t = _match(txs)
            if t is not None:
                break
            if len(txs) < 50:
                break
    if t is None:
        return None
    sp = t.get("standardizedProperties") or {}
    gtx = t.get("globalTx") or {}
    dest = gtx.get("destinationTx") or {}
    origin = gtx.get("originTx") or {}
    # toChain 0 / absent = a generic message, not a token transfer:
    # no destination chain to name.
    to_chain_id = sp.get("toChain")
    return {
        "kind": "wormhole",
        "bridge": "Wormhole",
        "from_chain": _wh_chain(sp.get("fromChain") or t.get("emitterChain")),
        "to_chain": _wh_chain(to_chain_id) if to_chain_id else None,
        "amount": _fmt_amount(sp.get("amount"), sp.get("normalizedDecimals")),
        "token_address": sp.get("tokenAddress") or None,
        "origin_tx_hash": origin.get("txHash") or t.get("txHash"),
        "destination_tx_hash": dest.get("txHash"),
        "destination_status": dest.get("status"),
        "timestamp": t.get("timestamp"),
        "explorer_url": f"https://wormholescan.io/#/tx/{t.get('id')}" if t.get("id") else None,
        "note": "Wormhole only — no all-bridge total exists keyless.",
    }


async def _bridge_leg(chain, tx_hash, kind, sender=None):
    """Bridge leg for a tx, CCTP first (EVM only), then Wormhole.
    Returns None when the tx is not a bridge transfer."""
    if kind == "evm":
        leg = await _cctp_leg(chain, tx_hash)
        if leg is not None:
            return leg
    if kind in ("evm", "solana"):
        return await _wormhole_leg(tx_hash, sender=sender)
    return None


def _plain(v, decimals=9):
    """Float -> plain decimal string (no scientific notation), trimmed
    to `decimals` places. For SOL (9dp) / BTC (8dp) display values."""
    if v is None:
        return None
    try:
        from decimal import Decimal

        d = Decimal(str(v)).quantize(Decimal(1).scaleb(-decimals))
        return format(d.normalize(), "f")
    except Exception:
        return str(v)


# ---------------------------------------------------------------- EVM
async def _track_evm(chain, h):
    t = await _tx(chain, h)
    pending_rpc = None
    if not isinstance(t, dict):
        # Blockscout does not know it: pending, or not found at all.
        try:
            pending_rpc = await rpc(chain, "eth_getTransactionByHash", [h])
        except Exception:
            pending_rpc = None
        if not isinstance(pending_rpc, dict) or not pending_rpc.get("hash"):
            raise UpstreamError("Transaction not found", status=404)
        if pending_rpc.get("blockNumber") is not None:
            # Mined but Blockscout has not indexed it yet — report as
            # submitted, not confirmed (no block data to verify).
            raise UpstreamError(
                "Transaction is mined but not yet indexed by Blockscout — try again shortly",
                status=404,
            )
    if isinstance(t, dict):
        block = t.get("block")
        ts = t.get("timestamp")
        confirmations = t.get("confirmations")
        if confirmations is None and block is not None:
            head = await _chain_head(chain)
            if head is not None:
                confirmations = head - int(block) + 1
        value_eth = t.get("value_eth") or "0"
        fee_eth = t.get("fee_eth") or "0"
        from_addr = (t.get("from") or {}).get("hash")
        to_addr = (t.get("to") or {}).get("hash")
        status = t.get("status")
        transfers = t.get("token_transfers") or []
        # Native price at execution: Blockscout's historic rate wins.
        hist = t.get("historic_eth_price_usd")
        if hist is not None:
            try:
                native_price, price_basis = float(hist), "at-execution"
            except (TypeError, ValueError):
                native_price, price_basis = await _native_price_at(chain, ts)
        else:
            native_price, price_basis = await _native_price_at(chain, ts)
    else:
        # Pending (RPC-only) tx.
        block = None
        ts = None
        confirmations = 0
        value_eth = wei_to_eth(pending_rpc.get("value"))
        fee_eth = None
        from_addr = pending_rpc.get("from")
        to_addr = pending_rpc.get("to")
        status = "pending"
        transfers = []
        native_price, price_basis = None, "unavailable"

    # Primary amount: largest token transfer by USD when priced, else
    # the first transfer; else the native value.
    amount = {"value": value_eth, "symbol": CHAINS[chain]["native"],
              "kind": "native", "token_address": None,
              "usd_value": _usd(value_eth, native_price),
              "usd_price": native_price, "price_basis": price_basis}
    priced = []
    if transfers:
        priced = await asyncio.gather(
            *[_token_price_at(chain, (tt.get("token") or {}).get("address"), ts)
              for tt in transfers]
        )
        best = None
        best_usd = None
        for tt, (p, basis) in zip(transfers, priced):
            usd = _usd(tt.get("amount"), p)
            if best is None or (usd is not None and (best_usd is None or usd > best_usd)):
                best, best_usd = (tt, p, basis, usd), usd
        tt, p, basis, usd = best
        tok = tt.get("token") or {}
        amount = {"value": tt.get("amount"), "symbol": tok.get("symbol"),
                  "kind": "token", "token_address": tok.get("address"),
                  "usd_value": usd, "usd_price": p, "price_basis": basis}
    fee_usd = _usd(fee_eth, native_price) if fee_eth is not None else None
    confirmed = block is not None
    threshold = FINALITY_CONFIRMATIONS.get(chain, 64)
    bridge = await _bridge_leg(chain, h, "evm", sender=from_addr) if confirmed else None
    return {
        "chain": chain,
        "chain_label": CHAINS[chain]["label"],
        "kind": "evm",
        "hash": h,
        "status": ("success" if status in ("ok", "success") else status) or ("pending" if not confirmed else "unknown"),
        "block": block,
        "timestamp": ts,
        "age": age(ts) if ts else "",
        "confirmations": confirmations if confirmed else 0,
        "finality_threshold": threshold,
        "from": from_addr,
        "to": to_addr,
        "amount": amount,
        "token_transfers": [
            {
                "token": (tt.get("token") or {}).get("symbol"),
                "token_address": (tt.get("token") or {}).get("address"),
                "amount": tt.get("amount"),
                "from": (tt.get("from") or {}).get("hash"),
                "to": (tt.get("to") or {}).get("hash"),
            }
            for tt in transfers
        ],
        "fee": {"value": fee_eth, "symbol": CHAINS[chain]["native"],
                "usd_value": fee_usd, "usd_price": native_price,
                "price_basis": price_basis if fee_eth is not None else "unavailable"},
        "explorer_url": f"{_EXPLORER_UI[chain]}/tx/{h}",
        "timeline": _timeline(confirmed, confirmations, threshold, bridge),
        "bridge": bridge,
        "notes": HONEST_NOTES,
    }


# ---------------------------------------------------------------- Solana
async def _track_solana(sig):
    t = await sol_tx(sig)
    slot = t.get("slot")
    block_time = t.get("block_time")
    try:
        head_slot = await _sol_rpc("getSlot", fast_first=True)
        slot_depth = (int(head_slot) - int(slot) + 1) if slot is not None else None
    except Exception:
        slot_depth = None
    confirmed = slot is not None and t.get("status") != "failed"
    fee_sol = t.get("fee_sol")
    # Sender: first signer. Recipient / amount: the largest POSITIVE
    # SOL balance change (what an account received). Using the largest
    # absolute change would report the fee payer's small negative
    # change (amount + fee) as the "amount" on token/bridge txs where
    # no SOL is received — probed live 2026-11-06 on a Wormhole tx
    # whose only SOL movement was the ~0.000008 fee.
    accounts = t.get("accounts") or []
    sender = next((a.get("address") for a in accounts if a.get("signer")), None)
    changes = [(a.get("change_sol") or 0, a.get("address")) for a in accounts]
    positive = [(c, a) for c, a in changes if c > 0]
    recipient = None
    largest_received = None
    if positive:
        largest_received = max(positive, key=lambda c: c[0])
        recipient = largest_received[1]
    token_changes = t.get("token_changes") or []
    price, basis = await _native_price_at("solana", block_time)
    if token_changes:
        best = max(token_changes, key=lambda c: abs(c.get("change") or 0))
        # Token USD at execution needs the mint's daily price — the
        # token-changes payload carries no mint price, so price it live
        # only when it is a well-known mint via DexScreener is out of
        # scope here: report the amount with usd null and say so.
        amount = {"value": str(best.get("change")), "symbol": None,
                  "kind": "token", "token_address": best.get("mint"),
                  "usd_value": None, "usd_price": None,
                  "price_basis": "unavailable",
                  "note": "Token USD value not priced — no keyless execution-day price for arbitrary SPL mints in this endpoint."}
        recipient = best.get("owner")
    elif largest_received is not None:
        amount = {"value": _plain(largest_received[0]), "symbol": "SOL",
                  "kind": "native", "token_address": None,
                  "usd_value": _usd(largest_received[0], price),
                  "usd_price": price, "price_basis": basis,
                  "note": "Largest positive SOL balance change in the transaction; the main value moved, if any, may be a token transfer (see token changes / bridge leg)."}
    else:
        amount = {"value": None, "symbol": "SOL", "kind": "native",
                  "token_address": None, "usd_value": None,
                  "usd_price": price, "price_basis": basis,
                  "note": "No SOL received in this transaction (the only SOL movement is the fee); the value moved, if any, is a token transfer not captured in the parsed balances."}
    fee_usd = _usd(fee_sol, price)
    bridge = await _bridge_leg("solana", sig, "solana", sender=sender) if confirmed else None
    return {
        "chain": "solana",
        "chain_label": "Solana",
        "kind": "solana",
        "hash": sig,
        "status": t.get("status") or "unknown",
        "block": slot,
        "slot": slot,
        "timestamp": block_time,
        "age": age(datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat()) if block_time else "",
        "confirmations": slot_depth,
        "confirmations_unit": "slots",
        "finality_threshold": SOL_FINALITY_SLOTS,
        "from": sender,
        "to": recipient,
        "amount": amount,
        "token_transfers": [
            {"token": None, "token_address": c.get("mint"),
             "amount": str(c.get("change")), "from": None,
             "to": c.get("owner")}
            for c in token_changes
        ],
        "fee": {"value": _plain(fee_sol) if fee_sol is not None else None,
                "symbol": "SOL", "usd_value": fee_usd,
                "usd_price": price, "price_basis": basis},
        "explorer_url": f"https://explorer.solana.com/tx/{sig}",
        "timeline": _timeline(confirmed, slot_depth, SOL_FINALITY_SLOTS, bridge),
        "bridge": bridge,
        "notes": HONEST_NOTES,
    }


# ---------------------------------------------------------------- Bitcoin
async def _track_bitcoin(txid):
    t = await btc_tx(txid)
    block_time = t.get("block_time")
    confirmations = t.get("confirmations") or 0
    confirmed = bool(t.get("confirmed"))
    price, basis = await _btc_price_at(block_time)
    outputs = t.get("outputs") or []
    inputs = t.get("inputs") or []
    largest_out = max(outputs, key=lambda o: o.get("value_sats") or 0) if outputs else None
    fee_btc = None
    if t.get("fee") is not None:
        fee_btc = _plain(t["fee"] / 100_000_000, decimals=8)
    amount = {
        "value": str(largest_out.get("value_btc")) if largest_out else "0",
        "symbol": "BTC",
        "kind": "bitcoin-output",
        "token_address": None,
        "usd_value": _usd(largest_out.get("value_btc") if largest_out else None, price),
        "usd_price": price,
        "price_basis": basis,
        "note": "Largest single output — change outputs cannot be identified from chain data alone.",
    }
    return {
        "chain": "bitcoin",
        "chain_label": "Bitcoin",
        "kind": "bitcoin",
        "hash": txid,
        "status": "success" if confirmed else "pending",
        "block": t.get("block_height"),
        "timestamp": block_time,
        "age": age(datetime.fromtimestamp(block_time, tz=timezone.utc).isoformat()) if block_time else "",
        "confirmations": confirmations,
        "finality_threshold": BTC_FINALITY_CONFIRMATIONS,
        "from": (inputs[0].get("address") if inputs else None),
        "senders": sorted({i.get("address") for i in inputs if i.get("address")}),
        "to": largest_out.get("address") if largest_out else None,
        "amount": amount,
        "total_moved_btc": t.get("total_out_btc"),
        "total_moved_usd": _usd(t.get("total_out_btc"), price),
        "token_transfers": [],
        "fee": {"value": str(fee_btc) if fee_btc is not None else None,
                "symbol": "BTC",
                "fee_rate_sat_vb": t.get("fee_rate_sat_vb"),
                "usd_value": _usd(fee_btc, price),
                "usd_price": price, "price_basis": basis},
        "explorer_url": f"https://mempool.space/tx/{txid}",
        "timeline": _timeline(confirmed, confirmations, BTC_FINALITY_CONFIRMATIONS, None),
        "bridge": None,
        "notes": HONEST_NOTES,
    }


# ---------------------------------------------------------------- public entry points
def _validate(chain, h):
    if chain not in SETTLE_CHAINS:
        raise UpstreamError(
            f"Unknown chain (expected one of {', '.join(sorted(SETTLE_CHAINS))})",
            status=400,
        )
    h = (h or "").strip()
    if not h:
        raise UpstreamError("hash is required", status=400)
    if chain in CHAINS and not _EVM_HASH_RE.match(h):
        raise UpstreamError("Invalid transaction hash (expected 0x + 64 hex chars)", status=400)
    if chain == "solana" and not _is_sig(h):
        raise UpstreamError("Invalid transaction signature (expected base58, 86-88 chars)", status=400)
    if chain == "bitcoin" and not _is_hex64(h):
        raise UpstreamError("Invalid transaction id (expected 64 hex chars)", status=400)
    return h.lower() if chain in CHAINS or chain == "bitcoin" else h


async def track_settlement(chain, h):
    """Full tracker payload: timeline + receipt fields + bridge leg.
    Cached 60s (confirmations move; a confirmed tx is near-immutable
    but the bridge leg can still progress)."""
    h = _validate(chain, h)
    key = f"settle:{chain}:{h}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    if chain in CHAINS:
        out = await _track_evm(chain, h)
    elif chain == "solana":
        out = await _track_solana(h)
    else:
        out = await _track_bitcoin(h)
    _cache_set(key, out, ttl=60)
    return out


async def receipt(chain, h):
    """Shareable receipt: the tracker's receipt fields only, plus a
    self link and generation time. `?format=json` is accepted and
    returns this same JSON (the page's Download button uses it)."""
    d = await track_settlement(chain, h)
    return {
        "receipt": True,
        "chain": d["chain"],
        "chain_label": d["chain_label"],
        "hash": d["hash"],
        "status": d["status"],
        "block": d.get("block"),
        "timestamp": d.get("timestamp"),
        "age": d.get("age"),
        "confirmations": d.get("confirmations"),
        "confirmations_unit": d.get("confirmations_unit", "confirmations"),
        "finality_threshold": d.get("finality_threshold"),
        "economically_final": bool(
            d.get("confirmations") is not None
            and d["confirmations"] >= d["finality_threshold"]
        ),
        "from": d.get("from"),
        "to": d.get("to"),
        "amount": d["amount"],
        "token_transfers": d.get("token_transfers") or [],
        "total_moved_btc": d.get("total_moved_btc"),
        "total_moved_usd": d.get("total_moved_usd"),
        "fee": d["fee"],
        "bridge": d.get("bridge"),
        "explorer_url": d["explorer_url"],
        "receipt_url": f"/#/receipt/{d['chain']}/{d['hash']}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "notes": d["notes"],
    }
