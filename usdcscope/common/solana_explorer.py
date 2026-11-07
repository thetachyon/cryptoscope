"""Solana explorer data for SolScope — slots, transactions, accounts,
mints, programs. Keyless, public Solana RPC only.

Sources (all probed 2026-11-05 from this sandbox):
- Fast methods (getSlot, getBalance, getRecentPerformanceSamples,
  getEpochInfo, getAccountInfo): solana.publicnode.com first, then
  api.mainnet-beta.solana.com.
- Indexed/heavy methods (getBlock, getTransaction,
  getSignaturesForAddress, getTokenSupply, getTokenLargestAccounts,
  getProgramAccounts): mainnet-beta first (publicnode rejects indexed
  requests: "Indexed requests require a personal token").
- getBlock needs maxSupportedTransactionVersion 1 (versioned txs exist).
- getTokenLargestAccounts is 429-prone on mainnet-beta — one retry, then
  holders are reported unavailable (never faked).
- getSupply is indexed — mainnet-beta only.
- Prices/metadata reuse common.solana (DexScreener + CoinGecko, keyless).
  Jupiter APIs are unreachable from this sandbox — NOT used.

Slot/tx data is immutable once confirmed, so detail responses are
cached 10 minutes; lists/stats are cached 15-60s.
"""
import asyncio
import time

from common.clients import (
    UpstreamError,
    _cache_get,
    _cache_set,
)
from common.solana import (
    SOL_ACCOUNTS_RPC,
    SOL_BALANCE_RPC,
    _sol_rpc,
    is_solana_address,
    sol_balance,
    sol_native_quote,
    sol_token_holdings,
    sol_token_prices,
)

LAMPORTS_PER_SOL = 1_000_000_000

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

# Well-known program ids -> display names (jsonParsed also reports a
# `program` name for parsed instructions; this map covers unparsed ones).
PROGRAM_NAMES = {
    "11111111111111111111111111111111": "System Program",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "Token Program",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb": "Token-2022 Program",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL": "Associated Token Program",
    "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr": "Memo Program",
    "Memo1UhkJRfJc8p9R7kZ5hJ5tQ5g5g5g5g5g5g5g5g5g5": "Memo Program",
    "ComputeBudget111111111111111111111111111111": "Compute Budget Program",
    "AddressLookupTab1e1111111111111111111111111": "Address Lookup Table",
    "Stake11111111111111111111111111111111111111": "Stake Program",
    "Vote111111111111111111111111111111111111111": "Vote Program",
    "BPFLoader2111111111111111111111111111111111": "BPF Loader 2",
    "BPFLoaderUpgradeab1e11111111111111111111111": "BPF Upgradeable Loader",
    "Ed25519SigVerify111111111111111111111111111": "Ed25519 Program",
    "KeccakSecp256k11111111111111111111111111111": "Secp256k1 Program",
    "Config1111111111111111111111111111111111111": "Config Program",
    "NativeLoader1111111111111111111111111111111": "Native Loader",
    "Feature1111111111111111111111111111111111111": "Feature Program",
}

# jsonParsed `program` slugs -> display names.
PARSED_PROGRAM_NAMES = {
    "system": "System Program",
    "spl-token": "Token Program",
    "spl-token-2022": "Token-2022 Program",
    "spl-associated-token-account": "Associated Token Program",
    "spl-memo": "Memo Program",
    "vote": "Vote Program",
    "stake": "Stake Program",
    "bpf-loader-upgradeable": "BPF Upgradeable Loader",
    "bpf-loader": "BPF Loader",
    "compute-budget": "Compute Budget Program",
    "address-lookup-table": "Address Lookup Table",
}

BLOCK_OPTS_SIGS = {
    "encoding": "json",
    "maxSupportedTransactionVersion": 1,
    "transactionDetails": "signatures",
    "rewards": False,
}
TX_OPTS = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1}


# ---------------------------------------------------------------- rpc routing
async def _rpc(method, params=None, timeout=None, fast_first=False):
    """One Solana RPC call with endpoint fallback. `fast_first` tries
    publicnode first (cheap methods); otherwise mainnet-beta first
    (indexed methods). A 429 on the first endpoint retries once on the
    other after a short sleep."""
    order = [SOL_BALANCE_RPC, SOL_ACCOUNTS_RPC] if fast_first else [SOL_ACCOUNTS_RPC, SOL_BALANCE_RPC]
    last = None
    for url in order:
        for attempt in (0, 1):
            try:
                return await _sol_rpc(url, method, params or [], timeout=timeout)
            except UpstreamError as e:
                last = e
                # Rate-limit statuses get one retry on the same endpoint
                # after a pause (mainnet-beta throttles heavy methods
                # with 429/403); other errors move straight on.
                if ("429" in str(e) or "403" in str(e)) and attempt == 0:
                    await asyncio.sleep(1.5)
                    continue
                break
    raise last if last is not None else UpstreamError("Solana RPC unreachable")


def _age_unix(ts):
    """Unix timestamp -> '12 secs ago' style relative age."""
    if not ts:
        return ""
    try:
        secs = int(time.time() - int(ts))
    except (TypeError, ValueError):
        return ""
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs} secs ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins} mins ago"
    hrs = mins // 60
    if hrs < 24:
        return f"{hrs} hrs {mins % 60} mins ago"
    return f"{hrs // 24} days ago"


def _is_sig(s):
    return (
        isinstance(s, str)
        and 86 <= len(s) <= 88
        and all(ch in _B58 for ch in s)
    )


def _sig_short(s):
    return {"signature": s, "short": (s[:6] + "…" + s[-6:]) if len(s) > 12 else s}


def _sig_row(r):
    return {
        "signature": r.get("signature"),
        "slot": r.get("slot"),
        "block_time": r.get("blockTime"),
        "age": _age_unix(r.get("blockTime")),
        "err": r.get("err"),
        "memo": r.get("memo"),
    }


def _lamports(v):
    try:
        return int(v or 0) / LAMPORTS_PER_SOL
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- stats
async def sol_stats():
    key = "solstats"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    slot, epoch, perf, supply, (price, chg) = await asyncio.gather(
        _rpc("getSlot", fast_first=True),
        _rpc("getEpochInfo", fast_first=True),
        _rpc("getRecentPerformanceSamples", [1], fast_first=True),
        _rpc("getSupply"),
        sol_native_quote(),
        return_exceptions=True,
    )
    if isinstance(slot, Exception):
        raise slot if isinstance(slot, UpstreamError) else UpstreamError(str(slot))
    tps = None
    try:
        s0 = (perf or [{}])[0]
        if s0.get("samplePeriodSecs"):
            tps = round(float(s0.get("numTransactions") or 0) / float(s0["samplePeriodSecs"]), 1)
    except Exception:
        tps = None
    circ = tot = None
    try:
        v = (supply or {}).get("value") or {}
        circ = int(v.get("circulating") or 0) / LAMPORTS_PER_SOL
        tot = (int(v.get("circulating") or 0) + int(v.get("nonCirculating") or 0)) / LAMPORTS_PER_SOL
    except Exception:
        pass
    tx_count = None
    if isinstance(epoch, dict):
        tx_count = epoch.get("transactionCount")
    out = {
        "slot": slot,
        "block_height": (epoch or {}).get("blockHeight") if isinstance(epoch, dict) else None,
        "epoch": (epoch or {}).get("epoch") if isinstance(epoch, dict) else None,
        "slot_index": (epoch or {}).get("slotIndex") if isinstance(epoch, dict) else None,
        "slots_in_epoch": (epoch or {}).get("slotsInEpoch") if isinstance(epoch, dict) else None,
        "transaction_count": tx_count,
        "tps": tps,
        "sol_price_usd": price,
        "sol_change_24h_pct": chg,
        "circulating_supply": circ,
        "total_supply": tot,
    }
    _cache_set(key, out)
    return out


# ---------------------------------------------------------------- slots
async def _block_sigs(slot, timeout=25.0):
    """getBlock with signatures only. Returns the block dict, or None for
    a skipped slot."""
    return await _rpc("getBlock", [slot, BLOCK_OPTS_SIGS], timeout=timeout)


async def sol_slots(limit=10, before=None):
    """Latest confirmed slots, newest first. `before` is a slot cursor
    for Load-more (fetch older slots). Skipped slots are skipped."""
    limit = max(1, min(int(limit or 10), 25))
    key = f"solSlots:{limit}:{before}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    head = await _rpc("getSlot", fast_first=True)
    start = int(before) - 1 if before is not None else head
    # Over-fetch: some slots are skipped (no block).
    want = list(range(start, max(start - limit * 3, -1), -1))
    blocks = await asyncio.gather(
        *[_block_sigs(s) for s in want], return_exceptions=True
    )
    items = []
    for s, b in zip(want, blocks):
        if isinstance(b, Exception) or not isinstance(b, dict):
            continue
        sigs = b.get("signatures") or []
        items.append(
            {
                "slot": s,
                "block_height": b.get("blockHeight"),
                "block_time": b.get("blockTime"),
                "age": _age_unix(b.get("blockTime")),
                "tx_count": len(sigs),
                "blockhash": b.get("blockhash"),
                "parent_slot": b.get("parentSlot"),
                "leader": None,
            }
        )
        if len(items) >= limit:
            break
    out = {"items": items, "next_slot": (items[-1]["slot"] - 1) if items else None}
    _cache_set(key, out, ttl=15)
    return out


async def sol_slot(n):
    """One slot: details + up to 100 signatures."""
    if str(n).lower() == "latest":
        n = await _rpc("getSlot", fast_first=True)
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise UpstreamError("Invalid slot", status=400)
    key = f"solSlot:{n}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    block = await _block_sigs(n, timeout=30.0)
    if not isinstance(block, dict):
        raise UpstreamError("Slot not found (skipped or future slot)", status=404)
    sigs = block.get("signatures") or []
    out = {
        "slot": n,
        "block_height": block.get("blockHeight"),
        "blockhash": block.get("blockhash"),
        "parent_slot": block.get("parentSlot"),
        "previous_blockhash": block.get("previousBlockhash"),
        "block_time": block.get("blockTime"),
        "age": _age_unix(block.get("blockTime")),
        "tx_count": len(sigs),
        "signatures": sigs[:100],
        "signatures_truncated": len(sigs) > 100,
    }
    _cache_set(key, out, ttl=600)
    return out


# ---------------------------------------------------------------- transactions
def _parse_instruction(ix):
    """One jsonParsed instruction -> {program, program_id, type, info,
    accounts, data}. Unparsed instructions keep program_id + raw data."""
    if not isinstance(ix, dict):
        return {"program": "Unknown Program", "program_id": None, "type": None, "info": None}
    pid = ix.get("programId")
    parsed = ix.get("parsed")
    if isinstance(parsed, dict):
        slug = ix.get("program") or ""
        name = PARSED_PROGRAM_NAMES.get(slug) or PROGRAM_NAMES.get(pid or "", slug or "Unknown Program")
        return {
            "program": name,
            "program_id": pid,
            "type": parsed.get("type"),
            "info": parsed.get("info"),
            "accounts": ix.get("accounts"),
            "data": None,
        }
    return {
        "program": PROGRAM_NAMES.get(pid or "", "Unknown Program"),
        "program_id": pid,
        "type": None,
        "info": None,
        "accounts": ix.get("accounts"),
        "data": ix.get("data"),
    }


async def sol_tx(sig):
    if not _is_sig(sig):
        raise UpstreamError("Invalid transaction signature", status=400)
    key = f"solTx:{sig}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    res = await _rpc("getTransaction", [sig, TX_OPTS], timeout=30.0)
    if not isinstance(res, dict):
        raise UpstreamError("Transaction not found", status=404)
    meta = res.get("meta") or {}
    msg = (res.get("transaction") or {}).get("message") or {}
    keys = msg.get("accountKeys") or []
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    accounts = []
    for i, k in enumerate(keys):
        pubkey = k.get("pubkey") if isinstance(k, dict) else k
        pb = pre[i] if i < len(pre) else None
        qb = post[i] if i < len(post) else None
        accounts.append(
            {
                "address": pubkey,
                "signer": bool(k.get("signer")) if isinstance(k, dict) else False,
                "writable": bool(k.get("writable")) if isinstance(k, dict) else False,
                "pre_balance_sol": _lamports(pb) if pb is not None else None,
                "post_balance_sol": _lamports(qb) if qb is not None else None,
                "change_sol": (_lamports(qb) - _lamports(pb)) if pb is not None and qb is not None else None,
            }
        )
    instructions = [_parse_instruction(ix) for ix in msg.get("instructions") or []]
    # Inner instructions, grouped under their parent index.
    inner = []
    for grp in meta.get("innerInstructions") or []:
        for ix in grp.get("instructions") or []:
            parsed = _parse_instruction(ix)
            parsed["parent_index"] = grp.get("index")
            inner.append(parsed)
    # SPL token balance changes (non-zero only).
    token_changes = []
    pre_t = {(t.get("owner"), t.get("mint")): t for t in meta.get("preTokenBalances") or []}
    post_t = {(t.get("owner"), t.get("mint")): t for t in meta.get("postTokenBalances") or []}
    for key_t in set(pre_t) | set(post_t):
        a = pre_t.get(key_t) or {}
        b = post_t.get(key_t) or {}
        au = (a.get("uiTokenAmount") or {}).get("uiAmount")
        bu = (b.get("uiTokenAmount") or {}).get("uiAmount")
        try:
            delta = (float(bu or 0) - float(au or 0))
        except (TypeError, ValueError):
            continue
        if delta == 0:
            continue
        token_changes.append(
            {
                "owner": key_t[0],
                "mint": key_t[1],
                "pre": au,
                "post": bu,
                "change": delta,
            }
        )
    token_changes = token_changes[:20]
    err = meta.get("err")
    out = {
        "signature": sig,
        "slot": res.get("slot"),
        "block_time": res.get("blockTime"),
        "age": _age_unix(res.get("blockTime")),
        "status": "failed" if err else "success",
        "err": err,
        "fee_lamports": meta.get("fee"),
        "fee_sol": _lamports(meta.get("fee")),
        "instructions": instructions,
        "inner_instructions": inner,
        "accounts": accounts,
        "token_changes": token_changes,
        "logs": meta.get("logMessages") or [],
        "compute_units_consumed": meta.get("computeUnitsConsumed"),
    }
    _cache_set(key, out, ttl=600)
    return out


# ---------------------------------------------------------------- signatures
async def _signatures(address, limit=25, before=None):
    params = [address, {"limit": limit}]
    if before:
        params[1]["before"] = before
    res = await _rpc("getSignaturesForAddress", params, timeout=25.0)
    rows = [_sig_row(r) for r in res or []]
    return {
        "items": rows,
        "before": rows[-1]["signature"] if rows else None,
    }


# ---------------------------------------------------------------- accounts
async def sol_address(addr):
    if not is_solana_address(addr or ""):
        raise UpstreamError("Invalid Solana address", status=400)
    key = f"solAddr:{addr}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    info, balance, holdings, sigs = await asyncio.gather(
        _rpc("getAccountInfo", [addr, {"encoding": "jsonParsed"}], fast_first=True),
        sol_balance(addr),
        sol_token_holdings(addr),
        _signatures(addr, limit=25),
        return_exceptions=True,
    )
    if isinstance(info, Exception):
        raise info if isinstance(info, UpstreamError) else UpstreamError(str(info))
    value = (info or {}).get("value") if isinstance(info, dict) else None
    if value is None:
        raise UpstreamError("Account not found", status=404)
    data = value.get("data") or {}
    parsed = data.get("parsed") if isinstance(data, dict) else None
    ptype = (parsed or {}).get("type") if isinstance(parsed, dict) else None
    pinfo = (parsed or {}).get("info") if isinstance(parsed, dict) else None
    if ptype == "mint":
        typ = "mint"
    elif value.get("executable"):
        typ = "program"
    else:
        typ = "account"
    token_account = None
    if ptype == "account" and isinstance(pinfo, dict) and pinfo.get("tokenAmount"):
        token_account = {
            "mint": pinfo.get("mint"),
            "amount": (pinfo.get("tokenAmount") or {}).get("uiAmountString"),
            "decimals": (pinfo.get("tokenAmount") or {}).get("decimals"),
            "state": pinfo.get("state"),
        }
    bal = 0
    if not isinstance(balance, Exception):
        bal = balance
    hold = holdings if not isinstance(holdings, Exception) else {
        "items": [], "total_usd": 0.0, "tokens_count": 0,
        "tokens_priced_count": 0, "nft_count": 0,
    }
    out = {
        "address": addr,
        "type": typ,
        "sol_balance": _lamports(bal),
        "sol_balance_lamports": bal,
        "owner": value.get("owner"),
        "executable": bool(value.get("executable")),
        "lamports": value.get("lamports"),
        "data_length": value.get("space"),
        "token_account": token_account,
        "token_holdings": hold,
        "signatures": (sigs or {}).get("items", []) if not isinstance(sigs, Exception) else [],
        "signatures_before": (sigs or {}).get("before") if not isinstance(sigs, Exception) else None,
    }
    _cache_set(key, out)
    return out


async def sol_address_signatures(addr, before=None):
    if not is_solana_address(addr or ""):
        raise UpstreamError("Invalid Solana address", status=400)
    return await _signatures(addr, limit=25, before=before)


# ---------------------------------------------------------------- tokens (SPL mints)
async def sol_token(mint):
    if not is_solana_address(mint or ""):
        raise UpstreamError("Invalid mint address", status=400)
    key = f"solToken:{mint}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    info, supply = await asyncio.gather(
        _rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}], fast_first=True),
        _rpc("getTokenSupply", [mint]),
        return_exceptions=True,
    )
    if isinstance(info, Exception):
        raise info if isinstance(info, UpstreamError) else UpstreamError(str(info))
    value = (info or {}).get("value") if isinstance(info, dict) else None
    data = (value or {}).get("data") or {}
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or parsed.get("type") != "mint":
        raise UpstreamError("Not a token mint", status=404)
    minfo = parsed.get("info") or {}
    decimals = minfo.get("decimals")
    sup = None
    if not isinstance(supply, Exception) and isinstance(supply, dict):
        sup = (supply.get("value") or {})
    prices = await sol_token_prices(
        [{"mint": mint, "ui_amount": (sup or {}).get("uiAmountString")}]
    )
    p = prices.get(mint) or {}
    # Holders: getTokenLargestAccounts is 429-prone — one retry inside
    # _rpc, then report unavailable honestly.
    holders = None
    holders_note = None
    try:
        largest = await _rpc("getTokenLargestAccounts", [mint], timeout=30.0)
        lval = (largest or {}).get("value") or []
        total_raw = 0
        try:
            total_raw = int((sup or {}).get("amount") or minfo.get("supply") or 0)
        except (TypeError, ValueError):
            total_raw = 0
        holders = []
        for h in lval:
            try:
                amt = int(h.get("amount") or 0)
            except (TypeError, ValueError):
                amt = 0
            ui = (h.get("uiAmountString") or h.get("uiAmount"))
            share = (amt / total_raw * 100.0) if total_raw else None
            holders.append(
                {
                    "address": h.get("address"),
                    "amount": h.get("amount"),
                    "ui_amount": ui,
                    "share_pct": share,
                }
            )
    except UpstreamError as e:
        holders_note = f"Top holders unavailable: {e.msg}"
    out = {
        "address": mint,
        "name": p.get("name"),
        "symbol": p.get("symbol"),
        "decimals": decimals,
        "supply": (sup or {}).get("uiAmountString") or minfo.get("supply"),
        "supply_raw": (sup or {}).get("amount") or minfo.get("supply"),
        "mint_authority": minfo.get("mintAuthority"),
        "freeze_authority": minfo.get("freezeAuthority"),
        "is_initialized": bool(minfo.get("isInitialized")),
        "usd_price": p.get("usd_price"),
        "usd_change_24h_pct": p.get("usd_change_24h_pct"),
        "holders": holders,
        "holders_note": holders_note,
    }
    _cache_set(key, out)
    return out


async def sol_token_transfers(mint, before=None):
    if not is_solana_address(mint or ""):
        raise UpstreamError("Invalid mint address", status=400)
    return await _signatures(mint, limit=25, before=before)


# ---------------------------------------------------------------- programs
async def sol_program(addr):
    if not is_solana_address(addr or ""):
        raise UpstreamError("Invalid program address", status=400)
    key = f"solProg:{addr}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    info, sigs = await asyncio.gather(
        _rpc("getAccountInfo", [addr, {"encoding": "jsonParsed"}], fast_first=True),
        _signatures(addr, limit=25),
        return_exceptions=True,
    )
    if isinstance(info, Exception):
        raise info if isinstance(info, UpstreamError) else UpstreamError(str(info))
    value = (info or {}).get("value") if isinstance(info, dict) else None
    if value is None or not value.get("executable"):
        raise UpstreamError("Not a program account", status=404)
    out = {
        "address": addr,
        "executable": True,
        "owner": value.get("owner"),
        "lamports": value.get("lamports"),
        "data_length": value.get("space"),
        "signatures": (sigs or {}).get("items", []) if not isinstance(sigs, Exception) else [],
        "signatures_before": (sigs or {}).get("before") if not isinstance(sigs, Exception) else None,
    }
    _cache_set(key, out)
    return out


# ---------------------------------------------------------------- search
async def sol_search(q):
    q = (q or "").strip()
    if not q:
        raise UpstreamError("Empty search", status=400)
    if q.isdigit():
        return {"type": "slot", "slot": int(q)}
    if _is_sig(q):
        return {"type": "transaction", "signature": q}
    if is_solana_address(q):
        info = await _rpc("getAccountInfo", [q, {"encoding": "jsonParsed"}], fast_first=True)
        value = (info or {}).get("value") if isinstance(info, dict) else None
        if value is None:
            raise UpstreamError("Address not found", status=404)
        data = value.get("data") or {}
        parsed = data.get("parsed") if isinstance(data, dict) else None
        if isinstance(parsed, dict) and parsed.get("type") == "mint":
            return {"type": "token", "address": q}
        if value.get("executable"):
            return {"type": "program", "address": q}
        return {"type": "account", "address": q}
    raise UpstreamError("No match: expected a slot number, a transaction signature, or a base58 address", status=404)


# ---------------------------------------------------------------- docs
SOL_DOCS = [
    {"method": "GET", "suffix": "/stats", "description": "Solana network stats: current slot, epoch, TPS, SOL price, circulating/total supply, total transaction count.", "params": [], "example": "/api/solscope/stats"},
    {"method": "GET", "suffix": "/slots", "description": "Latest confirmed slots, newest first (tx counts included).", "params": ["limit", "before"], "example": "/api/solscope/slots?limit=10"},
    {"method": "GET", "suffix": "/slot/{n}", "description": "Slot by height or 'latest': blockhash, parent slot, time, tx count, first 100 signatures.", "params": [], "example": "/api/solscope/slot/latest"},
    {"method": "GET", "suffix": "/tx/{sig}", "description": "Transaction detail: status, fee, parsed instructions, account balance changes, token changes, logs.", "params": [], "example": "/api/solscope/tx/28tFJX1d..."},
    {"method": "GET", "suffix": "/tx/{sig}/explain", "description": "Plain-English explanation of a transaction: one sentence ('What happened') plus a flow summary (SOL balance changes and SPL token changes, owner + mint + amount). System/SPL transfers and known swap programs (Jupiter, Raydium, Orca) get specific sentences; other instructions are named by program and type. Token symbols are not resolved — mints are shown shortened.", "params": [], "example": "/api/solscope/tx/28tFJX1d.../explain"},
    {"method": "GET", "suffix": "/address/{addr}", "description": "Account overview: SOL balance, type (account/program/mint), SPL holdings with USD values, recent signatures.", "params": [], "example": "/api/solscope/address/vines1vzrYbzLMRdu58ou5XTby4qAqVRLmqo36NKPTg"},
    {"method": "GET", "suffix": "/address/{addr}/signatures", "description": "Signatures involving an address (paginated via ?before=).", "params": ["before"], "example": "/api/solscope/address/vines1vzr.../signatures"},
    {"method": "GET", "suffix": "/address/{addr}/risk", "description": "Address risk score 0-100 (low / elevated / high / critical) with human-readable reasons: direct OFAC SDN match (100/critical) or 1-hop proximity to a sanctioned address via counterparties from the 10 most recent transactions. The OFAC SDN list holds only 2 Solana addresses, so Solana scoring is mainly a direct match against those two. Keyless; label set vendored from the OFAC SDN list (2026-11-05, 514 addresses). Heuristic screening signal, not a legal determination.", "params": [], "example": "/api/solscope/address/vines1vzrYbzLMRdu58ou5XTby4qAqVRLmqo36NKPTg/risk"},
    {"method": "GET", "suffix": "/address/{addr}/crosschain", "description": "Cross-chain identity: Wormhole bridge transfers involving the address (Wormholescan keyless API; Wormhole only) plus the WalletScope portfolio link. EVM presence is not applicable — a Solana address is a different format, and linkage to an EVM address is shown ONLY when an actual bridge transfer is observed; there is no keyless way to prove two different-format addresses belong to the same person.", "params": [], "example": "/api/solscope/address/vines1vzrYbzLMRdu58ou5XTby4qAqVRLmqo36NKPTg/crosschain"},
    {"method": "GET", "suffix": "/token/{mint}", "description": "SPL mint: decimals, supply, mint/freeze authority, USD price, top holders (when the RPC allows).", "params": [], "example": "/api/solscope/token/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"},
    {"method": "GET", "suffix": "/token/{mint}/security", "description": "Token security verdict (low / elevated / high) with per-check pass/warn/fail/unknown: mint authority, freeze authority, holder concentration (top-10 share). Owner powers, tax, honeypot simulation and DEX liquidity are EVM-only and are listed as n/a, never pretended to have run. Keyless; heuristic screening signal, not an audit.", "params": [], "example": "/api/solscope/token/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v/security"},
    {"method": "GET", "suffix": "/token/{mint}/transfers", "description": "Recent signatures involving a mint.", "params": ["before"], "example": "/api/solscope/token/EPjFWdd5.../transfers"},
    {"method": "GET", "suffix": "/program/{addr}", "description": "Program account: owner, data length, recent invoking signatures.", "params": [], "example": "/api/solscope/program/TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
    {"method": "GET", "suffix": "/search", "description": "Universal search: slot number, transaction signature (base58, 88 chars), or base58 address (account/token/program).", "params": ["q"], "example": "/api/solscope/search?q=vines1vzrYbzLMRdu58ou5XTby4qAqVRLmqo36NKPTg"},
]


def sol_docs_payload(api_prefix):
    return {
        "chains": ["solana"],
        "endpoints": [
            {
                "method": e["method"],
                "path": api_prefix + e["suffix"],
                "description": e["description"],
                "params": e["params"],
                "example": e["example"].replace("/api/solscope", api_prefix),
                # Paid-API model extension (phase 3): propagate any
                # extra fields (paid_api, example_response, free_tier,
                # paid_tier) so future entries render the same way.
                **{k: v for k, v in e.items()
                   if k not in ("method", "suffix", "description", "params", "example")},
            }
            for e in SOL_DOCS
        ]
        + [
            {"method": "GET", "path": api_prefix + "/chains", "description": "List the chains this app serves.", "params": [], "example": api_prefix + "/chains"},
            {"method": "GET", "path": api_prefix + "/docs", "description": "This endpoint list.", "params": [], "example": api_prefix + "/docs"},
        ],
    }
