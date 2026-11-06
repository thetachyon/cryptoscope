"""Bitcoin explorer data for BitScope — blocks, transactions, addresses,
mempool. Keyless, mempool.space primary, Blockstream Esplora fallback.

Sources (all probed 2026-11-05 from this sandbox):
- mempool.space https://mempool.space/api — blocks, block/:hash
  (+ /txs/:start, /txids), block-height/:height, tx/:txid (+ /status),
  address/:addr (+ /txs[/:last_seen], /utxo), mempool, mempool/recent,
  v1/fees/recommended, v1/difficulty-adjustment, v1/mining/hashrate/3d,
  v1/block/:hash (extras: pool, totalFees, avgFee), v1/tx/:txid/rbf,
  v1/historical-price.
- Blockstream Esplora https://blockstream.info/api — same core paths
  (blocks, block, tx, address, mempool). No v1 fees / difficulty /
  hashrate / price endpoints there.
- BTC price: mempool.space historical-price (latest hourly entry) with
  CoinGecko fallback (keyless, cached).

Notes:
- mempool.space/Esplora tx objects carry `weight` but no `vsize`;
  vsize = ceil(weight / 4). Fee rate (sat/vB) = fee / vsize.
- The address UTXO endpoint returns plain text ("Too many unspent
  transaction outputs") for very large UTXO sets — surfaced as an
  honest note, never faked.
- Block/tx detail responses are immutable once confirmed, so they are
  cached 10 minutes; lists/stats are cached 15-60s.
"""
import asyncio
import hashlib
import time

import httpx

from common.clients import (
    HEADERS,
    UpstreamError,
    _cache_get,
    _cache_set,
    coingecko,
)

MEMPOOL_BASE = "https://mempool.space/api"
ESPLORA_BASE = "https://blockstream.info/api"

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"

SAT_PER_BTC = 100_000_000


# ---------------------------------------------------------------- http
async def _fetch(base, path, timeout=10.0):
    """GET base+path. Returns (status, parsed JSON or raw text).
    Transport failures raise UpstreamError."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(base + path)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        raise UpstreamError(f"Bitcoin API unreachable ({type(e).__name__})")
    ct = (r.headers.get("content-type") or "")
    if "application/json" in ct:
        try:
            return r.status_code, r.json()
        except Exception:
            raise UpstreamError("Bitcoin API returned invalid JSON")
    return r.status_code, r.text


async def _api(path, mempool_only=False, timeout=10.0, text_ok=False):
    """GET a path from mempool.space, falling back to Esplora for shared
    paths. Returns parsed JSON, or None on 404. Plain-text upstream
    errors (e.g. 'Invalid Bitcoin address') become a 400 UpstreamError.
    `text_ok` allows plain-text successes (tip height and block-height
    lookups return bare text, not JSON)."""
    last = None
    for base in ([MEMPOOL_BASE] if mempool_only else [MEMPOOL_BASE, ESPLORA_BASE]):
        try:
            status, data = await _fetch(base, path, timeout=timeout)
        except UpstreamError as e:
            last = e
            continue
        if status == 404:
            last = None
            continue
        if status >= 400:
            if isinstance(data, str) and data.strip():
                raise UpstreamError(data.strip()[:200], status=400)
            raise UpstreamError(f"Bitcoin API returned HTTP {status}")
        if isinstance(data, str):
            if text_ok:
                return data.strip()
            # Non-JSON success body (the UTXO-too-many error arrives as
            # HTTP 200 with text in some cases) — treat as an error the
            # caller can surface honestly.
            raise UpstreamError(data.strip()[:200], status=400)
        return data
    if last is not None:
        raise last
    return None


# ---------------------------------------------------------------- helpers
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


def _sats(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _btc(v):
    """Satoshis -> BTC float (8 dp, trimmed by the frontend)."""
    return _sats(v) / SAT_PER_BTC


def _vsize(tx):
    """mempool.space txs carry weight, not vsize: vsize = ceil(weight/4)."""
    w = tx.get("weight")
    if w is None:
        return tx.get("size")
    try:
        return (int(w) + 3) // 4
    except (TypeError, ValueError):
        return tx.get("size")


def _is_hex64(s):
    return (
        isinstance(s, str)
        and len(s) == 64
        and all(c in "0123456789abcdefABCDEF" for c in s)
    )


def _b58check_valid(addr):
    """Base58Check decode + checksum verify (legacy 1... / P2SH 3...)."""
    try:
        n = 0
        for ch in addr:
            n = n * 58 + _B58.index(ch)
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        # Leading '1's are leading zero bytes.
        pad = 0
        for ch in addr:
            if ch == "1":
                pad += 1
            else:
                break
        raw = b"\x00" * pad + raw
        if len(raw) < 5:
            return False
        payload, check = raw[:-4], raw[-4:]
        return hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4] == check
    except (ValueError, OverflowError):
        return False


def is_bitcoin_address(addr):
    """Shape-check a Bitcoin address: legacy/P2SH base58check, or bech32
    bc1q (P2WPKH, 42 chars) / bc1p (P2TR, 62 chars). Checksum validity is
    left to the upstream, which reports 'Invalid Bitcoin address'."""
    if not isinstance(addr, str) or not addr:
        return False
    if addr[0] in ("1", "3") and 26 <= len(addr) <= 35:
        return all(c in _B58 for c in addr) and _b58check_valid(addr)
    if addr.startswith("bc1"):
        body = addr[3:]
        if len(addr) == 42 and addr[3] == "q":
            return all(c in _B32 for c in body)
        if len(addr) == 62 and addr[3] == "p":
            return all(c in _B32 for c in body)
    return False


def _tx_short(t):
    """mempool.space/Esplora tx -> compact row (lists + block pages)."""
    st = t.get("status") or {}
    vs = _vsize(t)
    fee = t.get("fee")
    return {
        "txid": t.get("txid"),
        "fee": fee,
        "fee_rate_sat_vb": (round(fee / vs, 2) if fee is not None and vs else None),
        "vsize": vs,
        "size": t.get("size"),
        "weight": t.get("weight"),
        "confirmed": bool(st.get("confirmed")),
        "block_height": st.get("block_height"),
        "block_hash": st.get("block_hash"),
        "block_time": st.get("block_time"),
        "age": _age_unix(st.get("block_time")),
        "vin_count": len(t.get("vin") or []),
        "vout_count": len(t.get("vout") or []),
        "is_coinbase": bool((t.get("vin") or [{}])[0].get("is_coinbase")),
    }


def _tx_detail(t):
    """Full transaction: inputs with prevout addresses/values, outputs,
    totals, fee rate, RBF handled by the caller for unconfirmed txs."""
    st = t.get("status") or {}
    vs = _vsize(t)
    fee = t.get("fee")
    vin = []
    total_in = 0
    for i in t.get("vin") or []:
        prev = i.get("prevout") or {}
        val = _sats(prev.get("value"))
        total_in += val
        vin.append(
            {
                "txid": i.get("txid"),
                "vout": i.get("vout"),
                "is_coinbase": bool(i.get("is_coinbase")),
                "address": prev.get("scriptpubkey_address"),
                "value_sats": val if prev else None,
                "value_btc": _btc(val) if prev else None,
                "script_type": prev.get("scriptpubkey_type"),
                "sequence": i.get("sequence"),
            }
        )
    vout = []
    total_out = 0
    for o in t.get("vout") or []:
        val = _sats(o.get("value"))
        total_out += val
        vout.append(
            {
                "address": o.get("scriptpubkey_address"),
                "value_sats": val,
                "value_btc": _btc(val),
                "script_type": o.get("scriptpubkey_type"),
                "scriptpubkey_asm": o.get("scriptpubkey_asm"),
            }
        )
    is_cb = bool((t.get("vin") or [{}])[0].get("is_coinbase"))
    return {
        "txid": t.get("txid"),
        "version": t.get("version"),
        "locktime": t.get("locktime"),
        "fee": fee,
        "fee_rate_sat_vb": (round(fee / vs, 2) if fee is not None and vs else None),
        "vsize": vs,
        "size": t.get("size"),
        "weight": t.get("weight"),
        "sigops": t.get("sigops"),
        "is_coinbase": is_cb,
        "confirmed": bool(st.get("confirmed")),
        "block_height": st.get("block_height"),
        "block_hash": st.get("block_hash"),
        "block_time": st.get("block_time"),
        "age": _age_unix(st.get("block_time")),
        "inputs": vin,
        "outputs": vout,
        "total_in_sats": None if is_cb else total_in,
        "total_in_btc": None if is_cb else _btc(total_in),
        "total_out_sats": total_out,
        "total_out_btc": _btc(total_out),
    }


def _block_short(b):
    return {
        "height": b.get("height"),
        "id": b.get("id"),
        "hash": b.get("id"),
        "timestamp": b.get("timestamp"),
        "age": _age_unix(b.get("timestamp")),
        "tx_count": b.get("tx_count"),
        "size": b.get("size"),
        "weight": b.get("weight"),
        "difficulty": b.get("difficulty"),
        "nonce": b.get("nonce"),
        "bits": b.get("bits"),
        "version": b.get("version"),
        "merkle_root": b.get("merkle_root"),
        "previousblockhash": b.get("previousblockhash"),
        "mediantime": b.get("mediantime"),
    }


# ---------------------------------------------------------------- price
async def btc_price():
    """(price_usd, change_24h_pct). mempool.space hourly history first
    (latest entry + the entry ~24h back), CoinGecko fallback."""
    key = "btcPrice"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    out = (None, None)
    try:
        d = await _api("/v1/historical-price?currency=USD", mempool_only=True, timeout=10.0)
        prices = (d or {}).get("prices") or []
        if prices:
            latest = prices[0].get("USD")
            # Entries are hourly, newest first; find the first >= 24h old.
            now = prices[0].get("time") or 0
            old = None
            for p in prices:
                if now - (p.get("time") or 0) >= 24 * 3600:
                    old = p.get("USD")
                    break
            chg = None
            if latest is not None and old:
                chg = round((latest - old) / old * 100.0, 2)
            out = (float(latest) if latest is not None else None, chg)
    except UpstreamError:
        out = (None, None)
    if out[0] is None:
        d = await coingecko("/simple/price", {"ids": "bitcoin", "vs_currencies": "usd", "include_24hr_change": "true"})
        b = (d or {}).get("bitcoin") or {}
        out = (b.get("usd"), b.get("usd_24h_change"))
    _cache_set(key, out, ttl=300)
    return out


# ---------------------------------------------------------------- stats
async def btc_stats():
    key = "btcStats"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    tip, mp, fees, diff, hr, (price, chg) = await asyncio.gather(
        _api("/blocks/tip/height", text_ok=True),
        _api("/mempool"),
        _api("/v1/fees/recommended", mempool_only=True),
        _api("/v1/difficulty-adjustment", mempool_only=True),
        _api("/v1/mining/hashrate/3d", mempool_only=True),
        btc_price(),
        return_exceptions=True,
    )
    if isinstance(tip, Exception):
        raise tip if isinstance(tip, UpstreamError) else UpstreamError(str(tip))
    mp = mp if isinstance(mp, dict) else {}
    fees = fees if isinstance(fees, dict) else {}
    diff = diff if isinstance(diff, dict) else {}
    hr = hr if isinstance(hr, dict) else {}
    out = {
        "height": int(tip) if str(tip).isdigit() else tip,
        "difficulty": hr.get("currentDifficulty") or diff.get("currentDifficulty"),
        "hashrate_hs": hr.get("currentHashrate"),
        "mempool_count": mp.get("count"),
        "mempool_vsize": mp.get("vsize"),
        "mempool_total_fee_sats": mp.get("total_fee"),
        "fees": {
            "fastest": fees.get("fastestFee"),
            "half_hour": fees.get("halfHourFee"),
            "hour": fees.get("hourFee"),
            "economy": fees.get("economyFee"),
            "minimum": fees.get("minimumFee"),
        },
        "difficulty_adjustment": {
            "progress_pct": diff.get("progressPercent"),
            "difficulty_change_pct": diff.get("difficultyChange"),
            "remaining_blocks": diff.get("remainingBlocks"),
            "next_retarget_height": diff.get("nextRetargetHeight"),
            "estimated_retarget_date": diff.get("estimatedRetargetDate"),
            "time_avg_secs": diff.get("timeAvg"),
        },
        "btc_price_usd": price,
        "btc_change_24h_pct": chg,
    }
    _cache_set(key, out)
    return out


# ---------------------------------------------------------------- blocks
async def btc_blocks(before=None):
    """Latest blocks, newest first. `before` is a start height for
    Load-more (mempool.space /blocks/:height returns 15 blocks at or
    below that height)."""
    key = f"btcBlocks:{before}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    path = f"/blocks/{int(before)}" if before is not None else "/blocks"
    d = await _api(path)
    if not isinstance(d, list):
        raise UpstreamError("Bitcoin API returned unexpected blocks data")
    items = [_block_short(b) for b in d]
    out = {
        "items": items,
        "next_height": (items[-1]["height"] - 1) if items else None,
    }
    _cache_set(key, out, ttl=30)
    return out


async def _resolve_block_hash(n):
    """Block height, hash, or 'latest' -> block hash."""
    if str(n).lower() == "latest":
        h = await _api("/blocks/tip/height", text_ok=True)
        n = h
    if isinstance(n, str) and n.isdigit():
        h = await _api(f"/block-height/{int(n)}", text_ok=True)
        if not isinstance(h, str):
            raise UpstreamError("Block not found", status=404)
        return h
    if _is_hex64(n):
        return n.lower()
    raise UpstreamError("Invalid block (expected height, hash, or 'latest')", status=400)


async def btc_block(n):
    """Block detail: fields + pool/miner + fees (v1 extras, best effort)."""
    h = await _resolve_block_hash(n)
    key = f"btcBlock:{h}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    b = await _api(f"/block/{h}")
    if not isinstance(b, dict):
        raise UpstreamError("Block not found", status=404)
    out = _block_short(b)
    # v1 extras: pool name, total fees, averages (mempool.space only).
    try:
        ex = await _api(f"/v1/block/{h}", mempool_only=True, timeout=10.0)
        e = (ex or {}).get("extras") or {}
        pool = e.get("pool") or {}
        out["pool"] = pool.get("name") if pool else None
        out["pool_slug"] = pool.get("slug") if pool else None
        out["total_fees_sats"] = e.get("totalFees")
        out["avg_fee_sats"] = e.get("avgFee")
        out["avg_fee_rate_sat_vb"] = e.get("avgFeeRate")
        out["reward_sats"] = e.get("reward")
        out["coinbase_addresses"] = e.get("coinbaseAddresses") or []
        out["segwit_tx_count"] = e.get("segwitTotalTxs")
        out["total_inputs"] = e.get("totalInputs")
        out["total_outputs"] = e.get("totalOutputs")
        out["total_output_btc"] = (_btc(e["totalOutputAmt"]) if e.get("totalOutputAmt") is not None else None)
    except UpstreamError:
        pass
    _cache_set(key, out, ttl=600)
    return out


async def btc_block_txs(n, start=0):
    """Transactions in a block, 25 per page (mempool.space /txs/:start)."""
    h = await _resolve_block_hash(n)
    try:
        start = max(0, int(start or 0))
    except (TypeError, ValueError):
        start = 0
    d = await _api(f"/block/{h}/txs/{start}")
    if not isinstance(d, list):
        raise UpstreamError("Block not found", status=404)
    return {
        "block_hash": h,
        "start": start,
        "items": [_tx_short(t) for t in d],
        "next_start": (start + len(d)) if len(d) == 25 else None,
    }


# ---------------------------------------------------------------- transactions
async def btc_tx(txid):
    if not _is_hex64(txid or ""):
        raise UpstreamError("Invalid transaction id", status=400)
    txid = txid.lower()
    key = f"btcTx:{txid}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    t = await _api(f"/tx/{txid}")
    if not isinstance(t, dict):
        raise UpstreamError("Transaction not found", status=404)
    out = _tx_detail(t)
    st = t.get("status") or {}
    tip = None
    if st.get("confirmed") and st.get("block_height") is not None:
        try:
            tip = await _api("/blocks/tip/height", text_ok=True)
            out["confirmations"] = int(tip) - int(st["block_height"]) + 1
        except UpstreamError:
            out["confirmations"] = None
    else:
        out["confirmations"] = 0
        # Unconfirmed: check RBF status (mempool.space only).
        try:
            rbf = await _api(f"/v1/tx/{txid}/rbf", mempool_only=True, timeout=10.0)
            out["rbf_replaces"] = (rbf or {}).get("replaces")
            out["rbf_replacements"] = (rbf or {}).get("replacements")
        except UpstreamError:
            out["rbf_replaces"] = None
            out["rbf_replacements"] = None
    _cache_set(key, out, ttl=600 if out["confirmed"] else 30)
    return out


# ---------------------------------------------------------------- addresses
async def btc_address(addr):
    if not is_bitcoin_address(addr or ""):
        raise UpstreamError("Invalid Bitcoin address", status=400)
    key = f"btcAddr:{addr}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await _api(f"/address/{addr}")
    if not isinstance(d, dict):
        raise UpstreamError("Address not found", status=404)
    chain = d.get("chain_stats") or {}
    memp = d.get("mempool_stats") or {}
    funded = _sats(chain.get("funded_txo_sum"))
    spent = _sats(chain.get("spent_txo_sum"))
    mfunded = _sats(memp.get("funded_txo_sum"))
    mspent = _sats(memp.get("spent_txo_sum"))
    out = {
        "address": addr,
        "balance_sats": funded - spent,
        "balance_btc": _btc(funded - spent),
        "funded_sats": funded,
        "spent_sats": spent,
        "tx_count": chain.get("tx_count"),
        "unconfirmed_balance_sats": mfunded - mspent,
        "unconfirmed_tx_count": memp.get("tx_count"),
    }
    _cache_set(key, out)
    return out


async def btc_address_txs(addr, after=None):
    """Address transactions, newest first (50/page). `after` is the last
    seen txid cursor. The paged route is /txs/chain/:last_seen_txid
    (confirmed txs) — the plain /txs/:txid form does NOT exist on
    mempool.space or Esplora (both 404 "endpoint does not exist"),
    which is why address-page load-more and any multi-page scan
    failed before this fix (found 2026-11-06 while building the
    pre-flight check)."""
    if not is_bitcoin_address(addr or ""):
        raise UpstreamError("Invalid Bitcoin address", status=400)
    path = f"/address/{addr}/txs"
    if after:
        path += f"/chain/{after}"
    d = await _api(path)
    if not isinstance(d, list):
        raise UpstreamError("Address not found", status=404)
    return {
        "items": [_tx_short(t) for t in d],
        "next_after": (d[-1].get("txid") if d else None),
    }


async def btc_counterparties(addr):
    """Counterparties from an address's most recent transactions.

    One mempool.space page (25 txs): inputs' prevout addresses plus
    outputs' addresses, excluding the address itself. Used by the
    risk funder walk (common/risk.py) — the address page loads the
    same 25 transactions.
    """
    if not is_bitcoin_address(addr or ""):
        raise UpstreamError("Invalid Bitcoin address", status=400)
    d = await _api(f"/address/{addr}/txs")
    if not isinstance(d, list):
        raise UpstreamError("Address not found", status=404)
    cps = set()
    for t in d:
        for vin in t.get("vin") or []:
            a = (vin.get("prevout") or {}).get("scriptpubkey_address")
            if a and a != addr:
                cps.add(a)
        for vout in t.get("vout") or []:
            a = vout.get("scriptpubkey_address")
            if a and a != addr:
                cps.add(a)
    return {"counterparties": sorted(cps), "txs_scanned": len(d)}


async def btc_address_utxo(addr):
    """UTXOs for an address. mempool.space refuses very large sets with a
    plain-text error — surfaced as an honest note."""
    if not is_bitcoin_address(addr or ""):
        raise UpstreamError("Invalid Bitcoin address", status=400)
    try:
        d = await _api(f"/address/{addr}/utxo", timeout=15.0)
    except UpstreamError as e:
        if "Too many" in e.msg:
            return {"items": [], "total": None, "note": e.msg}
        raise
    if not isinstance(d, list):
        raise UpstreamError("Address not found", status=404)
    items = [
        {
            "txid": u.get("txid"),
            "vout": u.get("vout"),
            "value_sats": _sats(u.get("value")),
            "value_btc": _btc(u.get("value")),
            "status": u.get("status") or {},
        }
        for u in d[:100]
    ]
    return {
        "items": items,
        "total": len(d),
        "truncated": len(d) > 100,
        "note": None,
    }


# ---------------------------------------------------------------- mempool
def _fee_bands(histogram):
    """fee_histogram [[feeRate, vsize], ...] (descending) -> coarse bands
    for the mempool page: count + vsize per sat/vB range."""
    bands = [
        ("0–1", 0, 1),
        ("1–2", 1, 2),
        ("2–5", 2, 5),
        ("5–10", 5, 10),
        ("10–25", 10, 25),
        ("25–50", 25, 50),
        ("50+", 50, float("inf")),
    ]
    out = [{"band": name, "count": 0, "vsize": 0} for name, _, _ in bands]
    for rate, vs in (histogram or []):
        try:
            r, v = float(rate), int(vs)
        except (TypeError, ValueError):
            continue
        for i, (_, lo, hi) in enumerate(bands):
            if lo <= r < hi:
                out[i]["count"] += 1
                out[i]["vsize"] += v
                break
    return out


async def btc_mempool():
    key = "btcMempool"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    mp, fees, recent = await asyncio.gather(
        _api("/mempool"),
        _api("/v1/fees/recommended", mempool_only=True),
        _api("/mempool/recent", mempool_only=True),
        return_exceptions=True,
    )
    if isinstance(mp, Exception):
        raise mp if isinstance(mp, UpstreamError) else UpstreamError(str(mp))
    mp = mp if isinstance(mp, dict) else {}
    fees = fees if isinstance(fees, dict) else {}
    recent = recent if isinstance(recent, list) else []
    out = {
        "count": mp.get("count"),
        "vsize": mp.get("vsize"),
        "total_fee_sats": mp.get("total_fee"),
        "fee_histogram_bands": _fee_bands(mp.get("fee_histogram")),
        "fees": {
            "fastest": fees.get("fastestFee"),
            "half_hour": fees.get("halfHourFee"),
            "hour": fees.get("hourFee"),
            "economy": fees.get("economyFee"),
            "minimum": fees.get("minimumFee"),
        },
        "recent": [
            {
                "txid": t.get("txid"),
                "fee": t.get("fee"),
                "fee_rate_sat_vb": (round(t["fee"] / t["vsize"], 2) if t.get("fee") is not None and t.get("vsize") else None),
                "vsize": t.get("vsize"),
                "value_sats": t.get("value"),
            }
            for t in recent[:25]
        ],
    }
    _cache_set(key, out, ttl=15)
    return out


# ---------------------------------------------------------------- search
async def btc_search(q):
    q = (q or "").strip()
    if not q:
        raise UpstreamError("Empty search", status=400)
    if q.isdigit():
        return {"type": "block", "height": int(q)}
    if _is_hex64(q):
        # Could be a txid or a block hash: tx first, then block.
        t = await _api(f"/tx/{q.lower()}")
        if isinstance(t, dict):
            return {"type": "transaction", "txid": q.lower()}
        b = await _api(f"/block/{q.lower()}")
        if isinstance(b, dict):
            return {"type": "block", "hash": q.lower()}
        raise UpstreamError("No transaction or block with that hash", status=404)
    if is_bitcoin_address(q):
        return {"type": "address", "address": q}
    raise UpstreamError(
        "No match: expected a block height, a transaction id, a block hash, or a Bitcoin address",
        status=404,
    )


# ---------------------------------------------------------------- docs
BTC_DOCS = [
    {"method": "GET", "suffix": "/stats", "description": "Bitcoin network stats: latest block height, difficulty, hashrate estimate, mempool size, recommended fees, BTC price.", "params": [], "example": "/api/bitscope/stats"},
    {"method": "GET", "suffix": "/blocks", "description": "Latest blocks, newest first (10 per page; ?before=<height> for older pages).", "params": ["before"], "example": "/api/bitscope/blocks"},
    {"method": "GET", "suffix": "/block/{height|hash}", "description": "Block detail: height, hash, timestamp, tx count, size/weight, difficulty, miner (pool), fees collected. 'latest' also works.", "params": [], "example": "/api/bitscope/block/latest"},
    {"method": "GET", "suffix": "/block/{height|hash}/txs", "description": "Transactions in a block, 25 per page (?start=<index>).", "params": ["start"], "example": "/api/bitscope/block/latest/txs"},
    {"method": "GET", "suffix": "/tx/{txid}", "description": "Transaction detail: fee + fee rate (sat/vB), inputs with prevout addresses/values, outputs, totals, confirmations, RBF status when unconfirmed.", "params": [], "example": "/api/bitscope/tx/642ea37c2570cdfba163cce0cce2e98f24022ebc7451e4c31c3145dd0f062e79"},
    {"method": "GET", "suffix": "/tx/{txid}/explain", "description": "Plain-English explanation of a transaction: one sentence ('What happened') plus a per-output flow summary (address + amount). Coinbase transactions are reported as miner rewards. Change outputs cannot be identified from chain data alone, so every output is listed.", "params": [], "example": "/api/bitscope/tx/642ea37c2570cdfba163cce0cce2e98f24022ebc7451e4c31c3145dd0f062e79/explain"},
    {"method": "GET", "suffix": "/address/{addr}", "description": "Address overview: confirmed + unconfirmed balance, tx count.", "params": [], "example": "/api/bitscope/address/1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"},
    {"method": "GET", "suffix": "/address/{addr}/txs", "description": "Address transactions, newest first (50/page; ?after=<last txid> for older pages).", "params": ["after"], "example": "/api/bitscope/address/1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa/txs"},
    {"method": "GET", "suffix": "/address/{addr}/utxo", "description": "UTXOs for an address (first 100 shown). Very large UTXO sets are refused by the upstream and reported as a note.", "params": [], "example": "/api/bitscope/address/1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa/utxo"},
    {"method": "GET", "suffix": "/address/{addr}/risk", "description": "Address risk score 0-100 (low / elevated / high / critical) with human-readable reasons: direct OFAC SDN match (100/critical), N-hop proximity to a sanctioned address via a bounded funder walk over the address's 25 most recent transactions (3 hops max), and sanctioned-mixer interaction. Keyless; label set vendored from the OFAC SDN list (2026-11-05, 514 addresses, 260 Bitcoin). Heuristic screening signal, not a legal determination.", "params": [], "example": "/api/bitscope/address/1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa/risk"},
    {"method": "GET", "suffix": "/address/{addr}/crosschain", "description": "Cross-chain identity: Wormhole bridge transfers involving the address (Wormholescan keyless API; Wormhole only — Wormhole does not bridge native Bitcoin, so transfers are expected to be absent unless the address appears as a bridge destination). EVM presence is not applicable — a Bitcoin address is a different format, and linkage to an EVM/Solana address is shown ONLY when an actual bridge transfer is observed; there is no keyless way to prove two different-format addresses belong to the same person.", "params": [], "example": "/api/bitscope/address/1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa/crosschain"},
    {"method": "GET", "suffix": "/mempool", "description": "Mempool: unconfirmed tx count + size, fee-rate bands, recent unconfirmed transactions, recommended fee rates.", "params": [], "example": "/api/bitscope/mempool"},
    {"method": "GET", "suffix": "/search", "description": "Universal search: block height, transaction id, block hash, or Bitcoin address (legacy/bech32/taproot).", "params": ["q"], "example": "/api/bitscope/search?q=1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"},
]


def btc_docs_payload(api_prefix):
    return {
        "chains": ["bitcoin"],
        "endpoints": [
            {
                "method": e["method"],
                "path": api_prefix + e["suffix"],
                "description": e["description"],
                "params": e["params"],
                "example": e["example"].replace("/api/bitscope", api_prefix),
            }
            for e in BTC_DOCS
        ]
        + [
            {"method": "GET", "path": api_prefix + "/chains", "description": "List the chains this app serves.", "params": [], "example": api_prefix + "/chains"},
            {"method": "GET", "path": api_prefix + "/docs", "description": "This endpoint list.", "params": [], "example": api_prefix + "/docs"},
        ],
    }
