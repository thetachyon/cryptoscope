"""Recipient pre-flight compliance check (phase 2, P2) — keyless.

One screen BEFORE sending: paste a recipient address (optionally a
coin + chain + amount) and get a pass / warn / block verdict built
from the checks the *Scope apps already know how to run — nothing is
rebuilt here, only composed:

  - sanctions risk — common/risk.py (vendored OFAC SDN set, 514
    addresses, 2026-11-05; 3-hop EVM/Bitcoin walk, 1-hop Solana).
  - EOA vs contract — eth_getCode via the public RPC (EVM), with
    Blockscout as fallback. An EIP-7702 delegation (code 0xef01…)
    is still a key-controlled EOA, not a contract — Blockscout
    reports is_contract True for delegated EOAs (vitalik.eth is
    one), so the flag alone is not trusted. Solana: getAccountInfo
    type; not applicable on Bitcoin.
  - known labels — labels.json (curated, Ethereum mainnet only) is
    a FRONTEND asset: the preflight page reads it with labelFor()
    and shows the label next to the address. The API does not
    duplicate it; the SDN name on a direct hit comes from risk.py.
  - holds the coin — balanceOf eth_call (EVM token), native balance
    (ETH/SOL/BTC), SPL token accounts (Solana).
  - frozen / blacklisted — on-chain read functions via public RPC
    eth_call (USDC isBlacklisted, USDT isBlackListed, PYUSD
    isFrozen, FDUSD frozen — function names from the live ABIs
    recorded in common/freeze.py; any other ERC-20 is probed with
    all five known read functions and the first decodable bool
    wins). The recipient AND up to 10 recent counterparties are
    checked. TUSD has a WRITE-only blacklist (setBlacklisted) with
    no on-chain read, so its status is honestly UNKNOWN, never
    assumed clean. DAI/USDe have no freeze function at all.
    Solana SPL: the mint's freeze authority + the recipient token
    account's state (frozen/initialized) via the public RPC.
  - first-seen + activity recency — Blockscout tx pages + the
    /counters endpoint for the exact tx count (the /addresses
    record does NOT carry transactions_count), mempool.space tx
    pages (Bitcoin), getSignaturesForAddress (Solana). First-seen
    is EXACT only when the scanned pages cover the address's whole
    history (pages exhausted, or tx count known and small); for
    high-activity addresses it is reported UNKNOWN with the reason,
    never guessed from a partial scan.

Verdict rules (worst check wins):
  - BLOCK: direct OFAC SDN match (score 100); 1-hop sanctioned
    counterparty (score >= 75); recipient frozen/blacklisted for
    the coin being sent; Blockscout scam flag.
  - WARN: sanctioned counterparty 2-3 hops out or a mixer touch
    (score 40-74); recipient is a contract (confirm it can receive
    the coin); address has no on-chain record / no activity yet;
    frozen status unverifiable for the coin being sent (TUSD);
    recipient frozen on a checked coin other than the one being
    sent; a recent counterparty frozen.
  - PASS: everything checked is clean.
  - UNKNOWN on a single check never invents a result — it is shown
    with its reason and does not by itself change the verdict,
    except the TUSD case above (unknown frozen status on the coin
    being sent is a warn, because the issuer CAN blacklist).

Honest limits (also stated in every response):
  - A pass is NOT proof the recipient is clean: the risk walk only
    covers the transactions the address page loads, the SDN list is
    dated 2026-11-05, and the blacklist is not enumerable on-chain
    (only the recipient + 10 recent counterparties are probed).
  - Stablecoin symbols resolve to their Ethereum-mainnet contracts
    only; on another EVM chain pass the token contract address
    (0x...) instead. USDD is Tron-native — its EVM balance/freeze
    checks are not applicable.
  - Scores are a heuristic screening signal, not a legal
    determination. Nothing here creates or signs a transaction.
"""
import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from common.abi import _decode_args, _encode_args, _function_selector
from common.bitcoin import (
    btc_address,
    btc_address_txs,
    is_bitcoin_address,
)
from common.chains import CHAINS
from common.clients import UpstreamError, _cache_get, _cache_set, bs, rpc
from common.formatting import wei_to_eth
from common.risk import bitcoin_risk, evm_risk, solana_risk
from common.solana import (
    SOL_ACCOUNTS_RPC,
    _sol_rpc,
    is_solana_address,
    sol_balance,
    sol_token_accounts,
)
from common.stablecoin import STABLECOINS
from common.payments_readiness import _FREEZE_FNS

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")

# Freeze/blacklist READ function per stablecoin scope, from the live
# implementation ABIs recorded in common/freeze.py (2026-11-06).
# None = no on-chain read exists (TUSD: write-only setBlacklisted;
# DAI/USDe: no freeze function at all; USDD: Tron, not covered).
FREEZE_READ_FN = {
    "usdtscope": "isBlackListed",
    "usdcscope": "isBlacklisted",
    "pyusdsscope": "isFrozen",
    "fdusdsscope": "frozen",
    "tusdscope": None,
    "daiscope": None,
    "usdescope": None,
    "usddscope": None,
}

# Counterparties probed for frozen status (bounded; see module docs).
FREEZE_CP_CAP = 10
# Tx-history pages scanned for first-seen (EVM 50/page, BTC 25/page,
# Solana 25/page). First-seen is exact only if the whole history fits
# in the scanned pages.
ACTIVITY_PAGES = 5

METHOD = (
    "Pre-flight compliance check (phase 2, P2). Sanctions risk reuses "
    "common/risk.py (OFAC SDN list, 514 addresses, 2026-11-05). "
    "EOA/contract from Blockscout /addresses (EVM) or getAccountInfo "
    "(Solana). Coin balance from balanceOf eth_call / native balance / "
    "SPL token accounts. Frozen status from on-chain freeze/blacklist "
    "read functions via public RPC eth_call (recipient + up to 10 "
    "recent counterparties). First-seen/activity from on-chain tx "
    "pages. Nothing here creates or signs a transaction."
)
LIMITS = [
    "A pass is not proof the recipient is clean: the risk walk covers "
    "only the transactions the address page loads, the SDN list is "
    "dated 2026-11-05, and the issuer blacklist is not enumerable "
    "on-chain (only the recipient + 10 recent counterparties probed).",
    "Stablecoin symbols resolve to their Ethereum-mainnet contracts "
    "only; on another EVM chain pass the token contract address (0x...) "
    "instead. USDD is Tron-native — its EVM balance/freeze checks do "
    "not apply.",
    "Scores are a heuristic screening signal, not a legal determination.",
]


# ---------------------------------------------------------------- classify
async def _classify(address):
    """(family, address, resolved_from). ENS names resolve on Ethereum
    mainnet via risk._resolve_evm_name; anything unresolvable is a
    400, never a guessed family."""
    a = (address or "").strip()
    if not a:
        raise UpstreamError("address is required", status=400)
    if _EVM_RE.fullmatch(a):
        return "evm", a, None
    # Bitcoin before Solana: a bech32 address is base58-alphabet
    # text of Solana-like length, so only the checksum-validated
    # Bitcoin test may claim it; a Solana address never passes the
    # Bitcoin check.
    if is_bitcoin_address(a):
        return "bitcoin", a, None
    if is_solana_address(a):
        return "solana", a, None
    # Last resort: ENS name (or other non-hex input) on Ethereum.
    from common.risk import _resolve_evm_name

    resolved = await _resolve_evm_name(a)
    if resolved is None:
        raise UpstreamError(
            "Invalid address (expected 0x + 40 hex chars, a Solana "
            "base58 address, a Bitcoin address, or an ENS name)",
            status=400,
        )
    return "evm", resolved, a


def _stablecoin_scope(symbol):
    for key, cfg in STABLECOINS.items():
        if cfg["symbol"].lower() == (symbol or "").lower():
            return key, cfg
    return None, None


async def _resolve_coin(coin, family, chain):
    """Coin parameter -> resolution dict, or None when no coin given.

    Symbols: the 8 stablecoins (mainnet contracts only), ETH / SOL /
    BTC (native for the matching family). A 0x address is an ERC-20
    contract on the selected EVM chain (symbol/decimals from
    Blockscout). A base58 address is an SPL mint (Solana only).
    Anything else is a 400 with the reason."""
    if coin is None or str(coin).strip() == "":
        return None
    c = str(coin).strip()
    scope_key, cfg = _stablecoin_scope(c)
    if scope_key is not None:
        if cfg["primary"] == "tron":
            return {
                "kind": "stablecoin_tron", "symbol": cfg["symbol"],
                "name": cfg["name"], "scope": scope_key,
                "contract": cfg["contract"], "decimals": cfg["decimals"],
                "chain": "tron",
                "note": "USDD is Tron-native; EVM balance/freeze checks do not apply.",
            }
        if family != "evm":
            raise UpstreamError(
                f"{cfg['symbol']} is an Ethereum token — it does not apply "
                f"to a {family} address",
                status=400,
            )
        if chain != "ethereum":
            raise UpstreamError(
                f"{cfg['symbol']} resolves to its Ethereum-mainnet contract "
                f"only — pass the {chain} token contract address (0x...) "
                "instead, or use chain=ethereum",
                status=400,
            )
        return {
            "kind": "stablecoin", "symbol": cfg["symbol"],
            "name": cfg["name"], "scope": scope_key,
            "contract": cfg["contract"], "decimals": cfg["decimals"],
            "chain": "ethereum",
        }
    if c.upper() in ("ETH", "SOL", "BTC"):
        want = {"ETH": "evm", "SOL": "solana", "BTC": "bitcoin"}[c.upper()]
        if family != want:
            raise UpstreamError(
                f"{c.upper()} is the native coin of {want}, not of a "
                f"{family} address",
                status=400,
            )
        native = {"evm": "ETH", "solana": "SOL", "bitcoin": "BTC"}[family]
        return {"kind": "native", "symbol": native, "name": native,
                "decimals": 18 if family == "evm" else 9 if family == "solana" else 8}
    if _EVM_RE.fullmatch(c):
        if family != "evm":
            raise UpstreamError(
                "A 0x token contract address only applies to an EVM recipient",
                status=400,
            )
        d = await bs(chain, f"/tokens/{c}")
        if not isinstance(d, dict):
            raise UpstreamError(
                f"Token contract {c} not found on {chain}", status=400
            )
        return {
            "kind": "token", "symbol": d.get("symbol") or "TOKEN",
            "name": d.get("name") or c, "contract": c,
            "decimals": d.get("decimals") if isinstance(d.get("decimals"), int) else 18,
            "chain": chain,
        }
    if _B58_RE.fullmatch(c):
        if family != "solana":
            raise UpstreamError(
                "An SPL mint address only applies to a Solana recipient",
                status=400,
            )
        return {"kind": "spl", "symbol": c[:8] + "…", "name": "SPL token",
                "mint": c, "decimals": None, "chain": "solana"}
    raise UpstreamError(
        "Unknown coin — use a stablecoin symbol (USDT, USDC, DAI, USDe, "
        "FDUSD, PYUSD, TUSD, USDD), a native symbol (ETH, SOL, BTC), a "
        "0x token contract address, or an SPL mint address",
        status=400,
    )


def _parse_amount(amount):
    if amount is None or str(amount).strip() == "":
        return None
    try:
        v = Decimal(str(amount).strip())
    except InvalidOperation:
        raise UpstreamError("amount must be a number", status=400)
    if v <= 0:
        raise UpstreamError("amount must be greater than 0", status=400)
    return v


# ---------------------------------------------------------------- eth_call
async def _eth_call_bool(chain, contract, fn, arg):
    """eth_call fn(address) -> True/False, or None when the function is
    absent (revert / empty return / undecodable). Transport failures
    propagate as UpstreamError (callers turn them into unknown)."""
    sel = _function_selector(fn, ["address"])
    data = "0x" + (sel + _encode_args(["address"], [arg])).hex()
    try:
        res = await rpc(chain, "eth_call",
                        [{"to": contract, "data": data}, "latest"])
    except UpstreamError as e:
        if e.status == 400:
            return None  # revert = function absent on this contract
        raise
    if not res or res == "0x":
        return None
    try:
        vals = _decode_args(["bool"], bytes.fromhex(res[2:]))
    except Exception:
        return None
    return bool(vals[0])


async def _balance_of(chain, contract, addr, decimals):
    """ERC-20 balanceOf via eth_call -> human-unit Decimal, or None on
    revert/transport failure (callers report unknown)."""
    sel = _function_selector("balanceOf", ["address"])
    data = "0x" + (sel + _encode_args(["address"], [addr])).hex()
    try:
        res = await rpc(chain, "eth_call",
                        [{"to": contract, "data": data}, "latest"])
    except UpstreamError:
        return None
    if not res or res == "0x":
        return None
    try:
        raw = int(res, 16)
    except ValueError:
        return None
    return Decimal(raw) / (Decimal(10) ** int(decimals))


# ---------------------------------------------------------------- checks
def _check(cid, label, status, detail, **extra):
    out = {"id": cid, "label": label, "status": status, "detail": detail}
    out.update(extra)
    return out


async def _sanctions_check(family, chain, addr):
    try:
        if family == "evm":
            r = await evm_risk(chain, addr)
        elif family == "bitcoin":
            r = await bitcoin_risk(addr)
        else:
            r = await solana_risk(addr)
    except UpstreamError as e:
        return _check("sanctions", "Sanctions risk (OFAC SDN)", "unknown",
                      f"Risk check failed upstream: {e.msg}"), None
    except Exception as e:
        return _check("sanctions", "Sanctions risk (OFAC SDN)", "unknown",
                      f"Risk check failed: {e}"), None
    score = r.get("score", 0)
    direct = r.get("direct_hit")
    if direct is not None:
        status, detail = "block", (
            f"BLOCK — this address is on the OFAC SDN list as "
            f"{direct['name']} ({direct['program']}; {direct['currency']} "
            f"address). Do not send."
        )
    elif score >= 75:
        status, detail = "block", (
            f"BLOCK — risk score {score}/100 ({r.get('level')}): a "
            f"sanctioned address is 1 hop away. Do not send."
        )
    elif score >= 40:
        status, detail = "warn", (
            f"Risk score {score}/100 ({r.get('level')}): a sanctioned "
            f"address or mixer is 2-3 hops away. Review before sending."
        )
    else:
        status, detail = "pass", (
            f"Risk score {score}/100 (low): no OFAC SDN match for this "
            f"address or its checked counterparties."
        )
    return _check("sanctions", "Sanctions risk (OFAC SDN)", status, detail,
                  score=score, level=r.get("level"),
                  reasons=r.get("reasons"), hits=r.get("hits"),
                  hits_total=r.get("hits_total"),
                  coverage=r.get("coverage")), r


def _compact_tx(t):
    """Small stable shape for latest/first tx extras (raw Blockscout
    objects are multi-KB and leak nothing useful here)."""
    if not isinstance(t, dict):
        return None
    frm = t.get("from") or {}
    to = t.get("to") or {}
    return {
        "hash": t.get("hash"),
        "timestamp": t.get("timestamp"),
        "from": frm.get("hash") if isinstance(frm, dict) else None,
        "to": to.get("hash") if isinstance(to, dict) else None,
    }


async def _evm_activity(chain, addr):
    """Address record + tx pages. Returns (check, counterparties,
    latest_tx, first_tx_or_None, tx_count)."""
    rec = await bs(chain, f"/addresses/{addr}")
    if rec is None:
        return (
            _check("activity", "Address history", "warn",
                   f"No on-chain record for this address on "
                   f"{CHAINS[chain]['label']} — it has never transacted "
                   "there (fresh address, or mistyped). Double-check "
                   "before sending."),
            [], None, None, 0,
        )
    # The /addresses record does NOT carry transactions_count (absent
    # even for vitalik.eth) — the /counters endpoint does (as a string).
    tx_count = None
    try:
        counters = await bs(chain, f"/addresses/{addr}/counters")
        if isinstance(counters, dict) and counters.get("transactions_count") is not None:
            tx_count = int(counters["transactions_count"])
    except (UpstreamError, ValueError, TypeError):
        tx_count = None
    is_contract = bool(rec.get("is_contract"))
    items, params, scanned, exhausted = [], {}, 0, False
    try:
        for _ in range(ACTIVITY_PAGES):
            d = await bs(chain, f"/addresses/{addr}/transactions", params=params)
            if not isinstance(d, dict):
                break
            page = d.get("items", []) or []
            items.extend(page)
            scanned += len(page)
            nxt = d.get("next_page_params")
            if not nxt or not page:
                exhausted = True
                break
            params = nxt
    except UpstreamError as e:
        return (
            _check("activity", "Address history", "unknown",
                   f"Transaction history unavailable: {e.msg}"),
            [], None, None, tx_count,
        )
    cps = set()
    for t in items:
        for side in (t.get("from"), t.get("to")):
            h = (side or {}).get("hash")
            if h and h.lower() != addr.lower():
                cps.add(h.lower())
    latest = items[0] if items else None
    # Pending txs sit at the top of the list with no timestamp — the
    # recency display uses the latest CONFIRMED tx.
    latest_confirmed = next((t for t in items if t.get("timestamp")), None)
    first = None
    first_note = None
    history_complete = exhausted or (tx_count is not None and tx_count <= scanned)
    if not items:
        first_note = "No transactions on this chain."
    elif history_complete:
        first = items[-1]
    else:
        total_txt = f"{tx_count} total" if tx_count is not None else "unknown total"
        first_note = (
            f"First transaction not determined — history paging is "
            f"capped at {ACTIVITY_PAGES} pages ({scanned} of "
            f"{total_txt} transactions scanned)."
        )
    if not items:
        # Fresh warn only when nothing was scanned: the counters
        # endpoint can lag (a live check once read 0 while 250 txs
        # were scanned), so a zero counter never overrides scanned
        # transactions.
        status, detail = "warn", (
            "Address exists on-chain but has zero transactions — a "
            "fresh/unused address. Double-check before sending."
        )
    else:
        latest_txt = ""
        if latest_confirmed and latest_confirmed.get("timestamp"):
            latest_txt = (
                f" Last activity {latest_confirmed['timestamp'][:10]}."
            )
        elif items:
            # Hot contracts (e.g. USDC) always have pending txs at the
            # top of the list — those carry no timestamp/block. Say so
            # instead of silently dropping the recency line.
            pending = sum(1 for t in items if not t.get("timestamp"))
            latest_txt = (
                f" Latest activity: {pending} pending (unconfirmed) "
                f"transaction(s) at the top of the list — no confirmed "
                f"tx in the scanned pages."
            )
        first_txt = (
            f" First transaction {first['timestamp'][:10]}."
            if first and first.get("timestamp") else
            (f" {first_note}" if first_note else "")
        )
        if tx_count is not None:
            count_txt = f"{tx_count} transaction(s)"
        else:
            count_txt = (f"{scanned}+ transactions scanned "
                         "(total unknown — counters unavailable)")
        status, detail = "pass", (
            f"{count_txt} on {CHAINS[chain]['label']}."
            f"{latest_txt}{first_txt}"
        )
    return (
        _check("activity", "Address history", status, detail,
               transactions_count=tx_count, is_contract=is_contract,
               latest_tx=_compact_tx(latest), first_tx=_compact_tx(first),
               first_tx_note=first_note, txs_scanned=scanned),
        sorted(cps), latest, first, tx_count,
    )


async def _btc_activity(addr):
    try:
        info = await btc_address(addr)
    except UpstreamError as e:
        return (
            _check("activity", "Address history", "unknown",
                   f"Address lookup failed: {e.msg}"),
            [], None, None, 0,
        )
    tx_count = info.get("tx_count") or 0
    items, scanned, after, page_error = [], 0, None, None
    for _ in range(ACTIVITY_PAGES):
        try:
            page = await btc_address_txs(addr, after=after)
        except UpstreamError as e:
            if not items:
                return (
                    _check("activity", "Address history", "unknown",
                           f"Transaction history unavailable: {e.msg}"),
                    [], None, None, tx_count,
                )
            # Mid-scan paging failure: keep what was scanned and say
            # so, instead of discarding the whole check.
            page_error = e.msg
            break
        rows = page.get("items", []) or []
        items.extend(rows)
        scanned += len(rows)
        after = page.get("next_after")
        if not rows or not after:
            break
    latest = items[0] if items else None
    first = None
    first_note = None
    if not items:
        first_note = "No transactions."
    elif tx_count <= scanned:
        first = items[-1]
    else:
        first_note = (
            f"First transaction not determined — history paging is "
            f"capped at {ACTIVITY_PAGES} pages ({scanned} of "
            f"{tx_count} transactions scanned)."
        )
        if page_error:
            first_note += f" Paging stopped early: {page_error}."
    if not items:
        status, detail = "warn", (
            "Address has zero transactions — a fresh/unused address. "
            "Double-check before sending."
        )
    else:
        def _d(ts):
            if not ts:
                return "unknown date"
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        latest_txt = f" Last activity {_d(latest.get('block_time'))}." if latest else ""
        first_txt = (
            f" First transaction {_d(first.get('block_time'))}."
            if first else (f" {first_note}" if first_note else "")
        )
        status, detail = "pass", (
            f"{tx_count} transaction(s).{latest_txt}{first_txt}"
        )
    def _compact_btc(t):
        if not isinstance(t, dict):
            return None
        return {"txid": t.get("txid"), "block_time": t.get("block_time"),
                "block_height": t.get("block_height")}
    return (
        _check("activity", "Address history", status, detail,
               transactions_count=tx_count,
               latest_tx=_compact_btc(latest), first_tx=_compact_btc(first),
               first_tx_note=first_note, txs_scanned=scanned),
        [], latest, first, tx_count,
    )


async def _sol_activity(addr):
    from common.solana_explorer import _signatures

    try:
        sigs = await _signatures(addr, limit=25)
    except UpstreamError as e:
        return (
            _check("activity", "Address history", "unknown",
                   f"Signature history unavailable: {e.msg}"),
            [], None, None, None,
        )
    items = sigs.get("items", []) or []
    scanned = len(items)
    before = sigs.get("before")
    try:
        for _ in range(ACTIVITY_PAGES - 1):
            if not before or scanned >= 25 * ACTIVITY_PAGES:
                break
            page = await _signatures(addr, limit=25, before=before)
            rows = page.get("items", []) or []
            items.extend(rows)
            scanned += len(rows)
            before = page.get("before")
            if not rows:
                break
    except UpstreamError:
        pass  # partial scan is reported as scanned, not as failure
    latest = items[0] if items else None
    first = None
    first_note = None
    if not items:
        first_note = "No signatures found."
    elif len(items) < 25:
        first = items[-1]  # single page holds the whole history
    else:
        first_note = (
            f"First activity not determined — signature history paging "
            f"is capped at {ACTIVITY_PAGES} pages ({scanned} signatures "
            "scanned); the public RPC exposes no total count."
        )
    if not items:
        status, detail = "warn", (
            "No signatures found for this address — a fresh/unused "
            "address, or an address that has never transacted. "
            "Double-check before sending."
        )
    else:
        def _d(ts):
            if not ts:
                return "unknown date"
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
        latest_txt = f" Last activity {_d(latest.get('block_time'))}." if latest else ""
        first_txt = (
            f" First activity {_d(first.get('block_time'))}."
            if first else (f" {first_note}" if first_note else "")
        )
        status, detail = "pass", (
            f"{scanned} recent signature(s) scanned.{latest_txt}{first_txt}"
        )
    return (
        _check("activity", "Address history", status, detail,
               latest_tx=latest, first_tx=first,
               first_tx_note=first_note, txs_scanned=scanned),
        [], latest, first, None,
    )


async def _address_type_check(family, chain, addr):
    """EOA vs contract (EVM / Solana). Bitcoin: not applicable."""
    if family == "bitcoin":
        return _check("address_type", "Address type", "na",
                      "Bitcoin has no contracts — every address is a "
                      "key-controlled address.")
    if family == "solana":
        try:
            info = await _sol_rpc(
                SOL_ACCOUNTS_RPC, "getAccountInfo",
                [addr, {"encoding": "jsonParsed"}])
        except UpstreamError as e:
            return _check("address_type", "Address type", "unknown",
                          f"Account lookup failed: {e.msg}")
        value = (info or {}).get("value") if isinstance(info, dict) else None
        if value is None:
            return _check("address_type", "Address type", "warn",
                          "Account does not exist on Solana yet (no "
                          "lamports, no data) — a fresh address. "
                          "Double-check before sending.")
        if value.get("executable"):
            return _check("address_type", "Address type", "warn",
                          "Recipient is an on-chain PROGRAM, not a wallet "
                          "— only send if you know the program accepts "
                          "this transfer.",
                          account_type="program")
        data = value.get("data") or {}
        parsed = data.get("parsed") if isinstance(data, dict) else None
        ptype = (parsed or {}).get("type") if isinstance(parsed, dict) else None
        if ptype == "mint":
            return _check("address_type", "Address type", "warn",
                          "Recipient is a token MINT account, not a wallet "
                          "— do not send funds to a mint.",
                          account_type="mint")
        if ptype == "account":
            return _check("address_type", "Address type", "warn",
                          "Recipient is a TOKEN account, not a wallet — "
                          "only send the matching token to it.",
                          account_type="token_account")
        return _check("address_type", "Address type", "pass",
                      "Recipient is a regular Solana wallet account.",
                      account_type="account")
    # EVM: ground truth is eth_getCode, not Blockscout's is_contract
    # flag — an EOA with an EIP-7702 delegation (code 0xef01 + 20-byte
    # delegate, e.g. vitalik.eth -> AmbireAccount7702) is still a
    # key-controlled account, but Blockscout reports is_contract True
    # for it. eth_getCode: empty = EOA, 0xef01… = delegated EOA,
    # anything else = contract. Blockscout is the fallback when the
    # RPC fails, and the source of the scam flag either way.
    rec = await bs(chain, f"/addresses/{addr}")
    if rec is not None and rec.get("is_scam"):
        return _check("address_type", "Address type", "block",
                      "Blockscout flags this address as a SCAM address. "
                      "Do not send.",
                      is_contract=bool(rec.get("is_contract")),
                      is_scam=True)
    if rec is None:
        return _check("address_type", "Address type", "warn",
                      "No on-chain record on this chain — treated as a "
                      "fresh externally-owned account until proven "
                      "otherwise.",
                      is_contract=None)
    code = None
    try:
        code = await rpc(chain, "eth_getCode", [addr, "latest"])
    except UpstreamError:
        code = None
    ens = rec.get("ens_domain_name")
    if code is not None:
        code = str(code).lower()
        if code in ("0x", "", "0x0"):
            detail = "Recipient is an externally-owned account (EOA)."
            if ens:
                detail += f" ENS: {ens}."
            return _check("address_type", "Address type", "pass", detail,
                          is_contract=False, ens=ens)
        if code.startswith("0xef01") and len(code) in (46, 48):
            # 46 hex chars = 0xef01 + 20-byte delegate (EIP-7702 spec);
            # 48 observed live for vitalik.eth (an extra 0x00 byte
            # before the delegate). Either way the delegate is the
            # last 40 hex chars.
            delegate = "0x" + code[-40:]
            return _check("address_type", "Address type", "pass",
                          f"Recipient is an EOA with an EIP-7702 "
                          f"delegation to {delegate} — still "
                          "key-controlled (the delegation can be changed "
                          "by the key holder), not a contract.",
                          is_contract=False, ens=ens,
                          eip7702_delegate=delegate)
        name = rec.get("name") or "unnamed contract"
        return _check("address_type", "Address type", "warn",
                      f"Recipient is a CONTRACT ({name}) — confirm it "
                      "can receive this coin; sending to a contract "
                      "that does not handle incoming transfers can lock "
                      "funds.",
                      is_contract=True, name=rec.get("name"),
                      is_verified=bool(rec.get("is_verified")))
    # RPC failed: fall back to Blockscout's flag, labelled as such.
    if rec.get("is_contract"):
        name = rec.get("name") or "unnamed contract"
        return _check("address_type", "Address type", "warn",
                      f"Recipient is a CONTRACT ({name}) — confirm it "
                      "can receive this coin; sending to a contract "
                      "that does not handle incoming transfers can lock "
                      "funds. (Code check via RPC failed; Blockscout "
                      "flag used instead.)",
                      is_contract=True, name=rec.get("name"),
                      is_verified=bool(rec.get("is_verified")))
    detail = ("Recipient is an externally-owned account (EOA). "
              "(Code check via RPC failed; Blockscout flag used instead.)")
    if ens:
        detail += f" ENS: {ens}."
    return _check("address_type", "Address type", "pass", detail,
                  is_contract=False, ens=ens)


async def _holdings_check(family, chain, addr, coin, amount):
    """Whether the recipient already holds the coin (+ vs amount)."""
    if coin is None:
        return _check("holdings", "Recipient holds the coin", "na",
                      "No coin given — pass ?coin= to check whether the "
                      "recipient already holds it (and whether it is "
                      "frozen for that coin).")
    sym = coin["symbol"]
    kind = coin["kind"]
    if kind == "stablecoin_tron":
        return _check("holdings", f"Recipient holds {sym}", "na",
                      coin.get("note", "USDD is Tron-native; not checked "
                      "on EVM chains."))
    balance = None
    balance_note = None
    try:
        if family == "evm":
            if kind == "native":
                rec = await bs(chain, f"/addresses/{addr}")
                if rec is None:
                    balance = Decimal("0")
                    balance_note = "No on-chain record — balance treated as 0."
                else:
                    balance = Decimal(wei_to_eth(rec.get("coin_balance")) or "0")
            else:
                balance = await _balance_of(chain, coin["contract"], addr,
                                            coin["decimals"])
                if balance is None:
                    balance_note = "balanceOf call failed upstream."
        elif family == "solana":
            if kind == "native":
                lamports = await sol_balance(addr)
                balance = Decimal(lamports) / Decimal(10 ** 9)
            else:
                accts = await sol_token_accounts(addr)
                hit = next((a for a in accts if a.get("mint") == coin["mint"]), None)
                if hit is None:
                    balance = Decimal("0")
                else:
                    dec = hit.get("decimals")
                    raw = hit.get("amount")
                    balance = (Decimal(str(hit.get("ui_amount")))
                               if hit.get("ui_amount") is not None
                               else Decimal(str(raw)) / (Decimal(10) ** int(dec or 0)))
        else:  # bitcoin
            info = await btc_address(addr)
            balance = Decimal(str(info.get("balance_btc") or "0"))
    except UpstreamError as e:
        return _check("holdings", f"Recipient holds {sym}", "unknown",
                      f"Balance lookup failed upstream: {e.msg}")
    except Exception as e:
        return _check("holdings", f"Recipient holds {sym}", "unknown",
                      f"Balance lookup failed: {e}")
    if balance is None:
        return _check("holdings", f"Recipient holds {sym}", "unknown",
                      balance_note or "Balance lookup returned nothing.")
    bal_txt = f"{balance.normalize():f} {sym}"
    if balance > 0:
        detail = f"Recipient already holds {bal_txt}."
    else:
        detail = (f"Recipient holds no {sym} — this would be its first "
                  f"receipt of the coin. Nothing wrong with that, but "
                  f"confirm the address is right.")
    if amount is not None:
        if balance >= amount:
            detail += (f" It already holds at least the {amount.normalize():f} "
                       f"{sym} you plan to send.")
        else:
            detail += (f" It holds less than the {amount.normalize():f} "
                       f"{sym} you plan to send.")
    return _check("holdings", f"Recipient holds {sym}", "pass", detail,
                  balance=str(balance), symbol=sym,
                  amount=str(amount) if amount is not None else None)


def _freeze_tokens_for(coin):
    """(tokens to probe, mode). Each token: {symbol, contract, read_fn}.
    No coin -> USDC + USDT (the two freezable majors). A stablecoin
    with no read function -> its own entry with read_fn None (the
    check reports why it cannot be verified)."""
    if coin is None:
        out = []
        for scope in ("usdcscope", "usdtscope"):
            cfg = STABLECOINS[scope]
            out.append({"symbol": cfg["symbol"], "contract": cfg["contract"],
                        "read_fn": FREEZE_READ_FN[scope], "scope": scope})
        return out, "majors"
    if coin["kind"] in ("stablecoin",):
        return [{"symbol": coin["symbol"], "contract": coin["contract"],
                 "read_fn": FREEZE_READ_FN.get(coin["scope"]),
                 "scope": coin["scope"]}], "coin"
    if coin["kind"] == "token":
        return [{"symbol": coin["symbol"], "contract": coin["contract"],
                 "read_fn": "probe_all", "scope": None}], "coin"
    return [], "none"


async def _probe_token_frozen(chain, token, addr):
    """(frozen_bool_or_None, note). None = cannot be determined for
    this token (no read function / function absent / upstream fail)."""
    fn = token["read_fn"]
    if fn is None:
        scope = token.get("scope")
        if scope == "tusdscope":
            return None, (
                f"{token['symbol']} has a WRITE-only blacklist "
                "(setBlacklisted) with no on-chain read function — "
                "frozen status cannot be verified on-chain."
            )
        if scope in ("daiscope", "usdescope"):
            return None, (
                f"{token['symbol']} has no freeze/blacklist function in "
                "its token ABI — the issuer cannot freeze holder balances."
            )
        return None, f"No freeze read function known for {token['symbol']}."
    if fn == "probe_all":
        for cand in _FREEZE_FNS:
            try:
                v = await _eth_call_bool(chain, token["contract"], cand, addr)
            except UpstreamError:
                return None, f"Freeze probe failed upstream for {token['symbol']}."
            if v is not None:
                return v, f"Read via {cand}()."
        return None, (
            f"No freeze/blacklist read function found on "
            f"{token['symbol']} — the token may not be freezable."
        )
    try:
        v = await _eth_call_bool(chain, token["contract"], fn, addr)
    except UpstreamError:
        return None, f"Freeze probe failed upstream for {token['symbol']}."
    if v is None:
        return None, (
            f"{fn}() is absent or undecodable on {token['symbol']} — "
            "frozen status cannot be verified."
        )
    return v, f"Read via {fn}()."


async def _frozen_check_evm(chain, addr, coin, counterparties):
    tokens, mode = _freeze_tokens_for(coin)
    if not tokens:
        return _check("frozen", "Frozen / blacklisted", "na",
                      "Native-coin transfer — no issuer can freeze the "
                      "native coin. Pick a token coin to check issuer "
                      "freezes.")
    results = []
    worst = "pass"
    details = []
    for token in tokens:
        frozen, note = await _probe_token_frozen(chain, token, addr)
        results.append({"symbol": token["symbol"], "contract": token["contract"],
                        "frozen": frozen, "note": note})
        if frozen is True:
            if mode == "coin":
                worst = "block"
                details.append(
                    f"BLOCK — recipient is FROZEN/BLACKLISTED for "
                    f"{token['symbol']} ({note}) Sending {token['symbol']} "
                    "would fail or the funds would be frozen."
                )
            else:
                worst = "warn" if worst != "block" else worst
                details.append(
                    f"Recipient is frozen/blacklisted for "
                    f"{token['symbol']} ({note}) — do not send "
                    f"{token['symbol']} to it; other coins are unaffected."
                )
        elif frozen is False:
            details.append(
                f"Not frozen for {token['symbol']} ({note})"
            )
        else:
            # Unknown: warn only when it is the coin being sent and
            # the issuer is known to have freeze power (TUSD).
            if mode == "coin" and token.get("scope") == "tusdscope":
                worst = "warn" if worst == "pass" else worst
                details.append(
                    f"Frozen status for {token['symbol']} CANNOT be "
                    f"verified on-chain ({note}) — the issuer can "
                    "blacklist, so treat this as unverified, not clean."
                )
            elif mode == "coin":
                details.append(
                    f"Frozen status for {token['symbol']} unknown: {note}"
                )
            else:
                details.append(
                    f"{token['symbol']}: {note}"
                )
    # Counterparties (bounded, same token set, recipient's recent txs).
    # A checked token's own contract address is excluded: it shows up
    # as a counterparty (it is the `to` of every transfer() call) and
    # its own blacklist status is not a recipient-risk signal — Circle
    # blacklists the USDC contract itself (isBlacklisted(USDC) == true,
    # verified live 2026-11-06), which would otherwise read as a
    # frozen counterparty on every USDC recipient.
    cp_frozen = []
    token_contracts = {t["contract"].lower() for t in tokens}
    cps_to_check = [cp for cp in counterparties
                    if cp.lower() not in token_contracts][:FREEZE_CP_CAP]
    if cps_to_check:
        async def _one(cp):
            out = []
            for token in tokens:
                if token["read_fn"] is None:
                    continue
                try:
                    v, _n = await _probe_token_frozen(chain, token, cp)
                except UpstreamError:
                    v = None
                if v is True:
                    out.append(token["symbol"])
            return cp, out
        try:
            found = await asyncio.gather(
                *[_one(cp) for cp in cps_to_check])
        except Exception:
            found = []
        for cp, syms in found:
            if syms:
                cp_frozen.append({"address": cp, "frozen_for": syms})
    if cp_frozen:
        if worst == "pass":
            worst = "warn"
        shown = ", ".join(
            f"{c['address'][:6]}…{c['address'][-4:]} "
            f"({', '.join(c['frozen_for'])})" for c in cp_frozen[:3])
        details.append(
            f"{len(cp_frozen)} of the {len(cps_to_check)} "
            f"recent counterparties checked are frozen/blacklisted: {shown}."
            + (" (showing 3)" if len(cp_frozen) > 3 else "")
        )
    else:
        details.append(
            f"No frozen/blacklisted address among the "
            f"{len(cps_to_check)} recent "
            f"counterparties checked."
            if cps_to_check else
            "No recent counterparties to check (no transactions scanned)."
        )
    return _check("frozen", "Frozen / blacklisted", worst,
                  " ".join(details), tokens=results,
                  counterparties_checked=len(cps_to_check),
                  counterparties_frozen=cp_frozen)


async def _frozen_check_solana(addr, coin):
    if coin is None or coin["kind"] == "native":
        return _check("frozen", "Frozen / blacklisted", "na",
                      "Native SOL cannot be frozen by an issuer. Pick an "
                      "SPL token (paste its mint address as ?coin=) to "
                      "check freeze status.")
    if coin["kind"] != "spl":
        return _check("frozen", "Frozen / blacklisted", "na",
                      "Freeze check on Solana applies to SPL tokens only.")
    mint = coin["mint"]
    try:
        mint_info = await _sol_rpc(
            SOL_ACCOUNTS_RPC, "getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    except UpstreamError as e:
        return _check("frozen", "Frozen / blacklisted", "unknown",
                      f"Mint lookup failed: {e.msg}")
    mval = (mint_info or {}).get("value") if isinstance(mint_info, dict) else None
    if mval is None:
        return _check("frozen", "Frozen / blacklisted", "unknown",
                      "Mint account not found on Solana.")
    mdata = (mval.get("data") or {})
    minfo = (mdata.get("parsed") or {}).get("info") if isinstance(mdata, dict) else {}
    freeze_auth = (minfo or {}).get("freezeAuthority")
    if freeze_auth is None:
        return _check("frozen", "Frozen / blacklisted", "pass",
                      "This token's mint has NO freeze authority — the "
                      "issuer cannot freeze holder token accounts.",
                      freeze_authority=None)
    try:
        accts = await _sol_rpc(
            SOL_ACCOUNTS_RPC, "getTokenAccountsByOwner",
            [addr, {"mint": mint}, {"encoding": "jsonParsed"}])
    except UpstreamError as e:
        return _check("frozen", "Frozen / blacklisted", "unknown",
                      f"Token-account lookup failed: {e.msg}",
                      freeze_authority=freeze_auth)
    vals = (accts or {}).get("value") if isinstance(accts, dict) else None
    if not vals:
        return _check("frozen", "Frozen / blacklisted", "pass",
                      "Recipient holds no token account for this mint, "
                      "so there is nothing frozen — but the mint DOES "
                      "have a freeze authority "
                      f"({str(freeze_auth)[:8]}…), so a future freeze is "
                      "possible.",
                      freeze_authority=freeze_auth)
    state = None
    for a in vals:
        try:
            state = a["account"]["data"]["parsed"]["info"]["state"]
        except (KeyError, TypeError):
            continue
        if state:
            break
    if state == "frozen":
        return _check("frozen", "Frozen / blacklisted", "block",
                      "BLOCK — the recipient's token account for this "
                      "mint is FROZEN on-chain. Do not send.",
                      freeze_authority=freeze_auth, state=state)
    return _check("frozen", "Frozen / blacklisted", "pass",
                  f"Recipient token account state: {state or 'unknown'}; "
                  f"mint freeze authority exists "
                  f"({str(freeze_auth)[:8]}…) but this account is not frozen.",
                  freeze_authority=freeze_auth, state=state)


# ---------------------------------------------------------------- endpoint
async def preflight(address, coin=None, chain=None, amount=None):
    """Full pre-flight check. Raises UpstreamError(400) for bad input;
    per-check upstream failures become 'unknown' checks, never a 500."""
    family, addr, resolved_from = await _classify(address)
    if family == "evm":
        chain = chain or "ethereum"
        if chain not in CHAINS:
            raise UpstreamError(
                f"Unknown chain '{chain}' (EVM chains: "
                f"{', '.join(CHAINS)})", status=400)
    elif family == "solana":
        if chain is not None and chain != "solana":
            raise UpstreamError("chain must be 'solana' for a Solana address",
                                status=400)
        chain = "solana"
    else:
        if chain is not None and chain != "bitcoin":
            raise UpstreamError("chain must be 'bitcoin' for a Bitcoin address",
                                status=400)
        chain = "bitcoin"
    coin_res = await _resolve_coin(coin, family, chain)
    amount_dec = _parse_amount(amount)

    cache_key = (f"preflight:{family}:{chain}:{addr.lower()}:"
                 f"{(coin or '').lower()}:{(amount or '')}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Sanctions + activity + holdings run in parallel; frozen needs
    # the activity counterparties, so it runs after.
    sanc_task = asyncio.create_task(_sanctions_check(family, chain, addr))
    if family == "evm":
        act_task = asyncio.create_task(_evm_activity(chain, addr))
    elif family == "bitcoin":
        act_task = asyncio.create_task(_btc_activity(addr))
    else:
        act_task = asyncio.create_task(_sol_activity(addr))
    hold_task = asyncio.create_task(
        _holdings_check(family, chain, addr, coin_res, amount_dec))

    (sanc_check, risk), (act_check, cps, _latest, _first, _n), hold_check = (
        await asyncio.gather(sanc_task, act_task, hold_task))

    type_check = await _address_type_check(family, chain, addr)
    if family == "evm":
        frozen_check = await _frozen_check_evm(chain, addr, coin_res, cps)
    elif family == "solana":
        frozen_check = await _frozen_check_solana(addr, coin_res)
    else:
        frozen_check = _check("frozen", "Frozen / blacklisted", "na",
                              "Bitcoin has no issuer freeze — no address "
                              "can be frozen or blacklisted on-chain.")

    checks = [sanc_check, type_check, hold_check, frozen_check, act_check]
    if any(c["status"] == "block" for c in checks):
        verdict = "block"
    elif any(c["status"] == "warn" for c in checks):
        verdict = "warn"
    else:
        verdict = "pass"
    reasons = {
        "block": "Do not send — at least one check blocks this recipient.",
        "warn": "Proceed only after reviewing the warnings below.",
        "pass": "All checks pass — nothing found that blocks this send.",
    }
    out = {
        "address": addr,
        "resolved_from": resolved_from,
        "family": family,
        "chain": chain,
        "chain_label": CHAINS[chain]["label"] if family == "evm" else (
            "Solana" if family == "solana" else "Bitcoin"),
        "coin": coin_res,
        "amount": str(amount_dec) if amount_dec is not None else None,
        "verdict": verdict,
        "verdict_reason": reasons[verdict],
        "checks": checks,
        "risk": risk,
        "method": METHOD,
        "limits": LIMITS,
    }
    _cache_set(cache_key, out, ttl=60)
    return out
