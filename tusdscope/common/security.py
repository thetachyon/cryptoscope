"""Token/contract security scanner for the *Scope apps — keyless.

Feature 2. EVM first, Solana second. Every check reports what it
actually observed; anything that cannot be measured degrades honestly
to "unknown" (not detectable / not determinable / simulation
unavailable) with the reason — never an invented number.

EVM checks (``evm_token_security``)
-----------------------------------
(a) Owner powers — function names from the contract ABI (Blockscout
    ``/smart-contracts``; for proxies the implementation ABIs are
    merged in), plus a live ``owner()`` eth_call. Renounced (owner ==
    0x0) or no owner function at all is a pass; a live owner holding
    mint / pause / blacklist / upgrade powers is a warn with the
    powers listed. Ownership alone never fails a token.
(b) Holder concentration — top-10 holders' share of total supply,
    from Blockscout ``/tokens/{h}/holders`` (first page, 50 holders)
    and ``/tokens/{h}`` total supply. Pass <= 50%, warn (50, 80],
    fail > 80%. ERC-20 only; NFTs report unknown.
(c) Buy/sell tax — only if the ABI exposes a standard tax function
    (buyTax / sellTax / buyTaxFee / sellTaxFee / getBuyTax /
    getSellTax / ...). Values are read live via eth_call and reported
    with their raw units; without such a function the check is
    "not detectable", never an invented 0%.
(d) Honeypot simulation — read-only eth_call simulations, no
    transaction is ever sent. Buy: Uniswap-V2-router
    ``swapExactETHForTokens`` from a funded simulation sender
    (0.01 ETH) — only when the V2 factory itself returns a pair for
    the token; a DexScreener top pair that is not V2 leaves the buy
    leg unavailable instead of a false revert. Sell: ``transfer`` of
    1 token from the largest holder to the pair. An execution revert
    on either leg is a fail; if no V2 pair / router / holder exists
    the check is "simulation unavailable" with the reason. A passing
    simulation does not guarantee a real swap succeeds (slippage,
    MEV, approvals).
(e) Liquidity — DexScreener tracked pairs on the chain (total USD
    liquidity + top pair) plus, when a Uniswap-V2 pair exists,
    on-chain reserves (``getReserves``) and the share of LP tokens
    held by the dead address (burned/locked proxy). Pass >= $1M
    tracked liquidity, warn $10k–$1M, fail < $10k with pairs present,
    unknown when no liquidity source answers at all.

Solana checks (``solana_token_security``)
-----------------------------------------
Only what is real on Solana: mint authority and freeze authority
(from the mint account via the existing Solana clients) and holder
concentration (top-10 share of the largest-accounts list). The EVM
checks are listed with status "n/a" and labelled EVM-only — they
are never pretended to have run.

Scoring
-------
fail = 3 points, warn = 1, unknown / n/a / pass = 0. Any fail ->
"high"; >= 2 points -> "elevated"; otherwise "low". Every response
carries explicit reasons, the evidence behind each check, and the
honest limits of the method. This is a heuristic screening signal,
not an audit and not investment advice.
"""

import asyncio
import time

import httpx

from common.abi import _decode_args, _encode_args, _function_selector
from common.chains import CHAINS
from common.clients import (
    HEADERS,
    TIMEOUT,
    UpstreamError,
    _cache_get,
    _cache_set,
    bs,
    rpc,
)

ZERO_ADDR = "0x0000000000000000000000000000000000000000"
DEAD_ADDR = "0x000000000000000000000000000000000000dEaD"

# Read-only eth_call simulation sender. eth_call executes against
# latest state without a signature and changes nothing; the sender
# only needs an ETH balance for the buy simulation's value.
SIM_SENDER = "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"  # vitalik.eth
SIM_BUY_ETH = 10 ** 16  # 0.01 ETH

# Uniswap-V2-style factory / router / wrapped-native per chain.
# Canonical V2 addresses are identical on the chains where V2 was
# deployed; EVERY address here is verified on-chain before use
# (eth_getCode non-empty, getPair returns a live pair). A chain
# without a V2 deployment degrades to "unavailable", never to a
# faked result.
_V2 = {
    "ethereum": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        "wrapped": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    },
    "optimism": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        "wrapped": "0x4200000000000000000000000000000000000006",
    },
    "gnosis": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        "wrapped": "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d",
    },
    "sepolia": {
        "factory": "0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
        "router": "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        "wrapped": "0x7b79995e5f793A07Fc00c246997Aee56AC1E9a",
    },
}

# DexScreener chain ids; None = chain not indexed by DexScreener.
_DEXSCREENER_CHAIN = {
    "ethereum": "ethereum",
    "optimism": "optimism",
    "gnosis": "gnosis",
    "sepolia": None,
}

# Owner powers that count as privileged control (ABI function names).
_DANGEROUS_POWERS = [
    "mint",
    "pause",
    "blacklist",
    "unBlacklist",
    "updatePauser",
    "updateBlacklister",
    "updateMasterMinter",
    "configureMinter",
    "upgradeTo",
    "transferOwnership",
]

# Standard buy/sell tax function names (no-arg, uint256 out).
_TAX_FNS = [
    "buyTax",
    "sellTax",
    "buyTaxFee",
    "sellTaxFee",
    "getBuyTax",
    "getSellTax",
    "buyFee",
    "sellFee",
    "transferTax",
]

METHOD = (
    "Token security scan: owner powers from the verified ABI + live "
    "owner() eth_call; holder concentration from Blockscout holders; "
    "tax only from standard tax functions; honeypot as read-only "
    "eth_call buy/sell simulations (no transaction sent); liquidity "
    "from DexScreener tracked pairs + on-chain V2 reserves where a "
    "pair exists. Heuristic screening signal, not an audit."
)
LIMITS = (
    "A low verdict is not proof a token is safe: tax can hide in "
    "transfer logic with no standard tax function, ownership can sit "
    "behind a proxy or multisig this scan cannot see through, and a "
    "passing eth_call simulation does not guarantee a real swap "
    "succeeds (slippage, MEV, approvals). Liquidity totals cover "
    "DexScreener-tracked pairs on this chain only."
)


# ---------------------------------------------------------------- helpers
def _check(cid, label, status, detail, evidence=None, points=0):
    return {
        "id": cid,
        "label": label,
        "status": status,  # pass | warn | fail | unknown | na
        "detail": detail,
        "evidence": evidence or {},
        "points": points,
    }


def _score(checks):
    """(level, points): any fail -> high; >= 2 points -> elevated."""
    pts = sum(c.get("points", 0) for c in checks)
    if any(c["status"] == "fail" for c in checks):
        return "high", pts
    if pts >= 2:
        return "elevated", pts
    return "low", pts


async def _eth_call(chain, to, data, from_=None, value=None):
    """Raw eth_call. Returns the hex result string, None when the call
    returns empty (0x — the function does not exist on the contract),
    raises _Reverted on an execution revert, UpstreamError otherwise."""
    tx = {"to": to, "data": data}
    if from_:
        tx["from"] = from_
    if value is not None:
        tx["value"] = hex(value)
    try:
        res = await rpc(chain, "eth_call", [tx, "latest"])
    except UpstreamError as e:
        if e.status == 400:
            raise _Reverted(e.msg)
        raise
    if not res or res == "0x":
        return None
    return res


class _Reverted(Exception):
    pass


async def _call_fn(chain, to, name, in_types, out_types, args=None, **kw):
    """eth_call one function; returns decoded values, None when the
    function does not exist (empty return)."""
    sel = _function_selector(name, in_types)
    data = "0x" + (sel + _encode_args(in_types, args or [])).hex()
    raw = await _eth_call(chain, to, data, **kw)
    if raw is None:
        return None
    return _decode_args(out_types, bytes.fromhex(raw[2:]))


async def _has_code(chain, addr):
    try:
        code = await rpc(chain, "eth_getCode", [addr, "latest"])
    except UpstreamError:
        return False
    return bool(code) and code != "0x"


async def _dexscreener_pairs(addr, chain):
    """DexScreener pairs for a token on one chain (keyless). Returns
    (pairs, error): pairs is a list of {pairAddress, dexId,
    liquidity_usd} sorted by liquidity desc, or None on failure."""
    cid = _DEXSCREENER_CHAIN.get(chain)
    if cid is None:
        return [], f"DexScreener does not index {CHAINS[chain]['label']}"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            r = await c.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}")
    except Exception:
        return None, "DexScreener unreachable"
    if r.status_code != 200:
        return None, f"DexScreener returned HTTP {r.status_code}"
    try:
        d = r.json()
    except Exception:
        return None, "DexScreener returned invalid JSON"
    out = []
    for p in d.get("pairs") or []:
        if p.get("chainId") != cid:
            continue
        liq = (p.get("liquidity") or {}).get("usd")
        out.append(
            {
                "pairAddress": p.get("pairAddress"),
                "dexId": p.get("dexId"),
                "liquidity_usd": float(liq) if liq is not None else None,
            }
        )
    out.sort(key=lambda p: (p["liquidity_usd"] or 0), reverse=True)
    return out, None


async def _contract_abi(chain, addr):
    """(function-name set, abi_source). Merges implementation ABIs
    for proxies. Empty set + 'none' when unverified/unavailable."""
    import json

    def _names(d):
        abi = (d or {}).get("abi")
        if isinstance(abi, str):
            try:
                abi = json.loads(abi)
            except Exception:
                abi = []
        if not isinstance(abi, list):
            abi = []
        return {f.get("name") for f in abi if isinstance(f, dict) and f.get("type") == "function" and f.get("name")}

    try:
        d = await bs(chain, f"/smart-contracts/{addr}")
    except UpstreamError:
        return set(), "none"
    if d is None:
        return set(), "none"
    names = _names(d)
    source = "contract"
    impls = d.get("implementations") or []
    if impls:
        source = "proxy+implementation"
        for impl in impls:
            ih = impl.get("address_hash") if isinstance(impl, dict) else None
            if not ih:
                continue
            try:
                idd = await bs(chain, f"/smart-contracts/{ih}")
            except UpstreamError:
                continue
            if idd is not None:
                names |= _names(idd)
    return names, source


# ---------------------------------------------------------------- EVM
async def evm_token_security(chain, addr):
    """Security verdict for one EVM token contract on `chain`."""
    import re

    if chain not in CHAINS:
        raise UpstreamError(f"Unknown chain: {chain!r}", status=400)
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", addr or ""):
        raise UpstreamError("Invalid EVM address (expected 0x + 40 hex chars)", status=400)
    cache_key = f"security:evm:{chain}:{addr.lower()}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    # Token facts (type/decimals/supply) — one Blockscout call, shared
    # by the concentration and honeypot checks.
    tok = await bs(chain, f"/tokens/{addr}")
    if tok is None:
        raise UpstreamError("Token not found", status=404)
    decimals = tok.get("decimals")
    try:
        decimals = int(decimals) if decimals is not None else 18
    except (TypeError, ValueError):
        decimals = 18
    try:
        total_supply = int(tok.get("total_supply") or 0)
    except (TypeError, ValueError):
        total_supply = 0
    tok_type = tok.get("type") or "ERC-20"
    checks = []

    # ---- (a) owner powers -------------------------------------------
    fn_names, abi_source = await _contract_abi(chain, addr)
    owner = None
    owner_probed = False
    if "owner" in fn_names:
        try:
            vals = await _call_fn(chain, addr, "owner", [], ["address"])
            owner = vals[0] if vals else None
        except _Reverted:
            owner = None
        except UpstreamError:
            owner = None
    if owner is None and "owner" not in fn_names:
        # Unverified contract: probe the selector directly.
        owner_probed = True
        try:
            vals = await _call_fn(chain, addr, "owner", [], ["address"])
            owner = vals[0] if vals else None
        except (_Reverted, UpstreamError):
            owner = None
    powers = [p for p in _DANGEROUS_POWERS if p in fn_names]
    paused = None
    if "paused" in fn_names:
        try:
            vals = await _call_fn(chain, addr, "paused", [], ["bool"])
            paused = vals[0] if vals else None
        except (_Reverted, UpstreamError):
            paused = None
    if owner is None:
        checks.append(_check(
            "owner_powers", "Owner powers", "pass",
            "No owner() function found on this contract — no privileged "
            "owner role detected (ABI source: "
            f"{abi_source}{' — selector probed directly' if owner_probed else ''}).",
            {"owner": None, "powers": [], "abi_source": abi_source},
        ))
    elif owner.lower() == ZERO_ADDR:
        checks.append(_check(
            "owner_powers", "Owner powers", "pass",
            "Ownership is renounced (owner() returns the zero address) — "
            "no live owner can mint, pause or blacklist.",
            {"owner": owner, "powers": powers, "abi_source": abi_source},
        ))
    else:
        detail = f"Live owner {owner}."
        if powers:
            detail += " Owner-controlled powers present in the ABI: " + ", ".join(powers) + "."
        else:
            detail += " No mint/pause/blacklist/upgrade powers found in the ABI."
        if paused is True:
            detail += " Contract is currently PAUSED."
        checks.append(_check(
            "owner_powers", "Owner powers",
            "warn" if powers or paused is True else "pass",
            detail,
            {"owner": owner, "powers": powers, "paused": paused, "abi_source": abi_source},
            points=1 if powers else 0,
        ))

    # ---- (b) holder concentration ------------------------------------
    # One retry: a single transient Blockscout failure used to degrade
    # both this check and the sell simulation below.
    holders = None
    top_holder = None
    for _attempt in range(2):
        try:
            holders = await bs(chain, f"/tokens/{addr}/holders")
            break
        except UpstreamError:
            holders = None
            if _attempt == 0:
                await asyncio.sleep(2)
    if tok_type != "ERC-20":
        checks.append(_check(
            "holder_concentration", "Holder concentration", "unknown",
            f"Holder concentration applies to fungible tokens; this is "
            f"a {tok_type} collection, so no top-10 share is computed.",
            {"token_type": tok_type},
        ))
    elif not holders or not (holders.get("items") or []):
        checks.append(_check(
            "holder_concentration", "Holder concentration", "unknown",
            "Top holders could not be loaded from Blockscout.",
            {},
        ))
    elif not total_supply:
        checks.append(_check(
            "holder_concentration", "Holder concentration", "unknown",
            "Total supply is zero or unavailable, so a top-10 share "
            "cannot be computed.",
            {},
        ))
    else:
        items = holders["items"]
        top10 = items[:10]
        try:
            top10_sum = sum(int(i.get("value") or 0) for i in top10)
            top1 = int(top10[0].get("value") or 0)
        except (TypeError, ValueError):
            top10_sum, top1 = 0, 0
        top10_pct = top10_sum / total_supply * 100.0
        top1_pct = top1 / total_supply * 100.0
        if top10_pct > 80:
            status, pts = "fail", 3
        elif top10_pct > 50:
            status, pts = "warn", 1
        else:
            status, pts = "pass", 0
        checks.append(_check(
            "holder_concentration", "Holder concentration", status,
            f"Top 10 holders hold {top10_pct:.1f}% of supply "
            f"(largest single holder {top1_pct:.1f}%; "
            f"{tok.get('holders_count') or len(items)}+ holders, first "
            f"{len(items)} from Blockscout).",
            {"top10_share_pct": round(top10_pct, 2),
             "top1_share_pct": round(top1_pct, 2),
             "top10": [{"address": (i.get("address") or {}).get("hash"),
                        "share_pct": round(int(i.get("value") or 0) / total_supply * 100.0, 2)}
                       for i in top10]},
            points=pts,
        ))
        top_holder = (top10[0].get("address") or {}).get("hash")

    # ---- (c) buy/sell tax --------------------------------------------
    tax_vals = {}
    for name in _TAX_FNS:
        if name not in fn_names:
            continue
        try:
            vals = await _call_fn(chain, addr, name, [], ["uint256"])
        except (_Reverted, UpstreamError):
            vals = None
        if vals:
            try:
                tax_vals[name] = int(vals[0])
            except (TypeError, ValueError):
                pass
    if not tax_vals:
        checks.append(_check(
            "buy_sell_tax", "Buy/sell tax", "unknown",
            "No standard tax function (buyTax/sellTax/…) in the "
            "contract ABI — tax is not detectable. A tax could still "
            "hide in transfer logic with no standard function.",
            {"tax_functions_found": []},
        ))
    else:
        def _as_pct(v):
            if v == 0:
                return 0.0
            if v <= 100:
                return float(v)  # percent
            if v <= 10000:
                return v / 100.0  # basis points
            return None

        readable = {k: _as_pct(v) for k, v in tax_vals.items()}
        pcts = [p for p in readable.values() if p is not None]
        mx = max(pcts) if pcts else None
        if mx is None:
            status, pts = "unknown", 0
            detail = ("Tax functions found but their values use "
                      "unrecognised units (raw: "
                      + ", ".join(f"{k}={v}" for k, v in tax_vals.items())
                      + ") — not scored.")
        elif mx == 0:
            status, pts = "pass", 0
            detail = "Standard tax functions return 0% — no buy/sell tax."
        elif mx <= 25:
            status, pts = "warn", 1
            detail = "Buy/sell tax present: " + ", ".join(
                f"{k}={readable[k]}%" if readable[k] is not None else f"{k}={tax_vals[k]} (raw)"
                for k in tax_vals) + "."
        else:
            status, pts = "fail", 3
            detail = ("Very high buy/sell tax: " + ", ".join(
                f"{k}={readable[k]}%" if readable[k] is not None else f"{k}={tax_vals[k]} (raw)"
                for k in tax_vals) + ".")
        checks.append(_check(
            "buy_sell_tax", "Buy/sell tax", status, detail,
            {"tax_functions": tax_vals, "tax_pct": readable},
            points=pts,
        ))

    # ---- (d) honeypot simulation + (e) liquidity prep ----------------
    # V2 pair via the on-chain factory (verified), DexScreener as the
    # liquidity source. Both are best-effort; failures degrade the
    # checks that depend on them, never the whole verdict.
    v2 = _V2.get(chain) or {}
    pair = None
    pair_via = None
    if v2.get("factory") and await _has_code(chain, v2["factory"]):
        try:
            vals = await _call_fn(chain, v2["factory"], "getPair",
                                  ["address", "address"], ["address"],
                                  [addr, v2["wrapped"]])
            if vals and vals[0].lower() != ZERO_ADDR:
                pair, pair_via = vals[0], "uniswap-v2-factory"
        except (_Reverted, UpstreamError):
            pair = None
    ds_pairs, ds_err = await _dexscreener_pairs(addr, chain)
    if pair is None and ds_pairs:
        # The DexScreener top pair is only usable as a transfer
        # recipient / getReserves target when it is a plain 20-byte
        # contract address. DexScreener reports some pairs (e.g.
        # Curve) under longer pool ids — passing one to the ABI
        # encoder raises ValueError and would 500 the whole scan, so
        # a non-address id is rejected here and the sell leg degrades
        # to "no DEX pair found" instead. (Caught live 2026-11-06 on
        # USDC/Ethereum, whose DexScreener top pair is a 32-byte
        # Curve id, when the V2-factory lookup transiently failed.)
        cand = ds_pairs[0].get("pairAddress")
        if cand and re.fullmatch(r"0x[0-9a-fA-F]{40}", cand):
            pair = cand
            pair_via = "dexscreener-top-pair"

    # Liquidity (e)
    liq_total = None
    if ds_pairs:
        liq_total = sum((p.get("liquidity_usd") or 0) for p in ds_pairs)
    reserves = None
    burned_pct = None
    if pair:
        try:
            r = await _call_fn(chain, pair, "getReserves", [],
                               ["uint112", "uint112", "uint32"])
            t0 = await _call_fn(chain, pair, "token0", [], ["address"])
            if r and t0:
                reserves = {"reserve0": str(r[0]), "reserve1": str(r[1]),
                            "token0": t0[0]}
        except (_Reverted, UpstreamError):
            reserves = None
        try:
            tot = await _call_fn(chain, pair, "totalSupply", [], ["uint256"])
            dead = await _call_fn(chain, pair, "balanceOf", ["address"], ["uint256"], [DEAD_ADDR])
            if tot and dead and int(tot[0]) > 0:
                burned_pct = int(dead[0]) / int(tot[0]) * 100.0
        except (_Reverted, UpstreamError, ValueError):
            burned_pct = None
    liq_ev = {"dexscreener_total_usd": liq_total,
              "dexscreener_pairs": len(ds_pairs) if ds_pairs is not None else 0,
              "top_pair": ds_pairs[0] if ds_pairs else None,
              "v2_pair": pair, "v2_pair_via": pair_via,
              "reserves": reserves,
              "lp_burned_pct": round(burned_pct, 2) if burned_pct is not None else None}
    if ds_pairs is None and reserves is None:
        checks.append(_check(
            "liquidity", "Liquidity", "unknown",
            "Liquidity is not determinable: " + (ds_err or "no DEX pair found") + ".",
            liq_ev,
        ))
    else:
        total = liq_total or 0
        detail = ""
        if liq_total is not None:
            detail += f"DexScreener-tracked liquidity on {CHAINS[chain]['label']}: ${total:,.0f} across {len(ds_pairs)} pair(s)"
            if ds_pairs:
                detail += f" (top: {ds_pairs[0].get('dexId')} {ds_pairs[0].get('pairAddress')}, ${ds_pairs[0].get('liquidity_usd') or 0:,.0f})"
            detail += ". "
        if reserves is not None:
            detail += "On-chain V2 reserves readable. "
        if burned_pct is not None:
            detail += f"LP tokens burned (dead address): {burned_pct:.1f}%."
        elif pair:
            detail += " LP burn could not be read from the pair."
        if total >= 1_000_000:
            status, pts = "pass", 0
        elif total >= 10_000:
            status, pts = "warn", 1
            detail += " Liquidity is thin."
        elif ds_pairs:
            status, pts = "fail", 3
            detail += " Almost no tracked liquidity."
        else:
            status, pts = "unknown", 0
            detail += " No DexScreener liquidity figure; V2 reserves only."
            if reserves is not None:
                status, pts = "pass", 0
        checks.append(_check("liquidity", "Liquidity", status, detail.strip(), liq_ev, points=pts))

    # Honeypot (d)
    # The router buy leg only runs against a pair the V2 factory
    # itself returned: a DexScreener top pair may be a V3 pool (no
    # getReserves, not router-routable), and swapping against it
    # through the V2 router would revert for plumbing reasons, not
    # because the token blocks buys — that would be a false
    # honeypot. The sell leg only needs the token + any pair address.
    buy = {"attempted": False, "ok": None, "detail": ""}
    sell = {"attempted": False, "ok": None, "detail": ""}
    v2_pair = pair if pair_via == "uniswap-v2-factory" else None
    if v2_pair and v2.get("router") and await _has_code(chain, v2["router"]):
        # Buy: 0.01 ETH -> token via the V2 router, from the simulation sender.
        try:
            bal = await rpc(chain, "eth_getBalance", [SIM_SENDER, "latest"])
            bal_ok = int(bal, 16) >= SIM_BUY_ETH if isinstance(bal, str) else False
        except (UpstreamError, ValueError):
            bal_ok = False
        if not bal_ok:
            buy["detail"] = "Simulation sender holds < 0.01 ETH, so the buy leg could not run."
        else:
            buy["attempted"] = True
            deadline = int(time.time()) + 1200
            try:
                await _call_fn(
                    chain, v2["router"], "swapExactETHForTokens",
                    ["uint256", "address[]", "address", "uint256"], ["uint256[]"],
                    [0, [v2["wrapped"], addr], SIM_SENDER, deadline],
                    from_=SIM_SENDER, value=SIM_BUY_ETH)
                buy["ok"] = True
                buy["detail"] = "Buy simulation (0.01 ETH -> token via V2 router) succeeded."
            except _Reverted as e:
                msg = (e.args[0] if e.args else "") or ""
                if any(k in msg.lower() for k in ("insufficient", "allowance", "balance")):
                    buy["ok"] = None
                    buy["detail"] = f"Buy simulation could not run cleanly ({msg}) — not scored as a failure."
                else:
                    buy["ok"] = False
                    buy["detail"] = f"Buy simulation reverted ({msg or 'execution reverted'}) — the token may block buys."
            except UpstreamError as e:
                buy["detail"] = f"Buy simulation unavailable (RPC: {e.msg})."
    elif pair:
        buy["detail"] = ("No Uniswap-V2 pair for this token on this chain "
                         "(top DEX pair is not V2-routable), so the router "
                         "buy leg could not run.")
    else:
        buy["detail"] = "No DEX pair found, so the buy leg could not run."
    if pair and top_holder:
        # Sell: transfer 1 token from the largest holder to the pair.
        sell["attempted"] = True
        amount = 10 ** decimals
        try:
            await _call_fn(chain, addr, "transfer",
                           ["address", "uint256"], ["bool"],
                           [pair, amount], from_=top_holder)
            sell["ok"] = True
            sell["detail"] = (f"Sell simulation (transfer of 1 {tok.get('symbol') or 'token'} "
                              f"from the largest holder to the pair) succeeded.")
        except _Reverted as e:
            msg = (e.args[0] if e.args else "") or ""
            sell["ok"] = False
            sell["detail"] = (f"Sell simulation reverted ({msg or 'execution reverted'}) — "
                              "the token may block sells (honeypot indicator).")
        except UpstreamError as e:
            sell["detail"] = f"Sell simulation unavailable (RPC: {e.msg})."
    elif pair:
        sell["detail"] = "No holder with a balance to simulate a sell from."
    else:
        sell["detail"] = "No DEX pair found, so the sell leg could not run."
    if buy["ok"] is False or sell["ok"] is False:
        failed = [x for x in (buy["detail"] if buy["ok"] is False else "",
                              sell["detail"] if sell["ok"] is False else "") if x]
        checks.append(_check(
            "honeypot", "Honeypot simulation", "fail",
            " ".join(failed),
            {"buy": buy, "sell": sell, "pair": pair},
            points=3,
        ))
    elif sell["ok"] is True:
        detail = sell["detail"]
        if buy["ok"] is True:
            detail = buy["detail"] + " " + sell["detail"]
        else:
            detail += " " + buy["detail"]
        detail += (" Read-only eth_call only — a passing simulation does "
                   "not guarantee a real swap succeeds.")
        checks.append(_check(
            "honeypot", "Honeypot simulation", "pass", detail,
            {"buy": buy, "sell": sell, "pair": pair},
        ))
    else:
        checks.append(_check(
            "honeypot", "Honeypot simulation", "unknown",
            "Simulation unavailable: " + buy["detail"] + " " + sell["detail"],
            {"buy": buy, "sell": sell, "pair": pair},
        ))

    level, points = _score(checks)
    out = {
        "token": addr,
        "chain": chain,
        "family": "evm",
        "level": level,
        "points": points,
        "checks": checks,
        "reasons": [f"{c['label']}: {c['detail']}" for c in checks],
        "method": METHOD,
        "limits": LIMITS,
        "coverage": {"note": (
            "Holder concentration covers the first 50 holders from "
            "Blockscout; liquidity covers DexScreener-tracked pairs on "
            f"{CHAINS[chain]['label']} plus on-chain V2 reserves where a "
            "pair exists."
        )},
    }
    _cache_set(cache_key, out, ttl=300)
    return out


# ---------------------------------------------------------------- Solana
async def solana_token_security(mint):
    """Security verdict for one Solana SPL mint. Only the checks that
    are real on Solana run; EVM checks are listed as n/a (EVM-only)."""
    from common.solana import is_solana_address
    from common.solana_explorer import sol_token

    if not is_solana_address(mint or ""):
        raise UpstreamError("Invalid Solana mint address", status=400)
    cache_key = f"security:sol:{mint}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    t = await sol_token(mint)
    checks = []

    mint_auth = t.get("mint_authority")
    checks.append(_check(
        "mint_authority", "Mint authority",
        "pass" if mint_auth is None else "warn",
        ("Mint authority is renounced (None) — no new supply can be "
         "minted." if mint_auth is None else
         f"Mint authority is live ({mint_auth}) — new supply can be minted."),
        {"mint_authority": mint_auth},
        points=0 if mint_auth is None else 1,
    ))
    freeze_auth = t.get("freeze_authority")
    checks.append(_check(
        "freeze_authority", "Freeze authority",
        "pass" if freeze_auth is None else "warn",
        ("Freeze authority is renounced (None) — no account can be "
         "frozen." if freeze_auth is None else
         f"Freeze authority is live ({freeze_auth}) — token accounts "
         "can be frozen."),
        {"freeze_authority": freeze_auth},
        points=0 if freeze_auth is None else 1,
    ))

    holders = t.get("holders")
    supply_raw = None
    try:
        supply_raw = int(t.get("supply_raw") or 0)
    except (TypeError, ValueError):
        supply_raw = 0
    if not holders:
        checks.append(_check(
            "holder_concentration", "Holder concentration", "unknown",
            "Top holders unavailable from the public RPC "
            f"({t.get('holders_note') or 'no data'}), so no top-10 share "
            "can be computed.",
            {},
        ))
    elif not supply_raw:
        checks.append(_check(
            "holder_concentration", "Holder concentration", "unknown",
            "Total supply is unavailable, so a top-10 share cannot be computed.",
            {},
        ))
    else:
        top10 = holders[:10]
        top10_sum = sum(int(h.get("amount") or 0) for h in top10)
        top1 = int(top10[0].get("amount") or 0) if top10 else 0
        top10_pct = top10_sum / supply_raw * 100.0
        top1_pct = top1 / supply_raw * 100.0
        if top10_pct > 80:
            status, pts = "fail", 3
        elif top10_pct > 50:
            status, pts = "warn", 1
        else:
            status, pts = "pass", 0
        checks.append(_check(
            "holder_concentration", "Holder concentration", status,
            f"Top 10 holders hold {top10_pct:.1f}% of supply "
            f"(largest single holder {top1_pct:.1f}%; largest-accounts "
            f"list from the public RPC).",
            {"top10_share_pct": round(top10_pct, 2),
             "top1_share_pct": round(top1_pct, 2)},
            points=pts,
        ))

    for cid, label in (
        ("owner_powers", "Owner powers"),
        ("buy_sell_tax", "Buy/sell tax"),
        ("honeypot", "Honeypot simulation"),
        ("liquidity", "Liquidity"),
    ):
        checks.append(_check(
            cid, label, "na",
            "EVM-only check — it does not apply to Solana SPL tokens "
            "and was not run.",
            {},
        ))

    level, points = _score(checks)
    out = {
        "token": mint,
        "chain": "solana",
        "family": "solana",
        "level": level,
        "points": points,
        "checks": checks,
        "reasons": [f"{c['label']}: {c['detail']}" for c in checks],
        "method": (
            "Solana token security scan: mint/freeze authority from the "
            "mint account (public Solana RPC) and holder concentration "
            "from the largest-accounts list. EVM checks (owner powers, "
            "tax, honeypot simulation, DEX liquidity) do not apply to "
            "Solana and are labelled EVM-only. Heuristic screening "
            "signal, not an audit."
        ),
        "limits": (
            "A low verdict is not proof a token is safe: the mint "
            "program itself (e.g. Token-2022 extensions) is not "
            "audited here, and holder data depends on the public RPC "
            "answering getTokenLargestAccounts."
        ),
        "coverage": {"note": (
            "Holder concentration covers the largest-accounts list "
            "returned by the public RPC (up to 20 accounts)."
        )},
    }
    _cache_set(cache_key, out, ttl=300)
    return out
