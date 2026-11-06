"""Upstream clients for the *Scope apps — cache, Blockscout v2, JSON-RPC, CoinGecko.

All keyless. `bs`/`rpc` raise UpstreamError (502) on transport or
upstream failures; `coingecko` returns None on any failure (callers
degrade gracefully). Hoist note: this module has no FastAPI dependency.
"""
import os
import time

import httpx

from common.chains import CHAINS


# The sandbox sets NO_PROXY with bracketed IPv6 literals ([::1], [fd8b:...])
# which httpx's env-proxy parser rejects (InvalidURL). Strip the bracketed
# forms; the bare forms (::1, fd8b:...) remain and match identically.
for _k in ("no_proxy", "NO_PROXY"):
    _v = os.environ.get(_k)
    if _v:
        os.environ[_k] = ",".join(p for p in _v.split(",") if "[" not in p)


TIMEOUT = httpx.Timeout(10.0)

HEADERS = {"User-Agent": "EthScope/1.0"}

CACHE_TTL = 60  # seconds, for hot endpoints

CHART_TTL = 600  # seconds, for chart / CoinGecko data

TOKEN_PRICE_TTL = 300  # seconds, for per-token CoinGecko prices


_cache: dict = {}



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



# ---------------------------------------------------------------- http
async def bs(chain, path, params=None, timeout=None):
    """GET Blockscout v2 for a chain. Returns parsed JSON, None on 404,
    raises UpstreamError otherwise. `timeout` overrides the default
    10s (token-balances for token-rich addresses is a multi-MB download
    and needs longer)."""
    base = CHAINS[chain]["blockscout"]
    t = TIMEOUT if timeout is None else httpx.Timeout(timeout)
    try:
        async with httpx.AsyncClient(timeout=t, headers=HEADERS, follow_redirects=True) as c:
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
