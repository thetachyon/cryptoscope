"""Plain-English transaction explanations (Feature 3).

A "What happened" card for every transaction page: one sentence in
plain English plus a compact token-flow summary (each movement: token,
amount, from -> to). Available as data via
GET /api/<scope>/tx/<hash>/explain on every EVM scope, SolScope and
BitScope.

Method names come from two keyless sources, in order:
  1. a small local map of canonical signatures (transfer, approve,
     Uniswap-style swaps, mint/burn, deposit/withdraw, ...), whose
     selectors are computed here with the Keccak-256 in common.abi;
  2. the 4byte.directory public API (keyless, reachable from the
     sandbox, verified 2026-11-05) — a candidate is accepted only if
     its function name is in the curated KNOWN_NAMES set. 4byte
     returns several colliding signatures per selector (e.g.
     0xa9059cbb also maps to junk like workMyDirefulOwner), so an
     uncurated first result is never trusted.

When a transaction cannot be decoded, the sentence says so honestly
("method 0x... is not in the signature database") instead of
inventing a description. Swap amounts come from the transaction's
token transfers (the actual movements), never from the call's
minimum-out arguments. Change outputs on Bitcoin cannot be
identified from chain data alone, so every output is listed.
"""
import re
import time

import httpx

from common.abi import _decode_args, keccak_256
from common.chains import CHAINS
from common.clients import HEADERS, TIMEOUT, UpstreamError, _cache_get, _cache_set, bs
from common.formatting import short, token_amount, wei_to_eth


# ---------------------------------------------------------------- signature database
def _sel(text_sig: str) -> str:
    return "0x" + keccak_256(text_sig.encode()).hex()[:8]


# Canonical method signatures we decode with specific sentences.
# kind drives the sentence builder; types are used to decode args.
_LOCAL_SIGS = {
    # ERC-20
    "transfer(address,uint256)": "erc20_transfer",
    "transferFrom(address,address,uint256)": "erc20_transfer_from",
    "approve(address,uint256)": "approval",
    "increaseAllowance(address,uint256)": "approval",
    "decreaseAllowance(address,uint256)": "approval",
    "mint(address,uint256)": "mint",
    "mint(uint256)": "mint_self",
    "burn(uint256)": "burn",
    "burnFrom(address,uint256)": "burn_from",
    # WETH-style wrap / unwrap (also staking deposit/withdraw(uint256),
    # disambiguated by the token transfers — see _evm_explain)
    "deposit()": "deposit",
    "withdraw(uint256)": "withdraw",
    # Uniswap V2-style swaps
    "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)": "swap",
    "swapTokensForExactTokens(uint256,uint256,address[],address,uint256)": "swap",
    "swapExactETHForTokens(uint256,address[],address,uint256)": "swap",
    "swapTokensForExactETH(uint256,uint256,address[],address,uint256)": "swap",
    "swapExactTokensForETH(uint256,uint256,address[],address,uint256)": "swap",
    "swapETHForExactTokens(uint256,address[],address,uint256)": "swap",
    "swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)": "swap",
    "swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)": "swap",
    "swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)": "swap",
    # Uniswap V3-style swaps (tuple args are not decoded — named only)
    "exactInputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))": "swap",
    "exactInput((bytes,address,uint256,uint256,uint256))": "swap",
    "exactOutputSingle((address,address,uint24,address,uint256,uint256,uint256,uint160))": "swap",
    "exactOutput((bytes,address,uint256,uint256,uint256))": "swap",
    # Liquidity
    "addLiquidity(address,address,uint256,uint256,uint256,uint256,address,uint256)": "add_liquidity",
    "addLiquidityETH(address,uint256,uint256,uint256,address,uint256)": "add_liquidity",
    "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)": "remove_liquidity",
    "removeLiquidityETH(address,uint256,uint256,uint256,address,uint256)": "remove_liquidity",
    # NFTs
    "safeTransferFrom(address,address,uint256)": "nft_transfer",
    "safeTransferFrom(address,address,uint256,bytes)": "nft_transfer",
    "setApprovalForAll(address,bool)": "nft_approval",
    # Batching / generic account calls (named, inner calls not decoded)
    "multicall(bytes[])": "multicall",
    "multicall(uint256,bytes[])": "multicall",
    "execute(address,uint256,bytes)": "execute",
    # Common DeFi verbs (named, args summarised generically)
    "claim()": "generic",
    "claimRewards()": "generic",
    "stake(uint256)": "generic",
    "unstake(uint256)": "generic",
    "deposit(uint256)": "generic",
    "harvest()": "generic",
    "borrow(uint256)": "generic",
    "repay(uint256)": "generic",
}

SELECTOR_MAP = {_sel(sig): (sig, kind) for sig, kind in _LOCAL_SIGS.items()}

# Function names the 4byte lookup may resolve to a generic
# "Called <name> on <contract>" sentence. Deliberately narrow: a
# colliding junk signature (e.g. workMyDirefulOwner) must never be
# presented as the method. Anything outside this set is "unknown".
KNOWN_NAMES = {
    "transfer", "transferFrom", "approve", "increaseAllowance",
    "decreaseAllowance", "mint", "burn", "burnFrom", "deposit",
    "withdraw", "swapExactTokensForTokens", "swapTokensForExactTokens",
    "swapExactETHForTokens", "swapTokensForExactETH",
    "swapExactTokensForETH", "swapETHForExactTokens",
    "swapExactTokensForTokensSupportingFeeOnTransferTokens",
    "swapExactETHForTokensSupportingFeeOnTransferTokens",
    "swapExactTokensForETHSupportingFeeOnTransferTokens",
    "exactInput", "exactInputSingle", "exactOutput", "exactOutputSingle",
    "addLiquidity", "addLiquidityETH", "removeLiquidity",
    "removeLiquidityETH", "safeTransferFrom", "setApprovalForAll",
    "multicall", "execute", "claim", "claimRewards", "stake", "unstake",
    "harvest", "borrow", "repay", "supply", "liquidate", "vote",
    "propose", "delegate", "lock", "unlock", "buy", "sell", "open",
    "close", "wrap", "unwrap", "bridge", "send", "permit", "flashLoan",
    "compound", "create", "initialize", "pause", "unpause", "grantRole",
    "revokeRole", "transferOwnership", "renounceOwnership", "setOwner",
    "upgradeTo", "upgradeToAndCall", "fallback", "receive",
}

_FOURBYTE_URL = "https://www.4byte.directory/api/v1/signatures/"
_fourbyte_cache: dict = {}
_FOURBYTE_TTL = 3600  # seconds


def _parse_sig_types(text_sig: str):
    """'transfer(address,uint256)' -> ['address', 'uint256'].
    Returns None when the signature has tuple/struct args (not
    decodable by the minimal codec in common.abi)."""
    m = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\((.*)\)", text_sig or "")
    if not m:
        return None
    inner = m.group(1).strip()
    if not inner:
        return []
    if "(" in inner or ")" in inner:
        return None
    return [t.strip() for t in inner.split(",")]


async def _lookup_4byte(selector: str):
    """Resolve a 4-byte selector via 4byte.directory. Returns the
    canonical text signature, or None. Only a candidate whose function
    name is in KNOWN_NAMES is accepted; collisions are common and the
    first result is usually junk. Any failure degrades to None (the
    caller reports the method as unknown) — never raises."""
    if selector in _fourbyte_cache:
        ent = _fourbyte_cache[selector]
        if ent[0] > time.time():
            return ent[1]
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(_FOURBYTE_URL, params={"hex_signature": selector})
        if r.status_code != 200:
            return None
        results = (r.json() or {}).get("results") or []
    except Exception:
        return None
    # Prefer the oldest registration among known names — the canonical
    # signature is usually the earliest (4byte returns newest first).
    known = []
    for res in results:
        ts = (res or {}).get("text_signature") or ""
        name = ts.split("(")[0]
        if name in KNOWN_NAMES:
            known.append((res.get("created_at") or "", ts))
    known.sort()
    out = known[0][1] if known else None
    _fourbyte_cache[selector] = (time.time() + _FOURBYTE_TTL, out)
    return out


async def _resolve_method(selector: str):
    """(text_signature, kind, source) for a selector, or
    (None, 'unknown', None). Local map first, then 4byte."""
    if selector in SELECTOR_MAP:
        sig, kind = SELECTOR_MAP[selector]
        return sig, kind, "local"
    sig = await _lookup_4byte(selector)
    if sig:
        return sig, "generic", "4byte"
    return None, "unknown", None


# ---------------------------------------------------------------- shared helpers
def _addr_hash(a):
    return (a or {}).get("hash") if isinstance(a, dict) else a


def _short_addr(a):
    h = _addr_hash(a)
    return short(h) if h else "—"


def _fmt_amount(amount, symbol):
    return f"{amount} {symbol}" if symbol else str(amount)


async def _token_meta(chain, addr):
    """(symbol, decimals) for a token contract, via Blockscout
    /tokens/{addr} (cached). Falls back to the shortened address and
    0 decimals."""
    if not addr:
        return None, 0
    key = f"explain:token:{chain}:{addr.lower()}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    label, dec = None, 0
    try:
        d = await bs(chain, f"/tokens/{addr}")
        if isinstance(d, dict):
            label = d.get("symbol") or d.get("name")
            try:
                dec = int(d.get("decimals") or 0)
            except (TypeError, ValueError):
                dec = 0
    except UpstreamError:
        pass
    out = (label or short(addr), dec)
    _cache_set(key, out, ttl=3600)
    return out


async def _token_label(chain, addr):
    sym, _dec = await _token_meta(chain, addr)
    return sym


def _decode_calldata(text_sig, calldata_hex):
    """Decode calldata args for a text signature. Returns a list of
    decoded values, or None when the types are not decodable."""
    types = _parse_sig_types(text_sig)
    if types is None or not calldata_hex:
        return None
    try:
        raw = bytes.fromhex(calldata_hex[2:] if calldata_hex.startswith("0x") else calldata_hex)
    except ValueError:
        return None
    if len(raw) < 4:
        return None
    try:
        return _decode_args(types, raw[4:])
    except (ValueError, IndexError):
        return None


def _evm_flows(tx):
    """Token-flow summary from the tx detail's token_transfers, plus
    the native value movement when present."""
    flows = []
    for tt in tx.get("token_transfers") or []:
        tok = tt.get("token") or {}
        ttype = (tok.get("type") or "").upper()
        kind = "nft" if ttype in ("ERC-721", "ERC-1155") else "erc20"
        amount = tt.get("amount")
        if kind == "nft":
            amount = f"Token #{amount}" if ttype == "ERC-721" else f"{amount} unit(s)"
        flows.append(
            {
                "kind": kind,
                "token": {
                    "address": tok.get("address"),
                    "symbol": tok.get("symbol"),
                    "name": tok.get("name"),
                    "type": tok.get("type"),
                },
                "amount": amount,
                "from": tt.get("from"),
                "to": tt.get("to"),
                "transfer_type": tt.get("type"),
            }
        )
    value = tx.get("value_eth")
    try:
        has_value = value is not None and float(value) > 0
    except (TypeError, ValueError):
        has_value = False
    if has_value:
        flows.append(
            {
                "kind": "native",
                "token": None,
                "amount": value,
                "from": tx.get("from"),
                "to": tx.get("to"),
                "transfer_type": "value",
            }
        )
    return flows


# ---------------------------------------------------------------- EVM explain
async def evm_tx_explain(chain, h):
    """Plain-English explanation of one EVM transaction."""
    from common.explorer import _tx  # deferred: explorer imports formatting only

    t = await _tx(chain, h)
    if not isinstance(t, dict):
        return t  # JSONResponse (404) passthrough
    native = CHAINS[chain]["native"]
    sender = _addr_hash(t.get("from"))
    to = _addr_hash(t.get("to"))
    to_short = short(to) if to else None
    value = t.get("value_eth") or "0"
    try:
        value_f = float(value)
    except (TypeError, ValueError):
        value_f = 0.0
    status = t.get("status")
    failed = status in ("error", "failed", "0x0", 0)
    prefix = "This transaction failed (reverted), but it attempted to: " if failed else ""
    flows = _evm_flows(t)
    transfers = t.get("token_transfers") or []
    inp = t.get("input") or "0x"

    def token_transfer_for(token_addr=None, to_addr=None, from_addr=None):
        for tt in transfers:
            tok = (tt.get("token") or {}).get("address") or ""
            if token_addr and tok.lower() != token_addr.lower():
                continue
            if to_addr and _addr_hash(tt.get("to")) != to_addr:
                continue
            if from_addr and _addr_hash(tt.get("from")) != from_addr:
                continue
            return tt
        return None

    def transfer_sentence(tt):
        tok = tt.get("token") or {}
        sym = tok.get("symbol") or short(tok.get("address") or "")
        return f"{_fmt_amount(tt.get('amount'), sym)}"

    kind = None
    method = None
    method_source = None
    sentence = None

    # --- contract deployment
    if t.get("created_contract"):
        kind = "deployment"
        sentence = (
            f"{prefix}Deployed a new contract at {short(t['created_contract'])} "
            f"from {short(sender)}"
            + (f", sending {value} {native} to it" if value_f > 0 else "")
            + "."
        )
    # --- plain native transfer (no input data)
    elif (not inp or inp == "0x") and to:
        kind = "native_transfer"
        if value_f > 0:
            sentence = f"{prefix}Sent {value} {native} from {short(sender)} to {short(to)}."
        else:
            sentence = f"{prefix}Sent an empty transaction from {short(sender)} to {short(to)} (no value, no data)."
    elif inp and len(inp) >= 10:
        selector = inp[:10].lower()
        method, mkind, method_source = await _resolve_method(selector)
        name = method.split("(")[0] if method else None
        args = _decode_calldata(method, inp) if method else None
        kind = mkind if mkind != "generic" else "contract_call"
        if mkind == "unknown":
            kind = "unknown"
            sentence = (
                f"{prefix}Contract interaction with {to_short} — method {selector} "
                "is not in the signature database, so what it does cannot be described here."
            )
        elif mkind == "erc20_transfer":
            kind = "erc20_transfer"
            tt = token_transfer_for(token_addr=to)
            if tt:
                sentence = (
                    f"{prefix}Sent {transfer_sentence(tt)} from {short(sender)} "
                    f"to {_short_addr(tt.get('to'))}."
                )
            elif args:
                sym, dec = await _token_meta(chain, to)
                sentence = (
                    f"{prefix}Sent {_fmt_amount(token_amount(args[1], dec), sym)} "
                    f"from {short(sender)} to {short(args[0])}."
                )
            else:
                sentence = f"{prefix}Called transfer on {to_short}."
        elif mkind == "erc20_transfer_from":
            kind = "erc20_transfer"
            tt = token_transfer_for(token_addr=to)
            if tt:
                sentence = (
                    f"{prefix}Transferred {transfer_sentence(tt)} from "
                    f"{_short_addr(tt.get('from'))} to {_short_addr(tt.get('to'))} "
                    f"(sent by {short(sender)} as spender)."
                )
            elif args:
                sym, dec = await _token_meta(chain, to)
                sentence = (
                    f"{prefix}Transferred {_fmt_amount(token_amount(args[2], dec), sym)} "
                    f"from {short(args[0])} to {short(args[1])} (sent by {short(sender)} as spender)."
                )
            else:
                sentence = f"{prefix}Called transferFrom on {to_short}."
        elif mkind == "approval":
            kind = "approval"
            if args and len(args) >= 2:
                spender, raw_amt = args[0], args[1]
                unlimited = str(raw_amt) == str(2**256 - 1)
                sym, dec = await _token_meta(chain, to)
                if unlimited:
                    amt_txt = "an unlimited amount"
                else:
                    amt_txt = f"{token_amount(raw_amt, dec)} {sym}"
                sentence = (
                    f"{prefix}Approved {short(spender)} to spend {amt_txt} "
                    f"on behalf of {short(sender)}."
                )
            else:
                sentence = f"{prefix}Called {name} on {to_short}."
        elif mkind == "swap":
            kind = "swap"
            sentence = await _swap_sentence(chain, t, sender, to, value_f, native, prefix)
        elif mkind == "mint":
            kind = "mint"
            tt = token_transfer_for(token_addr=to, to_addr=(args[0] if args else None))
            if tt:
                sentence = f"{prefix}Minted {transfer_sentence(tt)} to {_short_addr(tt.get('to'))}."
            elif args:
                sym = await _token_label(chain, to)
                sentence = f"{prefix}Minted {sym} to {short(args[0])}."
            else:
                sentence = f"{prefix}Called mint on {to_short}."
        elif mkind == "mint_self":
            kind = "mint"
            tt = token_transfer_for(token_addr=to, to_addr=sender)
            if tt:
                sentence = f"{prefix}Minted {transfer_sentence(tt)} to {short(sender)}."
            else:
                sentence = f"{prefix}Called mint on {to_short}."
        elif mkind in ("burn", "burn_from"):
            kind = "burn"
            tt = token_transfer_for(token_addr=to)
            if tt:
                sentence = f"{prefix}Burned {transfer_sentence(tt)} from {_short_addr(tt.get('from'))}."
            else:
                sentence = f"{prefix}Called {name} on {to_short}."
        elif mkind == "deposit":
            kind = "wrap"
            tt = token_transfer_for(to_addr=sender)
            if tt and value_f > 0:
                tok = tt.get("token") or {}
                sentence = (
                    f"{prefix}Wrapped {value} {native} into "
                    f"{transfer_sentence(tt)}."
                )
            else:
                sentence = f"{prefix}Called deposit() on {to_short}."
        elif mkind == "withdraw":
            kind = "unwrap"
            tt = token_transfer_for(from_addr=sender)
            if tt:
                sentence = (
                    f"{prefix}Unwrapped {transfer_sentence(tt)} into {native} "
                    f"for {short(sender)}."
                )
            else:
                sentence = f"{prefix}Called withdraw on {to_short}."
        elif mkind in ("add_liquidity", "remove_liquidity"):
            kind = mkind
            verb = "Added liquidity" if mkind == "add_liquidity" else "Removed liquidity"
            if transfers:
                parts = [transfer_sentence(tt) for tt in transfers[:4]]
                sentence = (
                    f"{prefix}{verb} on {to_short}: " + ", ".join(parts)
                    + (" — see the token flows below for directions." if len(transfers) > 4 else ".")
                )
            else:
                sentence = f"{prefix}{verb} on {to_short}."
        elif mkind == "nft_transfer":
            kind = "nft_transfer"
            tt = transfers[0] if transfers else None
            if tt:
                tok = tt.get("token") or {}
                sym = tok.get("symbol") or tok.get("name") or short(tok.get("address") or "")
                sentence = (
                    f"{prefix}Transferred {sym} token #{tt.get('amount')} from "
                    f"{_short_addr(tt.get('from'))} to {_short_addr(tt.get('to'))}."
                )
            elif args:
                sentence = (
                    f"{prefix}Transferred an NFT (token #{args[2]}) from "
                    f"{short(args[0])} to {short(args[1])}."
                )
            else:
                sentence = f"{prefix}Called {name} on {to_short}."
        elif mkind == "nft_approval":
            kind = "nft_approval"
            if args:
                op, approved = args[0], args[1]
                verb = "Approved" if approved else "Revoked approval for"
                sentence = (
                    f"{prefix}{verb} {short(op)} to manage all NFTs of "
                    f"{short(sender)} on {to_short}."
                )
            else:
                sentence = f"{prefix}Called setApprovalForAll on {to_short}."
        elif mkind == "multicall":
            kind = "multicall"
            inner = len(args[0]) if args and args[0] is not None else None
            sentence = (
                f"{prefix}Called multicall on {to_short}"
                + (f" ({inner} batched call(s))" if inner is not None else "")
                + " — the inner calls are not decoded, so the individual actions are listed in the token flows below."
            )
        elif mkind == "execute":
            kind = "contract_call"
            if args:
                sentence = (
                    f"{prefix}Called execute on {to_short}: forwarded a call to "
                    f"{short(args[0])} with {args[1]} wei attached."
                )
            else:
                sentence = f"{prefix}Called execute on {to_short}."
        else:  # generic known method
            kind = "contract_call"
            sentence = f"{prefix}Called {method} on {to_short}."
    else:
        kind = "unknown"
        sentence = (
            f"{prefix}Transaction to {to_short or '—'} with input data that could not be decoded."
        )

    note = "Swap amounts come from the transaction's token transfers (actual movements), not from the call's minimum-out arguments."
    if t.get("token_transfers_overflow"):
        note += " Only the first page of token transfers is shown — more transfers exist."
    return {
        "chain": chain,
        "tx_hash": t.get("hash"),
        "kind": kind,
        "sentence": sentence,
        "method": method,
        "method_source": method_source,
        "status": status,
        "from": t.get("from"),
        "to": t.get("to"),
        "flows": flows,
        "note": note,
    }


async def _swap_sentence(chain, t, sender, router, value_f, native, prefix):
    """Swap sentence from the token transfers (actual movements).

    in  = token the sender sent (transfer from sender, or native value)
    out = token the sender received (transfer to sender)
    """
    transfers = t.get("token_transfers") or []
    ins = [tt for tt in transfers if _addr_hash(tt.get("from")) == sender]
    outs = [tt for tt in transfers if _addr_hash(tt.get("to")) == sender]
    # Exclude the router echoing the input back (fee-on-transfer paths
    # sometimes route the input token through the sender again).
    in_tt = ins[0] if ins else None
    out_tt = outs[0] if outs else None
    if in_tt and out_tt and (in_tt.get("token") or {}).get("address") == (out_tt.get("token") or {}).get("address"):
        out_tt = outs[1] if len(outs) > 1 else None

    def _desc(tt):
        tok = tt.get("token") or {}
        sym = tok.get("symbol") or short(tok.get("address") or "")
        return f"{tt.get('amount')} {sym}"

    router_s = short(router) if router else "a router"
    if in_tt and out_tt:
        return (
            f"{prefix}Swapped {_desc(in_tt)} for {_desc(out_tt)} "
            f"via {router_s}."
        )
    if value_f > 0 and out_tt:
        return (
            f"{prefix}Swapped {value_f} {native} for {_desc(out_tt)} "
            f"via {router_s}."
        )
    if in_tt and value_f == 0:
        # Token -> native: the native leg is an internal transfer, not a
        # token transfer, so the output amount is not visible here.
        tok = (in_tt.get("token") or {})
        sym = tok.get("symbol") or short(tok.get("address") or "")
        return (
            f"{prefix}Swapped {in_tt.get('amount')} {sym} for {native} "
            f"via {router_s} (the exact {native} amount is an internal transfer, not shown here)."
        )
    if in_tt:
        return f"{prefix}Swapped {_desc(in_tt)} via {router_s} (the output token is not visible in this transaction's token transfers)."
    if out_tt:
        return f"{prefix}Received {_desc(out_tt)} via a swap on {router_s} (the input token is not visible in this transaction's token transfers)."
    return f"{prefix}Swapped tokens via {router_s} (no token transfers were recorded for this transaction)."


# ---------------------------------------------------------------- Solana explain
SOL_SWAP_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter Aggregator",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDDwWcM4pRFD": "Jupiter Aggregator",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM",
    "whirLbMiicVdioFKDsnQ3WQyEHH4cJhWg3J8ZxXxXJ": "Orca Whirlpool",
    "9W959DqEETiGZocYWCQ8KkVv7xJp4Kf4JX8qg5qg5qg5": "Orca Swap",
}


async def sol_tx_explain(sig):
    """Plain-English explanation of one Solana transaction."""
    from common.solana_explorer import PROGRAM_NAMES, sol_tx

    t = await sol_tx(sig)
    accounts = t.get("accounts") or []
    ixs = [ix for ix in (t.get("instructions") or []) if ix.get("program_id") != "ComputeBudget111111111111111111111111111111"]
    inner = t.get("inner_instructions") or []
    token_changes = t.get("token_changes") or []
    failed = t.get("status") == "failed"
    prefix = "This transaction failed, but it attempted to: " if failed else ""
    flows = []
    for ch in token_changes:
        delta = ch.get("change")
        if isinstance(delta, (int, float)):
            amt = f"{delta:.9f}".rstrip("0").rstrip(".") or "0"
        else:
            amt = str(delta)
        flows.append(
            {
                "kind": "spl",
                "token": {"address": ch.get("mint"), "symbol": None, "name": None, "type": "SPL"},
                "amount": f"{'+' if delta and delta > 0 else ''}{amt}",
                "from": None,
                "to": {"hash": ch.get("owner")},
                "transfer_type": "received" if delta and delta > 0 else "sent",
            }
        )
    # SOL balance changes (excluding the fee payer's fee-only change is
    # not identifiable, so all non-zero changes are listed as flows).
    for a in accounts:
        ch = a.get("change_sol")
        if ch:
            s = f"{ch:.9f}".rstrip("0").rstrip(".")
            flows.append(
                {
                    "kind": "sol",
                    "token": None,
                    "amount": f"{'+' if ch > 0 else ''}{s or '0'}",
                    "from": None,
                    "to": {"hash": a.get("address")},
                    "transfer_type": "balance_change",
                }
            )

    def _ix_desc(ix):
        prog = ix.get("program") or SOL_SWAP_PROGRAMS.get(ix.get("program_id") or "", "Unknown program")
        typ = ix.get("type")
        info = ix.get("info") or {}
        if prog == "System Program" and typ == "transfer":
            lam = info.get("lamports")
            sol = _fmt_sol(lam)
            return ("transfer", f"Sent {sol} SOL from {short58(info.get('source'))} to {short58(info.get('destination'))}.")
        if prog in ("Token Program", "Token-2022 Program") and typ in ("transfer", "transferChecked"):
            amt = (info.get("tokenAmount") or {}).get("uiAmount") or info.get("amount")
            return ("spl_transfer", f"Sent {amt} tokens (mint {short58(info.get('mint'))}) from {short58(info.get('source'))} to {short58(info.get('destination'))}.")
        if typ:
            return ("call", f"Called {prog} · {typ}.")
        if prog == "Unknown Program" and ix.get("program_id"):
            return ("call", f"Called unknown program {short58(ix['program_id'])}.")
        return ("call", f"Called {prog}.")

    sentence = None
    kind = "contract_call"
    if len(ixs) == 1:
        dkind, desc = _ix_desc(ixs[0])
        if dkind in ("transfer", "spl_transfer"):
            kind = "sol_transfer" if dkind == "transfer" else "spl_transfer"
            sentence = prefix + desc
        else:
            sentence = prefix + desc
    elif ixs:
        # Swap? a known aggregator/AMM program among the instructions.
        swap_prog = None
        for ix in ixs + inner:
            pid = ix.get("program_id") or ""
            if pid in SOL_SWAP_PROGRAMS:
                swap_prog = SOL_SWAP_PROGRAMS[pid]
                break
        if swap_prog:
            kind = "swap"
            recv = [c for c in token_changes if c.get("change") and c["change"] > 0]
            sent = [c for c in token_changes if c.get("change") and c["change"] < 0]
            if recv and sent:
                sentence = (
                    f"{prefix}Swapped tokens via {swap_prog} "
                    f"(token changes are listed in the flows below)."
                )
            else:
                sentence = f"{prefix}Swapped tokens via {swap_prog}."
        else:
            descs = [_ix_desc(ix)[1] for ix in ixs]
            sentence = prefix + " ".join(descs)
    else:
        kind = "unknown"
        sentence = prefix + "No instructions could be decoded for this transaction."
    return {
        "chain": "solana",
        "signature": sig,
        "kind": kind,
        "sentence": sentence,
        "method": None,
        "method_source": None,
        "status": t.get("status"),
        "flows": flows,
        "note": "Token symbols are not resolved — SPL mints are shown shortened. SOL balance changes include the transaction fee paid by the fee payer.",
    }


def _fmt_sol(lam):
    """Lamports -> SOL string without scientific notation."""
    if not isinstance(lam, (int, float)):
        return None
    s = f"{lam / 1e9:.9f}".rstrip("0").rstrip(".")
    return s or "0"


def short58(s):
    if not s or len(s) < 12:
        return s or "—"
    return s[:4] + "…" + s[-4:]


# ---------------------------------------------------------------- Bitcoin explain
async def btc_tx_explain(txid):
    """Plain-English explanation of one Bitcoin transaction."""
    from common.bitcoin import btc_tx

    t = await btc_tx(txid)
    inputs = t.get("inputs") or []
    outputs = t.get("outputs") or []
    flows = [
        {
            "kind": "btc",
            "token": None,
            "amount": o.get("value_btc"),
            "from": None,
            "to": {"hash": o.get("address")},
            "transfer_type": o.get("script_type"),
        }
        for o in outputs
        if (o.get("value_sats") or 0) > 0
    ]
    if t.get("is_coinbase"):
        kind = "coinbase"
        dest = outputs[0].get("address") if outputs else None
        sentence = (
            f"Miner reward: created {t.get('total_out_btc')} BTC "
            f"(block subsidy + fees) paid to {dest or 'the miner'}."
        )
    else:
        kind = "transfer"
        n_in = len(inputs)
        in_addrs = {i.get("address") for i in inputs if i.get("address")}
        n_out = len(outputs)
        largest = max(outputs, key=lambda o: o.get("value_sats") or 0) if outputs else None
        sentence = (
            f"Sent {t.get('total_out_btc')} BTC from {n_in} input(s)"
            + (f" ({len(in_addrs)} distinct address(es))" if in_addrs else "")
            + f" to {n_out} output(s)."
        )
        if largest and largest.get("address"):
            sentence += f" Largest output: {largest.get('value_btc')} BTC to {largest['address']}."
        if t.get("fee") is not None:
            sentence += f" Fee: {t['fee']} sat ({t.get('fee_rate_sat_vb')} sat/vB)."
        if not t.get("confirmed"):
            sentence += " (Unconfirmed — still in the mempool.)"
    return {
        "chain": "bitcoin",
        "txid": txid,
        "kind": kind,
        "sentence": sentence,
        "method": None,
        "method_source": None,
        "status": "confirmed" if t.get("confirmed") else "unconfirmed",
        "flows": flows,
        "note": "Change outputs cannot be identified from chain data alone — every output is listed, including any change back to the sender.",
    }
