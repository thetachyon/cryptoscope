"""Cross-chain identity (Feature 4).

A "Cross-chain" section for every address page: for an EVM address,
its balance and activity on each live EVM chain (same address, different
chains — one row per chain, via the Blockscout clients the apps already
use), plus Wormhole bridge transfers involving the address, plus a link
to the full portfolio in WalletScope.

Sources (all keyless, zero new dependencies):
  - EVM presence: Blockscout v2 `/addresses/{addr}` per chain, fetched
    in parallel with a short (6s) timeout. A chain that fails shows
    "unavailable" and never blocks the section; a chain that 404s is
    "no activity" (the address simply has no history there).
  - Bridge linkage: Wormholescan keyless
    `/api/v1/transactions?address={addr}` (probed 2026-11-05 — it
    answers 200 for EVM and Solana addresses and returns the transfers
    the address originated or received). It matches the origin-tx
    sender and the destination address; it does NOT match an address
    that merely appears as a token sender inside a contract-initiated
    transfer (probed: the Polygon sender of a transfer to vitalik.eth
    returns 0 results for its own lookup). Amounts are shown only when
    Wormholescan standardizes them; otherwise the transfer is listed
    with "amount not reported by Wormholescan", never invented.
    Wormhole only — no all-bridge total exists keyless (DeFiLlama
    bridges is paywalled, probed 2026-11-06).

Honest limits (also carried verbatim in the `note` field and rendered
visibly under the section heading on every address page):
EVM <-> Solana <-> Bitcoin linkage is shown ONLY when an actual bridge
transfer is observed. There is no keyless way to prove two
different-format addresses belong to the same person, and nothing here
implies otherwise. The same-EOA rows are one account by construction
(the same address string on every EVM chain), not an identity claim.

SolScope / BitScope: EVM presence is not applicable (different address
format), so those pages show bridge linkage only, where observed.
WalletScope covers EVM + Solana only, so Bitcoin pages get no
WalletScope link.
"""
import asyncio
import re

import httpx

from common.chains import CHAINS
from common.clients import (
    HEADERS,
    TIMEOUT,
    UpstreamError,
    _cache_get,
    _cache_set,
    bs,
)
from common.formatting import wei_to_eth
from common.solana import is_solana_address

WORMHOLE_BASE = "https://api.wormholescan.io/api/v1"
WALLETSCOPE_URL = "https://walletscope.vercel.app"

# Expected Vercel domains, same pattern as every app's config.py SIBLINGS.
CHAIN_SCOPE = {
    "ethereum": "ethscope",
    "sepolia": "sepoliascope",
    "optimism": "optimismscope",
    "gnosis": "gnosisscope",
}

# Wormhole chain ids (https://wormholescan.io docs). Only the ids this
# feature has actually observed or that are long-established are named;
# anything else renders as "Chain {id}" rather than a guessed label.
WORMHOLE_CHAINS = {
    1: "Solana",
    2: "Ethereum",
    3: "Terra",
    4: "BNB Chain",
    5: "Polygon",
    6: "Avalanche",
    7: "Oasis",
    8: "Algorand",
    9: "Aurora",
    10: "Fantom",
    11: "Karura",
    12: "Acala",
    13: "Klaytn",
    14: "Celo",
    15: "NEAR",
    16: "Moonbeam",
    17: "Neon",
    18: "Terra 2",
    19: "Injective",
    20: "Osmosis",
    21: "Sui",
    22: "Aptos",
    23: "Arbitrum",
    24: "Optimism",
    25: "Gnosis",
    30: "Base",
}

HONEST_NOTE = (
    "EVM \u2194 Solana \u2194 Bitcoin linkage is shown ONLY when an actual "
    "bridge transfer is observed. There is no keyless way to prove two "
    "different-format addresses belong to the same person, and nothing "
    "here implies otherwise. The same-address rows above are one account "
    "by construction (the same address on every EVM chain), not an "
    "identity claim."
)

BRIDGES_NOTE = (
    "Wormhole transfers only (Wormholescan keyless API). No all-bridge "
    "total exists keyless \u2014 DeFiLlama bridges is paywalled. Lookup "
    "finds transfers the address originated or received."
)

_EVM_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_BTC_RE = re.compile(
    r"^(bc1[qpzry9x8gf2tvdw0s3jn54khce6mua7l]{11,71}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})$"
)

CROSSCHAIN_TTL = 120  # seconds


def _scope_url(chain: str, addr: str) -> str:
    return f"https://{CHAIN_SCOPE[chain]}.vercel.app/#/address/{addr}"


def _wh_chain(cid) -> str:
    try:
        cid = int(cid)
    except (TypeError, ValueError):
        return "Unknown chain"
    return WORMHOLE_CHAINS.get(cid, f"Chain {cid}")


def _fmt_amount(raw, decimals) -> str | None:
    """Raw integer amount string + decimals -> human decimal string."""
    if raw is None or raw == "":
        return None
    try:
        n = int(raw)
        d = int(decimals) if decimals is not None else 0
    except (TypeError, ValueError):
        return None
    if d <= 0:
        return str(n)
    s = str(n).rjust(d + 1, "0")
    whole, frac = s[:-d], s[-d:].rstrip("0")
    return whole + ("." + frac if frac else "")


# ---------------------------------------------------------------- EVM presence
async def _chain_presence(chain: str, addr: str) -> dict:
    """One row of same-EOA presence. Never raises: failure degrades to
    status "unavailable", a Blockscout 404 to "no activity". Balance
    comes from /addresses/{addr}; latest activity from the first row of
    /addresses/{addr}/transactions (Blockscout returns
    transactions_count as null for some addresses, so the count is
    reported only when upstream actually provides it)."""
    row = {
        "chain": chain,
        "label": CHAINS[chain]["label"],
        "native": CHAINS[chain]["native"],
        "url": _scope_url(chain, addr),
    }
    try:
        d = await bs(chain, f"/addresses/{addr}", timeout=6.0)
    except Exception:
        row["status"] = "unavailable"
        row["note"] = "Chain data unavailable (upstream unreachable)"
        return row
    # Latest activity is best-effort: its failure must not sink the row
    # (Blockscout rejects page_size on this endpoint, so the default
    # first page is fetched and only items[0] is kept).
    try:
        txs = await bs(chain, f"/addresses/{addr}/transactions", timeout=6.0)
    except Exception:
        txs = None
    if d is None:
        row["status"] = "no_activity"
        row["balance"] = "0"
        row["transactions_count"] = 0
        row["latest_tx"] = None
        return row
    row["status"] = "active"
    row["balance"] = wei_to_eth(d.get("coin_balance"))
    row["transactions_count"] = d.get("transactions_count")
    row["is_contract"] = bool(d.get("is_contract"))
    items = (txs or {}).get("items") or [] if isinstance(txs, dict) else []
    # First item can be a pending tx with a null timestamp (observed on
    # Sepolia) — take the first timestamped one, else the first item.
    first = next(
        (it for it in items if isinstance(it, dict) and it.get("timestamp")),
        None,
    )
    if first is None and items and isinstance(items[0], dict):
        first = items[0]
    row["latest_tx"] = (
        {"hash": first.get("hash"), "timestamp": first.get("timestamp")}
        if isinstance(first, dict)
        else None
    )
    return row


# ---------------------------------------------------------------- Wormhole bridges
async def _wh_transactions(addr: str, size: int = 10):
    """GET Wormholescan transactions for an address. Returns the parsed
    list, or None on any failure (callers degrade, never raise)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            r = await c.get(
                WORMHOLE_BASE + "/transactions",
                params={"address": addr, "page": 0, "size": size},
            )
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError):
        return None
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    txs = d.get("transactions")
    return txs if isinstance(txs, list) else None


def _bridge_transfer(t: dict, addr: str) -> dict:
    sp = t.get("standardizedProperties") or {}
    payload = t.get("payload") or {}
    gtx = t.get("globalTx") or {}
    origin = gtx.get("originTx") or {}
    dest = gtx.get("destinationTx") or {}
    from_chain_id = sp.get("fromChain") or payload.get("srcChainId") or t.get("emitterChain")
    to_chain_id = sp.get("toChain") or payload.get("destChainId")
    # Sender: only what Wormholescan standardizes. For Mayan Swift
    # transfers it standardizes no sender (the origin-tx sender is a
    # Mayan driver address on the emitter chain, not the user), so
    # from_address stays null rather than showing a misleading address
    # next to the source chain. Direction still uses originTx.from.
    from_addr = sp.get("fromAddress") or ""
    to_addr = sp.get("toAddress") or ""
    low = addr.lower()
    if to_addr and to_addr.lower() == low:
        direction = "in"
    elif (from_addr and from_addr.lower() == low) or (
        origin.get("from") and str(origin.get("from")).lower() == low
    ):
        direction = "out"
    else:
        direction = "involving"
    amount = _fmt_amount(sp.get("amount"), sp.get("normalizedDecimals"))
    return {
        "timestamp": t.get("timestamp"),
        "direction": direction,
        "from_chain": _wh_chain(from_chain_id),
        "from_chain_id": from_chain_id,
        "to_chain": _wh_chain(to_chain_id),
        "to_chain_id": to_chain_id,
        "from_address": from_addr or None,
        "to_address": to_addr or None,
        "amount": amount,
        "amount_reported": amount is not None,
        "token_address": sp.get("tokenAddress") or None,
        "app": ", ".join(sp.get("appIds") or []) or None,
        "origin_tx_hash": origin.get("txHash") or t.get("txHash"),
        "destination_tx_hash": dest.get("txHash"),
        "destination_status": dest.get("status"),
    }


async def _bridges(addr: str) -> dict:
    txs = await _wh_transactions(addr)
    if txs is None:
        return {
            "provider": "Wormholescan (Wormhole)",
            "transfers": [],
            "count": 0,
            "error": "Bridge data unavailable (Wormholescan unreachable)",
            "note": BRIDGES_NOTE,
        }
    return {
        "provider": "Wormholescan (Wormhole)",
        "transfers": [_bridge_transfer(t, addr) for t in txs[:10]],
        "count": len(txs),
        "error": None,
        "note": BRIDGES_NOTE,
    }


# ---------------------------------------------------------------- public entry points
def _kind_of(addr: str) -> str | None:
    if _EVM_RE.fullmatch(addr or ""):
        return "evm"
    if is_solana_address(addr or ""):
        return "solana"
    if _BTC_RE.match(addr or ""):
        return "bitcoin"
    return None


async def crosschain(addr: str) -> dict:
    """Cross-chain identity for one address (EVM, Solana or Bitcoin).

    EVM: same-EOA presence on all four live EVM chains + Wormhole
    transfers + WalletScope link. Solana: Wormhole transfers +
    WalletScope link (no EVM presence — different address format).
    Bitcoin: Wormhole transfers only (WalletScope has no Bitcoin).
    Never raises for a well-formed address: per-chain and bridge
    failures degrade in place. Raises UpstreamError (400) for a
    malformed address."""
    kind = _kind_of(addr)
    if kind is None:
        raise UpstreamError(
            "Invalid address (expected an EVM 0x address, a Solana "
            "base58 address, or a Bitcoin address)",
            status=400,
        )
    key = f"crosschain:{kind}:{addr.lower() if kind == 'evm' else addr}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    if kind == "evm":
        presence = await asyncio.gather(
            *(_chain_presence(c, addr) for c in CHAINS)
        )
        walletscope_url = f"{WALLETSCOPE_URL}/#/wallet/{addr}"
    else:
        presence = None
        walletscope_url = (
            f"{WALLETSCOPE_URL}/#/wallet/{addr}" if kind == "solana" else None
        )
    out = {
        "address": addr,
        "kind": kind,
        "evm_presence": presence,
        "bridges": await _bridges(addr),
        "walletscope_url": walletscope_url,
        "note": HONEST_NOTE,
    }
    _cache_set(key, out, ttl=CROSSCHAIN_TTL)
    return out


async def _resolve_evm_name(name):
    """Resolve an ENS name (or other non-hex input) to an EVM address via
    Blockscout /search on Ethereum, mirroring explorer._search's item
    parsing: `address` and `ens_domain` hits both carry `address_hash`.
    Returns the address, or None when no address hit is found. An
    upstream failure propagates as UpstreamError (honest 502), it is
    never reported as an invalid address."""
    d = await bs("ethereum", "/search", params={"q": name})
    if not isinstance(d, dict):
        return None
    for it in d.get("items", []) or []:
        if it.get("type") in ("address", "ens_domain"):
            h = it.get("address_hash")
            if h and _EVM_RE.fullmatch(h or ""):
                return h
    return None


async def evm_crosschain(chain: str, addr: str) -> dict:
    """Cross-chain identity for an EVM address viewed on `chain`."""
    if not _EVM_RE.fullmatch(addr or ""):
        resolved = await _resolve_evm_name(addr or "")
        if resolved is None:
            raise UpstreamError(
                "Invalid EVM address (expected 0x + 40 hex chars)", status=400
            )
        addr = resolved
    out = await crosschain(addr)
    out = dict(out)
    out["scope_chain"] = chain
    return out


async def solana_crosschain(addr: str) -> dict:
    """Cross-chain identity for a Solana address (bridge linkage only)."""
    if not is_solana_address(addr or ""):
        raise UpstreamError("Invalid Solana address", status=400)
    return await crosschain(addr)


async def bitcoin_crosschain(addr: str) -> dict:
    """Cross-chain identity for a Bitcoin address (bridge linkage only)."""
    if not _BTC_RE.match(addr or ""):
        raise UpstreamError("Invalid Bitcoin address", status=400)
    return await crosschain(addr)
