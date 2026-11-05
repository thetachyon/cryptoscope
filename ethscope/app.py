"""EthScope — Etherscan-style Ethereum mainnet block explorer.

Data backend: Blockscout v2 public API (https://eth.blockscout.com/api/v2).
Keyless: no API keys anywhere.

Local dev:  uvicorn app:app --port 8034
Vercel:      served via api/index.py (ASGI app) + vercel.json rewrites.
"""
import os
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# The sandbox sets NO_PROXY with bracketed IPv6 literals ([::1], [fd8b:...])
# which httpx's env-proxy parser rejects (InvalidURL). Strip the bracketed
# forms; the bare forms (::1, fd8b:...) remain and match identically.
for _k in ("no_proxy", "NO_PROXY"):
    _v = os.environ.get(_k)
    if _v:
        os.environ[_k] = ",".join(p for p in _v.split(",") if "[" not in p)

BS = "https://eth.blockscout.com/api/v2"
TIMEOUT = httpx.Timeout(10.0)
HEADERS = {"User-Agent": "EthScope/1.0"}
CACHE_TTL = 60  # seconds, for hot endpoints

_cache: dict = {}

app = FastAPI(title="EthScope", version="1.0.0")


# ---------------------------------------------------------------- cache
def _cache_get(key):
    ent = _cache.get(key)
    if ent and ent[0] > time.time():
        return ent[1]
    _cache.pop(key, None)
    return None


def _cache_set(key, val, ttl=CACHE_TTL):
    if len(_cache) > 512:  # simple bound
        _cache.clear()
    _cache[key] = (time.time() + ttl, val)


# ---------------------------------------------------------------- errors
class UpstreamError(Exception):
    def __init__(self, msg, status=502):
        self.msg = msg
        self.status = status


@app.exception_handler(UpstreamError)
async def _upstream_handler(request: Request, exc: UpstreamError):
    return JSONResponse({"error": exc.msg}, status_code=exc.status)


@app.exception_handler(Exception)
async def _generic_handler(request: Request, exc: Exception):
    if isinstance(exc, StarletteHTTPException):
        raise exc
    return JSONResponse({"error": "Internal server error"}, status_code=500)


# ---------------------------------------------------------------- http
async def bs(path, params=None):
    """GET Blockscout v2. Returns parsed JSON, None on 404, raises UpstreamError."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as c:
            r = await c.get(BS + path, params=params or {})
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        raise UpstreamError(f"Blockscout unreachable ({type(e).__name__})")
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise UpstreamError(f"Blockscout returned HTTP {r.status_code}")
    try:
        return r.json()
    except Exception:
        raise UpstreamError("Blockscout returned invalid JSON")


# ---------------------------------------------------------------- format helpers
def _wei_str(v) -> str:
    try:
        return str(int(str(v)))
    except (ValueError, TypeError):
        return "0"


def wei_to_eth(v) -> str:
    """Wei (int-as-string) -> ETH decimal string, trailing zeros stripped."""
    try:
        d = Decimal(_wei_str(v)) / Decimal(10**18)
    except InvalidOperation:
        return "0"
    s = format(d.normalize(), "f")
    return s


def gwei(v) -> str:
    try:
        return f"{Decimal(_wei_str(v)) / Decimal(10**9):.2f}"
    except InvalidOperation:
        return "0.00"


def token_amount(value, decimals) -> str:
    try:
        dec = int(decimals or 0)
        d = Decimal(_wei_str(value)) / (Decimal(10) ** dec)
    except (InvalidOperation, ValueError):
        return "0"
    s = format(d.normalize(), "f")
    if "." in s:
        i, f = s.split(".")
        f = f[:6].rstrip("0")
        s = i + ("." + f if f else "")
    return s


def short(h: str) -> str:
    if not h or len(h) < 18:
        return h or ""
    return h[:10] + "..." + h[-8:]


def age(ts: str) -> str:
    """ISO timestamp -> '12 secs ago' style relative age."""
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    except Exception:
        return ts
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
    days = hrs // 24
    return f"{days} days ago"


def addr_info(a) -> dict | None:
    """Compact address object: hash + display label + flags."""
    if not a or not isinstance(a, dict):
        return None
    h = a.get("hash") or ""
    label = a.get("ens_domain_name") or a.get("name") or h
    return {
        "hash": h,
        "label": label,
        "short": short(h) if label == h else label,
        "is_contract": bool(a.get("is_contract")),
        "is_scam": bool(a.get("is_scam")),
    }


def tx_short(t: dict) -> dict:
    return {
        "hash": t.get("hash"),
        "block": t.get("block"),
        "timestamp": t.get("timestamp"),
        "age": age(t.get("timestamp")),
        "from": addr_info(t.get("from")),
        "to": addr_info(t.get("to")),
        "value_eth": wei_to_eth(t.get("value")),
        "fee_eth": wei_to_eth((t.get("fee") or {}).get("value")) if isinstance(t.get("fee"), dict) else "0",
        "gas_price_gwei": gwei(t.get("gas_price")),
        "status": t.get("result") or t.get("status"),
        "method": t.get("method"),
        "type": t.get("type"),
    }


def block_short(b: dict) -> dict:
    return {
        "height": b.get("height"),
        "hash": b.get("hash"),
        "timestamp": b.get("timestamp"),
        "age": age(b.get("timestamp")),
        "miner": addr_info(b.get("miner")),
        "tx_count": b.get("transactions_count"),
        "gas_used": b.get("gas_used"),
        "gas_limit": b.get("gas_limit"),
        "gas_used_pct": round(float(b.get("gas_used_percentage") or 0), 2),
        "base_fee_gwei": gwei(b.get("base_fee_per_gas")),
        "burnt_fees_eth": wei_to_eth(b.get("burnt_fees")),
        "rewards": [
            {"recipient": r.get("recipient"), "type": r.get("type"), "value_eth": wei_to_eth(r.get("reward"))}
            for r in (b.get("rewards") or [])
        ],
        "size": b.get("size"),
        "withdrawals_count": b.get("withdrawals_count"),
        "blob_tx_count": b.get("blob_transactions_count"),
    }


def token_transfer_short(tt: dict) -> dict:
    tok = tt.get("token") or {}
    total = tt.get("total") or {}
    dec = total.get("decimals") if total.get("decimals") is not None else tok.get("decimals")
    return {
        "tx_hash": tt.get("transaction_hash"),
        "block": tt.get("block_number"),
        "timestamp": tt.get("timestamp"),
        "age": age(tt.get("timestamp")),
        "from": addr_info(tt.get("from")),
        "to": addr_info(tt.get("to")),
        "token": {
            "address": tok.get("address"),
            "name": tok.get("name"),
            "symbol": tok.get("symbol"),
            "decimals": dec,
            "type": tok.get("type"),
            "icon_url": tok.get("icon_url"),
        },
        "amount": token_amount(total.get("value"), dec),
        "type": tt.get("type"),
    }


def internal_tx_short(i: dict) -> dict:
    return {
        "tx_hash": i.get("transaction_hash"),
        "block": i.get("block_number"),
        "timestamp": i.get("timestamp"),
        "age": age(i.get("timestamp")),
        "from": addr_info(i.get("from")),
        "to": addr_info(i.get("to")),
        "value_eth": wei_to_eth(i.get("value")),
        "gas_limit": i.get("gas_limit"),
        "type": i.get("type"),
        "success": i.get("success"),
        "error": i.get("error"),
        "created_contract": i.get("created_contract", {}).get("hash") if isinstance(i.get("created_contract"), dict) else None,
    }


def decode_input(hexstr: str | None) -> str | None:
    """Return UTF-8 text of tx input if it is printable, else None."""
    if not hexstr or hexstr == "0x":
        return None
    try:
        raw = bytes.fromhex(hexstr[2:] if hexstr.startswith("0x") else hexstr)
        txt = raw.decode("utf-8")
    except Exception:
        return None
    if txt and all(32 <= ord(c) < 127 or c in "\n\r\t" for c in txt):
        return txt
    return None


# ---------------------------------------------------------------- API: stats / lists
@app.get("/api/ethscope/stats")
async def api_stats():
    key = "stats"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await bs("/stats")
    if d is None:
        raise UpstreamError("Stats not available")
    gp = d.get("gas_prices") or {}
    out = {
        "eth_price_usd": d.get("coin_price"),
        "eth_price_change_24h_pct": d.get("coin_price_change_percentage"),
        "market_cap_usd": d.get("market_cap"),
        "latest_block": d.get("total_blocks"),
        "total_blocks": d.get("total_blocks"),
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


@app.get("/api/ethscope/blocks")
async def api_blocks(request: Request):
    key = "blocks:" + str(sorted(dict(request.query_params).items()))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await bs("/blocks", params=dict(request.query_params))
    if d is None:
        raise UpstreamError("Blocks not available")
    out = {
        "items": [block_short(b) for b in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }
    _cache_set(key, out)
    return out


@app.get("/api/ethscope/txs")
async def api_txs_latest(request: Request):
    key = "txs:" + str(sorted(dict(request.query_params).items()))
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await bs("/transactions", params=dict(request.query_params))
    if d is None:
        raise UpstreamError("Transactions not available")
    out = {
        "items": [tx_short(t) for t in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }
    _cache_set(key, out)
    return out


# ---------------------------------------------------------------- API: block
async def _resolve_height(n: str) -> str:
    if n == "latest":
        d = await bs("/stats")
        if d and d.get("total_blocks"):
            return str(d["total_blocks"])
    return n


@app.get("/api/ethscope/block/{n}")
async def api_block(n: str):
    n = await _resolve_height(n)
    key = f"block:{n}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await bs(f"/blocks/{n}")
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


@app.get("/api/ethscope/block/{n}/txs")
async def api_block_txs(n: str, request: Request):
    n = await _resolve_height(n)
    height = n
    if n.startswith("0x") and len(n) == 66:
        b = await bs(f"/blocks/{n}")
        if b is None:
            return JSONResponse({"error": "Block not found"}, status_code=404)
        height = b.get("height")
    params = dict(request.query_params)
    params["block_number"] = height
    d = await bs("/transactions", params=params)
    if d is None:
        return JSONResponse({"error": "Block not found"}, status_code=404)
    return {
        "items": [tx_short(t) for t in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


# ---------------------------------------------------------------- API: transaction
@app.get("/api/ethscope/tx/{h}")
async def api_tx(h: str):
    d = await bs(f"/transactions/{h}")
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


@app.get("/api/ethscope/tx/{h}/token-transfers")
async def api_tx_token_transfers(h: str):
    d = await bs(f"/transactions/{h}/token-transfers")
    if d is None:
        return JSONResponse({"error": "Transaction not found"}, status_code=404)
    return {
        "items": [token_transfer_short(tt) for tt in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


@app.get("/api/ethscope/tx/{h}/logs")
async def api_tx_logs(h: str):
    d = await bs(f"/transactions/{h}/logs")
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


@app.get("/api/ethscope/tx/{h}/internal")
async def api_tx_internal(h: str):
    d = await bs(f"/transactions/{h}/internal-transactions")
    if d is None:
        return JSONResponse({"error": "Transaction not found"}, status_code=404)
    return {
        "items": [internal_tx_short(i) for i in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


# ---------------------------------------------------------------- API: address
@app.get("/api/ethscope/address/{h}")
async def api_address(h: str):
    d = await bs(f"/addresses/{h}")
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


@app.get("/api/ethscope/address/{h}/txs")
async def api_address_txs(h: str, request: Request):
    d = await bs(f"/addresses/{h}/transactions", params=dict(request.query_params))
    if d is None:
        return JSONResponse({"error": "Address not found"}, status_code=404)
    return {
        "items": [tx_short(t) for t in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


@app.get("/api/ethscope/address/{h}/token-transfers")
async def api_address_token_transfers(h: str, request: Request):
    d = await bs(f"/addresses/{h}/token-transfers", params=dict(request.query_params))
    if d is None:
        return JSONResponse({"error": "Address not found"}, status_code=404)
    return {
        "items": [token_transfer_short(tt) for tt in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


@app.get("/api/ethscope/address/{h}/internal")
async def api_address_internal(h: str, request: Request):
    d = await bs(f"/addresses/{h}/internal-transactions", params=dict(request.query_params))
    if d is None:
        return JSONResponse({"error": "Address not found"}, status_code=404)
    return {
        "items": [internal_tx_short(i) for i in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


@app.get("/api/ethscope/address/{h}/tokens")
async def api_address_tokens(h: str):
    d = await bs(f"/addresses/{h}/token-balances")
    if d is None:
        return JSONResponse({"error": "Address not found"}, status_code=404)
    items = []
    for tb in d if isinstance(d, list) else d.get("items", []):
        tok = tb.get("token") or {}
        items.append(
            {
                "address": tok.get("address"),
                "name": tok.get("name"),
                "symbol": tok.get("symbol"),
                "decimals": tok.get("decimals"),
                "type": tok.get("type"),
                "icon_url": tok.get("icon_url"),
                "balance": token_amount(tb.get("value"), tok.get("decimals")),
                "token_id": tb.get("token_id"),
            }
        )
    return {"items": items}


# ---------------------------------------------------------------- API: token
@app.get("/api/ethscope/token/{h}")
async def api_token(h: str):
    d = await bs(f"/tokens/{h}")
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


@app.get("/api/ethscope/token/{h}/transfers")
async def api_token_transfers(h: str, request: Request):
    d = await bs(f"/tokens/{h}/transfers", params=dict(request.query_params))
    if d is None:
        return JSONResponse({"error": "Token not found"}, status_code=404)
    return {
        "items": [token_transfer_short(tt) for tt in d.get("items", [])],
        "next_page_params": d.get("next_page_params"),
    }


# ---------------------------------------------------------------- API: search
@app.get("/api/ethscope/search")
async def api_search(q: str = ""):
    d = await bs("/search", params={"q": q})
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


# ---------------------------------------------------------------- static frontend (mounted last so /api/* routes win)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
