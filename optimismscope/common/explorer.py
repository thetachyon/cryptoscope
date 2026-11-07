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
from datetime import datetime, timezone

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
    _hex_int,
    _instance_short,
    addr_info,
    age,
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
        # Blockscout's detail endpoint lags its own list by ~1 min for
        # fresh txs (verified live 2026-11-07: the list showed the tx,
        # detail 404'd, then 200'd 45s later). A click from Latest
        # Transactions must not 404, so build the page from the RPC.
        return await _tx_from_rpc(chain, h)
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


async def _tx_from_rpc(chain, h):
    """Tx detail from the public RPC when Blockscout has not indexed the
    tx yet. Same response shape as _tx; Blockscout-only enrichment
    (token transfers, decoded input, historic price) is empty and the
    response is flagged `indexing` while the tx is confirmed but not
    indexed yet, so the frontend can say so instead of showing a
    "not found". A hash the RPC does not know either still 404s, so
    the frontend's block-hash fallback keeps working."""
    tx = await rpc(chain, "eth_getTransactionByHash", [h])
    if not isinstance(tx, dict) or not tx.get("hash"):
        return JSONResponse({"error": "Transaction not found"}, status_code=404)
    try:
        rc = await rpc(chain, "eth_getTransactionReceipt", [h])
    except UpstreamError:
        rc = None
    if not isinstance(rc, dict):
        rc = {}
    block_no = _hex_int(tx.get("blockNumber")) if tx.get("blockNumber") else None
    # Timestamp + base fee from the block header (confirmed txs only).
    ts_iso = None
    base_fee = None
    if block_no:
        try:
            blk = await rpc(chain, "eth_getBlockByNumber", [hex(block_no), False])
        except UpstreamError:
            blk = None
        if isinstance(blk, dict):
            ts_raw = _hex_int(blk.get("timestamp"))
            if ts_raw:
                ts_iso = datetime.fromtimestamp(ts_raw, tz=timezone.utc).isoformat().replace("+00:00", "Z")
            if blk.get("baseFeePerGas"):
                base_fee = _hex_int(blk.get("baseFeePerGas"))
    # Confirmations from the chain head.
    confirmations = None
    if block_no:
        try:
            head = await rpc(chain, "eth_blockNumber", [])
        except UpstreamError:
            head = None
        if head:
            confirmations = max(0, _hex_int(head) - block_no + 1)
    gas_used = _hex_int(rc.get("gasUsed")) if rc.get("gasUsed") else None
    eff_price = _hex_int(rc.get("effectiveGasPrice")) if rc.get("effectiveGasPrice") else _hex_int(tx.get("gasPrice"))
    fee_wei = gas_used * eff_price if gas_used and eff_price else 0
    raw = tx.get("input") or "0x"
    out = rpc_tx_short(tx)
    out.update(
        {
            "block": block_no,
            "timestamp": ts_iso,
            "age": age(ts_iso) if ts_iso else "",
            "status": "success" if rc.get("status") == "0x1" else ("failed" if rc.get("status") == "0x0" else "pending"),
            "confirmations": confirmations,
            "gas_limit": _hex_int(tx.get("gas")),
            "gas_used": gas_used,
            "gas_price_gwei": gwei(eff_price) if eff_price else "0.00",
            "max_fee_per_gas_gwei": gwei(_hex_int(tx.get("maxFeePerGas"))) if tx.get("maxFeePerGas") else "0.00",
            "max_priority_fee_gwei": gwei(_hex_int(tx.get("maxPriorityFeePerGas"))) if tx.get("maxPriorityFeePerGas") else "0.00",
            "base_fee_per_gas_gwei": gwei(base_fee) if base_fee else None,
            "fee_eth": wei_to_eth(fee_wei),
            "fee_type": str(_hex_int(tx.get("type"))) if tx.get("type") is not None else None,
            "burnt_fee_eth": wei_to_eth(gas_used * base_fee) if gas_used and base_fee else None,
            "revert_reason": None,
            "created_contract": rc.get("contractAddress"),
            "input": raw,
            "input_truncated": len(raw) > 2000,
            "input_utf8": decode_input(raw),
            "has_decoded_input": False,
            "decoded_method": None,
            "token_transfers": [],
            "token_transfers_overflow": False,
            "historic_eth_price_usd": None,
            "type": _hex_int(tx.get("type")) if tx.get("type") is not None else None,
            "position": _hex_int(tx.get("transactionIndex")) if tx.get("transactionIndex") else None,
            "indexing": bool(block_no),
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
    {'method': 'GET',
     'path': '/api/ethscope/quote/with-guard',
     'description': 'Quote & route API (phase 3, item 11): the phase 2, P1 quote engine plus the phase 2, P3 peg/volatility guard for both legs, in a single call — ranked swap/bridge routes (expected '
                    'amount out, all-in fees broken out, estimated time, minimum received, rate vs DeFiLlama spot) AND the guard verdict (ok / warn / block / unknown) for the coin you pay and the coin '
                    'you receive, plus a guard_summary (worst leg verdict + blocked flag). A new dedicated endpoint, not a rename: GET /api/walletscope/quote stays the engine backing and GET '
                    '/api/walletscope/quote/guard the standalone guard check, while /quote/with-guard is the productized contract. Built on common/quotes.py get_quote and common/guard.py quote_guard '
                    "wholesale (see common/quote_api.py); the best route's rate-vs-spot % is passed as the guard's slippage_pct input, exactly as the #/quote page does. Sources: LI.FI (EVM chains only, "
                    'keyless, 75 requests / 2h per shared IP — rate limits surface in `sources` as an error, never as an invented quote) plus a computed Circle CCTP estimate route for USDC -> USDC (1:1 '
                    'burn/mint, no bridge fee, gas from the live gas price, ~13-19 min — labelled estimate, no API returns CCTP pricing). Swap API returns 404 on every path probed and OpenOcean is '
                    'Cloudflare-blocked from this server, so neither contributes routes. Solana/Bitcoin have no keyless quote source and return zero routes, honestly — the guard verdicts are still '
                    'computed. A leg whose guard check fails is reported `unknown` with the error; the routes are still shown. A `block` guard verdict is a screening signal, not a transaction block. '
                    'Rendered at #/quote, which calls this endpoint and shows the routes, the embedded guard banners, and the API contract card. example_response below is the live response with routes '
                    'trimmed to the top 2 (15 routes returned in full). Guard thresholds: peg mode block at |deviation| > 50 bps or depeg-risk score >= 75, warn at > 10 bps or >= 50; volatility mode '
                    'block at |24h| >= 15% / |7d| >= 30% / |slippage| >= 5%, warn at 5% / 10% / 1%.',
     'params': ['from_chain', 'from_token (symbol or address)', 'to_chain', 'to_token (symbol or address)', 'amount (human units)', 'from_address (optional)'],
     'example': '/api/ethscope/quote/with-guard?from_chain=ethereum&from_token=USDC&to_chain=optimism&to_token=USDC&amount=100',
     'paid_api': True,
     'example_response': {'from': {'chain': 'ethereum', 'token': 'USDC', 'amount': '100', 'amount_raw': '100000000', 'amount_usd': 99.98785394999999},
                          'to': {'chain': 'optimism', 'token': 'USDC'},
                          'quoted_at': '2026-11-06T23:51:47.865720Z',
                          'cache_ttl_seconds': 30,
                          'routes': [{'source': 'Circle CCTP (estimate)',
                                      'kind': 'cctp',
                                      'via': 'Circle CCTP (native burn-and-mint)',
                                      'steps': ['Burn 100 USDC on Ethereum (TokenMessenger)', 'Circle attests the burn (~13-19 min)', 'Mint 100 USDC on Optimism (MessageTransmitter)'],
                                      'amount_out': '100',
                                      'amount_out_min': '100',
                                      'amount_out_usd': 99.98785394999999,
                                      'fees': {'gas_usd': 0.0184,
                                               'output_fee_usd': 0.0,
                                               'output_fee_detail': [],
                                               'note': 'No bridge fee and no slippage by construction (1:1 burn/mint). Gas is an estimate from the live gas price x a typical gas budget (200k burn + 150k '
                                                       'mint).'},
                                      'net_usd': 99.96948775034843,
                                      'time_seconds_est': 780,
                                      'time_label': '~13-19 min',
                                      'minimum_received': '100',
                                      'spender': None,
                                      'spenders': [],
                                      'approval_note': "CCTP burn requires approving Circle's TokenMessenger contract first — its address is not carried by any keyless quote source, so check it ad-hoc "
                                                       'on the allowance page (#/allowance) with the messenger address for your chain.',
                                      'vs_spot_pct': 0.0,
                                      'rank': 1},
                                     {'source': 'LI.FI',
                                      'kind': 'bridge',
                                      'via': 'AcrossV4',
                                      'steps': ['USDC -> USDC (cross-chain) via AcrossV4'],
                                      'amount_out': '99.739739',
                                      'amount_out_min': '99.739739',
                                      'amount_out_usd': 99.7884,
                                      'fees': {'gas_usd': 0.0236,
                                               'output_fee_usd': 0.2601,
                                               'output_fee_detail': [{'name': 'LIFI Fixed Fee', 'amount_usd': 0.2498, 'token': 'USDC'},
                                                                     {'name': 'Relayer fee', 'amount_usd': 0.01, 'token': 'USDC'},
                                                                     {'name': 'Relayer gas fee', 'amount_usd': 0.0003, 'token': 'USDC'}],
                                               'note': "Output fee is already deducted from the amount out; gas is paid on top in the source chain's native coin."},
                                      'net_usd': 99.7648,
                                      'time_seconds_est': 2,
                                      'time_label': '~2s',
                                      'minimum_received': '99.739739',
                                      'spender': '0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE',
                                      'spenders': [{'step': 1, 'tool': 'AcrossV4', 'approval_address': '0x1231DEB6f5749EF6cE6943a275A1D3E7486F4EaE'}],
                                      'vs_spot_pct': -0.26,
                                      'rank': 2}],
                          'sources': {'LI.FI': {'status': 'ok', 'routes': 14}, 'Circle CCTP (estimate)': {'status': 'ok', 'routes': 1}},
                          'notes': [],
                          'disclaimer': 'Estimates only, not executable quotes: amounts, fees and times move with the market and the quote expires quickly. No transaction is created or signed here.',
                          'quote_api': True,
                          'guard': {'from_leg': {'coin': 'USDC',
                                                 'symbol': 'USDC',
                                                 'chain': 'ethereum',
                                                 'chain_label': 'Ethereum',
                                                 'amount': '100',
                                                 'method': 'Peg guard at quote time (phase 2, P3). Peg mode (stablecoins): live peg deviation from DeFiLlama plus the phase-1 depeg early-warning score '
                                                           '(Par v0 provisional weights, UNCONFIRMED) — the score is reused from common/stablecoin_score, not re-implemented. Volatility mode (all other '
                                                           "coins): CoinGecko 24h change plus the change over the last ~7 daily closes, plus the quote's own rate-vs-spot figure when the quote screen "
                                                           'passes it as slippage_pct. Thresholds are stated in the response. Screening signal only — not investment advice, not a guarantee.',
                                                 'limits': ['A guard verdict is a screening signal at quote time, not a guarantee: the peg score is a heuristic (Par v0 provisional weights, UNCONFIRMED), '
                                                            "volatility is backward-looking daily closes, and slippage is the quote's own estimate.",
                                                            'The peg price is chain-independent (DeFiLlama global price for the coin); the chain parameter is context only for stablecoins.',
                                                            'The alternative suggestion is the lowest depeg-risk score among the other stablecoin scopes at check time — it is a suggestion, not a '
                                                            'recommendation to switch, and its own peg state is shown so a bad alternative is visible.'],
                                                 'as_of': 1794009115,
                                                 'scope': 'usdcscope',
                                                 'name': 'USD Coin',
                                                 'issuer': 'Circle',
                                                 'mode': 'peg',
                                                 'verdict': 'ok',
                                                 'verdict_reasons': ['Peg holds: deviation -1.66 bps, depeg-risk score 20.8 (low).'],
                                                 'peg': {'price_usd': 0.9998335366500001, 'price_source': 'defillama', 'deviation_bps': -1.66, 'peg_health': 'pegged', 'depeg_threshold_bps': 50},
                                                 'depeg_score': {'score': 20.8,
                                                                 'band': 'low',
                                                                 'inputs_unknown': [],
                                                                 'weights_used': {'redemption_quality': 0.4, 'reserve_liquidity': 0.25, 'secondary_market_depth': 0.2, 'issuer_operational': 0.15},
                                                                 'sub_scores': {'redemption_quality': 1.0, 'reserve_liquidity': 12.5, 'secondary_market_depth': 45.4, 'issuer_operational': 54.5},
                                                                 'error': None},
                                                 'volatility': None,
                                                 'slippage': None,
                                                 'alternative': None,
                                                 'alternative_note': None,
                                                 'thresholds': {'block': 'Block when |deviation| > 50 bps (currently off-peg) or depeg-risk score >= 75',
                                                                'warn': 'Warn when |deviation| > 10 bps or depeg-risk score >= 50',
                                                                'ok': 'Ok when |deviation| <= 10 bps and depeg-risk score < 50',
                                                                'block_deviation_bps': 50,
                                                                'block_score': 75,
                                                                'warn_deviation_bps': 10,
                                                                'warn_score': 50}},
                                    'to_leg': {'coin': 'USDC',
                                               'symbol': 'USDC',
                                               'chain': 'optimism',
                                               'chain_label': 'Optimism',
                                               'amount': '100',
                                               'method': 'Peg guard at quote time (phase 2, P3). Peg mode (stablecoins): live peg deviation from DeFiLlama plus the phase-1 depeg early-warning score (Par '
                                                         'v0 provisional weights, UNCONFIRMED) — the score is reused from common/stablecoin_score, not re-implemented. Volatility mode (all other coins): '
                                                         "CoinGecko 24h change plus the change over the last ~7 daily closes, plus the quote's own rate-vs-spot figure when the quote screen passes it as "
                                                         'slippage_pct. Thresholds are stated in the response. Screening signal only — not investment advice, not a guarantee.',
                                               'limits': ['A guard verdict is a screening signal at quote time, not a guarantee: the peg score is a heuristic (Par v0 provisional weights, UNCONFIRMED), '
                                                          "volatility is backward-looking daily closes, and slippage is the quote's own estimate.",
                                                          'The peg price is chain-independent (DeFiLlama global price for the coin); the chain parameter is context only for stablecoins.',
                                                          'The alternative suggestion is the lowest depeg-risk score among the other stablecoin scopes at check time — it is a suggestion, not a '
                                                          'recommendation to switch, and its own peg state is shown so a bad alternative is visible.'],
                                               'as_of': 1794009115,
                                               'scope': 'usdcscope',
                                               'name': 'USD Coin',
                                               'issuer': 'Circle',
                                               'mode': 'peg',
                                               'verdict': 'ok',
                                               'verdict_reasons': ['Peg holds: deviation -1.66 bps, depeg-risk score 20.8 (low).'],
                                               'peg': {'price_usd': 0.9998335366500001, 'price_source': 'defillama', 'deviation_bps': -1.66, 'peg_health': 'pegged', 'depeg_threshold_bps': 50},
                                               'depeg_score': {'score': 20.8,
                                                               'band': 'low',
                                                               'inputs_unknown': [],
                                                               'weights_used': {'redemption_quality': 0.4, 'reserve_liquidity': 0.25, 'secondary_market_depth': 0.2, 'issuer_operational': 0.15},
                                                               'sub_scores': {'redemption_quality': 1.0, 'reserve_liquidity': 12.5, 'secondary_market_depth': 45.4, 'issuer_operational': 54.5},
                                                               'error': None},
                                               'volatility': None,
                                               'slippage': None,
                                               'alternative': None,
                                               'alternative_note': None,
                                               'thresholds': {'block': 'Block when |deviation| > 50 bps (currently off-peg) or depeg-risk score >= 75',
                                                              'warn': 'Warn when |deviation| > 10 bps or depeg-risk score >= 50',
                                                              'ok': 'Ok when |deviation| <= 10 bps and depeg-risk score < 50',
                                                              'block_deviation_bps': 50,
                                                              'block_score': 75,
                                                              'warn_deviation_bps': 10,
                                                              'warn_score': 50}}},
                          'guard_summary': {'from_verdict': 'ok',
                                            'to_verdict': 'ok',
                                            'worst_verdict': 'ok',
                                            'blocked': False,
                                            'note': 'Worst of the two leg verdicts (block > warn > unknown > ok). A block verdict is a screening signal — do not pay in that coin — not a transaction '
                                                    'block.'},
                          'method': 'Quote & route API (phase 3, item 11). The phase 2, P1 quote engine (common/quotes.py get_quote: ranked LI.FI / Circle CCTP routes, fees, times, rate vs DeFiLlama '
                                    "spot) reused wholesale, plus the phase 2, P3 peg/volatility guard (common/guard.py quote_guard) for both legs in the same response — the best route's rate-vs-spot % "
                                    "is passed as the guard's slippage_pct input, exactly as the #/quote page does. This module only adds the guard block, the guard summary (worst leg verdict + blocked "
                                    'flag), and the contract fields (method, limits, api/quote/guard URLs).',
                          'limits': ['Quotes are estimates, not executable quotes: amounts, fees and times move with the market and the quote expires quickly (cached 30s server-side). No transaction is '
                                     'created or signed here.',
                                     'LI.FI covers EVM chains only, keyless, 75 requests / 2h per shared egress IP — a rate limit surfaces in `sources` as an error and the CCTP estimate route (if any) '
                                     'is still shown; a quote is never invented to fill the gap.',
                                     'Swap API (swapapi.dev) returns 404 on every path probed and OpenOcean is Cloudflare-blocked from this server, so neither contributes routes — do not re-add without '
                                     're-probing.',
                                     'Solana and Bitcoin have no keyless quote source reachable from this server (LI.FI lists EVM chains only; Jupiter is unreachable from the sandbox) — requests '
                                     'involving them return zero routes, honestly, never a fabricated quote.',
                                     'CCTP routes are computed estimates (1:1 burn/mint, no bridge fee, gas from the live gas price x a typical gas budget, ~13-19 min for the attestation), labelled as '
                                     'such in every route — no API returns CCTP pricing.',
                                     'A guard verdict is a screening signal at quote time, not investment advice and not a guarantee: the peg score is the Par v0 provisional heuristic (UNCONFIRMED '
                                     "weights), volatility is backward-looking (daily closes), and the slippage figure is the quote's own estimate.",
                                     'A leg whose guard check fails is reported `unknown` with the error — the routes are still shown and the response never claims a verdict it did not measure.'],
                          'api_url': '/api/walletscope/quote/with-guard?from_chain=ethereum&from_token=USDC&to_chain=optimism&to_token=USDC&amount=100',
                          'quote_url': '/api/walletscope/quote?from_chain=ethereum&from_token=USDC&to_chain=optimism&to_token=USDC&amount=100',
                          'guard_urls': {'from_leg': '/api/walletscope/quote/guard?coin=USDC&chain=ethereum&amount=100&slippage_pct=0.0',
                                         'to_leg': '/api/walletscope/quote/guard?coin=USDC&chain=optimism&amount=100&slippage_pct=0.0'}},
     'free_tier': 'Free tier today: keyless, no auth, no rate limit of our own. One transfer per call; quote cached 30s server-side, guard cached 60s (a repeat call inside the windows is served from '
                  "cache). A cold call fans out to LI.FI (one POST /v1/advanced/routes) plus the guard's live inputs (DeFiLlama peg price + phase-1 depeg score for stablecoins, CoinGecko 24h/7d moves "
                  "otherwise) and took ~15s live for the example below. Upstream rate limits apply — LI.FI's keyless budget is 75 requests / 2h per shared egress IP, so expect occasional LI.FI errors "
                  'surfaced in `sources` (the CCTP estimate route, if any, is still shown). The standalone /quote and /quote/guard endpoints stay free and keyless alongside this one.',
     'paid_tier': 'Paid tier (planned): API key required; calls metered per quote. Higher rate limits (a dedicated LI.FI key so the shared-IP budget no longer binds), longer quote caching / fresher '
                  're-quotes on demand, batch quotes for many transfers in one call, and webhooks on guard-verdict changes (ok -> warn/block) for a watched pair. No auth, metering, or billing is '
                  'enforced yet — this endpoint is free and keyless today.'},
    {"method": "GET", "path": "/api/ethscope/screen/address", "description": "Address screening API (phase 3, item 1): a single address in, score + band + explicit reasons + hop exposure out. Productizes the OFAC SDN risk walk (common/risk.py, reused — vendored OFAC SDN list, 514 addresses, 2026-11-05; 3-hop EVM/Bitcoin walk, 1-hop Solana) as a dedicated endpoint. Family is detected from the address (EVM / Solana / Bitcoin); ENS names resolve on Ethereum mainnet. Verdict: block (direct SDN match, or sanctioned counterparty 1 hop away), review (sanctioned counterparty or mixer 2-3 hops out), clear (nothing found in the transactions checked). ?chain= picks the EVM chain for the walk (default ethereum). A clear verdict is not proof the address is clean — see the response's limits. Rendered at #/screen.", "params": ["address", "chain (optional, EVM chain, default ethereum)"], "example": "/api/ethscope/screen/address?address=vitalik.eth",
     "paid_api": True,
     "example_response": {"address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045", "resolved_from": "vitalik.eth", "family": "evm", "chain": "ethereum", "chain_label": "Ethereum", "score": 0, "level": "low", "verdict": "clear", "verdict_reason": "Nothing found in the transactions checked — no OFAC SDN match for this address or its checked counterparties. This is not proof the address is clean (see limits).", "reasons": ["No OFAC SDN match for this address.", "No sanctioned counterparties among the 260 counterparties checked (3 hops, 498 transactions scanned)."], "direct_hit": None, "nearest_hop": None, "hits": [], "hits_total": 0, "exposure": {"nearest_sanctioned_hop": None, "sanctioned_hits_by_hop": {}, "hits_shown": 0, "counterparties_checked": 260, "txs_scanned": 498, "max_hop": 3, "walk_partial": False, "note": "Funder walk covers the first page of this address's transactions (up to 50, newest first) plus one page each for up to 10 first-hop and 5 second-hop counterparties — 3 hops max. Older transactions are not checked."}, "limits": ["A clear verdict is not proof the address is clean: the walk covers only the transactions the address page loads (first page), and the SDN list is a dated vendored snapshot (2026-11-05).", "Solana: the SDN list holds only 2 Solana addresses and the walk is 1 hop only (counterparties from the 10 most recent transactions); deeper hops are not available via the public RPC in this version.", "Tornado Cash is not in the vendored set — OFAC removed it from the SDN list in March 2025.", "Scores are a heuristic screening signal, not a legal determination."]},
     "free_tier": "Free tier today: keyless, no auth, no rate limit of our own. One address per call; risk walk cached 10 minutes server-side (screen response cached 60s). EVM walk covers the first tx page (up to 50 txs) plus one page each for up to 10 first-hop and 5 second-hop counterparties (3 hops max); Bitcoin 25 most recent txs with the same hop budgets; Solana 1 hop from the 10 most recent transactions. Upstream rate limits (Blockscout, public RPCs, mempool.space) apply — expect occasional 502s on slow walks.",
     "paid_tier": "Paid tier (planned): API key required; calls metered per screen. Higher rate limits, deeper walks (more hops, more transactions scanned), a fresher OFAC label set than the vendored 2026-11-05 snapshot, and batch screening of many addresses in one call. No auth, metering, or billing is enforced yet — this endpoint is free and keyless today."},
    {"method": "GET", "path": "/api/ethscope/screen/transaction", "description": "Transaction screening API (phase 3, item 2): a single transaction in, score + band + explicit reasons + hop exposure out, with the settlement summary (value, USD at execution, confirmations, economically-final flag). Reuses the settlement tracker (common/settlement.py, phase 2 Q1) for the tx data and the OFAC SDN risk walk (common/risk.py, vendored OFAC SDN list, 514 addresses, 2026-11-05) for sanctions classification: every counterparty of the tx is checked directly against the SDN list (a direct counterparty on the list scores 100/critical), and a capped number of counterparties (4, sender/recipient first) get the full funder walk, with hits shifted by one hop (scored exposure stops 3 hops from the transaction on EVM/Bitcoin, 2 hops on Solana where the walk is 1 hop only). Verdict: block (direct SDN counterparty, or score >= 75), review (score 40-74), clear (nothing found in the counterparties checked). ?chain= is the tx's chain (ethereum / sepolia / optimism / gnosis / solana / bitcoin); ?hash= the tx hash / signature / txid. A clear verdict is not proof the transaction is clean \u2014 see the response's limits. Rendered at #/txscreen.", "params": ["chain", "hash"], "example": "/api/ethscope/screen/transaction?chain=ethereum&hash=0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37",
     "paid_api": True,
     "example_response": {'tx_hash': '0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'chain': 'ethereum', 'chain_label': 'Ethereum', 'kind': 'evm', 'score': 0, 'level': 'low', 'verdict': 'clear', 'verdict_reason': "Nothing found in the counterparties checked — no OFAC SDN match for this transaction's counterparties or within the walked hops. This is not proof the transaction is clean (see limits).", 'reasons': ['No OFAC SDN match among the 4 counterparties checked directly.', 'No sanctioned counterparties within 3 hops of the 4 walked counterparties (1571 transactions scanned in their walks).'], 'direct_hits': [], 'nearest_hop': None, 'hits': [], 'hits_total': 0, 'hits_beyond_scope': 0, 'mixer_hits': [], 'exposure': {'nearest_sanctioned_hop': None, 'sanctioned_hits_by_hop': {}, 'hits_shown': 0, 'counterparties_checked': 4, 'counterparties_walked': 4, 'txs_scanned': 1571, 'max_hop': 3, 'walk_partial': False, 'note': 'Direct SDN check on all 4 counterparties; full funder walk on 4 (cap 4, sender/recipient first). Scored exposure stops 3 hops from this transaction.'}, 'settlement': {'status': 'success', 'confirmations': 5605, 'confirmations_unit': 'confirmations', 'finality_threshold': 64, 'economically_final': True, 'block': 26353245, 'timestamp': '2026-11-06T03:53:59.000000Z', 'age': '18 hrs 45 mins ago', 'from': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491', 'to': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D', 'amount': {'value': '0.03', 'symbol': 'WETH', 'kind': 'token', 'token_address': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', 'usd_value': 67.17, 'usd_price': 2239.1360259887474, 'price_basis': 'at-execution'}, 'value': '0.03', 'symbol': 'WETH', 'usd_value': 67.17, 'usd_price': 2239.1360259887474, 'price_basis': 'at-execution', 'fee': {'value': '0.00000554314888086', 'symbol': 'ETH', 'usd_value': 0.01, 'usd_price': 2240.055326092504, 'price_basis': 'at-execution'}, 'explorer_url': 'https://eth.blockscout.com/tx/0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'bridge': None}, 'limits': ['A clear verdict is not proof the transaction is clean: only the listed counterparties are checked directly, only a capped number of them get the full funder walk, and each walk covers only the transactions its page loads.', 'EVM: at most 4 counterparties walked (sender/recipient first); each walk is the address walk (first tx page plus one page each for up to 10 first-hop and 5 second-hop counterparties) — scored exposure stops 3 hops from this transaction.', 'Solana: at most 4 counterparties walked, and each Solana walk is 1 hop only — scored exposure stops 2 hops from this transaction. The SDN list holds only 2 Solana addresses.', 'Bitcoin: at most 4 counterparties walked (25 most recent txs per counterparty page); all output addresses are checked directly, but change outputs cannot be distinguished from payment outputs, so every output counts as a counterparty.', 'Internal EVM transfers (value moved inside a contract call with no token-transfer record) are not counterparties here — only the sender/recipient and the recorded token transfers are checked.', 'Tornado Cash is not in the vendored set — OFAC removed it from the SDN list in March 2025.', 'Scores are a heuristic screening signal, not a legal determination.']},
     "free_tier": "Free tier today: keyless, no auth, no rate limit of our own. One transaction per call; response cached 60s server-side (counterparty risk walks cached 10 minutes). Direct SDN check on all counterparties (EVM up to 10, Solana up to 12, Bitcoin up to 12); full funder walk on at most 4 counterparties, sender/recipient first. Upstream rate limits (Blockscout, public RPCs, mempool.space) apply \u2014 expect occasional 502s on slow walks.",
     "paid_tier": "Paid tier (planned): API key required; calls metered per screen. Higher rate limits, deeper walks (more counterparties walked, more hops, more transactions scanned), a fresher OFAC SDN label set than the vendored 2026-11-05 snapshot, and batch screening of many transactions in one call. No auth, metering, or billing is enforced yet \u2014 this endpoint is free and keyless today."},
    {'method': 'GET', 'path': '/api/ethscope/sanctions', 'description': 'Sanctions list API (phase 3, item 3): the vendored OFAC SDN crypto-address set as a queryable list — filter by program (?program=DPRK3 / CYBER2 / ...), chain family (?family=evm / bitcoin / solana / other), and SDN-name substring (?name=LAZARUS), with pagination (?page=, ?page_size=, default 50, max 100). Reuses the OFAC SDN label set from common/risk.py wholesale (514 addresses, snapshot 2026-11-05) — nothing is re-parsed. Every response carries source metadata (source, snapshot date, totals), counts by family / currency / program, and a `changes` block diffing the live set against the committed baseline snapshot (common/data/ofac_sdn_snapshot_2026-11-05.json): the snapshot shipped with this build IS the baseline, so added / removed are empty and the response says so — no newly-listed / delisted history exists before 2026-11-05 and none is claimed. On a rebuild from a fresh sdn.csv, run scripts/sdn_diff.py before rewriting the snapshot; added / removed then appear here. Program counts count each program tag separately, so they sum to more than the total. The list is a dated snapshot, not the live OFAC list, and a screening reference, not a legal determination. Rendered at #/sanctions.', 'params': ['program (optional, e.g. DPRK3)', 'family (optional, evm / bitcoin / solana / other)', 'name (optional, SDN-name substring)', 'page (optional, default 1)', 'page_size (optional, default 50, max 100)'], 'example': '/api/ethscope/sanctions?program=DPRK3&page_size=2', 'paid_api': True, 'example_response': {'entries': [{'address': '0x08723392ed15743cc38513c4925f5e6be5c17243', 'currency': 'ETH', 'family': 'evm', 'name': 'LAZARUS GROUP', 'programs': ['DPRK3'], 'program': '[DPRK3]', 'mixer': False}, {'address': '0x098b716b8aaf21512996dc57eb0615e2383e2f96', 'currency': 'ETH', 'family': 'evm', 'name': 'LAZARUS GROUP', 'programs': ['DPRK3'], 'program': '[DPRK3]', 'mixer': False}], 'page': 1, 'page_size': 2, 'total_pages': 26, 'total_filtered': 51, 'total': 514, 'filters': {'program': 'DPRK3', 'family': None, 'name': None}, 'counts': {'total': 514, 'by_family': {'bitcoin': 260, 'evm': 94, 'other': 158, 'solana': 2}, 'by_currency': {'BCH': 2, 'BNB': 1, 'BTG': 1, 'DASH': 5, 'DOGE': 2, 'ETH': 90, 'LTC': 8, 'SOL': 2, 'TRX': 92, 'USDT': 41, 'XBT': 260, 'XMR': 6, 'XVG': 1, 'ZEC': 3}, 'by_program': {'CAATSA - RUSSIA': 2, 'CYBER2': 176, 'CYBER3': 6, 'CYBER4': 49, 'DPRK2': 9, 'DPRK3': 51, 'DPRK4': 24, 'ELECTION-EO13848': 41, 'FTO': 17, 'HRIT-IR': 7, 'IFSR': 31, 'ILLICIT-DRUGS-EO14059': 72, 'IRAN': 6, 'IRAN-EO13902': 8, 'IRGC': 16, 'NPWMD': 18, 'RUSSIA-EO14024': 33, 'SDGT': 88, 'SDNTK': 12, 'TCO': 32, 'UKRAINE-EO13661': 2}, 'program_count_note': 'Program counts count each program tag separately: an entry listed under two programs is counted once in the total but once under each program, so program counts sum to more than the total.'}, 'programs': ['CAATSA - RUSSIA', 'CYBER2', 'CYBER3', 'CYBER4', 'DPRK2', 'DPRK3', 'DPRK4', 'ELECTION-EO13848', 'FTO', 'HRIT-IR', 'IFSR', 'ILLICIT-DRUGS-EO14059', 'IRAN', 'IRAN-EO13902', 'IRGC', 'NPWMD', 'RUSSIA-EO14024', 'SDGT', 'SDNTK', 'TCO', 'UKRAINE-EO13661'], 'families': ['evm', 'bitcoin', 'solana', 'other'], 'sdn': {'source': 'U.S. Treasury OFAC Specially Designated Nationals (SDN) list — sdn.csv, https://www.treasury.gov/ofac/downloads/sdn.csv', 'date': '2026-11-05', 'labels_total': 514}, 'changes': {'available': True, 'baseline': True, 'baseline_date': '2026-11-05', 'snapshot_file': 'ofac_sdn_snapshot_2026-11-05.json', 'snapshot_entries': 514, 'added': [], 'removed': [], 'added_count': 0, 'removed_count': 0, 'note': 'Diffed against the committed baseline snapshot (2026-11-05). This snapshot was generated from the same set it ships with, so it IS the baseline: no newly-listed / delisted history exists before 2026-11-05 and none is claimed. On a rebuild from a fresh sdn.csv, run scripts/sdn_diff.py before rewriting the snapshot — added / removed entries will appear here until the snapshot is updated.'}, 'method': 'Sanctions list (phase 3, item 3). Queryable OFAC SDN crypto-address list from common/risk.py (vendored OFAC SDN list, 514 addresses, 2026-11-05). Screening reference, not a legal determination.', 'limits': ['The list is a dated vendored snapshot (2026-11-05), not the live OFAC SDN list. Listings / delistings after that date are not reflected until the set is rebuilt from a fresh sdn.csv.', "Family 'other' covers the 158 addresses on chains Cryptoscope does not score (Tron, Monero, Litecoin, ...). They are listed for reference; the screen endpoints only score EVM, Bitcoin and Solana.", 'Program counts count each program tag separately: an entry listed under two programs is counted once in the total but once under each program, so program counts sum to more than the total.', 'Tornado Cash is not in the vendored set — OFAC removed it from the SDN list in March 2025.', 'This list is a screening reference, not a legal determination.']}, 'free_tier': 'Free tier today: keyless, no auth, no rate limit of our own. The whole list is served from memory (vendored snapshot, 514 addresses); one page per call, up to 100 entries. No upstream calls are made, so this endpoint never 502s.', 'paid_tier': 'Paid tier (planned): API key required; calls metered per list call. A fresher OFAC SDN label set than the vendored 2026-11-05 snapshot, change feeds (newly listed / delisted since the last snapshot, computed against the committed baseline), full-list export (CSV / JSON, no pagination), and webhook alerts on listings. No auth, metering, or billing is enforced yet — this endpoint is free and keyless today.'},
    {'method': 'GET', 'path': '/api/ethscope/freeze/feed', 'description': "Freeze event feed (phase 3, item 4): polling feed of freeze/blacklist/pause events per issuer across the freezable stablecoins (USDC, USDT, PYUSD, FDUSD, TUSD — Ethereum primary chain), paginated and filterable by coin (?coin=USDC, comma-separated, symbol or scope), event type (?kind=blacklisted / unblacklisted / destroyed / frozen / unfrozen / wiped / blacklist_set / paused / unpaused), holder address (?address=0x...), and time window (?from= / ?to= unix epoch, ?days=N last N days). Each event carries issuer/coin, event type, address, amount where known (destroyed black funds carry the burned amount in coin + USD at feed time), tx hash, block and timestamp, plus currently_frozen (from the window's frozen set — True/False for holder-state events, null for global/destroyed events). The `coins` coverage block lists all 8 stablecoin scopes: DAI/USDe have no freeze function in their token ABIs and USDD has none observed on Tron, so they are reported not_applicable with the evidence, never as silent empty feeds. Built on common/freeze.py (feature 10) wholesale — same Blockscout getLogs scan over the same window (365 days; 5-year lookback only for a coin with freeze signatures but zero holder-state events in 365d — TUSD) and the same currently-frozen set; per-coin event lists cached 300s. Frozen state is a LOWER-BOUND ESTIMATE (the blacklist is not enumerable on-chain; addresses frozen before the window are missed) and event windows are limited by log-scan depth — see the response's limits. Rendered at #/freeze-feed.", 'params': ['coin (optional, symbol or scope, comma-separated, default all freezable coins)', 'kind (optional, event type)', 'address (optional, 0x holder address)', 'from (optional, unix epoch seconds)', 'to (optional, unix epoch seconds)', 'days (optional, last N days, 1-1825)', 'page (optional, default 1)', 'page_size (optional, default 50, max 100)'], 'example': '/api/ethscope/freeze/feed?page_size=2', 'paid_api': True, 'example_response': {'events': [{'timestamp': 1793995895, 'date': '2026-11-06T20:11:35Z', 'coin': 'USDC', 'symbol': 'USDC', 'scope': 'usdcscope', 'issuer': 'Circle', 'chain': 'ethereum', 'event_type': 'blacklisted', 'kind': 'blacklisted', 'event': 'Blacklisted(address)', 'address': '0xdc8cb80786ed2823e5d6cdf22bd542db5b5b4b96', 'currently_frozen': True, 'amount': None, 'amount_usd': None, 'tx_hash': '0x94a933dc41d03291bcdb5e89a2c0a8cb8435c9b126e7ddc734fbe5ac0d92c0cf', 'block': 26358115}, {'timestamp': 1793995727, 'date': '2026-11-06T20:08:47Z', 'coin': 'USDC', 'symbol': 'USDC', 'scope': 'usdcscope', 'issuer': 'Circle', 'chain': 'ethereum', 'event_type': 'blacklisted', 'kind': 'blacklisted', 'event': 'Blacklisted(address)', 'address': '0x78b3ad18b66c41f766bbf452a609d004a2b2a1e1', 'currently_frozen': True, 'amount': None, 'amount_usd': None, 'tx_hash': '0x115654a38683babbed1df566590680a037550731b215e35b18390f23aca3a3cf', 'block': 26358101}], 'page': 1, 'page_size': 2, 'total_pages': 813, 'total_filtered': 1626, 'total_events': 1626, 'counts_by_coin': {'USDC': 579, 'USDT': 932, 'PYUSD': 101, 'FDUSD': 6, 'TUSD': 8}, 'counts_by_kind': {'blacklisted': 1120, 'destroyed': 204, 'frozen': 105, 'unblacklisted': 189, 'wiped': 2, 'blacklist_set': 6}, 'filters': {'coins': ['USDT', 'USDC', 'PYUSD', 'FDUSD', 'TUSD'], 'kind': None, 'address': None, 'from': None, 'to': None, 'days': None}, 'coins': [{'scope': 'usdtscope', 'symbol': 'USDT', 'name': 'Tether USD', 'issuer': 'Tether', 'status': 'freezable', 'reason': "TetherToken ABI: isBlackListed / addBlackList / removeBlackList / destroyBlackFunds / paused — freeze capability confirmed by both the ABI and feature 8's live probe.", 'events_in_window': 932, 'scan_status': 'ok', 'scan_note': None, 'window_days': 365, 'window_start_block': 23730912, 'window_end_block': 26358912, 'extended_lookback': False, 'extended_reason': None, 'currently_frozen_addresses': 555}, {'scope': 'usdcscope', 'symbol': 'USDC', 'name': 'USD Coin', 'issuer': 'Circle', 'status': 'freezable', 'reason': "FiatTokenV2_2 implementation ABI: isBlacklisted / blacklist / unBlacklist / paused — freeze capability confirmed by both the ABI and feature 8's live probe.", 'events_in_window': 579, 'scan_status': 'ok', 'scan_note': None, 'window_days': 365, 'window_start_block': 23730912, 'window_end_block': 26358912, 'extended_lookback': False, 'extended_reason': None, 'currently_frozen_addresses': 394}, {'scope': 'daiscope', 'symbol': 'DAI', 'name': 'Dai', 'issuer': 'MakerDAO / Sky', 'status': 'not_applicable', 'reason': 'Dai token ABI (22 functions) has no freeze/blacklist/pause function and no freeze event. rely/deny are admin authorisation (wards — who may mint/burn), not holder freezes: a denied address loses minting rights, its existing DAI keeps moving. No freeze/blacklist/pause function or event in the token ABI, so there are no freeze events to scan — reported as not applicable, not as zero.', 'events_in_window': 0, 'scan_status': 'not_applicable', 'scan_note': 'No freeze/blacklist/pause function or event in the token ABI, so there are no freeze events to scan — reported as not applicable, not as zero.', 'window_days': None, 'window_start_block': None, 'window_end_block': None, 'extended_lookback': False, 'extended_reason': None, 'currently_frozen_addresses': 0}, {'scope': 'usdescope', 'symbol': 'USDe', 'name': 'Ethena USDe', 'issuer': 'Ethena Labs', 'status': 'not_applicable', 'reason': 'Ethena USDe token ABI (25 functions) has no freeze/blacklist/pause function and no freeze event. No freeze/blacklist/pause function or event in the token ABI, so there are no freeze events to scan — reported as not applicable, not as zero.', 'events_in_window': 0, 'scan_status': 'not_applicable', 'scan_note': 'No freeze/blacklist/pause function or event in the token ABI, so there are no freeze events to scan — reported as not applicable, not as zero.', 'window_days': None, 'window_start_block': None, 'window_end_block': None, 'extended_lookback': False, 'extended_reason': None, 'currently_frozen_addresses': 0}, {'scope': 'fdusdsscope', 'symbol': 'FDUSD', 'name': 'First Digital USD', 'issuer': 'First Digital Labs', 'status': 'freezable', 'reason': "StablecoinV2 implementation ABI: frozen / freeze / unfreeze / paused — freeze capability confirmed by both the ABI and feature 8's live probe.", 'events_in_window': 6, 'scan_status': 'ok', 'scan_note': None, 'window_days': 365, 'window_start_block': 23730912, 'window_end_block': 26358912, 'extended_lookback': False, 'extended_reason': None, 'currently_frozen_addresses': 6}, {'scope': 'pyusdsscope', 'symbol': 'PYUSD', 'name': 'PayPal USD', 'issuer': 'PayPal (issued by Paxos)', 'status': 'freezable', 'reason': "PYUSD implementation ABI: isFrozen / freeze / unfreeze / wipeFrozenAddress / paused — freeze capability confirmed by both the ABI and feature 8's live probe.", 'events_in_window': 101, 'scan_status': 'ok', 'scan_note': None, 'window_days': 365, 'window_start_block': 23730912, 'window_end_block': 26358912, 'extended_lookback': False, 'extended_reason': None, 'currently_frozen_addresses': 99}, {'scope': 'tusdscope', 'symbol': 'TUSD', 'name': 'TrueUSD', 'issuer': 'TrueUSD (Techteryx)', 'status': 'freezable', 'reason': "TrueUSD implementation ABI exposes setBlacklisted(address,bool) + Blacklisted(address,bool) events + destroyBlackFunds — the issuer CAN blacklist. There is NO on-chain read function for blacklist status, so feature 8's eth_call read probe could not see it and scored freeze risk 100 (no function found). The probe result is a read-probe limitation, not proof of no freeze power — the ABI and the events below prove the power exists and has been used.", 'events_in_window': 8, 'scan_status': 'ok', 'scan_note': None, 'window_days': 1825, 'window_start_block': 13218912, 'window_end_block': 26358912, 'extended_lookback': True, 'extended_reason': 'No freeze/blacklist events in the 365d window, so the scan was extended to 5 years to find the last use.', 'currently_frozen_addresses': 4}, {'scope': 'usddscope', 'symbol': 'USDD', 'name': 'Decentralized USD', 'issuer': 'TRON DAO Reserve', 'status': 'not_applicable', 'reason': "TronScan's published method map for the USDD contract lists 11 observed functions (approve, burn, deny, mint, move, permit, pull, push, rely, transfer, transferFrom) — DAI-style, no freeze/blacklist/pause. The map is PARTIAL (built from observed transactions), so a rarely-called freeze function could be missing; there is no keyless Tron ABI endpoint to check further. No freeze/blacklist function appears in TronScan's published method map for the USDD contract (11 observed functions — a PARTIAL list built from observed transactions), so there are no freeze events to scan.", 'events_in_window': 0, 'scan_status': 'not_applicable', 'scan_note': "No freeze/blacklist function appears in TronScan's published method map for the USDD contract (11 observed functions — a PARTIAL list built from observed transactions), so there are no freeze events to scan.", 'window_days': None, 'window_start_block': None, 'window_end_block': None, 'extended_lookback': False, 'extended_reason': None, 'currently_frozen_addresses': 0}], 'method': 'Freeze event feed (phase 3, item 4). Freeze/blacklist/pause events per issuer, merged across the freezable stablecoins (USDC, USDT, PYUSD, FDUSD, TUSD — Ethereum primary chain) and paginated. Built on common/freeze.py (feature 10) wholesale: the same Blockscout getLogs scan over the same window (365 days; 5-year lookback only for a coin with freeze signatures but zero holder-state events in 365d — TUSD), the same currently-frozen set rebuilt by applying holder-state events in order (latest event per address wins), and the same capability evidence. DAI/USDe have no freeze function in their token ABI and USDD has none observed on Tron, so those coins are reported not_applicable with the evidence, never as empty feeds. A feed event is a screening signal, not a legal determination; it does not say why the issuer froze the address.', 'limits': ['Frozen supply / currently-frozen state is a LOWER-BOUND ESTIMATE: the issuer blacklist is not enumerable on-chain, so the currently-frozen set is rebuilt from the freeze events inside the scanned window only (365 days, 5-year lookback only for a coin with freeze signatures but zero holder-state events in 365d — TUSD). Addresses frozen before the window are missed.', 'Event windows are limited by log-scan depth: Blockscout getLogs is paged (offset=1000, up to 3 pages per event signature), so a very chatty signature could be truncated — the per-coin scan_status says ok / partial / unavailable when that happens.', 'Primary chain only (Ethereum for the 5 freezable coins). FDUSD/TUSD hold most of their supply off the primary chain (BSC / Tron); freezes on other chains are not covered.', 'Destroyed black funds (USDT/TUSD DestroyedBlackFunds) are funds the issuer BURNED out of a blacklisted address — seized, not merely frozen — and are never counted as frozen.', 'Pause/unpause events are global (they halt ALL transfers, not one holder) and carry no address.', 'USD amounts use the live price at feed time (DeFiLlama, same as /freeze); for destroyed funds that is the value destroyed, not a current balance.', "DAI and USDe have no freeze/blacklist/pause function in their token ABIs, so there are no freeze events to scan for them. USDD (Tron) has no freeze function in TronScan's published method map (11 observed functions, partial list)."], 'as_of': 1794005578}, 'free_tier': "Free tier today: keyless, no auth, no rate limit of our own. One page per call (default 50 events, max 100); per-coin event lists cached 300s server-side, so repeat calls inside the window are served from cache. A cold call scans up to 5 coins' getLogs over 365 days (5 years for silent TUSD) and can take ~30s. Upstream rate limits (Blockscout) apply — expect occasional 502s on slow scans.", 'paid_tier': 'Paid tier (planned): API key required; calls metered per feed call. Higher rate limits, longer retention / deeper windows than the 365d log scan, per-coin webhooks on new freeze events, and batch / export (CSV / JSON, no pagination). No auth, metering, or billing is enforced yet — this endpoint is free and keyless today.'},
    {"method": "GET", "path": "/api/ethscope/preflight", "description": "Recipient pre-flight compliance check (phase 2, P2): pass / warn / block for one recipient address BEFORE sending. Reuses the OFAC SDN risk walk (common/risk.py), EOA-vs-contract, coin balance (balanceOf / native / SPL), frozen/blacklisted status via on-chain read functions (recipient + up to 10 recent counterparties; TUSD has no read function so its status is honestly unknown), and first-seen/activity recency. Optional ?coin= (stablecoin symbol, native symbol, 0x contract, SPL mint), ?chain= (EVM chain, default ethereum; stablecoin symbols are mainnet-only), ?amount=. Works for EVM, Solana and Bitcoin addresses and ENS names. A pass is not proof the recipient is clean — see the response's limits. Nothing here creates or signs a transaction.", "params": ["address", "coin (optional)", "chain (optional)", "amount (optional)"], "example": "/api/ethscope/preflight?address=0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045&coin=USDC&amount=100"},
    {"method": "GET", "path": "/api/ethscope/freeze/watch", "description": "Freeze / blacklist watch (phase 2, Q6): is this address currently frozen/blacklisted, and does any freeze event name it? EVM addresses: live read probe via eth_call (USDC isBlacklisted, USDT isBlackListed, PYUSD isFrozen, FDUSD frozen — pre-flight probes reused) plus the freeze/blacklist events naming the address from the same window as the per-scope /freeze (365d, 5y extended lookback for silent coins) and the address's current balance in the coin. ?coin= is a stablecoin symbol (default: USDC + USDT majors); DAI/USDe have no freeze function (not_freezable); TUSD has no on-chain read function, so its state is event-derived, never live-verified; USDD is Tron-native (not_applicable). Solana addresses: ?coin= USDC / USDT checks that coin's Solana mint, or an SPL mint address — the mint's freeze authority plus the token account's current state (frozen / initialized) via the public Solana RPC; Solana has no freeze-event scan, so the answer is current-state only. Bitcoin addresses: not_applicable (no issuer freeze). The blacklist is NOT enumerable on-chain — 'not frozen' means the live probe says not frozen and no event in the scanned window names the address. Cached 300s per address. Rendered at #/freeze-watch.", "params": ["address", "coin (optional)", "chain (optional, solana for Solana mints)"], "example": "/api/ethscope/freeze/watch?address=0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48&coin=USDC"},
    {"method": "GET", "path": "/api/ethscope/settle", "description": "Settlement tracker (phase 2, Q1): live status timeline for one transaction — submitted -> confirmed (with confirmation count) -> economically final -> bridged leg minted on the destination (where applicable) — plus the receipt fields (amount, USD at execution, fee, sender/recipient, explorer link). Chains: ethereum, sepolia, optimism, gnosis, solana, bitcoin. Bridge legs: Circle CCTP (MessageSent log -> keccak message hash -> Circle iris-api attestation; a \"complete\" attestation means the mint is claimable — the destination mint tx itself is not linked keylessly) and Wormhole (Wormholescan ?txHash=). \"Economically final\" is a convention (64 confirmations EVM / 12 Sepolia / 32 Solana slots / 6 Bitcoin), not protocol finality. USD at execution uses the execution-day price where available, else the live price labelled estimate-current, else null.", "params": ["chain", "hash"], "example": "/api/ethscope/settle?chain=ethereum&hash=0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37"},
    {"method": "GET", "path": "/api/ethscope/receipt", "description": "Shareable receipt (phase 2, Q1): receipt-only view of /settle for one transaction — amount, USD value at execution, fees, sender/recipient, confirmations, economically-final flag, bridge leg, explorer link, receipt URL and generation time. ?format=json returns this same JSON (the receipt page's Download button uses it). Rendered at #/receipt/<chain>/<hash>.", "params": ["chain", "hash", "format (optional, json)"], "example": "/api/ethscope/receipt?chain=ethereum&hash=0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37&format=json"},
    {'method': 'GET', 'path': '/api/ethscope/verify', 'description': 'Settlement verification API (phase 3, item 9): one transaction hash in, a verification verdict out — confirmation status, finality stated explicitly against the per-chain economically-final threshold, USD value at execution with its price basis, the bridge leg (Circle CCTP attestation where applicable), and the shareable receipt, in a single call. A new dedicated endpoint, not a rename of /settle: /settle stays the Q1 tracker backing and /receipt the shareable receipt, while /verify is the productized contract — the tracker payload (common/settlement.py track_settlement, phase 2 Q1) plus the receipt (common/settlement.py receipt) plus the verdict, reused wholesale (see common/verify.py). Verdict: verified-final (confirmed at/above threshold), confirmed-not-final (confirmed but below threshold), pending (submitted, not yet mined), failed (error status). Chains: ethereum, sepolia, optimism, gnosis, solana, bitcoin. "Economically final" is a convention (64 confirmations EVM / 12 Sepolia / 32 Solana slots / 6 Bitcoin), not protocol finality. USD at execution uses the execution-day price where available, else the live price labelled estimate-current, else null — never invented. A CCTP attestation of "complete" means the mint is claimable; the destination mint tx itself is not linked keylessly. Rendered at #/verify (the #/track page calls this endpoint).', 'params': ['chain', 'hash'], 'example': '/api/ethscope/verify?chain=ethereum&hash=0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'paid_api': True, 'example_response': {'chain': 'ethereum', 'chain_label': 'Ethereum', 'kind': 'evm', 'hash': '0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'status': 'success', 'block': 26353245, 'timestamp': '2026-11-06T03:53:59.000000Z', 'age': '19 hrs 45 mins ago', 'confirmations': 5905, 'finality_threshold': 64, 'from': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491', 'to': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D', 'amount': {'value': '0.03', 'symbol': 'WETH', 'kind': 'token', 'token_address': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', 'usd_value': 67.13, 'usd_price': 2237.584379243101, 'price_basis': 'at-execution'}, 'token_transfers': [{'token': 'WETH', 'token_address': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', 'amount': '0.03', 'from': '0x0000000000000000000000000000000000000000', 'to': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D'}, {'token': 'WETH', 'token_address': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', 'amount': '0.03', 'from': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D', 'to': '0x3027d452fb89034649aCa6F324aEBa8596C75c2C'}, {'token': 'Gato', 'token_address': '0x768488C988Cf2493F0e7CCb3d56547F2384E2c27', 'amount': '60460.570297', 'from': '0x3027d452fb89034649aCa6F324aEBa8596C75c2C', 'to': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491'}], 'fee': {'value': '0.00000554314888086', 'symbol': 'ETH', 'usd_value': 0.01, 'usd_price': 2237.795778682559, 'price_basis': 'at-execution'}, 'explorer_url': 'https://eth.blockscout.com/tx/0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'timeline': [{'id': 'submitted', 'label': 'Submitted', 'status': 'done', 'detail': 'Seen by the network'}, {'id': 'confirmed', 'label': 'Confirmed', 'status': 'done', 'detail': '5905 confirmation(s)'}, {'id': 'final', 'label': 'Economically final', 'status': 'done', 'detail': 'Threshold: 64 confirmation(s) — see notes'}], 'bridge': None, 'notes': ['"Economically final" is a convention (Ethereum/Optimism/Gnosis 64 confirmations, Sepolia 12, Solana 32 slots, Bitcoin 6 confirmations), not protocol finality — a deeper reorg is still possible, just increasingly unlikely.', 'A CCTP attestation of "complete" means the mint is claimable; the actual mint transaction on the destination chain is not linked — no keyless source maps a Circle message hash to the destination mint tx.', 'Bitcoin amounts are the largest single output; change outputs cannot be identified from chain data alone. Total moved is reported alongside.', 'USD values at execution use the execution-day price where available; when it is not, the live price is used and labelled estimate-current, or the USD fields are null — never invented.'], 'verify': True, 'verdict': 'verified-final', 'verdict_reason': 'Confirmed with 5905 confirmations — at or above the economically-final threshold of 64 confirmations.', 'finality': {'confirmations': 5905, 'confirmations_unit': 'confirmations', 'threshold': 64, 'economically_final': True, 'verdict': 'verified-final', 'note': '"Economically final" is a convention (confirmations threshold 64 on Ethereum), not protocol finality — a deeper reorg is still possible, just increasingly unlikely.'}, 'value_usd_at_execution': 67.13, 'usd_price_at_execution': 2237.584379243101, 'price_basis': 'at-execution', 'receipt': {'receipt': True, 'chain': 'ethereum', 'chain_label': 'Ethereum', 'hash': '0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'status': 'success', 'block': 26353245, 'timestamp': '2026-11-06T03:53:59.000000Z', 'age': '19 hrs 45 mins ago', 'confirmations': 5905, 'confirmations_unit': 'confirmations', 'finality_threshold': 64, 'economically_final': True, 'from': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491', 'to': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D', 'amount': {'value': '0.03', 'symbol': 'WETH', 'kind': 'token', 'token_address': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', 'usd_value': 67.13, 'usd_price': 2237.584379243101, 'price_basis': 'at-execution'}, 'token_transfers': [{'token': 'WETH', 'token_address': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', 'amount': '0.03', 'from': '0x0000000000000000000000000000000000000000', 'to': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D'}, {'token': 'WETH', 'token_address': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', 'amount': '0.03', 'from': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D', 'to': '0x3027d452fb89034649aCa6F324aEBa8596C75c2C'}, {'token': 'Gato', 'token_address': '0x768488C988Cf2493F0e7CCb3d56547F2384E2c27', 'amount': '60460.570297', 'from': '0x3027d452fb89034649aCa6F324aEBa8596C75c2C', 'to': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491'}], 'total_moved_btc': None, 'total_moved_usd': None, 'fee': {'value': '0.00000554314888086', 'symbol': 'ETH', 'usd_value': 0.01, 'usd_price': 2237.795778682559, 'price_basis': 'at-execution'}, 'bridge': None, 'explorer_url': 'https://eth.blockscout.com/tx/0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'receipt_url': '/#/receipt/ethereum/0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'generated_at': '2026-11-06T23:39:32.524679+00:00', 'notes': ['"Economically final" is a convention (Ethereum/Optimism/Gnosis 64 confirmations, Sepolia 12, Solana 32 slots, Bitcoin 6 confirmations), not protocol finality — a deeper reorg is still possible, just increasingly unlikely.', 'A CCTP attestation of "complete" means the mint is claimable; the actual mint transaction on the destination chain is not linked — no keyless source maps a Circle message hash to the destination mint tx.', 'Bitcoin amounts are the largest single output; change outputs cannot be identified from chain data alone. Total moved is reported alongside.', 'USD values at execution use the execution-day price where available; when it is not, the live price is used and labelled estimate-current, or the USD fields are null — never invented.']}, 'verify_url': '/api/walletscope/verify?chain=ethereum&hash=0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'method': 'Settlement verification (phase 3, item 9). Settlement data from common/settlement.py track_settlement (phase 2, Q1) and the receipt from common/settlement.py receipt, both reused wholesale. This module only adds the verdict: confirmed/not-final/final stated explicitly against the per-chain threshold, the USD value at execution with its price basis, and the receipt embedded in the same response.', 'limits': ['"Economically final" is a convention (Ethereum/Optimism/Gnosis 64 confirmations, Sepolia 12, Solana 32 slots, Bitcoin 6 confirmations), not protocol finality — a deeper reorg is still possible, just increasingly unlikely.', 'A CCTP attestation of "complete" means the mint is claimable; the actual mint transaction on the destination chain is not linked — no keyless source maps a Circle message hash to the destination mint tx.', 'Bitcoin amounts are the largest single output; change outputs cannot be identified from chain data alone. Total moved is reported alongside.', 'USD values at execution use the execution-day price where available; when it is not, the live price is used and labelled estimate-current, or the USD fields are null — never invented.', 'A "verified-final" verdict is a statement about the observed confirmation count against a stated convention threshold, not protocol finality and not proof the payment was the intended one — check the amount, recipient and token before treating a transaction as settled.', 'The receipt embedded here is generated at call time (generated_at); a later call re-checks the transaction live — confirmations move, and a bridge leg can still progress after the receipt was first issued.']}, 'free_tier': 'Free tier today: keyless, no auth, no rate limit of our own. One transaction per call; response cached 60s server-side (the underlying tracker cache). One CCTP attestation lookup (Circle iris-api) and one Wormholescan lookup per EVM/Solana call when the tx is a bridge transfer. Upstream rate limits (Blockscout, public RPCs, mempool.space, Circle iris-api) apply — expect occasional 502s on slow calls.', 'paid_tier': 'Paid tier (planned): API key required; calls metered per verification. Higher rate limits, batch verification of many transactions in one call, webhooks on finality transitions (confirmed -> final, attestation published), and a stored receipt history with re-verification on demand. No auth, metering, or billing is enforced yet — this endpoint is free and keyless today.'},
    {"method": "GET", "path": "/api/ethscope/aftermath", "description": "Received-funds monitor (phase 2, Q4): what happened AFTER one receipt — the sender's onward transfers after the receipt (first page per chain: 50 native txs + 50 token transfers EVM, latest 25 Solana signatures with 10 parsed, first page of Bitcoin txs), direct OFAC SDN checks on every onward destination plus the counterparty's full risk walk (common/risk.py, reused), and the counterparty's frozen/blacklisted state (current read probe for the receipt token + USDC/USDT on Ethereum, plus a targeted freeze-event scan from the receipt block for freezable stablecoins on Ethereum). Forward-looking from receipt time only; a clean result is not proof of clean — onward transfers are the sender's later movements, not proven to be the same funds (no keyless taint tracking). Rendered as the Aftermath section on #/receipt/<chain>/<hash>; saved receipts are re-checked on visit at #/watchlist (localStorage only, no push alerts).", "params": ["chain", "hash"], "example": "/api/ethscope/aftermath?chain=ethereum&hash=0x3f5f7ce5cb588b8d8b443118c3610a5e19b6535faed20b94fca7f634788ddbeb"},
    {"method": "GET", "path": "/api/ethscope/accounting/export", "description": "Accounting export (phase 2, Q2): finance-grade cross-chain CSV of every send/receive for one address — date, tx hash, chain, coin, amount, USD value at time of transaction, fee in USD, counterparty, type (send/receive), FIFO cost basis and realized gain per send, plus QuickBooks/Xero general-journal columns (debit/credit accounts and legs, memo with hash). ?format=json returns the same rows plus summary, per-coin FIFO lots remaining, the full column mapping and honest-limit notes. All coins the wallet aggregates (native + tokens on Ethereum/Sepolia/Optimism/Gnosis, SOL + SPL on Solana, BTC for Bitcoin addresses). One CoinGecko daily-close series per coin; rows that fall back to the live price are labelled price_basis estimate-current; rows with no price have null USD fields, never invented. Sampled window: first page per chain (50 native txs + 50 token transfers per EVM chain, 10 Solana signatures, first page of Bitcoin txs). Token-transfer rows carry no fee (paid in native coin on the parent tx). This replaces the single-chain explorer CSV (no USD values) for bookkeeping.", "params": ["address", "format (optional, csv or json)"], "example": "/api/ethscope/accounting/export?address=0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045&format=csv"},
    {'method': 'GET', 'path': '/api/ethscope/accounting', 'description': 'Accounting API (phase 3, item 10): the phase 2, Q2 accounting export as a documented paid-API contract — one address in, the cross-chain send/receive ledger out (USD value at time of transaction, FIFO cost basis and realized gain per send, QuickBooks/Xero general-journal columns) in CSV or JSON. A new dedicated endpoint, not a rename: GET /api/walletscope/accounting/export stays exactly as it is (the download backing), while /accounting is the productized contract — the full export payload (common/accounting.py accounting_export, reused wholesale: rows, USD-at-time pricing from one CoinGecko daily-close series per coin, FIFO via the shared _fifo_run engine, journal columns, summary, per-coin FIFO lots remaining, column_mapping, honest-limit notes) PLUS method / limits / coverage / price_basis_counts and api/csv/json URLs. format=csv returns the same CSV bytes as the export endpoint (21 columns); the contract fields are JSON-only. coverage states the sampled window, the chains/coins present, the per-chain collection errors (surfaced, never silently dropped) and a complete flag — False whenever any chain errored (Gnosis errored with a Blockscout 403 in the live run below, so that export is partial). price_basis_counts splits rows into at-time (daily close on the tx date), estimate-current (live-price fallback) and unavailable (null USD, never invented). Sampled window: first page per chain (50 native txs + 50 token transfers per EVM chain, 10 Solana signatures, first page of Bitcoin txs — mempool.space page size, 25 at phase-2 verification, 50 observed 2026-11-06). Token-transfer rows carry no fee (paid in native coin on the parent tx). Rendered in the wallet dashboard Accounting Export card (#/wallet/…), which fetches this endpoint live and shows the summary, first 10 rows, per-coin FIFO and any chain errors. example_response below is the live response with rows truncated to the first 2 for display; the full response carries all 26 rows plus notes and column_mapping.', 'params': ['address', 'format (optional, csv or json, default json)'], 'example': '/api/ethscope/accounting?address=0x7C2080F3fC32Be38193262144AC98302f3b6b9A6&format=json', 'paid_api': True, 'example_response': {'address': '0x7C2080F3fC32Be38193262144AC98302f3b6b9A6', 'generated_at': '2026-11-06T23:45:50.711999Z', 'accounting': True, 'summary': {'rows': 26, 'receives': 18, 'sends': 8, 'total_received_usd': 367.43, 'total_sent_usd': 366.62, 'total_fees_usd': 0.18, 'total_cost_basis_usd': 366.63, 'total_realized_gain_usd': -0.01, 'rows_unpriced': 4, 'rows_estimate_current': 0, 'chains': ['ethereum', 'optimism'], 'coins': ['ETH', 'USDC', 'USDT', 'WATTOIN', 'opti-line.top ✅', 'optibase.website 🟢', 'optibase.website 🟢🟢']}, 'price_basis_counts': {'at-time': 22, 'estimate-current': 0, 'unavailable': 4}, 'coverage': {'window': 'first page per chain (50 native txs + 50 token transfers per EVM chain, 10 Solana signatures, 25 Bitcoin txs)', 'chains': ['ethereum', 'optimism'], 'coins': ['ETH', 'USDC', 'USDT', 'WATTOIN', 'opti-line.top ✅', 'optibase.website 🟢', 'optibase.website 🟢🟢'], 'errors': {'gnosis': ['txs: Blockscout returned HTTP 403', 'tt: Blockscout returned HTTP 403']}, 'chains_with_errors': ['gnosis'], 'complete': False}, 'rows': [{'date': '2026-08-26T12:23:59Z', 'tx_hash': '0x3f5f7ce5cb588b8d8b443118c3610a5e19b6535faed20b94fca7f634788ddbeb', 'chain': 'ethereum', 'coin': 'USDC', 'token_address': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48', 'type': 'receive', 'counterparty': '0xBef8a9456C24EAC7c2A4F9b0833887b18f5578f0', 'amount': '15', 'usd_price': 0.9999697903865089, 'amount_usd': 15.0, 'price_basis': 'at-time', 'fee_usd': None, 'fee_coin': '', 'cost_basis_usd': None, 'realized_gain_usd': None, 'fifo_lots': '', 'debit_account': 'Crypto Assets:USDC', 'credit_account': 'Other Income:Crypto Received', 'debit_usd': 15.0, 'credit_usd': 15.0, 'memo': 'Receive 15 USDC on Ethereum — tx 0x3f5f7ce5cb588b8d8b443118c3610a5e19b6535faed20b94fca7f634788ddbeb', 'fifo_lots_detail': []}, {'date': '2026-08-27T08:37:49Z', 'tx_hash': '0x6cfc0602ea2e67061c0a2ddacceff6619ad4d309922e745a739b9fed21d9dc47', 'chain': 'optimism', 'coin': 'optibase.website 🟢', 'token_address': '0x1c835F0118Ba23aceD5f5Ce2d866907658b2B31D', 'type': 'receive', 'counterparty': '0x259E9c58dBd4A0b6440E3D9B3Ab6E8572CfEbdFb', 'amount': '3427', 'usd_price': None, 'amount_usd': None, 'price_basis': 'unavailable', 'fee_usd': None, 'fee_coin': '', 'cost_basis_usd': None, 'realized_gain_usd': None, 'fifo_lots': '', 'debit_account': 'Crypto Assets:optibase.website 🟢', 'credit_account': 'Other Income:Crypto Received', 'debit_usd': None, 'credit_usd': None, 'memo': 'Receive 3427 optibase.website 🟢 on Optimism — tx 0x6cfc0602ea2e67061c0a2ddacceff6619ad4d309922e745a739b9fed21d9dc47', 'fifo_lots_detail': []}], 'fifo': [{'chain': 'ethereum', 'coin': 'ETH', 'token_address': '', 'transfers': 8, 'transfers_priced': 8, 'transfers_unpriced': 0, 'transfers_unmatched': 0, 'proceeds_usd': 1.67, 'cost_usd': 1.7, 'realized_pnl_usd': -0.03, 'lots_remaining': [{'amount': '0.000098054078140844', 'unit_cost_usd': 2468.524541953589, 'acquired_date': '2026-10-12'}, {'amount': '0.0001088648988', 'unit_cost_usd': 2253.0113341657943, 'acquired_date': '2026-10-31'}, {'amount': '0.0001421039781', 'unit_cost_usd': 2238.5755732929633, 'acquired_date': '2026-11-06'}]}, {'chain': 'ethereum', 'coin': 'USDC', 'token_address': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48', 'transfers': 9, 'transfers_priced': 9, 'transfers_unpriced': 0, 'transfers_unmatched': 0, 'proceeds_usd': 290.03, 'cost_usd': 290.01, 'realized_pnl_usd': 0.01, 'lots_remaining': []}, {'chain': 'ethereum', 'coin': 'USDT', 'token_address': '0xdAC17F958D2ee523a2206206994597C13D831ec7', 'transfers': 5, 'transfers_priced': 5, 'transfers_unpriced': 0, 'transfers_unmatched': 0, 'proceeds_usd': 74.93, 'cost_usd': 74.93, 'realized_pnl_usd': 0.0, 'lots_remaining': []}, {'chain': 'ethereum', 'coin': 'WATTOIN', 'token_address': '0x5eef5946AD78E614bB3Ee7b9eD1097c517ae97f7', 'transfers': 1, 'transfers_priced': 0, 'transfers_unpriced': 1, 'transfers_unmatched': 0, 'proceeds_usd': 0.0, 'cost_usd': 0.0, 'realized_pnl_usd': 0.0, 'lots_remaining': []}, {'chain': 'optimism', 'coin': 'opti-line.top ✅', 'token_address': '0xCd4eA8BD757F8e431923AE64B7cE98bCb7d08392', 'transfers': 1, 'transfers_priced': 0, 'transfers_unpriced': 1, 'transfers_unmatched': 0, 'proceeds_usd': 0.0, 'cost_usd': 0.0, 'realized_pnl_usd': 0.0, 'lots_remaining': []}, {'chain': 'optimism', 'coin': 'optibase.website 🟢', 'token_address': '0x1c835F0118Ba23aceD5f5Ce2d866907658b2B31D', 'transfers': 1, 'transfers_priced': 0, 'transfers_unpriced': 1, 'transfers_unmatched': 0, 'proceeds_usd': 0.0, 'cost_usd': 0.0, 'realized_pnl_usd': 0.0, 'lots_remaining': []}, {'chain': 'optimism', 'coin': 'optibase.website 🟢🟢', 'token_address': '0x4727b1F0610aB69a3862442d48152516D8EF4727', 'transfers': 1, 'transfers_priced': 0, 'transfers_unpriced': 1, 'transfers_unmatched': 0, 'proceeds_usd': 0.0, 'cost_usd': 0.0, 'realized_pnl_usd': 0.0, 'lots_remaining': []}], 'api_url': '/api/walletscope/accounting?address=0x7C2080F3fC32Be38193262144AC98302f3b6b9A6&format=json', 'csv_url': '/api/walletscope/accounting?address=0x7C2080F3fC32Be38193262144AC98302f3b6b9A6&format=csv', 'json_url': '/api/walletscope/accounting?address=0x7C2080F3fC32Be38193262144AC98302f3b6b9A6&format=json', 'export_url': '/api/walletscope/accounting/export?address=0x7C2080F3fC32Be38193262144AC98302f3b6b9A6&format=json', 'method': 'Accounting API (phase 3, item 10). The phase 2, Q2 accounting export (common/accounting.py accounting_export, USD value at time of transaction + FIFO cost basis + QuickBooks/Xero general-journal columns) reused wholesale, plus the paid-API contract fields: method, limits, coverage (sampled window, chains/coins present, per-chain errors surfaced, complete flag), price_basis_counts, and api/csv/json URLs. format=csv returns the same CSV bytes as GET /api/walletscope/accounting/export; the contract fields are JSON-only.', 'limits': ['Sampled window: first page per chain (50 native txs + 50 token transfers per EVM chain, 10 Solana signatures, 25 Bitcoin txs). Older history is NOT fetched — the export is complete only for addresses whose whole history fits the window.', "Rows with price_basis 'estimate-current' are priced at today's live price, not at transaction time; for old transactions that is a weak estimate, and their FIFO lots inherit the same weakness.", "Rows with price_basis 'unavailable' have null USD fields — no price was invented. Unpriced tokens (e.g. unlisted / spam tokens) still appear with their amount, so nothing is silently dropped.", "Token-transfer rows carry no fee_usd: the fee is paid in the native coin on the parent transaction and Blockscout's token-transfer endpoint does not return it. Native rows carry fee_usd.", 'Sepolia ETH has no market value — its USD fields are always null (never valued at mainnet ETH prices).', "Bitcoin rows are NET flow per transaction (received minus sent); change outputs cannot be identified from chain data alone. Counterparty is the first other address seen. Bitcoin fee_usd (sends only) is the whole transaction's fee — when several senders fund one transaction, the address's share is unknown.", 'Solana counterparties are best-effort (largest opposite balance change in the same transaction).', "FIFO cost basis reuses the PnL estimate's engine (common/wallet.py _fifo_run): every incoming transfer opens a lot at that day's price, every outgoing transfer disposes oldest-first. Disposals with no matching acquisition in the window are unmatched — cost_basis_usd is null, never guessed.", 'This is an estimate, not tax advice: transfers to exchanges, gifts and internal moves are all treated as disposals.', 'Per-chain collection errors are reported in `errors` and `coverage` — a chain that errored contributes no rows, so the export is partial for that address until the error clears. `coverage.complete` is False whenever any chain errored; never treat a partial export as a complete ledger.', 'CSV form carries the same rows as the JSON form; the contract fields (method, limits, coverage, price_basis_counts) are JSON-only — the CSV header is the 21 export columns, unchanged.', 'This is an estimate, not tax advice: transfers to exchanges, gifts and internal moves are all treated as disposals. FIFO lots are built only from the sampled window — a disposal whose acquisition predates the window is unmatched (cost_basis_usd null), never guessed.']}, 'free_tier': 'Free tier today: keyless, no auth, no rate limit of our own. One address per call; response cached 300s server-side (the export cache — a repeat call inside the window is served in milliseconds). A cold call fans out to 4 EVM chains plus one CoinGecko daily-close series per coin and took ~19s live. format=csv returns the same CSV bytes as the Q2 /accounting/export endpoint. Upstream rate limits (Blockscout, public Solana RPC, mempool.space, CoinGecko keyless tier) apply — expect occasional 502s or per-chain errors (surfaced in coverage.errors, never hidden).', 'paid_tier': 'Paid tier (planned): API key required; calls metered per export. Higher rate limits, full-history export beyond the first-page sampled window, scheduled / recurring exports, a stored export history with re-export on demand, and QuickBooks/Xero direct import plus webhooks on new activity for a watched address. No auth, metering, or billing is enforced yet — this endpoint is free and keyless today.'},
    {"method": "GET", "path": "/api/ethscope/recurring", "description": "Recurring-payment tracker (phase 2, Q5): repeating payments for one address — same counterparty, same coin, approximately the same amount, at a roughly regular interval. Per series: payment count, median interval + cadence label (daily / weekly / fortnightly / monthly / quarterly / half-yearly / yearly / every ~N days), expected next date, status (on_schedule / due / overdue) with missed-payment count and overdue days, late payments in the history, and amount drift (stddev / CV / max relative deviation from the median). 2-payment groups are reported as candidates (not enough to establish a cadence). ?direction=out (default, sends) / in (receives) / all. All coins the wallet aggregates (native + tokens on Ethereum/Sepolia/Optimism/Gnosis, SOL + SPL on Solana, BTC for Bitcoin addresses). Sampled window: first page per EVM chain (50 native txs + 50 token transfers per chain), latest 25 Solana signatures, first page of Bitcoin txs — a series is only as complete as the window, and absence of a series is not proof there is no recurring payment. Rendered at #/recurring.", "params": ["address", "direction (optional, out / in / all, default out)"], "example": "/api/ethscope/recurring?address=0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045&direction=out"},
    {'method': 'GET',
     'path': '/api/ethscope/token/security',
     'description': 'Token security API (phase 3, item 12): one token contract in, a security verdict out — level (low / '
                    'elevated / high) + points + per-check pass/warn/fail/unknown with the evidence behind each check. '
                    'EVM: owner powers (ABI + live owner() eth_call), holder concentration (top-10 share), buy/sell tax '
                    "(only if a standard tax function exists — otherwise 'not detectable', never an invented 0%), "
                    'honeypot simulation (read-only eth_call buy via the V2 router + sell transfer from the largest '
                    "holder; 'simulation unavailable' when no pair/router exists), and liquidity (DexScreener-tracked "
                    'pairs + on-chain V2 reserves / LP burn). Solana: mint/freeze authority + holder concentration, with '
                    'the EVM checks listed n/a and labelled EVM-only. A new dedicated endpoint, not a rename: GET '
                    '/api/<scope>/token/{h}/security stays exactly as it is (the token-page security card backing), '
                    'while /token/security is the productized contract — the same scan (common/security.py, reused '
                    'wholesale) plus the contract fields (security, api_url, method, limits, chain_label), with '
                    'cross-chain dispatch on ?chain=. Chains: ethereum / sepolia / optimism / gnosis / solana; bitcoin '
                    'is rejected (no token contracts to scan). Scoring: fail = 3 points, warn = 1; any fail -> high, >= '
                    "2 points -> elevated, otherwise low. Honest labels: tax 'not detectable' when no standard function "
                    'exists; liquidity totals are a stated lower bound (DexScreener-tracked pairs on this chain only); a '
                    'passing simulation does not guarantee a real swap succeeds; the scan is a heuristic, not an audit. '
                    'Rendered at #/token-security. example_response below is the live USDC-on-Ethereum response with the '
                    'top-10 holder evidence trimmed to the first 3 for display; the full response carries all 10.',
     'params': ['chain (ethereum / sepolia / optimism / gnosis / solana)',
                'address (token contract address, 0x…; Solana SPL mint)'],
     'example': '/api/ethscope/token/security?chain=ethereum&address=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
     'paid_api': True,
     'example_response': {'token': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
                          'chain': 'ethereum',
                          'family': 'evm',
                          'level': 'low',
                          'points': 1,
                          'checks': [{'id': 'owner_powers',
                                      'label': 'Owner powers',
                                      'status': 'warn',
                                      'detail': 'Live owner 0xfcb19e6a322b27c06842a71e8c725399f049ae3a. Owner-controlled '
                                                'powers present in the ABI: mint, pause, blacklist, unBlacklist, '
                                                'updatePauser, updateBlacklister, updateMasterMinter, configureMinter, '
                                                'upgradeTo, transferOwnership.',
                                      'evidence': {'owner': '0xfcb19e6a322b27c06842a71e8c725399f049ae3a',
                                                   'powers': ['mint',
                                                              'pause',
                                                              'blacklist',
                                                              'unBlacklist',
                                                              'updatePauser',
                                                              'updateBlacklister',
                                                              'updateMasterMinter',
                                                              'configureMinter',
                                                              'upgradeTo',
                                                              'transferOwnership'],
                                                   'paused': False,
                                                   'abi_source': 'proxy+implementation'},
                                      'points': 1},
                                     {'id': 'holder_concentration',
                                      'label': 'Holder concentration',
                                      'status': 'pass',
                                      'detail': 'Top 10 holders hold 23.6% of supply (largest single holder 6.2%; '
                                                '9570154+ holders, first 50 from Blockscout).',
                                      'evidence': {'top10_share_pct': 23.61,
                                                   'top1_share_pct': 6.19,
                                                   'top10': [{'address': '0x37305B1cD40574E4C5Ce33f8e8306Be057fD7341',
                                                              'share_pct': 6.19},
                                                             {'address': '0xffA69C0080582098aF595156240214b742735a5e',
                                                              'share_pct': 3.42},
                                                             {'address': '0xaD354CfBAa4A8572DD6Df021514a3931A8329Ef5',
                                                              'share_pct': 3.02}]},
                                      'points': 0},
                                     {'id': 'buy_sell_tax',
                                      'label': 'Buy/sell tax',
                                      'status': 'unknown',
                                      'detail': 'No standard tax function (buyTax/sellTax/…) in the contract ABI — tax '
                                                'is not detectable. A tax could still hide in transfer logic with no '
                                                'standard function.',
                                      'evidence': {'tax_functions_found': []},
                                      'points': 0},
                                     {'id': 'liquidity',
                                      'label': 'Liquidity',
                                      'status': 'pass',
                                      'detail': 'DexScreener-tracked liquidity on Ethereum: $10,743,312 across 2 pair(s) '
                                                '(top: uniswap '
                                                '0x2b21c65d9a7dc6926ee330a1c6e5a8037fd81774f3dc066536f800128e39f634, '
                                                '$7,759,561). On-chain V2 reserves readable. LP tokens burned (dead '
                                                'address): 0.0%.',
                                      'evidence': {'dexscreener_total_usd': 10743311.83,
                                                   'dexscreener_pairs': 2,
                                                   'top_pair': {'pairAddress': '0x2b21c65d9a7dc6926ee330a1c6e5a8037fd81774f3dc066536f800128e39f634',
                                                                'dexId': 'uniswap',
                                                                'liquidity_usd': 7759560.94},
                                                   'v2_pair': '0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc',
                                                   'v2_pair_via': 'uniswap-v2-factory',
                                                   'reserves': {'reserve0': '9614077404963',
                                                                'reserve1': '4292191907917462481618',
                                                                'token0': '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'},
                                                   'lp_burned_pct': 0.0},
                                      'points': 0},
                                     {'id': 'honeypot',
                                      'label': 'Honeypot simulation',
                                      'status': 'pass',
                                      'detail': 'Buy simulation (0.01 ETH -> token via V2 router) succeeded. Sell '
                                                'simulation (transfer of 1 USDC from the largest holder to the pair) '
                                                'succeeded. Read-only eth_call only — a passing simulation does not '
                                                'guarantee a real swap succeeds.',
                                      'evidence': {'buy': {'attempted': True,
                                                           'ok': True,
                                                           'detail': 'Buy simulation (0.01 ETH -> token via V2 router) '
                                                                     'succeeded.'},
                                                   'sell': {'attempted': True,
                                                            'ok': True,
                                                            'detail': 'Sell simulation (transfer of 1 USDC from the '
                                                                      'largest holder to the pair) succeeded.'},
                                                   'pair': '0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc'},
                                      'points': 0}],
                          'reasons': ['Owner powers: Live owner 0xfcb19e6a322b27c06842a71e8c725399f049ae3a. '
                                      'Owner-controlled powers present in the ABI: mint, pause, blacklist, unBlacklist, '
                                      'updatePauser, updateBlacklister, updateMasterMinter, configureMinter, upgradeTo, '
                                      'transferOwnership.',
                                      'Holder concentration: Top 10 holders hold 23.6% of supply (largest single holder '
                                      '6.2%; 9570154+ holders, first 50 from Blockscout).',
                                      'Buy/sell tax: No standard tax function (buyTax/sellTax/…) in the contract ABI — '
                                      'tax is not detectable. A tax could still hide in transfer logic with no standard '
                                      'function.',
                                      'Liquidity: DexScreener-tracked liquidity on Ethereum: $10,743,312 across 2 '
                                      'pair(s) (top: uniswap '
                                      '0x2b21c65d9a7dc6926ee330a1c6e5a8037fd81774f3dc066536f800128e39f634, $7,759,561). '
                                      'On-chain V2 reserves readable. LP tokens burned (dead address): 0.0%.',
                                      'Honeypot simulation: Buy simulation (0.01 ETH -> token via V2 router) succeeded. '
                                      'Sell simulation (transfer of 1 USDC from the largest holder to the pair) '
                                      'succeeded. Read-only eth_call only — a passing simulation does not guarantee a '
                                      'real swap succeeds.'],
                          'method': 'Token security API (phase 3, item 12). The token security scan from '
                                    'common/security.py reused wholesale — owner powers from the verified ABI + live '
                                    'owner() eth_call, holder concentration from Blockscout holders, buy/sell tax only '
                                    'from standard tax functions, honeypot as read-only eth_call buy/sell simulations, '
                                    'liquidity from DexScreener-tracked pairs + on-chain V2 reserves (EVM); mint/freeze '
                                    'authority + holder concentration on Solana, with the EVM checks labelled EVM-only. '
                                    'This module only adds the paid-API contract fields (security, api_url, method, '
                                    'limits, chain_label) and the cross-chain dispatch on ?chain= — no check is '
                                    're-implemented.',
                          'limits': ['A low verdict is not proof a token is safe: tax can hide in transfer logic with no '
                                     'standard tax function, ownership can sit behind a proxy or multisig this scan '
                                     'cannot see through, and a passing eth_call simulation does not guarantee a real '
                                     'swap succeeds (slippage, MEV, approvals). Liquidity totals cover '
                                     'DexScreener-tracked pairs on this chain only.',
                                     "Buy/sell tax is 'not detectable' when no standard tax function exists in the "
                                     'contract ABI — never an invented 0%. A tax can still hide in transfer logic with '
                                     'no standard function.',
                                     'Liquidity totals cover DexScreener-tracked pairs on this chain only — a stated '
                                     'lower bound, not all liquidity everywhere.',
                                     'A passing honeypot simulation does not guarantee a real swap succeeds (slippage, '
                                     'MEV, approvals). The simulations are read-only eth_call only; no transaction is '
                                     'ever sent.',
                                     'Holder concentration covers the first 50 holders from Blockscout (EVM) or the '
                                     'largest-accounts list from the public RPC (Solana, up to 20 accounts).',
                                     'On Solana the EVM checks (owner powers, tax, honeypot simulation, DEX liquidity) '
                                     "are listed with status 'n/a' and labelled EVM-only — they are never pretended to "
                                     'have run.',
                                     'Bitcoin has no token contracts to scan and is not accepted here — use the address '
                                     'screening API for Bitcoin addresses.',
                                     'Results are cached 300s server-side; a repeat call inside the window returns the '
                                     'same scan.'],
                          'coverage': {'note': 'Holder concentration covers the first 50 holders from Blockscout; '
                                               'liquidity covers DexScreener-tracked pairs on Ethereum plus on-chain V2 '
                                               'reserves where a pair exists.'},
                          'security': True,
                          'chain_label': 'Ethereum',
                          'api_url': '/api/walletscope/token/security?chain=ethereum&address=0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48'},
     'free_tier': 'Free tier today: keyless, no auth, no rate limit of our own. One token per call; response cached 300s '
                  'server-side (the scan cache — a repeat call inside the window is served in milliseconds). A cold call '
                  'fans out to Blockscout, the public RPC and DexScreener and took ~15s live (USDC on Ethereum). '
                  'Upstream rate limits (Blockscout, public RPCs, DexScreener) apply — expect occasional 502s or '
                  'per-check unknowns on slow calls (surfaced as unknown, never faked).',
     'paid_tier': 'Paid tier (planned): API key required; calls metered per scan. Higher rate limits, batch scanning of '
                  'many tokens in one call, webhooks on verdict changes for a watched token (e.g. ownership transferred, '
                  'liquidity drained below threshold), and a stored scan history with re-scan on demand. No auth, '
                  'metering, or billing is enforced yet — this endpoint is free and keyless today.'},
    {'method': 'GET', 'path': '/api/ethscope/explain', 'description': 'Plain-English transaction API (phase 3, item 13): one transaction hash in, a plain-English explanation out — one sentence ("What happened") plus the token-flow summary (token, amount, from -> to), the same explanation the "What happened" card on every transaction page shows. A new dedicated endpoint, not a rename: GET /api/<scope>/tx/{h}/explain stays exactly as it is (the tx-page card backing), while /explain is the productized paid-API contract — the same explanation payload (common/explain.py, feature 3, reused wholesale: local canonical signature map, then the keyless 4byte.directory API with only curated names accepted; swap amounts from the token transfers) plus the contract fields (explain, hash, chain_label, api_url, method, limits) and cross-chain dispatch on ?chain= (ethereum / sepolia / optimism / gnosis / solana / bitcoin). One rename vs the per-scope payload: the decoded text signature moves from `method` to `method_signature`, because `method` carries the contract method description here, as in every other phase-3 wrapper. Unknown methods are reported honestly ("method 0x... is not in the signature database"), multicall inner calls and proxy / meta-tx internals are not decoded (the sentence says so), swap amounts come from the token transfers (never from minimum-out arguments), and a token -> native swap cannot show the exact native amount (internal transfer — the sentence says so). Solana token symbols are not resolved (SPL mints shown shortened); Bitcoin change outputs cannot be identified (every output listed). Rendered at #/explain.', 'params': ['chain', 'hash'], 'example': '/api/ethscope/explain?chain=ethereum&hash=0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'paid_api': True, 'example_response': {'chain': 'ethereum', 'tx_hash': '0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'kind': 'swap', 'sentence': 'Swapped 0.03 ETH for 60460.570297 Gato via 0x7a250d56...59F2488D.', 'method_source': 'local', 'status': 'success', 'from': {'hash': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491', 'label': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491', 'short': '0xa1b9A6EC...70B7c491', 'is_contract': False, 'is_scam': False}, 'to': {'hash': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D', 'label': 'UniswapV2Router02', 'short': 'UniswapV2Router02', 'is_contract': True, 'is_scam': False}, 'flows': [{'kind': 'erc20', 'token': {'address': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', 'symbol': 'WETH', 'name': 'WETH', 'type': 'ERC-20'}, 'amount': '0.03', 'from': {'hash': '0x0000000000000000000000000000000000000000', 'label': '0x0000000000000000000000000000000000000000', 'short': '0x00000000...00000000', 'is_contract': False, 'is_scam': False}, 'to': {'hash': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D', 'label': 'UniswapV2Router02', 'short': 'UniswapV2Router02', 'is_contract': True, 'is_scam': False}, 'transfer_type': 'token_minting'}, {'kind': 'erc20', 'token': {'address': '0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2', 'symbol': 'WETH', 'name': 'WETH', 'type': 'ERC-20'}, 'amount': '0.03', 'from': {'hash': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D', 'label': 'UniswapV2Router02', 'short': 'UniswapV2Router02', 'is_contract': True, 'is_scam': False}, 'to': {'hash': '0x3027d452fb89034649aCa6F324aEBa8596C75c2C', 'label': 'UniswapV2Pair', 'short': 'UniswapV2Pair', 'is_contract': True, 'is_scam': False}, 'transfer_type': 'token_transfer'}, {'kind': 'erc20', 'token': {'address': '0x768488C988Cf2493F0e7CCb3d56547F2384E2c27', 'symbol': 'Gato', 'name': 'El Gato', 'type': 'ERC-20'}, 'amount': '60460.570297', 'from': {'hash': '0x3027d452fb89034649aCa6F324aEBa8596C75c2C', 'label': 'UniswapV2Pair', 'short': 'UniswapV2Pair', 'is_contract': True, 'is_scam': False}, 'to': {'hash': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491', 'label': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491', 'short': '0xa1b9A6EC...70B7c491', 'is_contract': False, 'is_scam': False}, 'transfer_type': 'token_transfer'}, {'kind': 'native', 'token': None, 'amount': '0.03', 'from': {'hash': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491', 'label': '0xa1b9A6EC88820C56DFbBfa53A9C449Cb70B7c491', 'short': '0xa1b9A6EC...70B7c491', 'is_contract': False, 'is_scam': False}, 'to': {'hash': '0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D', 'label': 'UniswapV2Router02', 'short': 'UniswapV2Router02', 'is_contract': True, 'is_scam': False}, 'transfer_type': 'value'}], 'note': "Swap amounts come from the transaction's token transfers (actual movements), not from the call's minimum-out arguments.", 'method_signature': 'swapExactETHForTokensSupportingFeeOnTransferTokens(uint256,address[],address,uint256)', 'explain': True, 'hash': '0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'chain_label': 'Ethereum', 'api_url': '/api/walletscope/explain?chain=ethereum&hash=0xf992b18ed585ddb7b68da195edac950af078d61c64959b63ed666439954d1f37', 'method': "Plain-English transaction API (phase 3, item 13). The tx explainer from common/explain.py (feature 3) reused wholesale — one sentence plus the token-flow summary, method names from the local canonical signature map then the keyless 4byte.directory API (only curated names accepted), swap amounts from the transaction's token transfers. This module only adds the cross-chain dispatch on ?chain= and the paid-API contract fields (explain, hash, chain_label, api_url, method, limits) — no method decoding or sentence building is re-implemented.", 'limits': ["A method the signature database does not know is reported honestly ('method 0x... is not in the signature database, so what it does cannot be described here') — never an invented description. The 4byte.directory fallback accepts only function names in the curated KNOWN_NAMES set; colliding junk signatures are never trusted.", 'Multicall inner calls are not decoded — the sentence says so and the individual actions are only visible in the token flows below. execute (proxy / meta-tx forwarding) shows the forwarded-to address and the wei attached, not the inner calldata.', "Swap amounts come from the transaction's token transfers (the actual movements), never from the call's minimum-out arguments. A token -> native swap cannot show the exact native amount — it is an internal transfer, not a token transfer — and the sentence says so.", "Only the first page of token transfers is shown when a transaction has more; the response's note says so.", 'Solana: token symbols are not resolved — SPL mints are shown shortened. SOL balance changes include the transaction fee paid by the fee payer.', 'Bitcoin: change outputs cannot be identified from chain data alone — every output is listed, including any change back to the sender.', "A failed (reverted) transaction's sentence describes what it attempted to do, prefixed as an attempt — never presented as if it succeeded.", 'Results are cached 60s server-side; a repeat call inside the window returns the same explanation.']},
     'free_tier': 'Free tier today: keyless, no auth, no rate limit of our own. One transaction per call; response cached 60s server-side (a repeat call inside the window is served from cache). A cold call fans out to Blockscout (tx detail + token transfers, plus token metadata) and to 4byte.directory only when the method is not in the local signature map, and took ~1-3s live for the example below. Upstream rate limits (Blockscout, public RPCs, mempool.space, 4byte.directory) apply — expect occasional 502s on slow calls; a 4byte failure degrades to "unknown method", never to an invented description. The per-scope /tx/{h}/explain endpoints stay free and keyless alongside this one.',
     'paid_tier': 'Paid tier (planned): API key required; calls metered per explanation. Higher rate limits, batch explanation of many transactions in one call, a larger curated signature database (more methods decoded without the 4byte fallback), and a stored explanation history with re-explanation on demand. No auth, metering, or billing is enforced yet — this endpoint is free and keyless today.'},
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
            entry = {
                "method": e["method"],
                "path": api_prefix + e["suffix"],
                "description": e["description"],
                "params": e["params"],
                "example": e["example"].replace("/api/ethscope/ethereum/", api_prefix + "/"),
            }
            # Paid-API model extension (phase 3): an entry may also
            # carry paid_api, example_response, free_tier, paid_tier
            # (or any future field). Entries without them render
            # exactly as before.
            for k, v in e.items():
                if k not in ("method", "suffix", "description", "params", "example"):
                    entry[k] = v
            endpoints.append(entry)
    endpoints.append(
        {"method": "GET", "path": api_prefix + "/chains", "description": "List the chains this app serves, with their Blockscout/RPC endpoints and native coin.", "params": [], "example": api_prefix + "/chains"}
    )
    endpoints.append(
        {"method": "GET", "path": api_prefix + "/docs", "description": "This endpoint list.", "params": [], "example": api_prefix + "/docs"}
    )
    if wallet_prefix:
        for e in DOCS_GLOBAL:
            entry = {
                "method": e["method"],
                "path": e["path"].replace("/api/ethscope", wallet_prefix),
                "description": e["description"],
                "params": e["params"],
                "example": e["example"].replace("/api/ethscope", wallet_prefix),
            }
            for k, v in e.items():
                if k not in ("method", "path", "description", "params", "example"):
                    entry[k] = v
            endpoints.append(entry)
    return {"chains": list(chains) if chains is not None else list(CHAINS.keys()), "endpoints": endpoints}
