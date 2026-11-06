"""Freeze & blacklist monitor for the *Scope stablecoin apps — keyless.

Feature 10 of the stablecoin analytics set. One response per coin,
in three parts:

  1. Capability — CAN the issuer freeze/blacklist holder balances?
     Reuses feature 8's live eth_call probes wholesale (imported from
     common.payments_readiness: `_freeze_input` — no second probe
     implementation lives here). One correction to feature 8's read:
     TUSD's implementation contract exposes a WRITE-only blacklist
     function, `setBlacklisted(address,bool)`, with no on-chain read
     function, so the read probe could not see it — the implementation
     ABI (fetched live 2026-11-06) proves the issuer CAN blacklist,
     and the `Blacklisted(address,bool)` events below show it has
     been used. That is stated in the capability block, not hidden.
  2. Frozen supply + freeze/blacklist EVENTS — recent on-chain events
     from Blockscout's Etherscan-compatible `getLogs` endpoint
     (the v2 `/addresses/{addr}/logs` endpoint rejects `topic0`
     filters with HTTP 422, probed 2026-11-06 — getLogs is the
     working route). Event signatures come from the live contract
     ABIs fetched 2026-11-06 (implementation contracts for the
     proxies — the proxy ABIs themselves only list AdminChanged /
     Upgraded). Frozen supply is the sum of `balanceOf` over the
     addresses the window's events show as CURRENTLY frozen,
     fetched in ONE Multicall3 `aggregate3` eth_call per 100
     addresses (hand-encoded with the repo's own ABI codec, byte-
     verified against eth-abi in probing — no new dependency).
  3. Sanctions exposure — the vendored OFAC SDN set from
     common/risk.py (514 addresses, rebuilt 2026-11-05 — REUSED,
     never rebuilt) checked against three address sets: the
     currently-frozen addresses, the top-50 holders, and the
     counterparties of the latest 250 transfers. A match is a
     direct SDN listing with its OFAC name/program; "no match"
     means none found in the sets checked, never a clean bill.

Honest limits (stated in every response, never hidden):
- The issuer's blacklist is NOT enumerable on-chain — there is no
  function listing blacklisted addresses. The frozen set is rebuilt
  from freeze events inside the scanned window only (365 days,
  extended to 5 years ONLY for a coin whose freeze signatures exist
  but produced zero events in 365d — TUSD). Addresses frozen
  BEFORE the window are missed, so frozen supply is a LOWER-BOUND
  ESTIMATE, never the total frozen supply. It is labelled as such
  everywhere it appears, including the overview rollup total.
- The events window is the primary chain only (Ethereum for 7
  coins). FDUSD/TUSD hold most of their supply off the primary
  chain (BSC / Tron); freezes on other chains are not covered.
- DAI and USDe have no freeze/blacklist/pause function in their
  token ABIs (DAI's rely/deny are admin authorisation — wards —
  not holder freezes), so there is nothing to enumerate and no
  freeze events to scan; that is reported as not-applicable with
  the evidence, not as zero.
- USDD (Tron): no freeze/blacklist function appears in TronScan's
  published method map (11 observed functions — a PARTIAL list
  built from observed transactions). No freeze events exist to
  scan, and the SDN set holds no Tron-format addresses, so the
  sanctions check is not applicable on Tron — both stated plainly.
- Pause/unpause events are global (they halt ALL transfers, not
  one holder) and are listed separately from per-holder freezes.
- Destroyed black funds (USDT/TUSD `DestroyedBlackFunds`) are
  funds the issuer BURNED out of a blacklisted address — seized,
  not merely frozen. They are reported separately and never added
  to frozen supply (those balances are gone).
"""
import re
import time

import httpx

from common.abi import (
    _encode_args,
    _function_selector,
    keccak_256,
)
from common.chains import CHAINS
from common.clients import (
    HEADERS,
    UpstreamError,
    _cache_get,
    _cache_set,
    bs,
    rpc,
)
from common.payments_readiness import _freeze_input
from common.preflight import (
    FREEZE_READ_FN,
    _classify,
    _frozen_check_solana,
    _probe_token_frozen,
    _resolve_coin,
)
from common.risk import (
    FAMILY_COUNTS,
    OFAC_SDN_COUNT,
    OFAC_SDN_DATE,
    OFAC_SDN_SOURCE,
    lookup,
)
from common.stablecoin import (
    _amount,
    _cfg,
    _coin_detail,
    _current_price,
    _evm_top_holders,
    _num,
    _token_overview,
    _ts_epoch,
)

CACHE_TTL = 300

# Event window: 365 days of blocks (~7,200 blocks/day at 12s).
WINDOW_DAYS = 365
BLOCKS_PER_DAY = 7200
# Extended lookback (5 years) used ONLY when a coin has freeze
# signatures but zero freeze events in the 365d window (TUSD).
EXTENDED_WINDOW_DAYS = 5 * 365

# Multicall3 (immutable, mainnet): batch balanceOf in one eth_call.
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL_CHUNK = 100
# Sequential balanceOf fallback cap (only if Multicall3 fails).
SEQUENTIAL_BALANCE_CAP = 50

MAX_EVENTS_SHOWN = 50
# Sanctions scan: latest N transfers (Blockscout pages of 50).
SANCTION_TRANSFER_PAGES = 5
SANCTION_TRANSFER_PAGE_SIZE = 50

# ---------------------------------------------------------------- event config
# Per-coin freeze/blacklist event signatures, from the LIVE contract
# ABIs fetched 2026-11-06 (implementation contracts for the proxies):
#   USDT  — TetherToken (direct, not a proxy): AddedBlackList /
#           RemovedBlackList / DestroyedBlackFunds / Pause / Unpause.
#   USDC  — FiatTokenV2_2 (impl 0x43506849D7C04F9138D1A2050bbF3A0c054402dd):
#           Blacklisted / UnBlacklisted / Pause / Unpause.
#   PYUSD — PYUSD impl (0x8C35CaA5FD5bDC64b6B11344aD57594A3676256a):
#           FreezeAddress / UnfreezeAddress / FrozenAddressWiped /
#           Pause / Unpause.
#   FDUSD — StablecoinV2 (impl 0xa6b2C3D2910246FB0ADB02e5f6B39e29026e6D50):
#           Freeze(caller, account) / Unfreeze(caller, account) /
#           Paused(address) / Unpaused(address).
#   TUSD  — TrueUSD (impl 0xDBC97a631C2Fee80417D5D69F32B198c8c39c27e):
#           Blacklisted(address, bool) / DestroyedBlackFunds.
#   DAI / USDe — no freeze/blacklist/pause function or event in the
#           token ABI at all (DAI's rely/deny are admin wards).
#   USDD  — Tron; no freeze event to scan (see module docstring).
#
# `addr_topic`: which topic (1-based) carries the affected holder
# address (None for global pause events). `addr_from_data`: the
# address is NOT indexed — it is the first 32-byte word of `data`
# (USDT's 2017-era events declare the parameter without `indexed`,
# so topics carry only topic0; probed live 2026-11-06). `kind`:
# the unified event kind. `state`: what the event sets the
# holder's frozen state to (None = not a holder-state event).
FREEZE_EVENT_CONFIG = {
    "usdtscope": [
        {"sig": "AddedBlackList(address)", "kind": "blacklisted",
         "addr_topic": None, "addr_from_data": True, "state": True},
        {"sig": "RemovedBlackList(address)", "kind": "unblacklisted",
         "addr_topic": None, "addr_from_data": True, "state": False},
        {"sig": "DestroyedBlackFunds(address,uint256)", "kind": "destroyed",
         "addr_topic": None, "state": None},
        {"sig": "Pause()", "kind": "paused", "addr_topic": None, "state": None},
        {"sig": "Unpause()", "kind": "unpaused", "addr_topic": None, "state": None},
    ],
    "usdcscope": [
        {"sig": "Blacklisted(address)", "kind": "blacklisted",
         "addr_topic": 1, "state": True},
        {"sig": "UnBlacklisted(address)", "kind": "unblacklisted",
         "addr_topic": 1, "state": False},
        {"sig": "Pause()", "kind": "paused", "addr_topic": None, "state": None},
        {"sig": "Unpause()", "kind": "unpaused", "addr_topic": None, "state": None},
    ],
    "pyusdsscope": [
        {"sig": "FreezeAddress(address)", "kind": "frozen",
         "addr_topic": 1, "state": True},
        {"sig": "UnfreezeAddress(address)", "kind": "unfrozen",
         "addr_topic": 1, "state": False},
        {"sig": "FrozenAddressWiped(address)", "kind": "wiped",
         "addr_topic": 1, "state": None},
        {"sig": "Pause()", "kind": "paused", "addr_topic": None, "state": None},
        {"sig": "Unpause()", "kind": "unpaused", "addr_topic": None, "state": None},
    ],
    "fdusdsscope": [
        # Freeze(caller, account): topic1 = caller (admin), topic2 =
        # the frozen account.
        {"sig": "Freeze(address,address)", "kind": "frozen",
         "addr_topic": 2, "state": True},
        {"sig": "Unfreeze(address,address)", "kind": "unfrozen",
         "addr_topic": 2, "state": False},
        {"sig": "Paused(address)", "kind": "paused",
         "addr_topic": 1, "state": None},
        {"sig": "Unpaused(address)", "kind": "unpaused",
         "addr_topic": 1, "state": None},
    ],
    "tusdscope": [
        # Blacklisted(address, bool): topic1 = address, data = bool
        # (1 = blacklisted, 0 = unblacklisted).
        {"sig": "Blacklisted(address,bool)", "kind": "blacklist_set",
         "addr_topic": 1, "state": "data_bool"},
        {"sig": "DestroyedBlackFunds(address,uint256)", "kind": "destroyed",
         "addr_topic": 1, "state": None},
    ],
    "daiscope": [],
    "usdescope": [],
    "usddscope": [],
}

# Per-coin ABI evidence for the capability block (live ABI fetch,
# 2026-11-06). `abi_can_freeze` is True where the implementation ABI
# exposes a freeze/blacklist WRITE function even when feature 8's
# read probe found no read function (TUSD).
ABI_NOTES = {
    "usdtscope": "TetherToken ABI: isBlackListed / addBlackList / removeBlackList / destroyBlackFunds / paused — freeze capability confirmed by both the ABI and feature 8's live probe.",
    "usdcscope": "FiatTokenV2_2 implementation ABI: isBlacklisted / blacklist / unBlacklist / paused — freeze capability confirmed by both the ABI and feature 8's live probe.",
    "pyusdsscope": "PYUSD implementation ABI: isFrozen / freeze / unfreeze / wipeFrozenAddress / paused — freeze capability confirmed by both the ABI and feature 8's live probe.",
    "fdusdsscope": "StablecoinV2 implementation ABI: frozen / freeze / unfreeze / paused — freeze capability confirmed by both the ABI and feature 8's live probe.",
    "tusdscope": "TrueUSD implementation ABI exposes setBlacklisted(address,bool) + Blacklisted(address,bool) events + destroyBlackFunds — the issuer CAN blacklist. There is NO on-chain read function for blacklist status, so feature 8's eth_call read probe could not see it and scored freeze risk 100 (no function found). The probe result is a read-probe limitation, not proof of no freeze power — the ABI and the events below prove the power exists and has been used.",
    "daiscope": "Dai token ABI (22 functions) has no freeze/blacklist/pause function and no freeze event. rely/deny are admin authorisation (wards — who may mint/burn), not holder freezes: a denied address loses minting rights, its existing DAI keeps moving.",
    "usdescope": "Ethena USDe token ABI (25 functions) has no freeze/blacklist/pause function and no freeze event.",
    "usddscope": "TronScan's published method map for the USDD contract lists 11 observed functions (approve, burn, deny, mint, move, permit, pull, push, rely, transfer, transferFrom) — DAI-style, no freeze/blacklist/pause. The map is PARTIAL (built from observed transactions), so a rarely-called freeze function could be missing; there is no keyless Tron ABI endpoint to check further.",
}

METHODOLOGY = (
    "Freeze & blacklist monitor (feature 10). Capability reuses "
    "feature 8's live eth_call probes for freeze/blacklist/pause "
    "functions on the primary-chain contract (TronScan method map "
    "for USDD), cross-checked against the live contract ABI "
    "(implementation contract for proxies). Freeze/blacklist events "
    "are on-chain logs on the PRIMARY chain only, from Blockscout's "
    "Etherscan-compatible getLogs endpoint over the last 365 days "
    "(5-year lookback only when a coin has freeze signatures but "
    "zero events in 365d — TUSD). The currently-frozen set is "
    "rebuilt by applying those events in order (latest event per "
    "address wins). Frozen supply is the sum of balanceOf over the "
    "currently-frozen addresses, batched through Multicall3 "
    "aggregate3 (one eth_call per 100 addresses) at the live price. "
    "THE ISSUER'S BLACKLIST IS NOT ENUMERABLE ON-CHAIN, so "
    "addresses frozen before the scanned window are missed and "
    "frozen supply is a LOWER-BOUND ESTIMATE, never the total "
    "frozen supply. Destroyed black funds are burned out of "
    "blacklisted addresses (seized) and are reported separately, "
    "never added to frozen supply. Sanctions exposure checks the "
    "vendored OFAC SDN set (514 addresses, 2026-11-05) against the "
    "currently-frozen addresses, the top-50 holders, and the "
    "counterparties of the latest 250 transfers; a match is a direct "
    "SDN listing with its OFAC name/program. No match means none "
    "found in the sets checked — not a clean bill. All figures are "
    "primary-chain only; FDUSD/TUSD hold most supply off the "
    "primary chain, so freezes on other chains are not covered."
)

FREEZE_METHODOLOGY_SHORT = (
    "Frozen supply is a LOWER-BOUND ESTIMATE: the blacklist is not "
    "enumerable on-chain, so it sums balances of addresses the "
    "scanned window's freeze events show as currently frozen; "
    "addresses frozen before the window are missed."
)

_EVM_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")

# Solana mints for the two freezable stablecoin majors (same
# canonical mints as common/quotes.py's token registry — verified
# live 2026-11-06: both mints exist and carry a freeze authority).
SOL_STABLE_MINTS = {
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}

# Holder-state event kinds (the kinds that set an address frozen /
# not-frozen). Used to decide the extended 5y lookback.
HOLDER_EVENT_KINDS = (
    "blacklisted", "unblacklisted", "frozen", "unfrozen", "blacklist_set",
)

WATCH_METHODOLOGY = (
    "Freeze / blacklist watch (phase 2, Q6). Current frozen state "
    "comes from the coin's on-chain freeze/blacklist READ function "
    "via a live eth_call (USDC isBlacklisted, USDT isBlackListed, "
    "PYUSD isFrozen, FDUSD frozen — same probes as the pre-flight "
    "check, reused). Freeze/blacklist EVENTS naming the address come "
    "from Blockscout's Etherscan-compatible getLogs over the same "
    "window as /freeze (365 days, extended to 5 years only when the "
    "coin has freeze signatures but zero holder-state events in "
    "365d — TUSD). TUSD has a WRITE-only blacklist "
    "(setBlacklisted) with no on-chain read function, so its current "
    "state is derived from its last freeze event, labelled "
    "event-derived, never presented as live-verified. DAI/USDe have "
    "no freeze function, so an address cannot be frozen for them. "
    "USDD is Tron-native: no EVM freeze check applies. On Solana, "
    "the check is the SPL mint's freeze authority plus the token "
    "account's current state (frozen / initialized) via the public "
    "Solana RPC — Solana has no freeze-event scan here, so the "
    "answer is current-state only. The issuer blacklist is NOT "
    "enumerable on-chain: a 'not frozen' answer means the live read "
    "probe says not frozen AND no freeze event in the scanned window "
    "names the address — it cannot prove the address was never "
    "frozen before the window."
)

WATCH_LIMITS = [
    "The issuer blacklist is not enumerable on-chain — 'not frozen' "
    "means the live read probe says not frozen and no freeze event "
    "in the scanned window names the address; it cannot prove the "
    "address was never frozen before the window.",
    "TUSD's frozen state is event-derived (no on-chain read "
    "function exists), not live-verified.",
    "Solana freeze status is current-state only — there is no "
    "freeze-event scan on Solana.",
    "A frozen verdict is a screening signal, not a legal "
    "determination; it does not say why the issuer froze the "
    "address.",
]


def _topic(sig):
    return "0x" + keccak_256(sig.encode()).hex()


def _topic_address(topic):
    """Indexed address topic (32-byte word) -> 0x address."""
    if not topic or len(topic) < 42:
        return None
    return "0x" + topic[-40:]


def _hex_int(v, default=None):
    try:
        return int(str(v), 16)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- getLogs
async def _getlogs(contract, topic0, from_block, to_block):
    """Blockscout Etherscan-compatible getLogs for one topic0 over a
    block range. Pages (offset=1000) until a short page. Returns
    (logs, status, status_note) — status ok / partial / unavailable.
    A non-list `result` (Etherscan-style error string) is unavailable.
    """
    root = CHAINS["ethereum"]["blockscout"].removesuffix("/api/v2") + "/api"
    logs = []
    pages_ok = 0
    stopped_early = False
    for page in range(1, 4):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0), headers=HEADERS,
                follow_redirects=True,
            ) as c:
                r = await c.get(root, params={
                    "module": "logs", "action": "getLogs",
                    "address": contract, "topic0": topic0,
                    "fromBlock": str(from_block), "toBlock": str(to_block),
                    "page": str(page), "offset": "1000",
                })
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError):
            stopped_early = True
            break
        if r.status_code != 200:
            stopped_early = True
            break
        try:
            d = r.json()
        except Exception:
            stopped_early = True
            break
        res = d.get("result") if isinstance(d, dict) else None
        if not isinstance(res, list):
            stopped_early = True
            break
        pages_ok += 1
        logs.extend(res)
        if len(res) < 1000:
            break
    if pages_ok == 0:
        return logs, "unavailable", (
            "Blockscout's getLogs query failed for this event — no "
            "events of this type could be retrieved."
        )
    if stopped_early:
        return logs, "partial", (
            "The log scan stopped early (upstream error) — the event "
            "list may be incomplete."
        )
    return logs, "ok", None


async def _latest_block():
    """Current Ethereum block number, cached 60s."""
    key = "freeze:latestBlock"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    v = int(await rpc("ethereum", "eth_blockNumber"), 16)
    _cache_set(key, v, ttl=60)
    return v


# ---------------------------------------------------------------- multicall
def _encode_aggregate3(calls):
    """Calldata for Multicall3.aggregate3((address,bool,bytes)[])
    with allowFailure=true for every call. Hand-encoded with the
    repo's own ABI codec — byte-verified against eth-abi 2026-11-06.
    Layout: selector + offset(0x20) + length + element offsets
    (relative to the offsets section, i.e. right after the length
    word) + tuple tails (address + offset 0x40 + len + padded data).
    """
    sel = _function_selector("aggregate3", ["(address,bool,bytes)[]"])
    n = len(calls)
    tails = b""
    offsets = []
    pos = n * 32
    for (tgt, data) in calls:
        offsets.append(pos)
        padded = data + b"\x00" * ((32 - len(data) % 32) % 32)
        tails += (
            bytes.fromhex(tgt[2:].rjust(64, "0"))
            + (1).to_bytes(32, "big")  # allowFailure = true
            + (96).to_bytes(32, "big")  # offset to bytes, relative to tuple start
            + len(data).to_bytes(32, "big")
            + padded
        )
        pos += 32 + 32 + 32 + 32 + len(padded)
    arr = (
        n.to_bytes(32, "big")
        + b"".join(o.to_bytes(32, "big") for o in offsets)
        + tails
    )
    return sel + (32).to_bytes(32, "big") + arr


def _decode_aggregate3(raw):
    """Decode aggregate3's return: a bare dynamic array of
    (bool success, bytes returnData) tuples — the deployed
    Multicall3 returns the Result array directly (verified live
    2026-11-06: first word is the array offset 0x20, NOT a block
    number — decoding it as (uint256, array) reads the length word
    as 96 and fails every batch). Returns
    [(success, return_bytes_or_None)] or None on bad data."""
    try:
        if len(raw) < 32:
            return None
        base = int.from_bytes(raw[0:32], "big")
        if base + 32 > len(raw):
            return None
        n = int.from_bytes(raw[base:base + 32], "big")
        if n > 10000 or len(raw) < base + 32 + n * 32:
            return None
        out = []
        for i in range(n):
            o = int.from_bytes(raw[base + 32 + i * 32: base + 64 + i * 32], "big")
            p = base + 32 + o
            if p + 64 > len(raw):
                return None
            success = raw[p + 31] != 0
            data_off = int.from_bytes(raw[p + 32:p + 64], "big")
            dp = p + data_off
            if dp + 32 > len(raw):
                return None
            ln = int.from_bytes(raw[dp:dp + 32], "big")
            out.append((success, raw[dp + 32:dp + 32 + ln] if ln <= 10 ** 6 else None))
        return out
    except (IndexError, ValueError):
        return None


async def _multicall_balances(token, addresses):
    """balanceOf for many addresses via Multicall3 aggregate3
    (allowFailure=true), chunked at MULTICALL_CHUNK. Returns
    ({address_lower: balance_raw_int}, status, status_note). A
    failed sub-call yields None for that address (counted as
    unknown, not zero). Raises UpstreamError only if every chunk
    fails at the transport level."""
    out = {}
    failed_chunks = 0
    for i in range(0, len(addresses), MULTICALL_CHUNK):
        chunk = addresses[i:i + MULTICALL_CHUNK]
        calls = []
        for a in chunk:
            data = _function_selector("balanceOf", ["address"]) + _encode_args(
                ["address"], [a]
            )
            calls.append((token, bytes(data)))
        payload = "0x" + _encode_aggregate3(calls).hex()
        try:
            res = await rpc("ethereum", "eth_call",
                            [{"to": MULTICALL3, "data": payload}, "latest"])
        except UpstreamError:
            failed_chunks += 1
            continue
        if not res or not isinstance(res, str) or not res.startswith("0x"):
            failed_chunks += 1
            continue
        dec = _decode_aggregate3(bytes.fromhex(res[2:]))
        if dec is None or len(dec) != len(chunk):
            failed_chunks += 1
            continue
        for addr, (ok, ret) in zip(chunk, dec):
            if ok and ret is not None and len(ret) == 32:
                out[addr.lower()] = int.from_bytes(ret, "big")
            else:
                out[addr.lower()] = None
    if failed_chunks and not out:
        raise UpstreamError("Multicall3 batch balance query failed")
    note = None
    if failed_chunks:
        note = (
            f"{failed_chunks} Multicall3 chunk(s) failed — balances "
            "for those addresses are unknown, not zero."
        )
    return out, ("partial" if failed_chunks else "ok"), note


async def _single_balance(token, addr):
    """One eth_call balanceOf. None on any failure."""
    try:
        data = _function_selector("balanceOf", ["address"]) + _encode_args(
            ["address"], [addr]
        )
        res = await rpc("ethereum", "eth_call",
                        [{"to": token, "data": "0x" + data.hex()}, "latest"])
        if not res or not isinstance(res, str) or not res.startswith("0x"):
            return None
        return int(res, 16)
    except UpstreamError:
        return None


# ---------------------------------------------------------------- events
def _parse_log(log, spec):
    """One getLogs row -> unified event dict (or None if unparseable).
    Timestamps are hex seconds; block numbers hex."""
    topics = log.get("topics") or []
    if not topics or topics[0] is None:
        return None
    ts = _hex_int(log.get("timeStamp"))
    addr = None
    data = log.get("data") or "0x"
    if spec.get("addr_topic"):
        idx = spec["addr_topic"]
        if idx < len(topics) and topics[idx]:
            addr = _topic_address(topics[idx])
    elif spec.get("addr_from_data") and len(data) >= 66:
        # Non-indexed address parameter: first 32-byte data word.
        addr = "0x" + data[26:66]
    amount_raw = None
    state = spec.get("state")
    if spec["kind"] == "destroyed":
        if len(data) >= 130:
            # USDT-style: neither parameter indexed — data holds
            # (address, amount) as two 32-byte words.
            addr = "0x" + data[26:66]
            amount_raw = int(data[66:130], 16)
        elif len(data) >= 66:
            # TUSD-style: the address is indexed (topic1, set above)
            # and the amount is the single 32-byte data word.
            amount_raw = int(data[2:66], 16)
    elif spec["kind"] == "blacklist_set":
        # data = bool word: 1 = blacklisted, 0 = unblacklisted.
        state = _hex_int(data, 0) != 0
    return {
        "timestamp": ts,
        "kind": spec["kind"],
        "event": spec["sig"],
        "address": addr,
        "amount_raw": amount_raw,
        "state": state,
        "tx_hash": log.get("transactionHash"),
        "block": _hex_int(log.get("blockNumber")),
    }


async def _scan_freeze_events(cfg, from_block, to_block):
    """Scan all freeze/blacklist/pause event signatures for a coin.
    Returns (events_newest_first, counts_by_kind, status, status_note,
    frozen_state) where frozen_state maps address -> {frozen, since}
    from applying holder-state events in chronological order."""
    specs = FREEZE_EVENT_CONFIG.get(cfg["_scope"], [])
    events = []
    counts = {}
    statuses = []
    notes = []
    for spec in specs:
        logs, st, note = await _getlogs(
            cfg["contract"], _topic(spec["sig"]), from_block, to_block
        )
        statuses.append(st)
        if note:
            notes.append(f"{spec['sig']}: {note}")
        for log in logs:
            ev = _parse_log(log, spec)
            if ev is not None:
                events.append(ev)
                counts[ev["kind"]] = counts.get(ev["kind"], 0) + 1
    events.sort(key=lambda e: (e["timestamp"] or 0, e["block"] or 0))
    # Currently-frozen set: apply holder-state events in order;
    # latest event per address wins. `since` = first freeze observed
    # inside the window (an address frozen before the window and
    # never re-frozen inside it is missed — see module docstring).
    frozen_state = {}
    for ev in events:
        if ev["state"] is None or not ev["address"]:
            continue
        addr = ev["address"].lower()
        if ev["state"]:
            if addr not in frozen_state or not frozen_state[addr]["frozen"]:
                frozen_state[addr] = {"frozen": True, "since": ev["timestamp"]}
            else:
                frozen_state[addr]["frozen"] = True
        else:
            if addr in frozen_state:
                frozen_state[addr]["frozen"] = False
    frozen_state = {a: v for a, v in frozen_state.items() if v["frozen"]}
    events_desc = sorted(
        events, key=lambda e: (-(e["timestamp"] or 0), -(e["block"] or 0))
    )
    if "unavailable" in statuses:
        status = "partial" if events else "unavailable"
    elif "partial" in statuses:
        status = "partial"
    else:
        status = "ok"
    status_note = "; ".join(notes) if notes else None
    return events_desc, counts, status, status_note, frozen_state


# ---------------------------------------------------------------- sanctions
async def _sanctions_scan(cfg, frozen_addresses):
    """Check the OFAC SDN set against three address sets. EVM only:
    the vendored set holds 94 EVM-format addresses and no
    Tron-format ones, so for USDD this returns not_applicable.
    Returns the sanctions block."""
    if cfg["primary"] != "ethereum":
        return {
            "status": "not_applicable",
            "summary": (
                "Sanctions check not applicable on Tron: the vendored "
                "OFAC SDN set holds 94 EVM-format, 260 Bitcoin and 2 "
                "Solana addresses — no Tron-format addresses — so "
                "TronScan's base58 holder/transfer addresses cannot "
                "match it. No Tron sanctions screening is performed."
            ),
            "matches": [],
            "matches_total": 0,
            "coverage": {
                "frozen_addresses_checked": 0,
                "top_holders_checked": 0,
                "transfers_scanned": 0,
                "counterparties_checked": 0,
            },
            "sdn": {
                "source": OFAC_SDN_SOURCE,
                "date": OFAC_SDN_DATE,
                "labels_total": OFAC_SDN_COUNT,
                "labels_for_family": FAMILY_COUNTS["evm"],
            },
            "method": (
                "Heuristic screening signal, not a legal "
                "determination. A low score does not prove an address "
                "is clean — it means no SDN match was found in the "
                "transactions checked."
            ),
        }
    matches = {}
    counterparties = set()
    transfers_scanned = 0
    earliest = latest = None
    holders_checked = 0
    transfer_status = "ok"
    transfer_note = None

    def _check(addr, context, extra=None):
        if not addr:
            return
        e = lookup(addr)
        if e is None:
            return
        key = addr.lower()
        if key not in matches:
            matches[key] = {
                "address": addr,
                "name": e["name"],
                "program": e["program"],
                "currency": e["currency"],
                "contexts": [],
            }
        if context not in matches[key]["contexts"]:
            matches[key]["contexts"].append(context)
        if extra:
            matches[key].update(extra)

    # (a) Currently-frozen addresses.
    for a in frozen_addresses:
        _check(a, "frozen_address")
    # (b) Top-50 holders.
    try:
        holders = await _evm_top_holders(cfg)
        holders_checked = len(holders)
        for h in holders:
            _check(h.get("address"), "top_holder",
                   {"balance": h.get("balance")} if h.get("balance") is not None else None)
    except UpstreamError:
        transfer_note = "Top-holder list unavailable — holder check skipped."
    # (c) Counterparties of the latest transfers (5 pages x 50).
    params = None
    for _ in range(SANCTION_TRANSFER_PAGES):
        try:
            d = await bs("ethereum",
                         f"/tokens/{cfg['contract']}/transfers",
                         params=params)
        except UpstreamError:
            transfer_status = "partial"
            transfer_note = (
                "The recent-transfers feed failed part-way — the "
                "counterparty check is partial."
            )
            break
        if d is None:
            transfer_status = "partial"
            break
        rows = d.get("items") or []
        if not rows:
            break
        for tt in rows:
            transfers_scanned += 1
            frm = (tt.get("from") or {}).get("hash")
            to = (tt.get("to") or {}).get("hash")
            for a in (frm, to):
                if a:
                    counterparties.add(a.lower())
                    _check(a, "transfer_counterparty")
            ts = _ts_epoch((tt.get("timestamp") if isinstance(tt.get("timestamp"), (int, float, str)) else None))
            if ts is not None:
                earliest = ts if earliest is None or ts < earliest else earliest
                latest = ts if latest is None or ts > latest else latest
        npp = d.get("next_page_params")
        if not npp:
            break
        params = npp
    mlist = sorted(matches.values(), key=lambda m: m["address"])
    if mlist:
        summary = (
            f"{len(mlist)} sanctioned address(es) found among the "
            "checked sets: "
            + "; ".join(
                f"{m['address'][:10]}…{m['address'][-4:]} ({m['name']}, "
                f"{m['program']}; {', '.join(m['contexts'])})"
                for m in mlist[:5]
            )
            + (f"; plus {len(mlist) - 5} more." if len(mlist) > 5 else ".")
        )
    else:
        summary = (
            "No OFAC SDN match among the checked sets — no sanctioned "
            "address holds the coin in the top 50, appears among the "
            "currently-frozen addresses, or appears among the "
            f"counterparties of the latest {transfers_scanned} "
            "transfers. That is a statement about the sets checked, "
            "not a clean bill for the coin."
        )
    return {
        "status": transfer_status,
        "summary": summary,
        "matches": mlist,
        "matches_total": len(mlist),
        "coverage": {
            "frozen_addresses_checked": len(frozen_addresses),
            "top_holders_checked": holders_checked,
            "transfers_scanned": transfers_scanned,
            "counterparties_checked": len(counterparties),
            "transfer_window_earliest": earliest,
            "transfer_window_latest": latest,
            "transfer_note": transfer_note,
        },
        "sdn": {
            "source": OFAC_SDN_SOURCE,
            "date": OFAC_SDN_DATE,
            "labels_total": OFAC_SDN_COUNT,
            "labels_for_family": FAMILY_COUNTS["evm"],
        },
        "method": (
            "Heuristic screening signal, not a legal determination. "
            "A low score does not prove an address is clean — it "
            "means no SDN match was found in the transactions checked."
        ),
    }


# ---------------------------------------------------------------- window scan
async def _scan_window(cfg):
    """Shared window scan for sc_freeze and the Q6 watch check:
    freeze/blacklist/pause events over the 365d window (extended to
    5 years ONLY when the coin has freeze signatures but zero
    holder-state events in 365d — TUSD). Returns (events_desc,
    counts, ev_status, ev_note, frozen_state, window). Tron /
    no-freeze-function coins return empty events with a
    not_applicable status and an empty window."""
    specs = FREEZE_EVENT_CONFIG.get(cfg["_scope"], [])
    if cfg["primary"] == "ethereum" and specs:
        latest_block = await _latest_block()
        window_days = WINDOW_DAYS
        from_block = max(0, latest_block - window_days * BLOCKS_PER_DAY)
        events, counts, ev_status, ev_note, frozen_state = await _scan_freeze_events(
            cfg, from_block, latest_block
        )
        holder_events = sum(
            counts.get(k, 0) for k in HOLDER_EVENT_KINDS
        )
        extended = False
        if holder_events == 0:
            from_block_5y = max(0, latest_block - EXTENDED_WINDOW_DAYS * BLOCKS_PER_DAY)
            events, counts, ev_status, ev_note, frozen_state = await _scan_freeze_events(
                cfg, from_block_5y, latest_block
            )
            window_days = EXTENDED_WINDOW_DAYS
            extended = True
        window = {
            "window_days": window_days,
            "window_start_block": from_block if not extended else from_block_5y,
            "window_end_block": latest_block,
            "extended_lookback": extended,
            "extended_reason": (
                "No freeze/blacklist events in the 365d window, so the "
                "scan was extended to 5 years to find the last use."
                if extended else None
            ),
        }
        return events, counts, ev_status, ev_note, frozen_state, window
    if cfg["primary"] == "tron":
        return [], {}, "not_applicable", (
            "USDD is Tron-native: no freeze/blacklist function appears "
            "in TronScan's published method map (11 observed functions, "
            "partial list), so there are no freeze events to scan. "
            "TronScan exposes no keyless per-contract event feed for "
            "this check."
        ), {}, {"window_days": None, "window_start_block": None,
                "window_end_block": None, "extended_lookback": False,
                "extended_reason": None}
    return [], {}, "not_applicable", (
        "This coin has no freeze/blacklist/pause function in its "
        "token ABI, so there are no freeze events to scan — "
        "reported as not applicable, not as zero."
    ), {}, {"window_days": None, "window_start_block": None,
            "window_end_block": None, "extended_lookback": False,
            "extended_reason": None}


# ---------------------------------------------------------------- entry
async def sc_freeze(scope):
    """GET /api/<scope>/freeze — feature 10: freeze capability
    (feature-8 probe reused), frozen supply (lower-bound estimate),
    freeze/blacklist events, destroyed black funds, and sanctions
    exposure. Cached 300s."""
    cfg = dict(_cfg(scope))
    cfg["_scope"] = scope
    specs = FREEZE_EVENT_CONFIG.get(scope, [])
    cache_key = f"scFreeze:{scope}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    detail = await _coin_detail(cfg)
    price, _ = await _current_price(cfg, detail)

    # --- capability: feature 8's probe, reused verbatim ---
    try:
        freeze_score, freeze_ev = await _freeze_input(cfg)
    except Exception as e:
        freeze_score, freeze_ev = None, {
            "note": f"Freeze probe failed: {e}",
            "freeze_functions_found": [],
            "pause_function_found": False,
        }
    probe_fns = freeze_ev.get("freeze_functions_found") or []
    abi_note = ABI_NOTES.get(scope, "")
    # TUSD: the read probe sees nothing, but the implementation ABI
    # proves a write-only blacklist exists. Capability follows the
    # ABI evidence, with the discrepancy stated, not smoothed over.
    abi_can_freeze = scope in (
        "usdtscope", "usdcscope", "pyusdsscope", "fdusdsscope", "tusdscope",
    )
    can_freeze = bool(probe_fns) or abi_can_freeze
    if scope == "tusdscope":
        probe_note = (
            "Feature 8's eth_call read probe found NO freeze/blacklist "
            "function (score 100) — because there is no on-chain READ "
            "function to probe. The TrueUSD implementation ABI exposes "
            "setBlacklisted(address,bool), so the issuer CAN blacklist; "
            "the probe result must not be read as 'cannot freeze'."
        )
    else:
        probe_note = freeze_ev.get("note")
    capability = {
        "can_freeze": can_freeze,
        "method": freeze_ev.get("method"),
        "probe_score": freeze_score,
        "probe_functions_found": probe_fns,
        "pause_function_found": bool(freeze_ev.get("pause_function_found")),
        "probe_note": probe_note,
        # The feature-8 probe note (quoted verbatim above) says
        # observed freeze events are "not tracked yet" — true when
        # feature 8 shipped, stale now. This field corrects it in
        # this feature's own response, without editing feature 8.
        "events_tracked": (
            "Observed freeze/blacklist events ARE tracked now — see "
            "the events table below (feature 10, this endpoint). The "
            "feature-8 probe note above predates feature 10."
        ),
        "abi_note": abi_note,
        "functions_probed": freeze_ev.get("probed_functions"),
    }

    # --- events + currently-frozen set (shared window scan) ---
    (events, counts, ev_status, ev_note, frozen_state,
     window) = await _scan_window(cfg)

    # --- frozen supply (lower-bound estimate) ---
    frozen_addrs = sorted(frozen_state.keys())
    frozen_block = {"status": None, "note": None, "amount": None,
                    "amount_usd": None, "addresses_count": 0,
                    "addresses_with_balance": 0,
                    "addresses_unknown": 0,
                    "share_of_primary_supply_pct": None,
                    "denominator": None, "denominator_source": None,
                    "top_frozen_holders": []}
    if cfg["primary"] == "ethereum" and specs:
        frozen_block["addresses_count"] = len(frozen_addrs)
        if frozen_addrs:
            balances = {}
            bal_status = "ok"
            bal_note = None
            try:
                balances, bal_status, bal_note = await _multicall_balances(
                    cfg["contract"], frozen_addrs
                )
            except UpstreamError:
                # Fallback: sequential balanceOf, capped.
                balances = {}
                for a in frozen_addrs[:SEQUENTIAL_BALANCE_CAP]:
                    v = await _single_balance(cfg["contract"], a)
                    balances[a.lower()] = v
                bal_status = "partial"
                bal_note = (
                    "Multicall3 batch query failed — balances checked "
                    f"one by one, capped at {SEQUENTIAL_BALANCE_CAP} "
                    "addresses."
                )
            nonzero = {a: v for a, v in balances.items()
                       if v is not None and v > 0}
            unknown = [a for a, v in balances.items() if v is None]
            missing = [a for a in frozen_addrs
                       if a.lower() not in balances]
            total_raw = sum(nonzero.values())
            amount = float(_amount(total_raw, cfg["decimals"] or 0) or 0)
            frozen_block.update({
                "status": "estimate_lower_bound",
                "amount": amount,
                "amount_usd": round(amount * price, 2) if price else None,
                "addresses_with_balance": len(nonzero),
                "addresses_unknown": len(unknown) + len(missing),
                "balance_status": bal_status,
                "top_frozen_holders": sorted(
                    ({"address": a,
                      "balance": float(_amount(v, cfg["decimals"] or 0) or 0),
                      "balance_usd": round(
                          float(_amount(v, cfg["decimals"] or 0) or 0) * price, 2)
                      if price else None,
                      "frozen_since": frozen_state.get(a, {}).get("since")}
                     for a, v in nonzero.items()),
                    key=lambda h: -h["balance"],
                )[:10],
                "note": (
                    FREEZE_METHODOLOGY_SHORT
                    + (f" {bal_note}" if bal_note else "")
                    + (f" {len(missing)} frozen address(es) not "
                       "balance-checked (cap/failure) — excluded, not "
                       "counted as zero." if missing else "")
                ),
            })
            # Share of primary-chain supply (Blockscout total_supply).
            try:
                tok = await _token_overview(cfg)
                denom = _num((tok or {}).get("total_supply"))
                if denom:
                    frozen_block["denominator"] = denom
                    frozen_block["denominator_source"] = (
                        "Blockscout total_supply on the primary chain "
                        "(Ethereum)"
                    )
                    frozen_block["share_of_primary_supply_pct"] = round(
                        amount / denom * 100.0, 4)
            except UpstreamError:
                pass
        else:
            frozen_block.update({
                "status": "none_observed",
                "note": (
                    "No address is shown as currently frozen by the "
                    "freeze events in the scanned window"
                    + (f" ({window['window_days']} days)" if window["window_days"] else "")
                    + ". That is a statement about the window, not "
                    "proof that no funds are frozen — addresses frozen "
                    "before the window are invisible to this scan."
                ),
            })
    elif cfg["primary"] == "tron":
        frozen_block.update({
            "status": "not_applicable",
            "note": (
                "No freeze function observed for USDD (TronScan method "
                "map, partial list), so no frozen supply can be "
                "enumerated."
            ),
        })
    else:
        frozen_block.update({
            "status": "not_applicable",
            "note": (
                "This coin has no freeze/blacklist function, so there "
                "is no frozen supply to measure."
            ),
        })

    # --- destroyed black funds (seized, not frozen) ---
    destroyed_events = [e for e in events if e["kind"] == "destroyed"]
    destroyed = None
    if any(s["kind"] == "destroyed" for s in specs):
        total_raw = sum(e["amount_raw"] or 0 for e in destroyed_events)
        total = float(_amount(total_raw, cfg["decimals"] or 0) or 0)
        destroyed = {
            "events_count": len(destroyed_events),
            "total_amount": total,
            "total_usd": round(total * price, 2) if price else None,
            "window_days": window["window_days"],
            "note": (
                "Funds the issuer BURNED out of blacklisted addresses "
                "(DestroyedBlackFunds) in the scanned window — seized, "
                "not merely frozen. Never added to frozen supply: "
                "those balances no longer exist."
            ),
        }
    else:
        destroyed = {
            "events_count": None,
            "total_amount": None,
            "total_usd": None,
            "window_days": window["window_days"],
            "note": (
                "This coin's contract has no destroy-black-funds "
                "function (USDC/PYUSD/FDUSD cannot burn frozen "
                "balances — frozen funds stay frozen)."
            ),
        }

    # --- sanctions exposure ---
    sanctions = await _sanctions_scan(cfg, frozen_addrs)

    out = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "decimals": cfg["decimals"],
        "price_usd": price,
        "capability": capability,
        "frozen_supply": frozen_block,
        "events": {
            "window": window,
            "status": ev_status,
            "status_note": ev_note,
            "counts_by_kind": counts,
            "total_events": sum(counts.values()),
            "events_shown": min(len(events), MAX_EVENTS_SHOWN),
            "events": [
                {
                    "timestamp": e["timestamp"],
                    "kind": e["kind"],
                    "event": e["event"],
                    "address": e["address"],
                    "amount": (
                        float(_amount(e["amount_raw"], cfg["decimals"] or 0) or 0)
                        if e["amount_raw"] is not None else None
                    ),
                    "amount_usd": (
                        round(float(_amount(e["amount_raw"], cfg["decimals"] or 0) or 0) * price, 2)
                        if e["amount_raw"] is not None and price else None
                    ),
                    "tx_hash": e["tx_hash"],
                    "block": e["block"],
                }
                for e in events[:MAX_EVENTS_SHOWN]
            ],
        },
        "destroyed_black_funds": destroyed,
        "sanctions": sanctions,
        "methodology": METHODOLOGY,
        "as_of": int(time.time()),
        "sources": [
            "Live eth_call probes on the primary-chain contract (feature 8, reused) + live contract ABI (Blockscout smart-contracts endpoint, implementation contract for proxies)",
            "Blockscout Etherscan-compatible getLogs (freeze/blacklist/pause events, primary chain)",
            "Multicall3 aggregate3 (batch balanceOf of currently-frozen addresses)",
            "Blockscout v2 token holders + transfers (sanctions counterparty scan, primary chain)",
            "OFAC SDN list via common/risk.py (vendored set, 514 addresses, 2026-11-05)",
            "DeFiLlama stablecoins API (live price)",
        ],
    }
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out

# ---------------------------------------------------------------- Q6 watch
# Freeze / blacklist watch (phase 2, Q6): is THIS address frozen for
# THIS coin, and does any freeze event name it? The read probe is the
# live truth for "currently frozen"; the event scan dates it and
# catches coins with no read function (TUSD). The window scan is the
# same one /freeze uses (365d, 5y extended lookback for silent
# coins), so a watch answer and the /freeze page never disagree
# about the window.


def _watch_event_row(e, cfg, price):
    """One freeze event naming the watched address, shaped like the
    /freeze events table."""
    return {
        "timestamp": e["timestamp"],
        "kind": e["kind"],
        "event": e["event"],
        "address": e["address"],
        "amount": (
            float(_amount(e["amount_raw"], cfg["decimals"] or 0) or 0)
            if e["amount_raw"] is not None else None
        ),
        "amount_usd": (
            round(float(_amount(e["amount_raw"], cfg["decimals"] or 0) or 0) * price, 2)
            if e["amount_raw"] is not None and price else None
        ),
        "tx_hash": e["tx_hash"],
        "block": e["block"],
    }


async def _evm_watch_one(cfg, address):
    """Watch check for one EVM stablecoin scope. `cfg` carries
    `_scope`. Returns the per-coin watch block (no caching here —
    the callers cache)."""
    scope = cfg["_scope"]
    symbol = cfg["symbol"]
    addr = address.lower()
    detail = await _coin_detail(cfg)
    price, _ = await _current_price(cfg, detail)

    # Current balance (how much would be frozen, if frozen).
    balance_raw = await _single_balance(cfg["contract"], address)
    balance = (
        float(_amount(balance_raw, cfg["decimals"] or 0) or 0)
        if balance_raw is not None else None
    )
    balance_block = {
        "amount": balance,
        "amount_usd": round(balance * price, 2) if balance is not None and price else None,
        "known": balance_raw is not None,
    }

    # DAI / USDe: no freeze function at all — cannot be frozen.
    if not FREEZE_EVENT_CONFIG.get(scope):
        return {
            "scope": scope,
            "symbol": symbol,
            "name": cfg["name"],
            "issuer": cfg["issuer"],
            "primary_chain": cfg["primary"],
            "contract": cfg["contract"],
            "chain": "ethereum",
            "frozen": False,
            "verdict": "not_freezable",
            "summary": (
                f"{symbol} has no freeze/blacklist function in its "
                "token ABI — the issuer cannot freeze holder balances, "
                "so this address cannot be frozen for "
                f"{symbol}."
            ),
            "read_probe": {"function": None, "frozen": None,
                           "note": "No freeze read function exists for this coin."},
            "events": {"window": None, "status": "not_applicable",
                       "status_note": "No freeze function — no freeze events exist to scan.",
                       "total_events_naming_address": 0, "events": []},
            "balance": balance_block,
            "methodology": WATCH_METHODOLOGY,
            "limits": WATCH_LIMITS,
            "as_of": int(time.time()),
        }

    # Live read probe (reused from preflight) + window event scan.
    token = {"symbol": symbol, "contract": cfg["contract"],
             "read_fn": FREEZE_READ_FN.get(scope), "scope": scope}
    probe_frozen, probe_note = await _probe_token_frozen("ethereum", token, address)
    (events, _counts, ev_status, ev_note, frozen_state,
     window) = await _scan_window(cfg)
    mine = [e for e in events if (e.get("address") or "").lower() == addr]
    holder_mine = [e for e in mine if e["kind"] in HOLDER_EVENT_KINDS]
    last_holder = holder_mine[-1] if holder_mine else None
    in_frozen_state = addr in frozen_state

    # Verdict: the live read probe is the truth when it answers.
    # TUSD has no read function — fall back to the last holder-state
    # event, labelled event-derived, never live-verified.
    if probe_frozen is True:
        frozen, verdict = True, "frozen"
        basis = f"Live read probe {token['read_fn']}() returns true."
    elif probe_frozen is False:
        frozen, verdict = False, "not_frozen"
        basis = f"Live read probe {token['read_fn']}() returns false."
    elif scope == "tusdscope" and last_holder is not None:
        frozen = bool(last_holder.get("state"))
        verdict = "frozen" if frozen else "not_frozen"
        basis = (
            "No on-chain read function exists for TUSD — state is "
            "derived from its last freeze event in the scanned "
            f"window ({last_holder['kind']} at block "
            f"{last_holder.get('block')}), labelled event-derived, "
            "not live-verified."
        )
    elif scope == "tusdscope":
        frozen, verdict = None, "unknown"
        basis = (
            "No on-chain read function exists for TUSD and no freeze "
            "event in the scanned window names this address — its "
            "blacklist status cannot be verified."
        )
    else:
        frozen, verdict = None, "unknown"
        basis = f"Freeze probe failed or is undecodable ({probe_note})"

    if verdict == "frozen":
        summary = (
            f"{address} IS frozen/blacklisted for {symbol}. {basis}"
            + (f" {len(mine)} freeze event(s) in the scanned window name "
               "this address." if mine else
               " No freeze event in the scanned window names this "
               "address — it was frozen before the window, or the "
               "freeze is not visible as an event.")
            + (f" Frozen balance: {balance} {symbol}"
               + (f" (${balance_block['amount_usd']:,.2f})"
                  if balance_block["amount_usd"] is not None else "")
               if balance is not None else "")
        )
    elif verdict == "not_frozen":
        summary = (
            f"{address} is NOT frozen/blacklisted for {symbol}. {basis}"
            + (f" {len(mine)} past freeze event(s) name this address "
               "(latest: "
               f"{last_holder['kind'] if last_holder else '—'}"
               + (f" at block {last_holder.get('block')}" if last_holder else "")
               + ") — the latest event leaves it unfrozen."
               if mine else
               " No freeze event in the scanned window names this address.")
        )
    elif verdict == "not_freezable":
        summary = f"{symbol} has no freeze function — this address cannot be frozen for it."
    else:
        summary = (
            f"Frozen status for {symbol} could not be verified for "
            f"{address}. {basis}"
            + (f" {len(mine)} freeze event(s) in the scanned window name "
               "this address — see the events below." if mine else "")
        )

    return {
        "scope": scope,
        "symbol": symbol,
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "chain": "ethereum",
        "frozen": frozen,
        "verdict": verdict,
        "summary": summary,
        "read_probe": {
            "function": token["read_fn"],
            "frozen": probe_frozen,
            "note": probe_note,
        },
        "events": {
            "window": window,
            "status": ev_status,
            "status_note": ev_note,
            "total_events_naming_address": len(mine),
            "in_currently_frozen_set": in_frozen_state,
            "events": [_watch_event_row(e, cfg, price) for e in mine],
        },
        "balance": balance_block,
        "methodology": WATCH_METHODOLOGY,
        "limits": WATCH_LIMITS,
        "as_of": int(time.time()),
    }


async def freeze_watch(scope, address):
    """GET /api/<scope>/freeze/watch?address= — per-scope watch
    check. Raises UpstreamError(400) for a bad address. Cached
    300s per (scope, address)."""
    if not address or not _EVM_ADDR_RE.fullmatch(str(address).strip()):
        raise UpstreamError(
            "Invalid address (expected 0x + 40 hex chars)", status=400
        )
    address = str(address).strip()
    cfg = dict(_cfg(scope))
    cfg["_scope"] = scope
    if cfg["primary"] == "tron":
        return {
            "scope": scope,
            "symbol": cfg["symbol"],
            "name": cfg["name"],
            "issuer": cfg["issuer"],
            "primary_chain": "tron",
            "contract": cfg["contract"],
            "chain": "tron",
            "frozen": None,
            "verdict": "not_applicable",
            "summary": (
                f"{cfg['symbol']} is Tron-native: no freeze/blacklist "
                "function appears in TronScan's published method map "
                "(11 observed functions, partial list), so there is no "
                "EVM freeze check to run for this address. TronScan "
                "exposes no keyless per-contract event feed for this "
                "check."
            ),
            "read_probe": {"function": None, "frozen": None,
                           "note": "Not applicable on Tron."},
            "events": {"window": None, "status": "not_applicable",
                       "status_note": "No freeze events to scan on Tron.",
                       "total_events_naming_address": 0, "events": []},
            "balance": {"amount": None, "amount_usd": None, "known": False},
            "methodology": WATCH_METHODOLOGY,
            "limits": WATCH_LIMITS,
            "as_of": int(time.time()),
        }
    cache_key = f"freezeWatch:{scope}:{address.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    out = await _evm_watch_one(cfg, address)
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out


async def freeze_watch_solana(address, mint, symbol=None):
    """Watch check for one SPL mint on Solana: the mint's freeze
    authority + the address's token-account state, via the public
    Solana RPC (same check as the pre-flight frozen check, reused,
    plus the token-account address and balance). No freeze-event
    scan exists on Solana — the answer is current-state only.
    Raises UpstreamError(400) for a bad mint/address."""
    from common.solana import SOL_ACCOUNTS_RPC, _sol_rpc, is_solana_address

    if not is_solana_address(address):
        raise UpstreamError(
            "Invalid address (expected a Solana base58 address)",
            status=400,
        )
    if not is_solana_address(mint):
        raise UpstreamError(
            "Invalid mint (expected an SPL mint base58 address)",
            status=400,
        )
    cache_key = f"freezeWatchSol:{address}:{mint}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    chk = await _frozen_check_solana(
        address, {"kind": "spl", "mint": mint}
    )
    status = chk.get("status")
    freeze_auth = chk.get("freeze_authority")
    # Token-account detail (address + balance + state) for the
    # watched mint — the pre-flight check returns the verdict but
    # not the account itself.
    account = None
    try:
        accts = await _sol_rpc(
            SOL_ACCOUNTS_RPC, "getTokenAccountsByOwner",
            [address, {"mint": mint}, {"encoding": "jsonParsed"}])
        vals = (accts or {}).get("value") if isinstance(accts, dict) else None
        if vals:
            a0 = vals[0]
            info = ((a0.get("account") or {}).get("data") or {}).get("parsed", {}).get("info") or {}
            account = {
                "address": a0.get("pubkey"),
                "state": info.get("state"),
                "balance": info.get("tokenAmount", {}).get("uiAmount"),
                "decimals": info.get("tokenAmount", {}).get("decimals"),
            }
    except UpstreamError:
        account = None
    if status == "block":
        frozen, verdict = True, "frozen"
    elif status == "pass":
        frozen, verdict = False, "not_frozen"
    else:
        frozen, verdict = None, "unknown"
    sym = symbol or mint[:8] + "…"
    if freeze_auth is None:
        summary = (
            f"{address} cannot be frozen for {sym} on Solana: the "
            "mint has NO freeze authority — the issuer cannot freeze "
            "holder token accounts."
        )
        verdict = "not_freezable"
        frozen = False
    elif account is None:
        summary = (
            f"{address} holds no token account for {sym} on Solana, "
            "so there is nothing frozen — but the mint DOES have a "
            f"freeze authority, so a future freeze is possible."
        )
    else:
        summary = chk.get("detail") or ""
    out = {
        "family": "solana",
        "chain": "solana",
        "address": address,
        "mint": mint,
        "symbol": sym,
        "freeze_authority": freeze_auth,
        "token_account": account,
        "frozen": frozen,
        "verdict": verdict,
        "summary": summary,
        "read_probe": {
            "function": "getTokenAccountsByOwner state + mint freezeAuthority",
            "frozen": frozen,
            "note": chk.get("detail") or "",
        },
        "events": {
            "window": None,
            "status": "not_applicable",
            "status_note": "Solana has no freeze-event scan in this monitor — the check is the token account's current state only.",
            "total_events_naming_address": 0,
            "events": [],
        },
        "methodology": WATCH_METHODOLOGY,
        "limits": WATCH_LIMITS,
        "as_of": int(time.time()),
    }
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out


async def freeze_watch_any(address, coin=None, chain=None):
    """GET /api/walletscope/freeze/watch?address=&coin=(&chain=) —
    cross-chain watch check. `address` may be an EVM address, a
    Solana address, or an ENS name (resolved on Ethereum mainnet).
    `coin` may be a stablecoin symbol (USDT, USDC, DAI, USDe, FDUSD,
    PYUSD, TUSD, USDD), an SPL mint address (Solana only), or empty
    (checks the two freezable majors, USDC + USDT). `chain=solana`
    with a stablecoin symbol checks that coin's Solana mint (USDC /
    USDT only — the other stablecoins have no canonical Solana mint
    in this app). Raises UpstreamError(400) for bad input."""
    family, addr, resolved_from = await _classify(address)
    if family == "bitcoin":
        return {
            "address": addr,
            "resolved_from": resolved_from,
            "family": "bitcoin",
            "chain": "bitcoin",
            "checks": [],
            "frozen_any": False,
            "verdict": "not_applicable",
            "summary": (
                "Bitcoin has no issuer freeze — no address can be "
                "frozen or blacklisted on-chain."
            ),
            "methodology": WATCH_METHODOLOGY,
            "limits": WATCH_LIMITS,
            "as_of": int(time.time()),
        }
    if family == "solana":
        if chain is not None and chain != "solana":
            raise UpstreamError(
                "chain must be 'solana' for a Solana address", status=400)
        chain = "solana"
        coin_s = (coin or "").strip()
        if not coin_s:
            # No coin: check the two freezable majors' Solana mints.
            mints = [("USDC", SOL_STABLE_MINTS["USDC"]),
                     ("USDT", SOL_STABLE_MINTS["USDT"])]
        elif coin_s.upper() in SOL_STABLE_MINTS:
            mints = [(coin_s.upper(), SOL_STABLE_MINTS[coin_s.upper()])]
        else:
            # A raw SPL mint address (validated inside
            # freeze_watch_solana); anything else is a 400 from
            # _resolve_coin below.
            res = await _resolve_coin(coin_s, family, chain)
            if res is None or res.get("kind") != "spl":
                raise UpstreamError(
                    "Unknown coin for a Solana address — use USDC, "
                    "USDT, or an SPL mint address", status=400)
            mints = [(res.get("symbol") or coin_s[:8] + "…", res["mint"])]
        checks = [await freeze_watch_solana(addr, m, s) for s, m in mints]
        frozen_any = any(c.get("frozen") is True for c in checks)
        return {
            "address": addr,
            "resolved_from": resolved_from,
            "family": "solana",
            "chain": "solana",
            "checks": checks,
            "frozen_any": frozen_any,
            "verdict": ("frozen" if frozen_any else
                        "not_frozen" if all(c.get("verdict") in
                                            ("not_frozen", "not_freezable")
                                            for c in checks)
                        else "unknown"),
            "summary": (
                f"{addr} is FROZEN for "
                + ", ".join(c["symbol"] for c in checks if c.get("frozen") is True)
                + " on Solana." if frozen_any else
                f"{addr} is not frozen for any checked Solana mint "
                f"({', '.join(c['symbol'] for c in checks)})."
            ),
            "methodology": WATCH_METHODOLOGY,
            "limits": WATCH_LIMITS,
            "as_of": int(time.time()),
        }
    # EVM.
    chain = chain or "ethereum"
    if chain != "ethereum":
        raise UpstreamError(
            f"{chain} is not an Ethereum stablecoin chain for this "
            "check — stablecoin symbols resolve to their "
            "Ethereum-mainnet contracts only", status=400)
    coin_s = (coin or "").strip()
    if not coin_s:
        scopes = ["usdcscope", "usdtscope"]
    else:
        res = await _resolve_coin(coin_s, family, chain)
        if res is None:
            raise UpstreamError(
                "Unknown coin — use a stablecoin symbol (USDT, USDC, "
                "DAI, USDe, FDUSD, PYUSD, TUSD, USDD), or leave coin "
                "empty to check USDC + USDT", status=400)
        if res.get("kind") == "stablecoin_tron":
            scope = res["scope"]
            cfg = dict(_cfg(scope))
            cfg["_scope"] = scope
            one = await freeze_watch(scope, addr)
            return {
                "address": addr,
                "resolved_from": resolved_from,
                "family": "evm",
                "chain": "ethereum",
                "checks": [one],
                "frozen_any": False,
                "verdict": "not_applicable",
                "summary": one["summary"],
                "methodology": WATCH_METHODOLOGY,
                "limits": WATCH_LIMITS,
                "as_of": int(time.time()),
            }
        if res.get("kind") != "stablecoin":
            raise UpstreamError(
                "Only stablecoin symbols apply to this watch check "
                "(native coins cannot be frozen; token contracts use "
                "the per-scope /freeze/watch endpoint)", status=400)
        scopes = [res["scope"]]
    checks = [await freeze_watch(s, addr) for s in scopes]
    frozen_any = any(c.get("frozen") is True for c in checks)
    if frozen_any:
        verdict = "frozen"
        summary = (
            f"{addr} is FROZEN for "
            + ", ".join(c["symbol"] for c in checks if c.get("frozen") is True)
            + "."
        )
    elif all(c.get("verdict") == "not_freezable" for c in checks):
        verdict = "not_freezable"
        summary = (
            f"{addr} cannot be frozen for "
            + ", ".join(c["symbol"] for c in checks)
            + " — no freeze function."
        )
    elif all(c.get("verdict") == "not_frozen" for c in checks):
        verdict = "not_frozen"
        summary = (
            f"{addr} is not frozen for "
            + ", ".join(c["symbol"] for c in checks) + "."
        )
    else:
        verdict = "unknown"
        summary = (
            f"Frozen status for {addr} could not be fully verified "
            f"for {', '.join(c['symbol'] for c in checks)} — see the "
            "per-coin checks."
        )
    return {
        "address": addr,
        "resolved_from": resolved_from,
        "family": "evm",
        "chain": "ethereum",
        "checks": checks,
        "frozen_any": frozen_any,
        "verdict": verdict,
        "summary": summary,
        "methodology": WATCH_METHODOLOGY,
        "limits": WATCH_LIMITS,
        "as_of": int(time.time()),
    }
