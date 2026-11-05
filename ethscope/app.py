"""EthScope — Etherscan-style block explorer (multichain).

Data backends (all keyless, no API keys anywhere):
- Blockscout v2 public API per chain (blocks, txs, addresses, tokens, NFTs,
  contract source, charts).
- Public JSON-RPC endpoints (pending block, tx count / nonce, eth_call for
  contract reads).
- CoinGecko keyless API (native-coin price charts, token USD prices).

Local dev:  uvicorn app:app --port 8034
Vercel:      served via api/index.py (ASGI app) + vercel.json rewrites.
"""
import json
import os
import re
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

# ---------------------------------------------------------------- chains
# Only chains whose Blockscout host answers AND whose public RPC answers were
# kept (verified 2026-11-05). Dropped: arbitrum.blockscout.com (connection
# fails), base.blockscout.com (HTTP 403), polygon.blockscout.com (HTTP 403
# Cloudflare).
CHAINS = {
    "ethereum": {
        "label": "Ethereum",
        "blockscout": "https://eth.blockscout.com/api/v2",
        "rpc": "https://ethereum.publicnode.com",
        "coingecko_platform": "ethereum",
        "coingecko_coin": "ethereum",
        "native": "ETH",
    },
    "sepolia": {
        "label": "Sepolia",
        "blockscout": "https://eth-sepolia.blockscout.com/api/v2",
        "rpc": "https://ethereum-sepolia.publicnode.com",
        "coingecko_platform": "ethereum",
        "coingecko_coin": "ethereum",
        "native": "ETH",
    },
    "optimism": {
        "label": "Optimism",
        "blockscout": "https://optimism.blockscout.com/api/v2",
        "rpc": "https://optimism.publicnode.com",
        "coingecko_platform": "optimistic-ethereum",
        "coingecko_coin": "ethereum",
        "native": "ETH",
    },
    "gnosis": {
        "label": "Gnosis",
        "blockscout": "https://gnosis.blockscout.com/api/v2",
        "rpc": "https://gnosis.publicnode.com",
        "coingecko_platform": "xdai",
        "coingecko_coin": "xdai",
        "native": "xDAI",
    },
}
DEFAULT_CHAIN = "ethereum"

TIMEOUT = httpx.Timeout(10.0)
HEADERS = {"User-Agent": "EthScope/1.0"}
CACHE_TTL = 60  # seconds, for hot endpoints
CHART_TTL = 600  # seconds, for chart / CoinGecko data
TOKEN_PRICE_TTL = 300  # seconds, for per-token CoinGecko prices

_cache: dict = {}

app = FastAPI(title="EthScope", version="1.1.0")


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


def _require_chain(chain: str):
    """Return None if the chain is known, else a 404 JSONResponse."""
    if chain not in CHAINS:
        return JSONResponse({"error": "Unknown chain"}, status_code=404)
    return None


# ---------------------------------------------------------------- http
async def bs(chain, path, params=None):
    """GET Blockscout v2 for a chain. Returns parsed JSON, None on 404,
    raises UpstreamError otherwise."""
    base = CHAINS[chain]["blockscout"]
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(base + path, params=params or {})
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


async def rpc(chain, method, params=None):
    """JSON-RPC call against a chain's public RPC. Returns the `result`
    field. A JSON-RPC error object becomes a 400 UpstreamError (e.g. a
    reverted eth_call); transport failures become 502."""
    url = CHAINS[chain]["rpc"]
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            r = await c.post(url, json=payload)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        raise UpstreamError(f"{CHAINS[chain]['label']} RPC unreachable ({type(e).__name__})")
    if r.status_code >= 400:
        raise UpstreamError(f"{CHAINS[chain]['label']} RPC returned HTTP {r.status_code}")
    try:
        d = r.json()
    except Exception:
        raise UpstreamError(f"{CHAINS[chain]['label']} RPC returned invalid JSON")
    if isinstance(d, dict) and d.get("error"):
        err = d["error"]
        msg = err.get("message") if isinstance(err, dict) else str(err)
        raise UpstreamError(f"RPC error: {msg}", status=400)
    return d.get("result") if isinstance(d, dict) else None


async def coingecko(path, params=None):
    """GET CoinGecko keyless API. Returns parsed JSON or None on any
    failure (callers degrade gracefully, never hang)."""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            r = await c.get("https://api.coingecko.com/api/v3" + path, params=params or {})
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError):
        return None
    if r.status_code != 200:
        return None
    try:
        return r.json()
    except Exception:
        return None


async def cg_token_price(platform, contract_address):
    """USD price for one token contract on a CoinGecko platform id.
    CoinGecko's keyless tier caps simple/token_price at ONE contract
    address per request (error 10012 otherwise), so this is per-address
    with a 5-min cache. Returns a float or None."""
    key = f"cgprice:{platform}:{contract_address.lower()}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await coingecko(
        f"/simple/token_price/{platform}",
        params={"contract_addresses": contract_address, "vs_currencies": "usd"},
    )
    price = None
    if isinstance(d, dict):
        entry = d.get(contract_address.lower()) or d.get(contract_address)
        if isinstance(entry, dict) and entry.get("usd") is not None:
            try:
                price = float(entry["usd"])
            except (TypeError, ValueError):
                price = None
    _cache_set(key, price, ttl=TOKEN_PRICE_TTL)
    return price


# ---------------------------------------------------------------- format helpers
def _wei_str(v) -> str:
    try:
        return str(int(str(v)))
    except (ValueError, TypeError):
        return "0"


def _hex_int(v) -> int:
    """Parse a hex (0x...) or decimal quantity to int; 0 on garbage."""
    try:
        s = str(v)
        return int(s, 16) if s.startswith(("0x", "0X")) else int(s)
    except (ValueError, TypeError):
        return 0


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


def rpc_tx_short(t: dict) -> dict:
    """tx_short shape for a raw JSON-RPC transaction object (pending block)."""
    return {
        "hash": t.get("hash"),
        "block": None,
        "timestamp": None,
        "age": "",
        "from": addr_info({"hash": t.get("from")}) if t.get("from") else None,
        "to": addr_info({"hash": t.get("to")}) if t.get("to") else None,
        "value_eth": wei_to_eth(_hex_int(t.get("value"))),
        "fee_eth": "0",
        "gas_price_gwei": f"{_hex_int(t.get('gasPrice')) / 10**9:.2f}" if t.get("gasPrice") else "0.00",
        "status": "pending",
        "method": None,
        "type": t.get("type"),
        "gas": _hex_int(t.get("gas")),
        "nonce": _hex_int(t.get("nonce")),
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


# ---------------------------------------------------------------- keccak-256 (pure python; stdlib sha3 is NOT keccak)
_KECCAK_RC = [
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
]
_KECCAK_R = [
    [0, 36, 3, 41, 18],
    [1, 44, 10, 45, 2],
    [62, 6, 43, 15, 61],
    [28, 55, 25, 21, 56],
    [27, 20, 39, 8, 14],
]
_KECCAK_MASK = (1 << 64) - 1


def _rol(x, n):
    return ((x << n) | (x >> (64 - n))) & _KECCAK_MASK if n else x & _KECCAK_MASK


def _keccak_f1600(a):
    for rnd in range(24):
        c = [a[x] ^ a[x + 5] ^ a[x + 10] ^ a[x + 15] ^ a[x + 20] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rol(c[(x + 1) % 5], 1) for x in range(5)]
        a = [a[x + 5 * y] ^ d[x] for y in range(5) for x in range(5)]
        b = [0] * 25
        for x in range(5):
            for y in range(5):
                b[y + 5 * ((2 * x + 3 * y) % 5)] = _rol(a[x + 5 * y], _KECCAK_R[x][y])
        a = [
            b[x + 5 * y] ^ ((~b[(x + 1) % 5 + 5 * y]) & b[(x + 2) % 5 + 5 * y])
            for y in range(5)
            for x in range(5)
        ]
        a[0] ^= _KECCAK_RC[rnd]
    return a


def keccak_256(data: bytes) -> bytes:
    """Keccak-256 (Ethereum's hash, not NIST SHA3-256)."""
    rate = 136  # bytes, for 512-bit capacity
    state = [0] * 25
    # pad10*1 with domain byte 0x01
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != rate - 1:
        padded.append(0x00)
    padded.append(0x80)
    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            state[i] ^= int.from_bytes(block[i * 8:(i + 1) * 8], "little")
        state = _keccak_f1600(state)
    return b"".join(state[i].to_bytes(8, "little") for i in range(4))


# ---------------------------------------------------------------- minimal ABI codec
_TYPE_RE = re.compile(r"^([a-zA-Z]+)([0-9]*)((?:\[\d*\])*)$")


def _parse_type(t: str):
    """'uint256[2][]' -> ('uint256', [2, None]); raises ValueError."""
    m = _TYPE_RE.match(t or "")
    if not m:
        raise ValueError(f"Unsupported ABI type: {t!r}")
    base = m.group(1) + m.group(2)
    dims = [int(x) if x else None for x in re.findall(r"\[(\d*)\]", m.group(3))]
    if base not in (
        "address", "bool", "string", "bytes",
        *[f"uint{n}" for n in range(8, 257, 8)],
        *[f"int{n}" for n in range(8, 257, 8)],
        *[f"bytes{n}" for n in range(1, 33)],
    ):
        raise ValueError(f"Unsupported ABI type: {t!r}")
    return base, dims


def _join_type(base, dims):
    return base + "".join(f"[{d if d is not None else ''}]" for d in dims)


def _is_dynamic(base, dims) -> bool:
    if dims:
        if dims[-1] is None:
            return True
        return _is_dynamic(base, dims[:-1])
    return base in ("string", "bytes")


def _static_size(base, dims) -> int:
    if dims:
        return dims[-1] * _static_size(base, dims[:-1])
    return 32


def _coerce_int(v, bits, signed):
    if isinstance(v, bool):
        raise ValueError("Boolean is not an integer argument")
    try:
        n = int(str(v), 0) if isinstance(v, str) and str(v).startswith(("0x", "0X")) else int(v)
    except (ValueError, TypeError):
        raise ValueError(f"Not an integer: {v!r}")
    lo, hi = (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if signed else (0, (1 << bits) - 1)
    if not lo <= n <= hi:
        raise ValueError(f"Integer {n} out of range for {'int' if signed else 'uint'}{bits}")
    return n


def _encode_static(base, dims, v) -> bytes:
    if dims:
        if not isinstance(v, (list, tuple)) or len(v) != dims[-1]:
            raise ValueError(f"Expected array of length {dims[-1]} for {_join_type(base, dims)}")
        elem = _join_type(base, dims[:-1])
        eb, ed = _parse_type(elem)
        return b"".join(_encode_static(eb, ed, item) for item in v)
    if base == "address":
        s = str(v)
        if not re.fullmatch(r"0x[0-9a-fA-F]{40}", s):
            raise ValueError(f"Not an address: {v!r}")
        return bytes(12) + bytes.fromhex(s[2:])
    if base.startswith("uint"):
        return _coerce_int(v, int(base[4:]), False).to_bytes(32, "big")
    if base.startswith("int"):
        n = _coerce_int(v, int(base[3:]), True)
        return (n % (1 << 256)).to_bytes(32, "big")
    if base == "bool":
        if not isinstance(v, bool):
            raise ValueError(f"Not a boolean: {v!r}")
        return (1 if v else 0).to_bytes(32, "big")
    if base.startswith("bytes") and base != "bytes":
        n = int(base[5:])
        s = v.hex() if isinstance(v, (bytes, bytearray)) else str(v)
        if s.startswith(("0x", "0X")):
            s = s[2:]
        try:
            raw = bytes.fromhex(s)
        except ValueError:
            raise ValueError(f"Not hex bytes: {v!r}")
        if len(raw) != n:
            raise ValueError(f"Expected {n} bytes for {base}, got {len(raw)}")
        return raw + bytes(32 - n)
    raise ValueError(f"Cannot statically encode {base}")


def _encode_dynamic(base, dims, v) -> bytes:
    if dims and dims[-1] is None:
        if not isinstance(v, (list, tuple)):
            raise ValueError(f"Expected array for {_join_type(base, dims)}")
        elem = _join_type(base, dims[:-1])
        body = _encode_args([elem] * len(v), list(v))
        return len(v).to_bytes(32, "big") + body
    if base == "string":
        raw = str(v).encode("utf-8")
        return len(raw).to_bytes(32, "big") + raw + bytes((-len(raw)) % 32)
    if base == "bytes":
        s = v.hex() if isinstance(v, (bytes, bytearray)) else str(v)
        if s.startswith(("0x", "0X")):
            s = s[2:]
        try:
            raw = bytes.fromhex(s)
        except ValueError:
            raise ValueError(f"Not hex bytes: {v!r}")
        return len(raw).to_bytes(32, "big") + raw + bytes((-len(raw)) % 32)
    raise ValueError(f"Cannot dynamically encode {base}")


def _encode_args(types, values) -> bytes:
    if len(types) != len(values):
        raise ValueError(f"Expected {len(types)} argument(s), got {len(values)}")
    heads, tails = [], []
    offset = 32 * len(types)
    for t, v in zip(types, values):
        base, dims = _parse_type(t)
        if _is_dynamic(base, dims):
            heads.append(offset.to_bytes(32, "big"))
            tail = _encode_dynamic(base, dims, v)
            tails.append(tail)
            offset += len(tail)
        else:
            heads.append(_encode_static(base, dims, v))
    return b"".join(heads + tails)


def _decode_static(base, dims, data, pos):
    if dims:
        sz = _static_size(base, dims[:-1])
        return [
            _decode_static(base, dims[:-1], data, pos + i * sz)
            for i in range(dims[-1])
        ]
    word = data[pos:pos + 32]
    if len(word) < 32:
        raise ValueError("Output too short")
    if base == "address":
        return "0x" + word[12:].hex()
    if base.startswith("uint"):
        return str(int.from_bytes(word, "big"))
    if base.startswith("int"):
        n = int.from_bytes(word, "big")
        bits = int(base[3:])
        if n >= 1 << (bits - 1):
            n -= 1 << bits
        return str(n)
    if base == "bool":
        return word[-1] != 0
    if base.startswith("bytes") and base != "bytes":
        return "0x" + word[:int(base[5:])].hex()
    raise ValueError(f"Cannot decode {base}")


def _decode_dynamic(base, dims, data, pos):
    if dims and dims[-1] is None:
        n = int.from_bytes(data[pos:pos + 32], "big")
        body = pos + 32
        return [
            _decode_at(_join_type(base, dims[:-1]), data, body + 32 * i, body)
            for i in range(n)
        ]
    n = int.from_bytes(data[pos:pos + 32], "big")
    raw = data[pos + 32:pos + 32 + n]
    if len(raw) < n:
        raise ValueError("Output too short")
    if base == "string":
        return raw.decode("utf-8", "replace")
    return "0x" + raw.hex()


def _decode_at(t, data, pos, origin):
    base, dims = _parse_type(t)
    if _is_dynamic(base, dims):
        off = int.from_bytes(data[pos:pos + 32], "big")
        return _decode_dynamic(base, dims, data, origin + off)
    return _decode_static(base, dims, data, pos)


def _decode_args(types, data: bytes):
    return [_decode_at(t, data, 32 * i, 0) for i, t in enumerate(types)]


def _function_selector(name, input_types) -> bytes:
    sig = f"{name}({','.join(input_types)})"
    return keccak_256(sig.encode())[:4]


# ---------------------------------------------------------------- core logic (chain-parameterized)
# Legacy /api/ethscope/* routes are mainnet aliases that call these with
# chain="ethereum"; chain-prefixed routes pass their {chain} through.
async def _resolve_height(chain, n: str) -> str:
    if n == "latest":
        d = await bs(chain, "/stats")
        if d and d.get("total_blocks"):
            return str(d["total_blocks"])
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
    out = {
        "chain": chain,
        "native": CHAINS[chain]["native"],
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
    """Token balances with USD values. Blockscout `exchange_rate` wins;
    otherwise one cached CoinGecko lookup per token (max 5 per call —
    CoinGecko's keyless tier allows only one contract address per
    simple/token_price request). ERC-721/1155 items get null USD fields.
    A CoinGecko failure never breaks the endpoint."""
    d = await bs(chain, f"/addresses/{h}/token-balances")
    if d is None:
        return JSONResponse({"error": "Address not found"}, status_code=404)
    items = []
    for tb in d if isinstance(d, list) else d.get("items", []):
        tok = tb.get("token") or {}
        items.append(
            {
                "address": tok.get("address_hash") or tok.get("address"),
                "name": tok.get("name"),
                "symbol": tok.get("symbol"),
                "decimals": tok.get("decimals"),
                "type": tok.get("type"),
                "icon_url": tok.get("icon_url"),
                "balance": token_amount(tb.get("value"), tok.get("decimals")),
                "token_id": tb.get("token_id"),
                "usd_price": None,
                "usd_value": None,
                "_exchange_rate": tok.get("exchange_rate"),
            }
        )
    # USD enrichment for fungible tokens only. Blockscout's exchange_rate
    # wins; otherwise one cached CoinGecko lookup per token (max 5 per
    # call — CoinGecko's keyless tier allows only one contract address
    # per simple/token_price request). ERC-721/1155 items get null USD
    # fields. A CoinGecko failure never breaks the endpoint.
    platform = CHAINS[chain]["coingecko_platform"]
    cg_lookups = 0
    total_usd = Decimal(0)
    for it in items:
        if it["type"] != "ERC-20" or not it["address"]:
            it.pop("_exchange_rate", None)
            continue
        price = None
        if it.get("_exchange_rate") is not None:
            try:
                price = float(it["_exchange_rate"])
            except (TypeError, ValueError):
                price = None
        elif cg_lookups < 5:
            cg_lookups += 1
            price = await cg_token_price(platform, it["address"])
        it.pop("_exchange_rate", None)
        if price is not None:
            try:
                bal = Decimal(it["balance"])
                val = bal * Decimal(str(price))
                it["usd_price"] = price
                it["usd_value"] = float(val)
                total_usd += val
            except (InvalidOperation, ValueError):
                pass
    return {"items": items, "total_usd": float(total_usd)}


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


def _instance_short(inst: dict) -> dict:
    return {
        "id": inst.get("id"),
        "image_url": inst.get("image_url"),
        "media_url": inst.get("media_url"),
        "media_type": inst.get("media_type"),
        "animation_url": inst.get("animation_url"),
        "external_app_url": inst.get("external_app_url"),
        "is_unique": inst.get("is_unique"),
        "metadata": inst.get("metadata"),
        "owner": addr_info(inst.get("owner")) if isinstance(inst.get("owner"), dict) else None,
        "token": (
            {
                "address": (inst.get("token") or {}).get("address"),
                "name": (inst.get("token") or {}).get("name"),
                "symbol": (inst.get("token") or {}).get("symbol"),
                "type": (inst.get("token") or {}).get("type"),
            }
            if isinstance(inst.get("token"), dict)
            else None
        ),
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
    {"method": "GET", "suffix": "/tx/{h}/token-transfers", "description": "Token transfers in a transaction.", "params": [], "example": "/api/ethscope/ethereum/tx/0x.../token-transfers"},
    {"method": "GET", "suffix": "/tx/{h}/logs", "description": "Event logs emitted by a transaction.", "params": [], "example": "/api/ethscope/ethereum/tx/0x.../logs"},
    {"method": "GET", "suffix": "/tx/{h}/internal", "description": "Internal transactions (calls) of a transaction.", "params": [], "example": "/api/ethscope/ethereum/tx/0x.../internal"},
    {"method": "GET", "suffix": "/address/{h}", "description": "Address overview: balance, ENS, contract flags, token info.", "params": [], "example": "/api/ethscope/ethereum/address/0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"},
    {"method": "GET", "suffix": "/address/{h}/txs", "description": "Transactions involving an address.", "params": ["page_number"], "example": "/api/ethscope/ethereum/address/0xd8dA.../txs"},
    {"method": "GET", "suffix": "/address/{h}/token-transfers", "description": "Token transfers involving an address.", "params": ["page_number"], "example": "/api/ethscope/ethereum/address/0xd8dA.../token-transfers"},
    {"method": "GET", "suffix": "/address/{h}/internal", "description": "Internal transactions involving an address.", "params": ["page_number"], "example": "/api/ethscope/ethereum/address/0xd8dA.../internal"},
    {"method": "GET", "suffix": "/address/{h}/tokens", "description": "Token balances with per-token USD price/value and total_usd (ERC-20 only; ERC-721/1155 get null USD fields).", "params": [], "example": "/api/ethscope/ethereum/address/0xd8dA.../tokens"},
    {"method": "GET", "suffix": "/address/{h}/txcount", "description": "Outgoing-transaction count (nonce) for an address, via public RPC.", "params": [], "example": "/api/ethscope/ethereum/address/0xd8dA.../txcount"},
    {"method": "GET", "suffix": "/token/{h}", "description": "Token overview: name, symbol, decimals, type, supply, holders, USD price.", "params": [], "example": "/api/ethscope/ethereum/token/0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"},
    {"method": "GET", "suffix": "/token/{h}/transfers", "description": "Transfers of a token.", "params": ["page_number"], "example": "/api/ethscope/ethereum/token/0xA0b8.../transfers"},
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


def _docs_payload():
    endpoints = []
    for e in DOCS_BASE:
        endpoints.append(
            {"method": e["method"], "path": "/api/ethscope" + e["suffix"], "description": e["description"], "params": e["params"], "example": e["example"].replace("/api/ethscope/ethereum/", "/api/ethscope/")}
        )
        endpoints.append(
            {"method": e["method"], "path": "/api/ethscope/{chain}" + e["suffix"], "description": e["description"], "params": e["params"], "example": e["example"]}
        )
    endpoints.append({"method": "GET", "path": "/api/ethscope/chains", "description": "List the supported chains with their Blockscout/RPC endpoints and native coin.", "params": [], "example": "/api/ethscope/chains"})
    endpoints.append({"method": "GET", "path": "/api/ethscope/docs", "description": "This endpoint list.", "params": [], "example": "/api/ethscope/docs"})
    return {"chains": list(CHAINS.keys()), "endpoints": endpoints}


# ---------------------------------------------------------------- global endpoints
@app.get("/api/ethscope/chains")
async def api_chains():
    return {
        "chains": [
            {
                "id": cid,
                "label": c["label"],
                "blockscout": c["blockscout"],
                "rpc": c["rpc"],
                "coingecko_platform": c["coingecko_platform"],
                "native": c["native"],
            }
            for cid, c in CHAINS.items()
        ]
    }


@app.get("/api/ethscope/docs")
async def api_docs():
    return _docs_payload()


# ---------------------------------------------------------------- legacy mainnet routes (aliases for chain=ethereum)
@app.get("/api/ethscope/stats")
async def api_stats():
    return await _stats(DEFAULT_CHAIN)


@app.get("/api/ethscope/blocks")
async def api_blocks(request: Request):
    return await _blocks(DEFAULT_CHAIN, dict(request.query_params))


@app.get("/api/ethscope/txs")
async def api_txs_latest(request: Request):
    return await _txs_latest(DEFAULT_CHAIN, dict(request.query_params))


@app.get("/api/ethscope/block/{n}")
async def api_block(n: str):
    return await _block(DEFAULT_CHAIN, n)


@app.get("/api/ethscope/block/{n}/txs")
async def api_block_txs(n: str, request: Request):
    return await _block_txs(DEFAULT_CHAIN, n, dict(request.query_params))


@app.get("/api/ethscope/tx/{h}")
async def api_tx(h: str):
    return await _tx(DEFAULT_CHAIN, h)


@app.get("/api/ethscope/tx/{h}/token-transfers")
async def api_tx_token_transfers(h: str):
    return await _tx_token_transfers(DEFAULT_CHAIN, h)


@app.get("/api/ethscope/tx/{h}/logs")
async def api_tx_logs(h: str):
    return await _tx_logs(DEFAULT_CHAIN, h)


@app.get("/api/ethscope/tx/{h}/internal")
async def api_tx_internal(h: str):
    return await _tx_internal(DEFAULT_CHAIN, h)


@app.get("/api/ethscope/address/{h}")
async def api_address(h: str):
    return await _address(DEFAULT_CHAIN, h)


@app.get("/api/ethscope/address/{h}/txs")
async def api_address_txs(h: str, request: Request):
    return await _address_txs(DEFAULT_CHAIN, h, dict(request.query_params))


@app.get("/api/ethscope/address/{h}/token-transfers")
async def api_address_token_transfers(h: str, request: Request):
    return await _address_token_transfers(DEFAULT_CHAIN, h, dict(request.query_params))


@app.get("/api/ethscope/address/{h}/internal")
async def api_address_internal(h: str, request: Request):
    return await _address_internal(DEFAULT_CHAIN, h, dict(request.query_params))


@app.get("/api/ethscope/address/{h}/tokens")
async def api_address_tokens(h: str):
    return await _address_tokens(DEFAULT_CHAIN, h)


@app.get("/api/ethscope/address/{h}/txcount")
async def api_address_txcount(h: str):
    return await _txcount(DEFAULT_CHAIN, h)


@app.get("/api/ethscope/token/{h}")
async def api_token(h: str):
    return await _token(DEFAULT_CHAIN, h)


@app.get("/api/ethscope/token/{h}/transfers")
async def api_token_transfers(h: str, request: Request):
    return await _token_transfers(DEFAULT_CHAIN, h, dict(request.query_params))


@app.get("/api/ethscope/token/{h}/instances")
async def api_token_instances(h: str, request: Request):
    return await _token_instances(DEFAULT_CHAIN, h, dict(request.query_params))


@app.get("/api/ethscope/token/{h}/instance/{iid}")
async def api_token_instance(h: str, iid: str):
    return await _token_instance(DEFAULT_CHAIN, h, iid)


@app.get("/api/ethscope/token/{h}/instance/{iid}/transfers")
async def api_token_instance_transfers(h: str, iid: str, request: Request):
    return await _token_instance_transfers(DEFAULT_CHAIN, h, iid, dict(request.query_params))


@app.get("/api/ethscope/contract/{addr}")
async def api_contract(addr: str):
    return await _contract(DEFAULT_CHAIN, addr)


@app.post("/api/ethscope/contract/{addr}/call")
async def api_contract_call(addr: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON body required"}, status_code=400)
    return await _contract_call(DEFAULT_CHAIN, addr, body)


@app.post("/api/ethscope/contract/{addr}/verify")
async def api_contract_verify(addr: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON body required"}, status_code=400)
    return await _contract_verify(DEFAULT_CHAIN, addr, body)


@app.get("/api/ethscope/charts/transactions")
async def api_chart_transactions():
    return await _chart_transactions(DEFAULT_CHAIN)


@app.get("/api/ethscope/charts/market")
async def api_chart_market():
    return await _chart_market(DEFAULT_CHAIN)


@app.get("/api/ethscope/charts/gas")
async def api_chart_gas():
    return await _chart_gas(DEFAULT_CHAIN)


@app.get("/api/ethscope/charts/price")
async def api_chart_price(days: int = 30):
    if days < 1 or days > 365:
        return JSONResponse({"error": "days must be between 1 and 365"}, status_code=400)
    return await _chart_price(DEFAULT_CHAIN, days)


@app.get("/api/ethscope/gastracker")
async def api_gastracker():
    return await _gastracker(DEFAULT_CHAIN)


@app.get("/api/ethscope/pending")
async def api_pending():
    return await _pending(DEFAULT_CHAIN)


@app.get("/api/ethscope/search")
async def api_search(q: str = ""):
    return await _search(DEFAULT_CHAIN, q)


# ---------------------------------------------------------------- chain-prefixed routes
@app.get("/api/ethscope/{chain}/stats")
async def api_chain_stats(chain: str):
    err = _require_chain(chain)
    return err if err else await _stats(chain)


@app.get("/api/ethscope/{chain}/blocks")
async def api_chain_blocks(chain: str, request: Request):
    err = _require_chain(chain)
    return err if err else await _blocks(chain, dict(request.query_params))


@app.get("/api/ethscope/{chain}/txs")
async def api_chain_txs(chain: str, request: Request):
    err = _require_chain(chain)
    return err if err else await _txs_latest(chain, dict(request.query_params))


@app.get("/api/ethscope/{chain}/block/{n}")
async def api_chain_block(chain: str, n: str):
    err = _require_chain(chain)
    return err if err else await _block(chain, n)


@app.get("/api/ethscope/{chain}/block/{n}/txs")
async def api_chain_block_txs(chain: str, n: str, request: Request):
    err = _require_chain(chain)
    return err if err else await _block_txs(chain, n, dict(request.query_params))


@app.get("/api/ethscope/{chain}/tx/{h}")
async def api_chain_tx(chain: str, h: str):
    err = _require_chain(chain)
    return err if err else await _tx(chain, h)


@app.get("/api/ethscope/{chain}/tx/{h}/token-transfers")
async def api_chain_tx_token_transfers(chain: str, h: str):
    err = _require_chain(chain)
    return err if err else await _tx_token_transfers(chain, h)


@app.get("/api/ethscope/{chain}/tx/{h}/logs")
async def api_chain_tx_logs(chain: str, h: str):
    err = _require_chain(chain)
    return err if err else await _tx_logs(chain, h)


@app.get("/api/ethscope/{chain}/tx/{h}/internal")
async def api_chain_tx_internal(chain: str, h: str):
    err = _require_chain(chain)
    return err if err else await _tx_internal(chain, h)


@app.get("/api/ethscope/{chain}/address/{h}")
async def api_chain_address(chain: str, h: str):
    err = _require_chain(chain)
    return err if err else await _address(chain, h)


@app.get("/api/ethscope/{chain}/address/{h}/txs")
async def api_chain_address_txs(chain: str, h: str, request: Request):
    err = _require_chain(chain)
    return err if err else await _address_txs(chain, h, dict(request.query_params))


@app.get("/api/ethscope/{chain}/address/{h}/token-transfers")
async def api_chain_address_token_transfers(chain: str, h: str, request: Request):
    err = _require_chain(chain)
    return err if err else await _address_token_transfers(chain, h, dict(request.query_params))


@app.get("/api/ethscope/{chain}/address/{h}/internal")
async def api_chain_address_internal(chain: str, h: str, request: Request):
    err = _require_chain(chain)
    return err if err else await _address_internal(chain, h, dict(request.query_params))


@app.get("/api/ethscope/{chain}/address/{h}/tokens")
async def api_chain_address_tokens(chain: str, h: str):
    err = _require_chain(chain)
    return err if err else await _address_tokens(chain, h)


@app.get("/api/ethscope/{chain}/address/{h}/txcount")
async def api_chain_address_txcount(chain: str, h: str):
    err = _require_chain(chain)
    return err if err else await _txcount(chain, h)


@app.get("/api/ethscope/{chain}/token/{h}")
async def api_chain_token(chain: str, h: str):
    err = _require_chain(chain)
    return err if err else await _token(chain, h)


@app.get("/api/ethscope/{chain}/token/{h}/transfers")
async def api_chain_token_transfers(chain: str, h: str, request: Request):
    err = _require_chain(chain)
    return err if err else await _token_transfers(chain, h, dict(request.query_params))


@app.get("/api/ethscope/{chain}/token/{h}/instances")
async def api_chain_token_instances(chain: str, h: str, request: Request):
    err = _require_chain(chain)
    return err if err else await _token_instances(chain, h, dict(request.query_params))


@app.get("/api/ethscope/{chain}/token/{h}/instance/{iid}")
async def api_chain_token_instance(chain: str, h: str, iid: str):
    err = _require_chain(chain)
    return err if err else await _token_instance(chain, h, iid)


@app.get("/api/ethscope/{chain}/token/{h}/instance/{iid}/transfers")
async def api_chain_token_instance_transfers(chain: str, h: str, iid: str, request: Request):
    err = _require_chain(chain)
    return err if err else await _token_instance_transfers(chain, h, iid, dict(request.query_params))


@app.get("/api/ethscope/{chain}/contract/{addr}")
async def api_chain_contract(chain: str, addr: str):
    err = _require_chain(chain)
    return err if err else await _contract(chain, addr)


@app.post("/api/ethscope/{chain}/contract/{addr}/call")
async def api_chain_contract_call(chain: str, addr: str, request: Request):
    err = _require_chain(chain)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON body required"}, status_code=400)
    return await _contract_call(chain, addr, body)


@app.post("/api/ethscope/{chain}/contract/{addr}/verify")
async def api_chain_contract_verify(chain: str, addr: str, request: Request):
    err = _require_chain(chain)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON body required"}, status_code=400)
    return await _contract_verify(chain, addr, body)


@app.get("/api/ethscope/{chain}/charts/transactions")
async def api_chain_chart_transactions(chain: str):
    err = _require_chain(chain)
    return err if err else await _chart_transactions(chain)


@app.get("/api/ethscope/{chain}/charts/market")
async def api_chain_chart_market(chain: str):
    err = _require_chain(chain)
    return err if err else await _chart_market(chain)


@app.get("/api/ethscope/{chain}/charts/gas")
async def api_chain_chart_gas(chain: str):
    err = _require_chain(chain)
    return err if err else await _chart_gas(chain)


@app.get("/api/ethscope/{chain}/charts/price")
async def api_chain_chart_price(chain: str, days: int = 30):
    err = _require_chain(chain)
    if err:
        return err
    if days < 1 or days > 365:
        return JSONResponse({"error": "days must be between 1 and 365"}, status_code=400)
    return await _chart_price(chain, days)


@app.get("/api/ethscope/{chain}/gastracker")
async def api_chain_gastracker(chain: str):
    err = _require_chain(chain)
    return err if err else await _gastracker(chain)


@app.get("/api/ethscope/{chain}/pending")
async def api_chain_pending(chain: str):
    err = _require_chain(chain)
    return err if err else await _pending(chain)


@app.get("/api/ethscope/{chain}/search")
async def api_chain_search(chain: str, q: str = ""):
    err = _require_chain(chain)
    return err if err else await _search(chain, q)


# ---------------------------------------------------------------- static frontend (mounted last so /api/* routes win)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
