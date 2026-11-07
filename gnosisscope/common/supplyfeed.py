"""Supply & flow feed (phase 3, item 8) — keyless.

A polling feed endpoint for stablecoin issuance:
  GET /api/overview/supply-feed   (all 8 stablecoin scopes, filterable)
  GET /api/<scope>/supply-feed    (one stablecoin scope)

Built wholesale on common/stablecoin.py — the feature-4 mint/burn
monitor (`sc_mint_burn`: 7/30/90d net-issuance windows + large
on-chain mint/burn events) and the feature-5 supply map
(`sc_supply_map`: per-chain supply + per-chain 7d/30d net change),
plus the daily supply series itself (`_coin_detail` /
`_supply_history` / `_daily_deltas`). This module only merges the
per-coin series + events, filters them, and paginates. No supply
series is re-derived and no mint/burn detection is re-implemented.

Two record types in one time-ordered feed (newest first):
  - daily_net_change: one row per coin per day — the day-over-day
    change in DeFiLlama's daily total circulating supply. This is the
    SERIES form of net issuance: a daily net figure, NOT a discrete
    mint/burn event, and it is labelled as such on every row.
  - on_chain_event: a large individual mint/burn — a Transfer with
    the zero address as counterparty on the coin's primary chain,
    above the feature-4 threshold (max($1,000,000, 0.1% of
    circulating supply), latest 20 per coin), reused verbatim from
    /mint-burn.

Filters: ?coin= (symbol or scope, comma-separated; default all 8
coins), ?kind= (net_change / mint / burn), ?from= / ?to= (unix epoch
seconds), ?days= (last N days, 1-365, default 30). Pagination:
?page= (1-based, default 1), ?page_size= (1-100, default 50).

Honest limits (stated in every response, never hidden):
- Net issuance is NET supply change from DeFiLlama, not gross
  mint/burn: a day's net mint is new issuance minus redemptions,
  and a small net figure can hide large offsetting gross flows.
  Daily rows are labelled "daily net change", never presented as
  discrete mint/burn events.
- On-chain events are primary-chain only (Ethereum for 7 coins,
  Tron for USDD). FDUSD/TUSD hold most of their supply off the
  primary chain (BSC / Tron); mint/burn events on other chains are
  not shown.
- USDT: Blockscout's zero-address tokentx query times out, so its
  on-chain events are reported unavailable. Tether's issue()/redeem()
  emit Issue/Redeem events, not zero-address Transfers, so a
  zero-address scan would undercount USDT mint/burn even if the
  query succeeded.
- TUSD: supply is frozen (no new mint/burn) — the flat daily series
  is shown instead of events.
- USDD: DeFiLlama's daily supply series has an 83-day gap
  (2026-07-07 -> 2026-09-28). Daily deltas spanning more than 2 days
  are excluded (they are multi-day jumps, not single-day mint/burn),
  so USDD's daily series has a hole over the gap and its 90d window
  is flagged partial (it covers 122 days). The gap is reported in
  the coin's coverage block.
- Per-chain flow figures (7d/30d net change per chain, from the
  supply map) combine bridges + local mint/burn — they are NOT a
  bridge-flow measurement. No keyless source reports per-coin bridge
  flows over a window.
"""
import asyncio
import time

from common.clients import UpstreamError, _cache_get, _cache_set
from common.stablecoin import (
    STABLECOINS,
    _coin_detail,
    _daily_deltas,
    _supply_history,
    sc_mint_burn,
    sc_supply_map,
)

CACHE_TTL = 300

# All 8 stablecoin scopes, in STABLECOINS order.
ALL_SCOPES = tuple(STABLECOINS.keys())

# Feed record kinds (?kind= filter). "net_change" is the daily
# series rows; "mint" / "burn" are the large on-chain events.
ALL_KINDS = ("net_change", "mint", "burn")

# Daily deltas are computed over the full history (up to ~11 years
# back), then filtered to the requested window — so ?from= can reach
# any date the DeFiLlama series covers. The ?days= filter itself is
# capped at 365, matching GET /api/<scope>/supply.
MAX_DAYS = 365
FULL_SERIES_DAYS = 4000

METHOD = (
    "Supply & flow feed (phase 3, item 8). Mint/burn events and net "
    "issuance as a feed, built on common/stablecoin.py wholesale: "
    "the feature-4 mint/burn monitor (sc_mint_burn — 7/30/90d net "
    "issuance windows from the DeFiLlama daily supply series, plus "
    "large on-chain mint/burn events on the primary chain) and the "
    "feature-5 supply map (sc_supply_map — per-chain supply and "
    "per-chain 7d/30d net change, bridges + local mint/burn "
    "combined). Two record types, newest first: daily_net_change "
    "rows (one per coin per day — the SERIES form of net issuance, "
    "labelled as such, never presented as discrete mint/burn "
    "events) and on_chain_event rows (large individual mints/burns "
    "above max($1M, 0.1% of circulating supply), latest 20 per "
    "coin). Net issuance is net supply change, NOT gross mint/burn."
)
LIMITS = [
    "Net issuance is the net change in DeFiLlama's daily total "
    "circulating supply — net supply change, NOT gross mint/burn. "
    "A day's net mint is new issuance minus redemptions; a small "
    "net figure can hide large offsetting gross flows. Daily rows "
    "are labelled 'daily net change', never presented as discrete "
    "mint/burn events.",
    "On-chain mint/burn events cover the primary chain only "
    "(Ethereum for 7 coins, Tron for USDD). FDUSD/TUSD hold most "
    "of their supply off the primary chain (BSC / Tron); mint/burn "
    "events on other chains are not shown.",
    "USDT on-chain events are unavailable: Blockscout's "
    "zero-address transfer query times out for USDT. Tether's "
    "issue()/redeem() emit Issue/Redeem events, not zero-address "
    "Transfers, so a zero-address scan would undercount USDT "
    "mint/burn even if the query succeeded.",
    "TUSD supply is frozen — no new mint/burn is expected, so an "
    "empty on-chain event list is consistent with the flat daily "
    "series shown instead.",
    "USDD's DeFiLlama daily supply series has an 83-day gap "
    "(2026-07-07 to 2026-09-28). Daily deltas spanning more than "
    "2 days are excluded, so USDD's daily series has a hole over "
    "the gap and its 90d net-issuance window is flagged partial "
    "(it covers 122 days, not 90). The gap is reported in USDD's "
    "coverage block.",
    "Per-chain flow figures (7d/30d net change per chain, from the "
    "supply map) combine bridges + local mint/burn — they are NOT "
    "a bridge-flow measurement. No keyless source reports per-coin "
    "bridge flows over a window (DeFiLlama bridges is paywalled; "
    "Wormholescan ignores the symbol filter).",
    "Net issuance is a PROXY for demand, not a direct measure of "
    "it. Redemptions, chain migrations (supply moving between "
    "chains) and issuer treasury moves also change total supply.",
]


def _parse_int(raw, name, default, lo, hi):
    if raw is None or raw == "":
        return default
    try:
        v = int(raw)
    except (TypeError, ValueError):
        raise UpstreamError(f"{name} must be an integer", status=400)
    if v < lo or v > hi:
        raise UpstreamError(
            f"{name} must be between {lo} and {hi}", status=400)
    return v


def _resolve_coins(raw):
    """?coin= value -> list of scopes. Accepts symbols (USDC) or
    scopes (usdcscope), comma-separated, case-insensitive. Default:
    all 8 stablecoin scopes. Raises UpstreamError(400) on unknown."""
    if not raw or not raw.strip():
        return list(ALL_SCOPES)
    by_symbol = {STABLECOINS[s]["symbol"].upper(): s for s in ALL_SCOPES}
    by_scope = {s.lower(): s for s in ALL_SCOPES}
    out = []
    for part in raw.split(","):
        p = part.strip()
        if not p:
            continue
        scope = by_symbol.get(p.upper()) or by_scope.get(p.lower())
        if scope is None:
            raise UpstreamError(
                f"Unknown coin '{p}' (coins: "
                + ", ".join(STABLECOINS[s]["symbol"] for s in ALL_SCOPES)
                + ")",
                status=400)
        if scope not in out:
            out.append(scope)
    if not out:
        return list(ALL_SCOPES)
    return out


def _iso(ts):
    if not ts:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _supply_gap(hist):
    """Largest gap in the daily supply series longer than 2 days, or
    None. USDD's DeFiLlama series has an 83-day gap (2026-07-07 ->
    2026-09-28); the daily deltas spanning it are excluded by
    _daily_deltas, so the feed's series has a matching hole."""
    worst = None
    for prev, cur in zip(hist, hist[1:]):
        gap = (cur["date"] - prev["date"]) // 86400
        if gap > 2 and (worst is None or gap > worst["gap_days"]):
            worst = {
                "start_date": prev["date"],
                "start": _iso(prev["date"]),
                "end_date": cur["date"],
                "end": _iso(cur["date"]),
                "gap_days": int(gap),
            }
    return worst


async def _coin_feed_bundle(scope):
    """Full (unpaginated) feed data for one coin, cached 300s.
    Returns (daily_rows, events, coin_block). `daily_rows` is the
    full daily net-change series (newest last); `events` are the
    coin's large on-chain mint/burn events from /mint-burn, newest
    first. Raises UpstreamError when the coin's mint/burn evaluation
    fails — the caller turns that into an error coverage block."""
    cache_key = f"supplyFeed:{scope}"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    cfg = STABLECOINS[scope]
    detail = await _coin_detail(cfg)
    hist = _supply_history(detail)
    deltas = _daily_deltas(hist, FULL_SERIES_DAYS)
    # Mint/burn (windows + large events) and supply map (per-chain
    # flow) run concurrently; both are cached 300s server-side.
    mb, smap = await asyncio.gather(
        sc_mint_burn(scope), sc_supply_map(scope), return_exceptions=True
    )
    if isinstance(mb, Exception):
        raise mb if isinstance(mb, UpstreamError) else UpstreamError(str(mb))
    if isinstance(smap, Exception):
        smap = None
    ni = mb.get("net_issuance") or {}
    le = mb.get("large_events") or {}
    ds = mb.get("demand_signal") or {}

    def _win(k):
        return (ni.get(k) or {})

    daily_rows = []
    supply_by_date = {h["date"]: h["supply_usd"] for h in hist}
    for d in deltas:
        supply = supply_by_date.get(d["date"])
        daily_rows.append({
            "record_type": "daily_net_change",
            "kind": "net_change",
            "timestamp": d["date"],
            "date": _iso(d["date"]),
            "coin": cfg["symbol"],
            "symbol": cfg["symbol"],
            "scope": scope,
            "issuer": cfg["issuer"],
            "delta_usd": d["delta_usd"],
            "supply_usd": supply,
            "delta_pct": (
                round(d["delta_usd"] / supply * 100.0, 4)
                if supply else None
            ),
            "label": (
                "Daily net supply change — net issuance, NOT a "
                "discrete mint/burn event."
            ),
        })
    events = []
    for e in le.get("events") or []:
        events.append({
            "record_type": "on_chain_event",
            "kind": e.get("direction"),
            "timestamp": e.get("timestamp"),
            "date": _iso(e.get("timestamp")),
            "coin": cfg["symbol"],
            "symbol": cfg["symbol"],
            "scope": scope,
            "issuer": cfg["issuer"],
            "chain": cfg["primary"],
            "direction": e.get("direction"),
            "amount": e.get("amount"),
            "amount_usd": e.get("amount_usd"),
            "tx_hash": e.get("tx_hash"),
            "block": e.get("block"),
            "from": e.get("from"),
            "to": e.get("to"),
        })
    chains = None
    chain_note = None
    if isinstance(smap, dict):
        chains = [
            {
                "chain": c["chain"],
                "supply_usd": c["supply_usd"],
                "share_pct": c["share_pct"],
                "change_7d": c.get("change_7d"),
                "change_30d": c.get("change_30d"),
            }
            for c in (smap.get("chains") or [])[:5]
        ]
        chain_note = (
            "Per-chain net change (7d/30d) combines bridges + local "
            "mint/burn — NOT a bridge-flow measurement. Covered "
            f"{smap.get('covered_share_pct')}% of total supply; "
            f"${smap.get('unknown_supply_usd')} unattributed."
            if smap.get("covered_share_pct") is not None else None
        )
    else:
        chain_note = (
            "Per-chain supply data unavailable for this coin right "
            "now (the supply-map evaluation failed); daily series "
            "and on-chain events above are unaffected."
        )
    coin_block = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "supply_usd": mb.get("supply_usd"),
        "price_usd": mb.get("price_usd"),
        "supply_history_first": hist[0]["date"] if hist else None,
        "supply_history_last": hist[-1]["date"] if hist else None,
        "supply_history_points": len(hist),
        "supply_gap": _supply_gap(hist),
        "flat_supply": bool(ni.get("flat_supply")),
        "net_7d_usd": _win("window_7d").get("net_usd"),
        "net_30d_usd": _win("window_30d").get("net_usd"),
        "net_90d_usd": _win("window_90d").get("net_usd"),
        "net_7d_partial": bool(_win("window_7d").get("partial")),
        "net_30d_partial": bool(_win("window_30d").get("partial")),
        "net_90d_partial": bool(_win("window_90d").get("partial")),
        "largest_mint_day": ni.get("largest_mint_day"),
        "largest_burn_day": ni.get("largest_burn_day"),
        "demand_direction": ds.get("direction"),
        "daily_rows": len(daily_rows),
        "on_chain_events": {
            "count": le.get("count"),
            "cap": le.get("cap"),
            "status": le.get("status"),
            "status_note": le.get("status_note"),
            "threshold_usd": mb.get("threshold_usd"),
            "primary_chain": le.get("primary_chain"),
            "primary_chain_share_pct": le.get("primary_chain_share_pct"),
            "scanned_zero_address_transfers": le.get(
                "scanned_zero_address_transfers"),
            "scan_earliest": le.get("scan_earliest"),
        },
        "chains": chains,
        "chain_note": chain_note,
        "error": None,
    }
    out = (daily_rows, events, coin_block)
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out


def _page(rows, page, page_size):
    total = len(rows)
    total_pages = max(1, -(-total // page_size))
    if page > total_pages:
        return [], total_pages
    start = (page - 1) * page_size
    return rows[start:start + page_size], total_pages


async def supply_feed(params, scopes=None):
    """Aggregate feed: merge the coins' daily net-change series +
    large on-chain mint/burn events, filter, paginate.

    params: coin, kind, from, to, days, page, page_size. `scopes`
    pins the coin set (used by the per-scope endpoint — a single
    scope). Raises UpstreamError(400) for invalid filters /
    pagination.
    """
    if scopes is None:
        scopes = _resolve_coins(params.get("coin"))
    kind = (params.get("kind") or "").strip().lower()
    if kind and kind not in ALL_KINDS:
        raise UpstreamError(
            f"Unknown kind '{kind}' (kinds: {', '.join(ALL_KINDS)})",
            status=400)
    now = int(time.time())
    days = _parse_int(params.get("days"), "days", None, 1, MAX_DAYS)
    from_ts = _parse_int(params.get("from"), "from", None, 0, now + 86400)
    to_ts = _parse_int(params.get("to"), "to", None, 0, now + 86400)
    if days is None and from_ts is None:
        days = 30
    if days is not None and from_ts is None:
        from_ts = now - days * 86400
    if from_ts is not None and to_ts is not None and from_ts > to_ts:
        raise UpstreamError("from must not be after to", status=400)
    page = _parse_int(params.get("page"), "page", 1, 1, 100000)
    page_size = _parse_int(params.get("page_size"), "page_size", 50, 1, 100)

    results = await asyncio.gather(
        *(_coin_feed_bundle(s) for s in scopes), return_exceptions=True
    )
    coins_blocks = []
    rows = []
    for scope, res in zip(scopes, results):
        if isinstance(res, Exception):
            cfg = STABLECOINS[scope]
            coins_blocks.append({
                "scope": scope,
                "symbol": cfg["symbol"],
                "name": cfg["name"],
                "issuer": cfg["issuer"],
                "error": str(res),
            })
            continue
        daily_rows, events, block = res
        coins_blocks.append(block)
        rows.extend(daily_rows)
        rows.extend(events)
    # Newest first; on-chain events sort before daily rows sharing a
    # timestamp (they are discrete events, the daily row is the day's
    # net). Per-coin lists are already ordered; re-sort after merge.
    rows.sort(key=lambda r: (
        -(r["timestamp"] or 0),
        0 if r["record_type"] == "on_chain_event" else 1,
    ))
    # Time window first: total_items is the row count inside the
    # requested window (before the kind filter), matching the
    # freeze feed's total_events semantics. The kind filter then
    # narrows to total_filtered.
    window_rows = rows
    if from_ts is not None:
        window_rows = [r for r in window_rows
                       if r["timestamp"] is not None and r["timestamp"] >= from_ts]
    if to_ts is not None:
        window_rows = [r for r in window_rows
                       if r["timestamp"] is not None and r["timestamp"] <= to_ts]
    total_rows = len(window_rows)

    filtered = window_rows
    if kind:
        filtered = [r for r in filtered if r["kind"] == kind]

    counts_by_coin = {}
    counts_by_kind = {}
    for r in filtered:
        counts_by_coin[r["coin"]] = counts_by_coin.get(r["coin"], 0) + 1
        counts_by_kind[r["kind"]] = counts_by_kind.get(r["kind"], 0) + 1
    page_items, total_pages = _page(filtered, page, page_size)

    def _total(field):
        vals = [b.get(field) for b in coins_blocks
                if b.get(field) is not None and not b.get("error")]
        return round(sum(vals), 2) if vals else None

    order = {s: i for i, s in enumerate(ALL_SCOPES)}
    coins_blocks.sort(key=lambda b: order[b["scope"]])
    return {
        "items": page_items,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "total_filtered": len(filtered),
        "total_items": total_rows,
        "counts_by_coin": counts_by_coin,
        "counts_by_kind": counts_by_kind,
        "totals": {
            "total_supply_usd": _total("supply_usd"),
            "total_net_7d_usd": _total("net_7d_usd"),
            "total_net_30d_usd": _total("net_30d_usd"),
            "total_net_90d_usd": _total("net_90d_usd"),
            "coins_included": sum(
                1 for b in coins_blocks if not b.get("error")),
            "large_events_in_window": (
                counts_by_kind.get("mint", 0)
                + counts_by_kind.get("burn", 0)
            ),
            "note": (
                "Totals sum only the coins whose mint/burn data "
                "loaded. Net issuance is net supply change, NOT "
                "gross mint/burn."
            ),
        },
        "filters": {
            "coins": [STABLECOINS[s]["symbol"] for s in scopes],
            "kind": kind or None,
            "from": from_ts,
            "to": to_ts,
            "days": days,
        },
        "coins": coins_blocks,
        "method": METHOD,
        "limits": LIMITS,
        "as_of": now,
    }


async def supply_feed_scope(scope, params):
    """Per-scope feed: GET /api/<scope>/supply-feed. The ?coin=
    filter is ignored — the scope IS the coin. All 8 coins have a
    DeFiLlama supply series, so there is no not-applicable case."""
    return await supply_feed(params, scopes=[scope])
