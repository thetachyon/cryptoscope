"""Allowance & approval checker (phase 2, P4) — keyless, read-only.

One question, answered from the chain: has `owner` already approved
`spender` to spend at least `amount` of `token`, and if not, what exact
approval would be needed? Nothing here builds, signs, or sends a
transaction — execution stays in the user's wallet. The response
states the approval that *would* be needed (spender, token, exact
amount, plus the unlimited-approval option with its risk note); the
user performs it themselves.

EVM (Ethereum / Sepolia / Optimism / Gnosis):
  - `allowance(owner, spender)` via public RPC eth_call (the token's
    own read function — no indexer, no allowance API).
  - Token metadata (symbol / decimals / name): the quote registry
    (common/quotes.resolve_token) for known symbols, else Blockscout
    /tokens/{addr}, else eth_call symbol()/decimals()/name() with a
    bytes32-symbol fallback for old tokens. A token whose decimals
    cannot be determined is reported with allowance in raw units only
    and `approval_needed` unknown — never guessed.
  - Spender identity: Blockscout /addresses/{spender} (name,
    is_contract, is_verified). The curated labels.json is a FRONTEND
    asset (see common/preflight.py) — the UI adds its label pill on
    top of this response; the API does not duplicate it.
  - Unlimited approval (2**256-1) is detected and labelled as such;
    the recommended approval is the EXACT amount, and the unlimited
    option is shown only with its risk note (a compromised or buggy
    spender could drain the full balance).

Solana (SPL tokens):
  - SPL approvals are token-account DELEGATES, not a global allowance:
    getTokenAccountsByOwner (mint filter, jsonParsed) on the public
    Solana RPC returns the owner's token account for the mint, whose
    parsed info carries `delegate` + `delegatedAmount`. The delegate
    is compared with `spender`; the delegated amount is the allowance.
  - This works for classic SPL Token accounts AND Token-2022 accounts
    (getTokenAccountsByOwner with a mint filter is program-agnostic),
    unlike getProgramAccounts, which rejects Token-2022.
  - If the owner has no token account for the mint, the delegated
    amount is 0 and the response says so — an approval alone is not
    enough there, the owner must first hold the token.

Native coins (ETH / xDAI / SOL / BTC) need no approval: the response
says so with approval_needed False, never a zero allowance.

Honest limits (also stated in every response):
  - The allowance is read at the latest block at check time; a newer
    approval / spend / revoke changes it. Re-check before acting.
  - Only the allowance for THIS (owner, token, spender) triple is
    read — other spenders, other tokens, and approvals granted by
    other owners are not covered.
  - Solana: the delegate amount is per token account; an owner with
    several accounts for one mint is summed and the account count is
    stated (normally exactly one).
"""
import re
from decimal import Decimal, InvalidOperation

from common.abi import _decode_args, _encode_args, _function_selector
from common.chains import CHAINS, WALLET_CHAINS
from common.clients import UpstreamError, _cache_get, _cache_set, bs, rpc
from common.solana import SOL_ACCOUNTS_RPC, _dexscreener_token, _sol_rpc, is_solana_address

_EVM_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_B58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
MAX_UINT256 = 2**256 - 1

METHOD = (
    "Allowance & approval checker (phase 2, P4). EVM: the token's "
    "allowance(owner, spender) read via public RPC eth_call, with "
    "token metadata from the quote registry / Blockscout / eth_call "
    "symbol()/decimals()/name() and spender identity from Blockscout "
    "/addresses. Solana: the SPL token account's delegate + "
    "delegatedAmount via getTokenAccountsByOwner (mint filter, "
    "jsonParsed) on the public Solana RPC. Read-only — nothing here "
    "builds, signs, or sends a transaction."
)
LIMITS = [
    "The allowance is read at the latest block at check time; a newer "
    "approval, spend, or revoke changes it. Re-check before acting.",
    "Only this (owner, token, spender) triple is read — other spenders, "
    "other tokens, and approvals granted by other owners are not covered.",
    "The recommended approval is the exact amount. An unlimited "
    "approval (2**256-1) lets the spender move the owner's full balance "
    "of the token until revoked — use it only for a spender you trust.",
    "Solana: the delegate amount is per token account; an owner with "
    "several accounts for one mint is summed and the account count is "
    "stated (normally exactly one).",
]


def _plain(d):
    """Decimal -> plain string, no scientific notation."""
    return format(d.normalize(), "f")


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
async def _eth_call(chain, contract, data):
    """eth_call with raw hex data -> result hex string, or None on a
    revert / empty return. Transport failures propagate."""
    try:
        res = await rpc(chain, "eth_call",
                        [{"to": contract, "data": data}, "latest"])
    except UpstreamError as e:
        if e.status == 400:
            return None  # revert = function absent on this contract
        raise
    if not res or res == "0x":
        return None
    return res


def _call_data(fn, types, args):
    sel = _function_selector(fn, types)
    return "0x" + (sel + _encode_args(types, args)).hex()


async def _eth_call_uint(chain, contract, fn, args):
    """eth_call fn(...) -> int, or None when absent/undecodable."""
    res = await _eth_call(chain, contract, _call_data(fn, ["address"] * len(args), args) if args else _call_data(fn, [], []))
    if res is None:
        return None
    try:
        vals = _decode_args(["uint256"], bytes.fromhex(res[2:]))
        return int(vals[0])
    except Exception:
        return None


async def _eth_call_uint8(chain, contract, fn):
    res = await _eth_call(chain, contract, _call_data(fn, [], []))
    if res is None:
        return None
    try:
        vals = _decode_args(["uint8"], bytes.fromhex(res[2:]))
        return int(vals[0])
    except Exception:
        return None


async def _eth_call_string(chain, contract, fn):
    """eth_call fn() -> str, or None. Tries ABI string first, then a
    bytes32 return (old tokens return bytes32 symbols/names)."""
    res = await _eth_call(chain, contract, _call_data(fn, [], []))
    if res is None:
        return None
    raw = bytes.fromhex(res[2:])
    try:
        vals = _decode_args(["string"], raw)
        if isinstance(vals[0], str) and vals[0]:
            return vals[0]
    except Exception:
        pass
    try:
        vals = _decode_args(["bytes32"], raw)
        b = bytes.fromhex(vals[0][2:])
        s = b.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
        return s or None
    except Exception:
        return None


# ---------------------------------------------------------------- EVM token
async def _evm_token_meta(chain, token_addr):
    """(symbol, name, decimals, source). Blockscout first, eth_call
    fallback. decimals None when neither source can supply it."""
    symbol = name = None
    decimals = None
    source = None
    try:
        d = await bs(chain, f"/tokens/{token_addr}", timeout=5.0)
        if isinstance(d, dict) and d.get("symbol"):
            symbol = d.get("symbol")
            name = d.get("name")
            try:
                decimals = int(d["decimals"])
            except (TypeError, ValueError):
                decimals = None
            source = "blockscout"
    except UpstreamError:
        pass
    if decimals is None:
        dec = await _eth_call_uint8(chain, token_addr, "decimals")
        if dec is not None:
            decimals = dec
            source = source or "eth_call"
    if symbol is None:
        symbol = await _eth_call_string(chain, token_addr, "symbol")
        if symbol is not None:
            source = source or "eth_call"
    if name is None:
        name = await _eth_call_string(chain, token_addr, "name")
        if name is not None:
            source = source or "eth_call"
    return symbol, name, decimals, source or "unknown"


async def _spender_info(chain, spender):
    """Blockscout identity for the spender contract/address. Bounded
    (5s): some contracts' /addresses records are very slow to serve
    (the Uniswap V2 Router times out even at 10s, per the labels.json
    curation note) — a timeout honestly yields nulls, and the UI's
    labels.json pill covers the well-known spenders."""
    info = {"address": spender, "name": None, "is_contract": None,
            "is_verified": None, "ens": None}
    try:
        d = await bs(chain, f"/addresses/{spender}", timeout=5.0)
    except UpstreamError:
        return info
    if not isinstance(d, dict):
        return info
    info["name"] = d.get("name")
    info["is_contract"] = d.get("is_contract")
    info["is_verified"] = d.get("is_verified")
    info["ens"] = d.get("ens_domain_name")
    return info


async def _token_name_only(chain, token_addr):
    """Best-effort token display name (Blockscout, bounded) — used
    only when the quote registry already supplied symbol/decimals."""
    try:
        d = await bs(chain, f"/tokens/{token_addr}", timeout=5.0)
        if isinstance(d, dict):
            return d.get("name")
    except UpstreamError:
        return None
    return None


async def _evm_allowance(owner, token, spender, chain, amount_dec):
    # Resolve the token: 0x contract, or a symbol from the quote
    # registry (any ERC-20 contract address works — the registry is
    # only a convenience for symbols).
    token_addr = None
    native_symbol = None
    registry_symbol = None
    if _EVM_RE.fullmatch(str(token).strip()):
        token_addr = str(token).strip()
    else:
        from common.quotes import resolve_token

        t = resolve_token(chain, str(token).strip())
        if t is None:
            raise UpstreamError(
                f"Unknown token '{token}' on {chain} — pass a token "
                "contract address (0x...) or a symbol from the quote "
                "token list",
                status=400,
            )
        if t["address"] is None:
            native_symbol = t["symbol"]
        else:
            token_addr = t["address"]
            registry_symbol = t["symbol"]
    if native_symbol is not None:
        return {
            "family": "evm",
            "chain": chain,
            "chain_label": CHAINS[chain]["label"],
            "owner": owner,
            "token": {"address": None, "symbol": native_symbol,
                      "name": native_symbol, "decimals": 18,
                      "is_native": True},
            "spender": {"address": spender, "name": None,
                        "is_contract": None, "is_verified": None,
                        "ens": None},
            "kind": "native",
            "allowance_raw": None,
            "allowance": None,
            "allowance_is_unlimited": False,
            "owner_balance_raw": None,
            "owner_balance": None,
            "amount": str(amount_dec) if amount_dec is not None else None,
            "amount_raw": None,
            "approval_needed": False,
            "status": "approved",
            "approval": None,
            "note": (f"{native_symbol} is the chain's native coin — "
                     "native transfers need no ERC-20 approval, so "
                     "there is nothing to approve."),
            "method": METHOD,
            "limits": LIMITS,
        }

    cache_key = (f"allowance:evm:{chain}:{token_addr.lower()}:"
                 f"{owner.lower()}:{spender.lower()}:"
                 f"{amount_dec if amount_dec is not None else '-'}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    import asyncio

    # Token metadata: the quote registry already carries verified
    # symbol/decimals for the curated tokens — use it first and skip
    # the Blockscout round-trip (only the display name still comes
    # from Blockscout, best-effort). Any other contract goes through
    # the Blockscout -> eth_call fallback in _evm_token_meta.
    # (Registry lookup is by address, so it applies whether the
    # caller passed a symbol or the contract address.)
    from common.quotes import resolve_token as _rt
    _rtok = _rt(chain, token_addr)
    registry_decimals = _rtok["decimals"] if _rtok is not None else None
    if _rtok is not None and registry_symbol is None:
        registry_symbol = _rtok["symbol"]
    if registry_decimals is not None:
        symbol, name, decimals, source = registry_symbol, None, registry_decimals, "quote-registry"
        meta_task = asyncio.create_task(_token_name_only(chain, token_addr))
    else:
        symbol, name, decimals, source = None, None, None, None
        meta_task = asyncio.create_task(_evm_token_meta(chain, token_addr))

    # allowance + balanceOf + spender identity run concurrently:
    # sequential Blockscout lookups were costing 10s+ each when a
    # contract's /addresses record is slow (V2 Router, LI.FI Diamond).
    allow_task = asyncio.create_task(
        _eth_call_uint(chain, token_addr, "allowance", [owner, spender]))
    bal_task = asyncio.create_task(
        _eth_call_uint(chain, token_addr, "balanceOf", [owner]))
    spender_task = asyncio.create_task(_spender_info(chain, spender))
    if registry_decimals is not None:
        name = await meta_task
        if name is not None:
            source = "quote-registry"
    else:
        symbol, name, decimals, source = await meta_task
    if registry_symbol is not None and symbol is None:
        symbol = registry_symbol
    allowance_raw = await allow_task
    balance_raw = await bal_task
    spender_info = await spender_task
    if allowance_raw is None:
        raise UpstreamError(
            f"allowance() call failed for {token_addr} on "
            f"{CHAINS[chain]['label']} — the address may not be an "
            "ERC-20 token, or the RPC rejected the call",
            status=400,
        )

    if decimals is not None:
        allowance_human = _plain(Decimal(allowance_raw) / (Decimal(10) ** decimals))
        balance_human = (_plain(Decimal(balance_raw) / (Decimal(10) ** decimals))
                         if balance_raw is not None else None)
    else:
        allowance_human = None
        balance_human = None

    is_unlimited = allowance_raw == MAX_UINT256
    amount_raw = None
    approval_needed = None
    status = "not_checked"
    approval = None
    if amount_dec is not None:
        if decimals is None:
            approval_needed = None
            status = "unknown"
        else:
            amount_raw = int(amount_dec * (Decimal(10) ** decimals))
            approval_needed = allowance_raw < amount_raw
            status = "approval_needed" if approval_needed else "approved"
            approval = {
                "required": bool(approval_needed),
                "spender": spender,
                "token": token_addr,
                "token_symbol": symbol,
                "amount_raw": str(amount_raw),
                "amount": _plain(amount_dec),
                "function": f"approve({spender}, {amount_raw})",
                "exact_note": (
                    "Exact approval: approve the spender for exactly "
                    f"{_plain(amount_dec)} {symbol or 'tokens'} "
                    f"({amount_raw} raw units). Recommended — it grants "
                    "no more than this transfer needs."
                ),
                "unlimited": {
                    "amount_raw": str(MAX_UINT256),
                    "function": f"approve({spender}, {MAX_UINT256})",
                    "note": (
                        "Unlimited approval (2**256-1) is the common "
                        "one-time alternative — it avoids re-approving "
                        "on every transfer, but a compromised or buggy "
                        "spender could then move the owner's full "
                        "balance of this token until revoked. Prefer "
                        "the exact approval above."
                    ),
                },
            }
            if is_unlimited:
                approval["exact_note"] += (
                    " The current allowance is already unlimited, so "
                    "no new approval is needed for any amount."
                )

    out = {
        "family": "evm",
        "chain": chain,
        "chain_label": CHAINS[chain]["label"],
        "owner": owner,
        "token": {"address": token_addr, "symbol": symbol, "name": name,
                  "decimals": decimals, "is_native": False,
                  "metadata_source": source},
        "spender": spender_info,
        "kind": "erc20",
        "allowance_raw": str(allowance_raw),
        "allowance": allowance_human,
        "allowance_is_unlimited": is_unlimited,
        "owner_balance_raw": str(balance_raw) if balance_raw is not None else None,
        "owner_balance": balance_human,
        "amount": str(amount_dec) if amount_dec is not None else None,
        "amount_raw": str(amount_raw) if amount_raw is not None else None,
        "approval_needed": approval_needed,
        "status": status,
        "approval": approval,
        "note": (
            "Allowance read at the latest block. "
            + (
                f"Owner holds {balance_human} {symbol}."
                if balance_human is not None and symbol else ""
            )
            + (
                " Token decimals could not be determined, so the "
                "allowance is reported in raw units only and whether "
                "an approval is needed for the amount is unknown."
                if decimals is None else ""
            )
        ).strip(),
        "method": METHOD,
        "limits": LIMITS,
    }
    _cache_set(cache_key, out, ttl=30)
    return out


# ---------------------------------------------------------------- Solana
async def _sol_allowance(owner, mint, spender, amount_dec):
    """SPL delegate check: the owner's token account for `mint`,
    its delegate + delegatedAmount vs `spender`."""
    cache_key = (f"allowance:sol:{mint}:{owner}:{spender}:"
                 f"{amount_dec if amount_dec is not None else '-'}")
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    try:
        accts = await _sol_rpc(
            SOL_ACCOUNTS_RPC, "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}])
    except UpstreamError as e:
        raise UpstreamError(f"Solana token-account lookup failed: {e.msg}")
    vals = (accts or {}).get("value") if isinstance(accts, dict) else None
    if vals is None:
        raise UpstreamError("Solana RPC returned no token accounts")

    accounts = []
    delegated_raw = 0
    decimals = None
    delegated_to_other = None
    for a in vals:
        try:
            info = a["account"]["data"]["parsed"]["info"]
            amt = info["tokenAmount"]
            raw = int(amt.get("amount") or 0)
            dec = amt.get("decimals")
            delegate = info.get("delegate")
            delegated_amt = info.get("delegatedAmount")
        except (KeyError, TypeError):
            continue
        if dec is not None:
            decimals = int(dec)
        accounts.append({
            "address": a.get("pubkey"),
            "state": info.get("state"),
            "delegate": delegate,
            "delegated_amount_raw": (
                str(delegated_amt.get("amount"))
                if isinstance(delegated_amt, dict) else None),
            "balance_raw": str(raw),
        })
        if delegate == spender and isinstance(delegated_amt, dict):
            try:
                delegated_raw += int(delegated_amt.get("amount") or 0)
            except (TypeError, ValueError):
                pass
        elif delegate:
            delegated_to_other = delegate

    # Token symbol, best effort (DexScreener only prices tokens with a
    # DEX pair — unpriced mints honestly report no symbol).
    symbol = None
    try:
        dex = await _dexscreener_token(mint)
        if dex:
            symbol = dex.get("symbol")
    except Exception:
        symbol = None

    # Decimals from the token account when one exists; otherwise from
    # the mint account itself (so amount_raw is still computable when
    # the owner holds none of the token).
    if decimals is None:
        try:
            mint_info = await _sol_rpc(
                SOL_ACCOUNTS_RPC, "getAccountInfo",
                [mint, {"encoding": "jsonParsed"}])
            mval = (mint_info or {}).get("value") if isinstance(mint_info, dict) else None
            minfo = ((mval or {}).get("data") or {}).get("parsed", {}).get("info") if isinstance((mval or {}).get("data"), dict) else None
            if isinstance(minfo, dict) and minfo.get("decimals") is not None:
                decimals = int(minfo["decimals"])
        except Exception:
            pass

    if decimals is not None:
        delegated_human = _plain(Decimal(delegated_raw) / (Decimal(10) ** decimals))
    else:
        delegated_human = None

    amount_raw = None
    approval_needed = None
    status = "not_checked"
    approval = None
    if amount_dec is not None:
        if decimals is None:
            # No token account at all: the owner holds none of this
            # token, so the delegated amount is 0 in any unit — an
            # approval is needed (and holding the token first is too).
            if not accounts:
                approval_needed = True
                status = "approval_needed"
            else:
                status = "unknown"
        else:
            amount_raw = int(amount_dec * (Decimal(10) ** decimals))
            approval_needed = delegated_raw < amount_raw
            status = "approval_needed" if approval_needed else "approved"
        if status in ("approval_needed", "approved"):
            approval = {
                "required": bool(approval_needed),
                "spender": spender,
                "token": mint,
                "token_symbol": symbol,
                "amount_raw": str(amount_raw) if amount_raw is not None else None,
                "amount": _plain(amount_dec),
                "function": (
                    "approve (SPL): delegate the spender on the owner's "
                    "token account for this mint"
                ),
                "exact_note": (
                    "Exact delegation: delegate the spender for exactly "
                    f"{_plain(amount_dec)} {symbol or 'tokens'}"
                    + (f" ({amount_raw} raw units)" if amount_raw is not None else "")
                    + ". Recommended — it grants no more than this transfer needs."
                ),
                "unlimited": {
                    "amount_raw": None,
                    "function": "approve (SPL) with the full token-account balance",
                    "note": (
                        "SPL has no 2**256-1 unlimited approval: the "
                        "maximum delegation is the token account's "
                        "balance. Delegating the full balance has the "
                        "same risk as an unlimited EVM approval — a "
                        "compromised delegate could move it all until "
                        "revoked (revoke instruction). Prefer the exact "
                        "delegation above."
                    ),
                },
            }

    note_parts = []
    if not accounts:
        note_parts.append(
            "Owner has no token account for this mint, so the delegated "
            "amount is 0. An approval alone is not enough here — the "
            "owner must first receive/hold this token (creating the "
            "token account), then delegate."
        )
    elif delegated_to_other:
        note_parts.append(
            f"This token account is currently delegated to a different "
            f"address ({delegated_to_other[:8]}…) — not to the spender "
            "asked about."
        )
    if decimals is None and accounts:
        note_parts.append(
            "Token decimals could not be determined, so the delegated "
            "amount is reported in raw units only."
        )
    out = {
        "family": "solana",
        "chain": "solana",
        "chain_label": "Solana",
        "owner": owner,
        "token": {"address": mint, "symbol": symbol, "name": None,
                  "decimals": decimals, "is_native": False,
                  "metadata_source": "dexscreener" if symbol else "unknown"},
        "spender": {"address": spender, "name": None,
                    "is_contract": None, "is_verified": None,
                    "ens": None},
        "kind": "spl",
        "allowance_raw": str(delegated_raw),
        "allowance": delegated_human,
        "allowance_is_unlimited": False,
        "token_accounts": accounts,
        "token_accounts_count": len(accounts),
        "delegated_to_other": delegated_to_other,
        "owner_balance_raw": None,
        "owner_balance": None,
        "amount": str(amount_dec) if amount_dec is not None else None,
        "amount_raw": str(amount_raw) if amount_raw is not None else None,
        "approval_needed": approval_needed,
        "status": status,
        "approval": approval,
        "note": " ".join(note_parts) or (
            "Delegate state read from the owner's token account for "
            "this mint at the latest slot."
        ),
        "method": METHOD,
        "limits": LIMITS,
    }
    _cache_set(cache_key, out, ttl=30)
    return out


# ---------------------------------------------------------------- endpoint
async def check_allowance(owner, token, spender, chain=None, amount=None):
    """Full allowance check. Raises UpstreamError(400) for bad input;
    upstream failures propagate as UpstreamError (the factory turns
    them into error JSON, never a 500)."""
    owner = (owner or "").strip()
    token = (token or "").strip()
    spender = (spender or "").strip()
    if not owner or not token or not spender:
        raise UpstreamError(
            "owner, token and spender are required", status=400)
    amount_dec = _parse_amount(amount)
    chain = (chain or "ethereum").strip().lower()

    if chain == "solana":
        if not is_solana_address(owner):
            raise UpstreamError(
                "owner must be a Solana address for chain=solana",
                status=400)
        if not is_solana_address(token):
            raise UpstreamError(
                "token must be an SPL mint address for chain=solana",
                status=400)
        if not is_solana_address(spender):
            raise UpstreamError(
                "spender must be a Solana address (the delegate) for "
                "chain=solana",
                status=400)
        return await _sol_allowance(owner, token, spender, amount_dec)

    if chain == "bitcoin":
        raise UpstreamError(
            "Bitcoin has no token approvals — allowance checks do not "
            "apply", status=400)
    if chain not in CHAINS:
        raise UpstreamError(
            f"Unknown chain '{chain}' (EVM chains: "
            f"{', '.join(CHAINS)}; solana for SPL tokens)",
            status=400)
    if not _EVM_RE.fullmatch(owner):
        raise UpstreamError(
            "owner must be an EVM address (0x + 40 hex chars)",
            status=400)
    if not _EVM_RE.fullmatch(spender):
        raise UpstreamError(
            "spender must be an EVM address (0x + 40 hex chars)",
            status=400)
    return await _evm_allowance(owner, token, spender, chain, amount_dec)
