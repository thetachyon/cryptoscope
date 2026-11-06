"""Chain-parameterized explorer core logic for the *Scope apps.

Every function takes `chain` (a key of `common.chains.CHAINS`) and talks
to that chain's Blockscout v2 API / public RPC / CoinGecko — nothing here
knows which app (ethscope, sepoliascope, ...) it serves. The thin app
entrypoints in `common/factory.py` bind one chain and register the routes.

DOCS_BASE is the single literal source of truth for the API docs; see
`_docs_payload(api_prefix, ...)` for how paths are expanded per app.
"""
import json
import re

import httpx

from fastapi.responses import JSONResponse

from common.abi import _decode_args, _encode_args, _function_selector, keccak_256
from common.chains import CHAINS
from common.clients import (
    CHART_TTL,
    HEADERS,
    TIMEOUT,
    UpstreamError,
    _cache_get,
    _cache_set,
    bs,
    coingecko,
    rpc,
)
from common.formatting import (
    _instance_short,
    addr_info,
    block_short,
    decode_input,
    gwei,
    internal_tx_short,
    rpc_tx_short,
    token_amount,
    token_transfer_short,
    tx_short,
    wei_to_eth,
)
from common.pricing import enrich_token_balances


# ---------------------------------------------------------------- core logic (chain-parameterized)
# Legacy /api/ethscope/* routes are mainnet aliases that call these with
# chain="ethereum"; chain-prefixed routes pass their {chain} through.
async def _chain_head(chain):
    """Live chain-head height, from the /blocks list.

    Blockscout's /stats `total_blocks` lags the live head by hundreds of
    blocks (verified 2026-11-05: /stats said 26352163 while /blocks was
    already at 26352724 — and the /stats value barely moves), so the head
    must come from /blocks itself. Falls back to /stats total_blocks if
    /blocks is unavailable.
    """
    try:
        # No params: Blockscout's /blocks rejects page_size ("Unexpected
        # field: page_size") — the default first page already starts at
        # the live head.
        d = await bs(chain, "/blocks")
        items = (d or {}).get("items") or []
        if items and items[0].get("height") is not None:
            return int(items[0]["height"])
    except Exception:
        pass
    try:
        d = await bs(chain, "/stats")
        if d and d.get("total_blocks"):
            return int(d["total_blocks"])
    except Exception:
        pass
    return None


async def _resolve_height(chain, n: str) -> str:
    if n == "latest":
        head = await _chain_head(chain)
        if head is not None:
            return str(head)
    return n


async def _stats(chain):
    key = f"{chain}:stats"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await bs(chain, "/stats")
    if d is None:
        raise UpstreamError("Stats not available")
    gp = d.get("gas_prices") or {}
    # Head from /blocks, not /stats total_blocks (stale by hundreds of
    # blocks — see _chain_head). Falls back to /stats when /blocks fails.
    head = await _chain_head(chain)
    live = head if head is not None else d.get("total_blocks")
    out = {
        "chain": chain,
        "native": CHAINS[chain]["native"],
        "eth_price_usd": d.get("coin_price"),
        "eth_price_change_24h_pct": d.get("coin_price_change_percentage"),
        "market_cap_usd": d.get("market_cap"),
        "latest_block": live,
        "total_blocks": live,
        "total_transactions": d.get("total_transactions"),
        "total_addresses": d.get("total_addresses"),
        "avg_block_time_s": round(float(d.get("average_block_time") or 0) / 1000, 1),
        "network_utilization_pct": round(float(d.get("network_utilization_percentage") or 0), 2),
        "gas_price_gwei": {
            "slow": gp.get("slow"),
            "average": gp.get("average"),
            "fast": gp.get("fast"),
        },
        "gas_price_updated": d.get("gas_price_updated_at"),
    }
    _cache_set(key, out)
    return out


async def _blocks(chain, params):
    key = f"{chain}:blocks:" + str(sorted(params.items()))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await bs(chain, "/blocks", params=params)
    if d is None:
        raise UpstreamError("Blocks not available")
    out = {
        "items": [block_short(b) for b in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }
    _cache_set(key, out)
    return out


async def _txs_latest(chain, params):
    key = f"{chain}:txs:" + str(sorted(params.items()))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await bs(chain, "/transactions", params=params)
    if d is None:
        raise UpstreamError("Transactions not available")
    out = {
        "items": [tx_short(t) for t in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }
    _cache_set(key, out)
    return out


async def _block(chain, n):
    n = await _resolve_height(chain, n)
    key = f"{chain}:block:{n}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await bs(chain, f"/blocks/{n}")
    if d is None:
        return JSONResponse({"error": "Block not found"}, status_code=404)
    out = block_short(d)
    out["parent_hash"] = d.get("parent_hash")
    out["nonce"] = d.get("nonce")
    out["difficulty"] = d.get("difficulty")
    out["total_difficulty"] = d.get("total_difficulty")
    out["uncles"] = d.get("uncles_hashes") or []
    out["gas_target_pct"] = d.get("gas_target_percentage")
    out["excess_blob_gas"] = d.get("excess_blob_gas")
    out["blob_gas_used"] = d.get("blob_gas_used")
    _cache_set(key, out)
    return out


async def _block_txs(chain, n, params):
    n = await _resolve_height(chain, n)
    height = n
    if n.startswith("0x") and len(n) == 66:
        b = await bs(chain, f"/blocks/{n}")
        if b is None:
            return JSONResponse({"error": "Block not found"}, status_code=404)
        height = b.get("height")
    p = dict(params)
    p["block_number"] = height
    d = await bs(chain, "/transactions", params=p)
    if d is None:
        return JSONResponse({"error": "Block not found"}, status_code=404)
    return {
        "items": [tx_short(t) for t in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


async def _tx(chain, h):
    d = await bs(chain, f"/transactions/{h}")
    if d is None:
        return JSONResponse({"error": "Transaction not found"}, status_code=404)
    fee = d.get("fee") or {}
    raw = d.get("raw_input") or "0x"
    out = tx_short(d)
    if out.get("block") is None:
        # tx detail endpoint exposes the block as `block_number`, not `block`
        out["block"] = d.get("block_number")
    out.update(
        {
            "nonce": d.get("nonce"),
            "position": d.get("position"),
            "confirmations": d.get("confirmations"),
            "gas_limit": d.get("gas_limit"),
            "gas_used": d.get("gas_used"),
            "max_fee_per_gas_gwei": gwei(d.get("max_fee_per_gas")),
            "max_priority_fee_gwei": gwei(d.get("max_priority_fee_per_gas")),
            "base_fee_per_gas_gwei": gwei(d.get("base_fee_per_gas")),
            "fee_eth": wei_to_eth(fee.get("value")),
            "fee_type": fee.get("type"),
            "burnt_fee_eth": wei_to_eth(d.get("transaction_burnt_fee")),
            "revert_reason": d.get("revert_reason"),
            "created_contract": (d.get("created_contract") or {}).get("hash") if isinstance(d.get("created_contract"), dict) else None,
            "input": raw,
            "input_truncated": len(raw) > 2000,
            "input_utf8": decode_input(raw),
            "has_decoded_input": bool(d.get("decoded_input")),
            "decoded_method": (d.get("decoded_input") or {}).get("method_call") if isinstance(d.get("decoded_input"), dict) else None,
            "token_transfers": [token_transfer_short(tt) for tt in (d.get("token_transfers") or [])],
            "token_transfers_overflow": bool(d.get("token_transfers_overflow")),
            "historic_eth_price_usd": d.get("historic_exchange_rate"),
        }
    )
    return out


async def _tx_token_transfers(chain, h):
    d = await bs(chain, f"/transactions/{h}/token-transfers")
    if d is None:
        return JSONResponse({"error": "Transaction not found"}, status_code=404)
    return {
        "items": [token_transfer_short(tt) for tt in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


async def _tx_logs(chain, h):
    d = await bs(chain, f"/transactions/{h}/logs")
    if d is None:
        return JSONResponse({"error": "Transaction not found"}, status_code=404)
    items = []
    for lg in d.get("items", []):
        items.append(
            {
                "address": addr_info(lg.get("address")),
                "topics": lg.get("topics") or [],
                "data": lg.get("data"),
                "log_index": lg.get("log_index"),
                "block": lg.get("block_number"),
            }
        )
    return {"items": items, "next_page_params": d.get("next_page_params")}


async def _tx_internal(chain, h):
    d = await bs(chain, f"/transactions/{h}/internal-transactions")
    if d is None:
        return JSONResponse({"error": "Transaction not found"}, status_code=404)
    return {
        "items": [internal_tx_short(i) for i in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


async def _address(chain, h):
    d = await bs(chain, f"/addresses/{h}")
    if d is None:
        return JSONResponse({"error": "Address not found"}, status_code=404)
    tok = d.get("token")
    return {
        "hash": d.get("hash"),
        "balance_eth": wei_to_eth(d.get("coin_balance")),
        "transactions_count": d.get("transactions_count"),
        "is_contract": bool(d.get("is_contract")),
        "is_verified": bool(d.get("is_verified")),
        "is_scam": bool(d.get("is_scam")),
        "ens": d.get("ens_domain_name"),
        "name": d.get("name"),
        "creator": d.get("creator_address_hash"),
        "creation_tx": d.get("creation_transaction_hash"),
        "has_token_transfers": bool(d.get("has_token_transfers")),
        "has_tokens": bool(d.get("has_tokens")),
        "token": (
            {
                "name": tok.get("name"),
                "symbol": tok.get("symbol"),
                "decimals": tok.get("decimals"),
                "type": tok.get("type"),
                "total_supply": token_amount(tok.get("total_supply"), tok.get("decimals")),
                "holders": tok.get("holders_count"),
                "exchange_rate_usd": tok.get("exchange_rate"),
            }
            if isinstance(tok, dict)
            else None
        ),
    }


async def _address_txs(chain, h, params):
    d = await bs(chain, f"/addresses/{h}/transactions", params=params)
    if d is None:
        return JSONResponse({"error": "Address not found"}, status_code=404)
    return {
        "items": [tx_short(t) for t in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


async def _address_token_transfers(chain, h, params):
    d = await bs(chain, f"/addresses/{h}/token-transfers", params=params)
    if d is None:
        return JSONResponse({"error": "Address not found"}, status_code=404)
    return {
        "items": [token_transfer_short(tt) for tt in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


async def _address_internal(chain, h, params):
    d = await bs(chain, f"/addresses/{h}/internal-transactions", params=params)
    if d is None:
        return JSONResponse({"error": "Address not found"}, status_code=404)
    return {
        "items": [internal_tx_short(i) for i in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


async def _address_tokens(chain, h):
    """Token balances with USD values (pricing.enrich_token_balances).
    ERC-721/1155 items get null USD fields. A CoinGecko failure never
    breaks the endpoint."""
    d = await bs(chain, f"/addresses/{h}/token-balances", timeout=45.0)
    if d is None:
        return JSONResponse({"error": "Address not found"}, status_code=404)
    raw = d if isinstance(d, list) else d.get("items", [])
    items, total_usd = await enrich_token_balances(chain, raw)
    return {"items": items, "total_usd": total_usd}


async def _token(chain, h):
    d = await bs(chain, f"/tokens/{h}")
    if d is None:
        return JSONResponse({"error": "Token not found"}, status_code=404)
    return {
        "address": d.get("address_hash"),
        "name": d.get("name"),
        "symbol": d.get("symbol"),
        "decimals": d.get("decimals"),
        "type": d.get("type"),
        "total_supply": token_amount(d.get("total_supply"), d.get("decimals")),
        "holders": d.get("holders_count"),
        "exchange_rate_usd": d.get("exchange_rate"),
        "circulating_supply": token_amount(d.get("circulating_supply"), d.get("decimals")) if d.get("circulating_supply") else None,
        "volume_24h_usd": d.get("volume_24h"),
        "icon_url": d.get("icon_url"),
    }


async def _token_transfers(chain, h, params):
    d = await bs(chain, f"/tokens/{h}/transfers", params=params)
    if d is None:
        return JSONResponse({"error": "Token not found"}, status_code=404)
    return {
        "items": [token_transfer_short(tt) for tt in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


async def _token_instances(chain, h, params):
    d = await bs(chain, f"/tokens/{h}/instances", params=params)
    if d is None:
        return JSONResponse({"error": "Token not found"}, status_code=404)
    return {
        "items": [_instance_short(i) for i in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


async def _token_instance(chain, h, iid):
    d = await bs(chain, f"/tokens/{h}/instances/{iid}")
    if d is None:
        return JSONResponse({"error": "Token instance not found"}, status_code=404)
    out = _instance_short(d)
    out["metadata"] = d.get("metadata")
    out["attributes"] = (d.get("metadata") or {}).get("attributes") if isinstance(d.get("metadata"), dict) else None
    return out


async def _token_instance_transfers(chain, h, iid, params):
    d = await bs(chain, f"/tokens/{h}/instances/{iid}/transfers", params=params)
    if d is None:
        return JSONResponse({"error": "Token instance not found"}, status_code=404)
    return {
        "items": [token_transfer_short(tt) for tt in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


async def _search(chain, q):
    d = await bs(chain, "/search", params={"q": q})
    if d is None:
        return {"items": []}
    items = []
    for it in d.get("items", []) or []:
        t = it.get("type")
        if t == "address":
            items.append({"type": "address", "hash": it.get("address_hash"), "name": it.get("name") or it.get("ens_domain_name") or it.get("address_hash")})
        elif t == "token":
            items.append({"type": "token", "hash": it.get("address_hash"), "name": f"{it.get('name')} ({it.get('symbol')})"})
        elif t == "transaction":
            items.append({"type": "transaction", "hash": it.get("transaction_hash"), "name": it.get("transaction_hash")})
        elif t == "block":
            items.append({"type": "block", "hash": it.get("block_hash"), "block_number": it.get("block_number"), "name": f"Block {it.get('block_number')}"})
        elif t == "ens_domain":
            items.append({"type": "address", "hash": it.get("address_hash"), "name": it.get("ens_info", {}).get("name") or it.get("name")})
    return {"items": items[:12]}


# ---------------------------------------------------------------- contracts
_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


async def _contract(chain, addr):
    d = await bs(chain, f"/smart-contracts/{addr}")
    if d is None:
        # Distinguish "not a contract" from "unknown address".
        a = await bs(chain, f"/addresses/{addr}")
        if a is None:
            return JSONResponse({"error": "Contract not found"}, status_code=404)
        if not a.get("is_contract"):
            return JSONResponse({"error": "Address is not a contract"}, status_code=404)
        return JSONResponse({"error": "Contract source not available"}, status_code=404)
    abi = d.get("abi")
    if isinstance(abi, str):
        try:
            abi = json.loads(abi)
        except Exception:
            abi = []
    if not isinstance(abi, list):
        abi = []
    impls = d.get("implementations") or []
    creation_tx = None
    try:
        a = await bs(chain, f"/addresses/{addr}")
        if a:
            creation_tx = a.get("creation_transaction_hash")
    except UpstreamError:
        pass
    return {
        "address": addr,
        "name": d.get("name"),
        "verified": bool(d.get("is_verified")),
        "is_verified": bool(d.get("is_verified")),
        "compiler_version": d.get("compiler_version"),
        "language": d.get("language"),
        "evm_version": d.get("evm_version"),
        "license_type": d.get("license_type"),
        "source_code": d.get("source_code"),
        "abi": abi,
        "additional_sources": d.get("additional_sources") or [],
        "proxy_type": d.get("proxy_type"),
        "implementations": [
            {"address": i.get("address_hash"), "name": i.get("name")}
            for i in impls
            if isinstance(i, dict)
        ],
        "verified_at": d.get("verified_at"),
        "optimization_enabled": d.get("optimization_enabled"),
        "creation_tx": creation_tx,
    }


async def _contract_call(chain, addr, body):
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON body required"}, status_code=400)
    fn = body.get("function") or {}
    name = fn.get("name")
    inputs = fn.get("inputs") or []
    outputs = fn.get("outputs") or []
    args = body.get("args") or []
    if not name or not isinstance(inputs, list) or not isinstance(outputs, list):
        return JSONResponse({"error": "Body must include function {name, inputs, outputs} and args"}, status_code=400)
    try:
        in_types = []
        for i in inputs:
            if not isinstance(i, dict) or "type" not in i:
                raise ValueError("Each function input needs a type")
            in_types.append(i["type"])
        out_types = []
        for o in outputs:
            if not isinstance(o, dict) or "type" not in o:
                raise ValueError("Each function output needs a type")
            out_types.append(o["type"])
        selector = _function_selector(name, in_types)
        calldata = selector + _encode_args(in_types, args)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    result = await rpc(chain, "eth_call", [{"to": addr, "data": "0x" + calldata.hex()}, "latest"])
    if not result or result == "0x":
        return JSONResponse(
            {"error": "Contract call returned no data — the function may not exist on this contract or it reverted"},
            status_code=400,
        )
    try:
        raw = bytes.fromhex(result[2:] if result.startswith("0x") else result)
        values = _decode_args(out_types, raw)
    except (ValueError, IndexError) as e:
        return JSONResponse({"error": f"Could not decode call output: {e}"}, status_code=400)
    return {
        "raw": result,
        "values": values,
        "outputs": [
            {"name": o.get("name"), "type": o.get("type"), "value": v}
            for o, v in zip(outputs, values)
        ],
    }


async def _contract_verify(chain, addr, body):
    """Forward a verification submission to Blockscout. Tries the v2
    multipart endpoint first, then the v1 form endpoint. The upstream
    result is surfaced verbatim-ish; a keyless rejection or timeout is
    reported as an error, never faked as success. NOTE (2026-11-05):
    the v2 endpoint times out from this sandbox, but the v1 endpoint
    answers keyless — a dummy submission got a real validation error
    ('Missing codeformat field', now included by default), proving the
    forwarding path works; no real contract was verified in testing."""
    if not isinstance(body, dict):
        return JSONResponse({"error": "JSON body required"}, status_code=400)
    source = body.get("source_code") or body.get("sourceCode") or ""
    name = body.get("contract_name") or body.get("contractname") or ""
    compiler = body.get("compiler_version") or body.get("compilerversion") or ""
    if not source or not name or not compiler:
        return JSONResponse(
            {"error": "Body must include source_code, contract_name and compiler_version"},
            status_code=400,
        )
    base = CHAINS[chain]["blockscout"]
    # --- v2: multipart POST /smart-contracts/{addr}/verification
    v2_fields = {
        "compiler_version": compiler,
        "contract_source_code": source,
        "contract_name": name,
        "constructor_args": body.get("constructor_args") or body.get("constructorArguements") or "",
        "evm_version": body.get("evm_version") or body.get("evmversion") or "",
        "optimization": str(body.get("optimization") if body.get("optimization") is not None else body.get("optimizationUsed") or "false").lower(),
        "optimization_runs": str(body.get("optimization_runs") or body.get("runs") or ""),
        "license_type": body.get("license_type") or body.get("licenseType") or "",
        "autodetect": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            r = await c.post(f"{base}/smart-contracts/{addr}/verification", data=v2_fields)
        try:
            payload = r.json()
        except Exception:
            payload = {"raw": r.text[:500]}
        if r.status_code == 200 and isinstance(payload, dict) and payload.get("message") != "Smart-contract already verified.":
            ok = payload.get("is_verified") or "verified" in str(payload.get("message", "")).lower()
            return {"ok": bool(ok), "message": payload.get("message"), "via": "v2", "upstream": payload}
        if r.status_code == 200:
            return {"ok": True, "message": payload.get("message"), "via": "v2", "upstream": payload}
        # fall through to v1 on any non-200 v2 response
        v2_error = {"status": r.status_code, "body": payload}
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        v2_error = {"status": None, "body": f"v2 verification endpoint unreachable ({type(e).__name__})"}
    # --- v1: form POST /api?module=contract&action=verifysourcecode
    v1_fields = {
        "contractaddress": addr,
        "contractname": name,
        "compilerversion": compiler,
        "sourceCode": source,
        "codeformat": body.get("codeformat") or "solidity-single-file",
        "constructorArguements": body.get("constructor_args") or body.get("constructorArguements") or "",
        "optimizationUsed": "1" if str(body.get("optimization") or body.get("optimizationUsed") or "").lower() in ("1", "true") else "0",
        "runs": str(body.get("optimization_runs") or body.get("runs") or "200"),
        "evmversion": body.get("evm_version") or body.get("evmversion") or "",
        "licenseType": body.get("license_type") or body.get("licenseType") or "",
    }
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            r = await c.post(f"{base}/api", data=v1_fields, params={"module": "contract", "action": "verifysourcecode"})
        try:
            payload = r.json()
        except Exception:
            payload = {"raw": r.text[:500]}
        if r.status_code == 200 and isinstance(payload, dict):
            return {"ok": payload.get("status") == "1", "message": payload.get("message") or payload.get("result"), "via": "v1", "upstream": payload}
        raise UpstreamError(f"Blockscout verification failed (v2: {v2_error}, v1: HTTP {r.status_code})")
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        raise UpstreamError(f"Blockscout verification unreachable (v2: {v2_error}; v1: {type(e).__name__})")


# ---------------------------------------------------------------- charts / gas / pending / txcount
async def _recent_blocks(chain, limit=50):
    """Latest blocks from Blockscout (default page is 50 items; this
    instance rejects page_size, so no pagination params are sent)."""
    d = await bs(chain, "/blocks")
    if d is None:
        raise UpstreamError("Blocks not available")
    return d.get("items", [])[:limit]


async def _chart_transactions(chain):
    key = f"{chain}:chart:tx"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await bs(chain, "/stats/charts/transactions")
    if d is None:
        raise UpstreamError("Transactions chart not available")
    out = {"chain": chain, "chart_data": d.get("chart_data", [])}
    _cache_set(key, out, ttl=CHART_TTL)
    return out


async def _chart_market(chain):
    key = f"{chain}:chart:market"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await bs(chain, "/stats/charts/market")
    if d is None:
        raise UpstreamError("Market chart not available")
    out = {
        "chain": chain,
        "available_supply": d.get("available_supply"),
        "chart_data": d.get("chart_data", []),
    }
    _cache_set(key, out, ttl=CHART_TTL)
    return out


async def _chart_gas(chain):
    """Gas history does not exist as a Blockscout chart on this instance
    (gas-price / average-gas-price return 'Unknown API v2 action'), so it
    is computed from the latest ~50 blocks' base_fee_per_gas."""
    key = f"{chain}:chart:gas"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    blocks = await _recent_blocks(chain, 50)
    items = [
        {
            "block": b.get("height"),
            "base_fee_gwei": gwei(b.get("base_fee_per_gas")),
            "timestamp": b.get("timestamp"),
        }
        for b in blocks
        if b.get("base_fee_per_gas") is not None
    ]
    out = {"chain": chain, "source": "base_fee_per_gas of latest blocks", "items": items}
    _cache_set(key, out)
    return out


async def _chart_price(chain, days):
    coin = CHAINS[chain]["coingecko_coin"]
    key = f"{chain}:chart:price:{days}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await coingecko(f"/coins/{coin}/market_chart", params={"vs_currency": "usd", "days": days})
    if d is None:
        raise UpstreamError("CoinGecko unreachable")
    out = {
        "chain": chain,
        "coin": coin,
        "vs_currency": "usd",
        "days": days,
        "prices": d.get("prices", []),
        "market_caps": d.get("market_caps", []),
        "total_volumes": d.get("total_volumes", []),
    }
    _cache_set(key, out, ttl=CHART_TTL)
    return out


async def _gastracker(chain):
    key = f"{chain}:gastracker"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    stats = await bs(chain, "/stats")
    if stats is None:
        raise UpstreamError("Stats not available")
    blocks = await _recent_blocks(chain, 20)
    history = [
        {"block": b.get("height"), "base_fee_gwei": gwei(b.get("base_fee_per_gas"))}
        for b in blocks
        if b.get("base_fee_per_gas") is not None
    ]
    gp = stats.get("gas_prices") or {}
    out = {
        "chain": chain,
        "gas_price_gwei": {"slow": gp.get("slow"), "average": gp.get("average"), "fast": gp.get("fast")},
        "base_fee_gwei": history[0]["base_fee_gwei"] if history else None,
        "base_fee_history": history,
        "avg_block_time_s": round(float(stats.get("average_block_time") or 0) / 1000, 1),
        "note": "Gas prices from Blockscout /stats; base-fee history computed from the latest 20 blocks.",
    }
    _cache_set(key, out)
    return out


async def _pending(chain):
    """Pending txs via the chain RPC's pending block. This is a sampled
    view of one pending block, not the full mempool."""
    result = await rpc(chain, "eth_getBlockByNumber", ["pending", True])
    txs = (result or {}).get("transactions") or [] if isinstance(result, dict) else []
    items = [rpc_tx_short(t) for t in txs[:50] if isinstance(t, dict)]
    return {
        "chain": chain,
        "sampled": True,
        "source": "pending block via public RPC",
        "note": "Sampled view: transactions from a single pending block, not the full mempool. Empty when the RPC returns no pending transactions.",
        "count": len(items),
        "items": items,
    }


async def _txcount(chain, h):
    result = await rpc(chain, "eth_getTransactionCount", [h, "latest"])
    if result is None:
        raise UpstreamError("RPC returned no transaction count")
    return {
        "address": h,
        "tx_count": int(result, 16) if isinstance(result, str) else int(result),
        "note": "nonce / outgoing-transaction count for EOAs",
    }



# ---------------------------------------------------------------- API docs data
# Single literal source of truth: each entry is expanded to one path under
# the app's own API prefix (e.g. /api/sepoliascope/stats). Examples in
# DOCS_BASE use /api/ethscope/ethereum/... and are rewritten the same way.
# ---------------------------------------------------------------- API docs data
# Single literal source of truth: each entry is expanded to a legacy
# mainnet path (/api/ethscope<suffix>) and a chain-prefixed path
# (/api/ethscope/{chain}<suffix>). The /chains and /docs endpoints are
# global and listed separately.
DOCS_BASE = [
    {"method": "GET", "suffix": "/stats", "description": "Chain stats: native price, latest block, totals, avg block time, gas prices.", "params": [], "example": "/api/ethscope/ethereum/stats"},
    {"method": "GET", "suffix": "/blocks", "description": "Latest blocks (paginated via next_page_params).", "params": ["page_number", "page_size"], "example": "/api/ethscope/ethereum/blocks"},
    {"method": "GET", "suffix": "/txs", "description": "Latest transactions (paginated via next_page_params).", "params": ["page_number"], "example": "/api/ethscope/ethereum/txs"},
    {"method": "GET", "suffix": "/block/{n}", "description": "Block by height, hash, or 'latest': details, gas, rewards, withdrawals.", "params": [], "example": "/api/ethscope/ethereum/block/latest"},
    {"method": "GET", "suffix": "/block/{n}/txs", "description": "Transactions in a block (by height or hash).", "params": ["page_number"], "example": "/api/ethscope/ethereum/block/latest/txs"},
    {"method": "GET", "suffix": "/tx/{h}", "description": "Transaction detail: fee breakdown, input hex + UTF-8 decode, token transfers.", "params": [], "example": "/api/ethscope/ethereum/tx/0x..."},
    {"method": "GET", "suffix": "/tx/{h}/explain", "description": "Plain-English explanation of a transaction: one sentence ('What happened') plus a token-flow summary (token, amount, from → to). Method names come from a local canonical signature map, then the keyless 4byte.directory API (only curated names are accepted); swap amounts come from the transaction's token transfers. An undecodable method is reported honestly as 'not in the signature database', never invented.", "params": [], "example": "/api/ethscope/ethereum/tx/0x.../explain"},
    {"method": "GET", "suffix": "/tx/{h}/token-transfers", "description": "Token transfers in a transaction.", "params": [], "example": "/api/ethscope/ethereum/tx/0x.../token-transfers"},
    {"method": "GET", "suffix": "/tx/{h}/logs", "description": "Event logs emitted by a transaction.", "params": [], "example": "/api/ethscope/ethereum/tx/0x.../logs"},
    {"method": "GET", "suffix": "/tx/{h}/internal", "description": "Internal transactions (calls) of a transaction.", "params": [], "example": "/api/ethscope/ethereum/tx/0x.../internal"},
    {"method": "GET", "suffix": "/address/{h}", "description": "Address overview: balance, ENS, contract flags, token info.", "params": [], "example": "/api/ethscope/ethereum/address/0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"},
    {"method": "GET", "suffix": "/address/{h}/txs", "description": "Transactions involving an address.", "params": ["page_number"], "example": "/api/ethscope/ethereum/address/0xd8dA.../txs"},
    {"method": "GET", "suffix": "/address/{h}/token-transfers", "description": "Token transfers involving an address.", "params": ["page_number"], "example": "/api/ethscope/ethereum/address/0xd8dA.../token-transfers"},
    {"method": "GET", "suffix": "/address/{h}/internal", "description": "Internal transactions involving an address.", "params": ["page_number"], "example": "/api/ethscope/ethereum/address/0xd8dA.../internal"},
    {"method": "GET", "suffix": "/address/{h}/tokens", "description": "Token balances with per-token USD price/value and total_usd (ERC-20 only; ERC-721/1155 get null USD fields).", "params": [], "example": "/api/ethscope/ethereum/address/0xd8dA.../tokens"},
    {"method": "GET", "suffix": "/address/{h}/txcount", "description": "Outgoing-transaction count (nonce) for an address, via public RPC.", "params": [], "example": "/api/ethscope/ethereum/address/0xd8dA.../txcount"},
    {"method": "GET", "suffix": "/address/{h}/risk", "description": "Address risk score 0-100 (low / elevated / high / critical) with human-readable reasons: direct OFAC SDN match (100/critical), N-hop proximity to a sanctioned address via a bounded funder walk over the transactions the address page loads (first page, up to 50 txs; 3 hops max), and sanctioned-mixer interaction. Keyless; label set vendored from the OFAC SDN list (2026-11-05, 514 addresses). Heuristic screening signal, not a legal determination.", "params": [], "example": "/api/ethscope/ethereum/address/0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045/risk"},
    {"method": "GET", "suffix": "/address/{h}/crosschain", "description": "Cross-chain identity: same-EOA presence on all four live EVM chains (native balance + transaction count per chain, via Blockscout; a failing chain shows 'unavailable', a 404 'no activity'), Wormhole bridge transfers involving the address (Wormholescan keyless API; Wormhole only — no all-bridge total exists keyless), and the WalletScope portfolio link. EVM-to-Solana/Bitcoin linkage is shown ONLY when an actual bridge transfer is observed; there is no keyless way to prove two different-format addresses belong to the same person.", "params": [], "example": "/api/ethscope/ethereum/address/0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045/crosschain"},
    {"method": "GET", "suffix": "/token/{h}", "description": "Token overview: name, symbol, decimals, type, supply, holders, USD price.", "params": [], "example": "/api/ethscope/ethereum/token/0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    {"method": "GET", "suffix": "/token/{h}/transfers", "description": "Transfers of a token.", "params": ["page_number"], "example": "/api/ethscope/ethereum/token/0xA0b8.../transfers"},
    {"method": "GET", "suffix": "/token/{h}/security", "description": "Token security verdict (low / elevated / high) with per-check pass/warn/fail/unknown: owner powers (ABI + live owner() eth_call), holder concentration (top-10 share), buy/sell tax (only if a standard tax function exists — otherwise 'not detectable'), honeypot simulation (read-only eth_call buy via the V2 router + sell transfer from the largest holder; 'simulation unavailable' when no pair/router exists), and liquidity (DexScreener-tracked pairs + on-chain V2 reserves / LP burn). Keyless; heuristic screening signal, not an audit.", "params": [], "example": "/api/ethscope/ethereum/token/0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48/security"},
    {"method": "GET", "suffix": "/token/{h}/instances", "description": "NFT instances (ERC-721/1155 tokens) of a collection.", "params": [], "example": "/api/ethscope/ethereum/token/0xED5AF388653567Af2F388E6224dC7C4b3241C544/instances"},
    {"method": "GET", "suffix": "/token/{h}/instance/{id}", "description": "One NFT instance: image/media/animation URLs, metadata attributes, owner.", "params": [], "example": "/api/ethscope/ethereum/token/0xED5A.../instance/1"},
    {"method": "GET", "suffix": "/token/{h}/instance/{id}/transfers", "description": "Transfers of one NFT instance.", "params": [], "example": "/api/ethscope/ethereum/token/0xED5A.../instance/1/transfers"},
    {"method": "GET", "suffix": "/contract/{addr}", "description": "Verified contract: source code, ABI, compiler, proxy info, implementations.", "params": [], "example": "/api/ethscope/ethereum/contract/0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    {"method": "POST", "suffix": "/contract/{addr}/call", "description": "Read-only contract call (eth_call). Body: {function: {name, inputs: [{name, type}], outputs: [{name, type}]}, args: [...]}. Minimal ABI codec: address, uint/int, bool, bytesN, bytes, string, arrays.", "params": ["function", "args"], "example": "POST /api/ethscope/ethereum/contract/0xA0b8.../call {\"function\": {\"name\": \"decimals\", \"inputs\": [], \"outputs\": [{\"name\": \"\", \"type\": \"uint8\"}]}, \"args\": []}"},
    {"method": "POST", "suffix": "/contract/{addr}/verify", "description": "Forward a source-verification submission to Blockscout (v2 multipart, v1 form fallback). Body: {source_code, contract_name, compiler_version, ...}. Upstream rejections are reported, never faked as success.", "params": ["source_code", "contract_name", "compiler_version"], "example": "POST /api/ethscope/ethereum/contract/0x.../verify {\"source_code\": \"...\", \"contract_name\": \"MyToken\", \"compiler_version\": \"v0.8.20+commit.a1b79de6\"}"},
    {"method": "GET", "suffix": "/charts/transactions", "description": "Daily transaction-count history (Blockscout chart, 10-min cache).", "params": [], "example": "/api/ethscope/ethereum/charts/transactions"},
    {"method": "GET", "suffix": "/charts/market", "description": "Daily price / market-cap history (Blockscout chart, 10-min cache).", "params": [], "example": "/api/ethscope/ethereum/charts/market"},
    {"method": "GET", "suffix": "/charts/gas", "description": "Base-fee history computed from the latest ~50 blocks (no Blockscout gas chart exists on this instance).", "params": [], "example": "/api/ethscope/ethereum/charts/gas"},
    {"method": "GET", "suffix": "/charts/price", "description": "Native-coin USD price history from CoinGecko (10-min cache).", "params": ["days"], "example": "/api/ethscope/ethereum/charts/price?days=30"},
    {"method": "GET", "suffix": "/gastracker", "description": "Gas prices (slow/average/fast), latest base fee, 20-block base-fee history, avg block time.", "params": [], "example": "/api/ethscope/ethereum/gastracker"},
    {"method": "GET", "suffix": "/pending", "description": "Sampled pending transactions from the chain RPC's pending block (not the full mempool).", "params": [], "example": "/api/ethscope/ethereum/pending"},
    {"method": "GET", "suffix": "/search", "description": "Universal search: addresses, tokens, transactions, blocks, ENS.", "params": ["q"], "example": "/api/ethscope/ethereum/search?q=vitalik.eth"},
]

# Global (cross-chain) endpoints: no legacy/chain-prefixed expansion —
# they aggregate all chains and are listed verbatim in _docs_payload.
DOCS_GLOBAL = [
    {"method": "GET", "path": "/api/ethscope/wallet/{address}", "description": "Cross-chain wallet (FAST): per-chain NATIVE balances with USD values, ENS, status. EVM chains + Solana (SOL via public Solana RPC, CoinGecko quote). Token holdings are NOT included — they load progressively via /wallet/{address}/tokens. total_usd here is the native-only total.", "params": [], "example": "/api/ethscope/wallet/0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"},
    {"method": "GET", "path": "/api/ethscope/wallet/{address}/tokens", "description": "Token holdings per chain (top 50 by USD value, totals, counts). Slow for token-rich addresses — Blockscout /token-balances is one giant unpaginated download. Solana SPL holdings come from the public Solana RPC (classic SPL Token program only) priced via DexScreener/CoinGecko. Optional ?chain= loads one chain only (incl. ?chain=solana).", "params": ["chain"], "example": "/api/ethscope/wallet/0xd8dA.../tokens?chain=ethereum"},
    {"method": "GET", "path": "/api/ethscope/wallet/{address}/history", "description": "Portfolio value history (ESTIMATE): today's balances x historical prices. Native coins + top 5 tokens by current value.", "params": ["days"], "example": "/api/ethscope/wallet/0xd8dA.../history?days=30"},
    {"method": "GET", "path": "/api/ethscope/wallet/{address}/pnl", "description": "Estimated realized PnL (FIFO) over the latest 50 token transfers per chain, priced at CoinGecko daily closes. Unpriced/unmatched transfers are counted and excluded, never invented.", "params": [], "example": "/api/ethscope/wallet/0xd8dA.../pnl"},
    {"method": "GET", "path": "/api/ethscope/wallet/{address}/nfts", "description": "NFT holdings across all chains (first 50 per chain).", "params": [], "example": "/api/ethscope/wallet/0xd8dA.../nfts"},
    {"method": "GET", "path": "/api/ethscope/wallet/{address}/activity", "description": "Recent transactions across all chains, merged and sorted by time (top 50), with per-chain next_page_params for Load-more. With ?chain=<id>&<cursor params> returns one further shaped page of that chain.", "params": ["chain (optional)", "block_number/index/items_count (cursor, optional)"], "example": "/api/ethscope/wallet/0xd8dA.../activity"},
    {"method": "GET", "path": "/api/ethscope/wallet/{address}/incoming", "description": "Incoming transfers only, for invoice reconciliation (phase 2, Q3): every received native + ERC-20 transfer on Ethereum/Sepolia/Optimism/Gnosis and SOL + SPL receipt on Solana for one address, newest first. Takes an address only — it accepts NO invoice data; matching (amount + due-date window) runs entirely in the browser on this payload (see #/invoices). Sampled window: first page per EVM chain (50 native txs + 50 token transfers per chain) and the latest 25 Solana signatures; SPL rows carry no symbol (coin is SPL:<mint[:8]>, match by mint address); Bitcoin is not included (the wallet serves EVM + Solana addresses only); failed txs and self-transfers are excluded.", "params": [], "example": "/api/ethscope/wallet/0xd8dA.../incoming"},
    {"method": "GET", "path": "/api/ethscope/allowance", "description": "Allowance & approval checker (phase 2, P4): read-only. Returns the token allowance a spender contract currently has from an owner (EVM: allowance(owner, spender) via public RPC eth_call; Solana: the SPL token account's delegate + delegatedAmount via getTokenAccountsByOwner), the spender's identity (Blockscout name / contract flags; the curated labels.json pill is added by the UI), whether an approval tx is required for ?amount= (allowance < amount), and the exact approval that would be needed (approve(spender, amount) — exact recommended, unlimited 2**256-1 shown only with its risk note; SPL has no unlimited approval, max is the account balance). Native coins need no approval. Token may be a contract address (any ERC-20 / SPL mint) or a symbol from the quote token list. Nothing here builds, signs, or sends a transaction — execution stays in the user's wallet. Rendered at #/allowance; quote routes carry their spender (LI.FI estimate.approvalAddress) and the #/quote screen shows an approval-needed / approved badge per route. Cached 30s.", "params": ["owner", "token (contract address, SPL mint, or symbol)", "spender (EVM address or Solana delegate)", "chain (optional, default ethereum; solana for SPL)", "amount (optional, human units)"], "example": "/api/ethscope/allowance?owner=0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045&token=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48&spender=0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D&chain=ethereum&amount=100"},
    {"method": "GET", "path": "/api/ethscope/quote", "description": "Quote & route comparison (phase 2, P1): ranked swap/bridge routes for one transfer — expected amount out, all-in fees broken out (gas paid on top, output fee already deducted from amount out), estimated time to finality, minimum received, and rate vs DeFiLlama spot. Sources: LI.FI (EVM chains only, keyless, 75 requests / 2h per shared IP — rate limits surface as an error, never as an invented quote) plus a computed Circle CCTP estimate route for USDC -> USDC. Quotes are cached 30s and labelled estimates; no transaction is created. Solana/Bitcoin have no keyless quote source and return zero routes.", "params": ["from_chain", "from_token (symbol or address)", "to_chain", "to_token (symbol or address)", "amount (human units)", "from_address (optional)"], "example": "/api/ethscope/quote?from_chain=ethereum&from_token=USDC&to_chain=optimism&to_token=USDC&amount=100"},
    {"method": "GET", "path": "/api/ethscope/quote/tokens", "description": "Token list for the quote screen: the curated, contract-verified tokens per chain, which chains LI.FI covers, which chains CCTP covers, and the honest limits (no Solana/Bitcoin quotes, no unverified Gnosis stablecoins).", "params": [], "example": "/api/ethscope/quote/tokens"},
    {"method": "GET", "path": "/api/ethscope/quote/guard", "description": "Peg guard at quote time (phase 2, P3): ok / warn / block (+ unknown when the peg cannot be verified) for one quoted coin, with thresholds stated in the response. Peg mode (one of the 8 stablecoins): live peg deviation in bps plus the phase-1 depeg early-warning score (reused, not re-implemented) — block when |deviation| > 50 bps or score >= 75, warn when |deviation| > 10 bps or score >= 50; on warn/block the lowest-depeg-score coin among the other 7 scopes is suggested as the nearest stable alternative, with its own peg state. Volatility mode (every other quotable coin): CoinGecko 24h change + change over the last ~7 daily closes + the quote's own slippage (?slippage_pct=, the best route's rate vs spot) — block at |24h| >= 15% / |7d| >= 30% / |slippage| >= 5%, warn at 5% / 10% / 1%. Screening signal only, not investment advice. Cached 60s.", "params": ["coin", "chain (optional for stablecoins, required otherwise)", "amount (optional)", "slippage_pct (optional, volatility mode)"], "example": "/api/ethscope/quote/guard?coin=<redacted>&chain=ethereum&amount=100"},
    {"method": "GET", "path": "/api/ethscope/preflight", "description": "Recipient pre-flight compliance check (phase 2, P2): pass / warn / block for one recipient address BEFORE sending. Reuses the OFAC SDN risk walk (common/risk.py), EOA-vs-contract, coin balance (balanceOf / native / SPL), frozen/blacklisted status via on-chain read functions (recipient + up to 10 recent counterparties; TUSD has no read function so its status is honestly unknown), and first-seen/activity recency. Optional ?coin= (stablecoin symbol, native symbol, 0x contract, SPL mint), ?chain= (EVM chain, default ethereum; stablecoin symbols are mainnet-only), ?amount=. Works for EVM, Solana and Bitcoin addresses and ENS names. A pass is not proof the recipient is clean — see the response's limits. Nothing here creates or signs a transaction.", "params": ["address", "coin (optional)", "chain (optional)", "amount (optional)"], "example": "/api/ethscope/preflight?address=0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045&coin=USDC&amount=100"},
    {"method": "GET", "path": "/api/ethscope/freeze/watch", "description": "Freeze / blacklist watch (phase 2, Q6): is this address currently frozen/blacklisted, and does any freeze event name it? EVM addresses: live read probe via eth_call (USDC isBlacklisted, USDT isBlackListed, PYUSD isFrozen, FDUSD frozen — pre-flight probes reused) plus the freeze/blacklist events naming the address from the same window as the per-scope /freeze (365d, 5y extended lookback for silent coins) and the address's current balance in the coin. ?coin= is a stablecoin symbol (default: USDC + USDT majors); DAI/USDe have no freeze function (not_freezable); TUSD has no on-chain read function, so its state is event-derived, never live-verified; USDD is Tron-native (not_applicable). Solana addresses: ?coin= USDC / USDT checks that coin's Solana mint, or an SPL mint address — the mint's freeze authority plus the token account's current state (frozen / initialized) via the public Solana RPC; Solana has no freeze-event scan, so the answer is current-state only. Bitcoin addresses: not_applicable (no issuer freeze). The blacklist is NOT enumerable on-chain — 'not frozen' means the live probe says not frozen and no event in the scanned window names the address. Cached 300s per address. Rendered at #/freeze-watch.", "params": ["address", "coin (optional)", "chain (optional, solana for Solana mints)"], "example": "/api/ethscope/freeze/watch?address=0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48&coin=USDC"},
    {"method": "GET", "path": "/api/ethscope/settle", "description": "Settlement tracker (phase 2, Q1): live status timeline for one transaction — submitted -> confirmed (with confirmation count) -> economically final -> bridged leg minted on the destination (where applicable) — plus the receipt fields (amount, USD at execution, fee, sender/recipient, explorer link). Chains: ethereum, sepolia, optimism, gnosis, solana, bitcoin. Bridge legs: Circle CCTP (MessageSent log -> keccak message hash -> Circle iris-api attestation; a \"complete\" attestation means the mint is claimable — the destination mint tx itself is not linked keylessly) and Wormhole (Wormholescan ?txHash=). \"Economically final\" is a convention (64 confirmations EVM / 12 Sepolia / 32 Solana slots / 6 Bitcoin), not protocol finality. USD at execution uses the execution-day price where available, else the live price labelled estimate-current, else null.", "params": ["chain", "hash"], "example": "/api/ethscope/settle?chain=ethereum&hash=0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37"},
    {"method": "GET", "path": "/api/ethscope/receipt", "description": "Shareable receipt (phase 2, Q1): receipt-only view of /settle for one transaction — amount, USD value at execution, fees, sender/recipient, confirmations, economically-final flag, bridge leg, explorer link, receipt URL and generation time. ?format=json returns this same JSON (the receipt page's Download button uses it). Rendered at #/receipt/<chain>/<hash>.", "params": ["chain", "hash", "format (optional, json)"], "example": "/api/ethscope/receipt?chain=ethereum&hash=0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37&format=json"},
    {"method": "GET", "path": "/api/ethscope/aftermath", "description": "Received-funds monitor (phase 2, Q4): what happened AFTER one receipt — the sender's onward transfers after the receipt (first page per chain: 50 native txs + 50 token transfers EVM, latest 25 Solana signatures with 10 parsed, latest 25 Bitcoin txs), direct OFAC SDN checks on every onward destination plus the counterparty's full risk walk (common/risk.py, reused), and the counterparty's frozen/blacklisted state (current read probe for the receipt token + USDC/USDT on Ethereum, plus a targeted freeze-event scan from the receipt block for freezable stablecoins on Ethereum). Forward-looking from receipt time only; a clean result is not proof of clean — onward transfers are the sender's later movements, not proven to be the same funds (no keyless taint tracking). Rendered as the Aftermath section on #/receipt/<chain>/<hash>; saved receipts are re-checked on visit at #/watchlist (localStorage only, no push alerts).", "params": ["chain", "hash"], "example": "/api/ethscope/aftermath?chain=ethereum&hash=0x3f5f7ce5cb588b8d8b443118c3610a5e19b6535faed20b94fca7f634788ddbeb"},
    {"method": "GET", "path": "/api/ethscope/accounting/export", "description": "Accounting export (phase 2, Q2): finance-grade cross-chain CSV of every send/receive for one address — date, tx hash, chain, coin, amount, USD value at time of transaction, fee in USD, counterparty, type (send/receive), FIFO cost basis and realized gain per send, plus QuickBooks/Xero general-journal columns (debit/credit accounts and legs, memo with hash). ?format=json returns the same rows plus summary, per-coin FIFO lots remaining, the full column mapping and honest-limit notes. All coins the wallet aggregates (native + tokens on Ethereum/Sepolia/Optimism/Gnosis, SOL + SPL on Solana, BTC for Bitcoin addresses). One CoinGecko daily-close series per coin; rows that fall back to the live price are labelled price_basis estimate-current; rows with no price have null USD fields, never invented. Sampled window: first page per chain (50 native txs + 50 token transfers per EVM chain, 10 Solana signatures, 25 Bitcoin txs). Token-transfer rows carry no fee (paid in native coin on the parent tx). This replaces the single-chain explorer CSV (no USD values) for bookkeeping.", "params": ["address", "format (optional, csv or json)"], "example": "/api/ethscope/accounting/export?address=0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045&format=csv"},
    {"method": "GET", "path": "/api/ethscope/recurring", "description": "Recurring-payment tracker (phase 2, Q5): repeating payments for one address — same counterparty, same coin, approximately the same amount, at a roughly regular interval. Per series: payment count, median interval + cadence label (daily / weekly / fortnightly / monthly / quarterly / half-yearly / yearly / every ~N days), expected next date, status (on_schedule / due / overdue) with missed-payment count and overdue days, late payments in the history, and amount drift (stddev / CV / max relative deviation from the median). 2-payment groups are reported as candidates (not enough to establish a cadence). ?direction=out (default, sends) / in (receives) / all. All coins the wallet aggregates (native + tokens on Ethereum/Sepolia/Optimism/Gnosis, SOL + SPL on Solana, BTC for Bitcoin addresses). Sampled window: first page per EVM chain (50 native txs + 50 token transfers per chain), latest 25 Solana signatures, latest 25 Bitcoin txs — a series is only as complete as the window, and absence of a series is not proof there is no recurring payment. Rendered at #/recurring.", "params": ["address", "direction (optional, out / in / all, default out)"], "example": "/api/ethscope/recurring?address=0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045&direction=out"},
]




def _docs_payload(api_prefix, wallet_prefix=None, chains=None, include_base=True):
    """Docs payload for one app.

    `api_prefix` — e.g. "/api/sepoliascope" — prefixes every DOCS_BASE
    entry. `wallet_prefix` — e.g. "/api/walletscope" — when given, the
    cross-chain wallet entries (DOCS_GLOBAL) are listed under it; scope
    apps omit them (the wallet lives in walletscope). `chains` lists the
    chains this app serves (default: all registered chains).
    `include_base=False` (walletscope) lists only the wallet endpoints.
    """
    endpoints = []
    if include_base:
        for e in DOCS_BASE:
            endpoints.append(
                {
                    "method": e["method"],
                    "path": api_prefix + e["suffix"],
                    "description": e["description"],
                    "params": e["params"],
                    "example": e["example"].replace("/api/ethscope/ethereum/", api_prefix + "/"),
                }
            )
    endpoints.append(
        {"method": "GET", "path": api_prefix + "/chains", "description": "List the chains this app serves, with their Blockscout/RPC endpoints and native coin.", "params": [], "example": api_prefix + "/chains"}
    )
    endpoints.append(
        {"method": "GET", "path": api_prefix + "/docs", "description": "This endpoint list.", "params": [], "example": api_prefix + "/docs"}
    )
    if wallet_prefix:
        for e in DOCS_GLOBAL:
            endpoints.append(
                {
                    "method": e["method"],
                    "path": e["path"].replace("/api/ethscope", wallet_prefix),
                    "description": e["description"],
                    "params": e["params"],
                    "example": e["example"].replace("/api/ethscope", wallet_prefix),
                }
            )
    return {"chains": list(chains) if chains is not None else list(CHAINS.keys()), "endpoints": endpoints}
