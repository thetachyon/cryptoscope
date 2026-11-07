"""Whale feed (phase 3, item 14) — keyless.

A polling feed endpoint for large stablecoin transfers:
  GET /api/overview/whale-feed   (all 8 stablecoin scopes, filterable)
  GET /api/<scope>/whale-feed    (one stablecoin scope)

Built wholesale on common/stablecoin.py — the feature-6 whale &
exchange exposure evaluation (`sc_whales`: holder concentration,
exchange attribution, and large recent transfers). This module only
merges the per-coin large-transfer lists, filters them, and
paginates. No large-transfer detection, holder/concentration logic,
or exchange attribution is re-implemented here.

One record type, newest first: a large_transfer row — one individual
primary-chain transfer whose USD value is >= that coin's threshold
(max($1,000,000, 0.01% of circulating supply), applied uniformly).
A transfer with the zero address as counterparty is labelled by
direction (mint: from the zero address; burn: to the zero address);
every other large transfer is direction "transfer". A large mint is
itself a whale event, so mint/burn transfers are NOT filtered out.

Filters: ?coin= (symbol or scope, comma-separated; default all 8
coins — overview feed only), ?min_usd= (minimum USD value per event;
the coin's own threshold still applies underneath — a min_usd below
a coin's threshold cannot surface smaller transfers, because the
scan never saw them; the effective threshold per coin is reported in
its coverage block), ?from= / ?to= (unix epoch seconds), ?days=
(last N days, 1-365, default 30). Pagination: ?page= (1-based,
default 1), ?page_size= (1-100, default 50).

Honest limits (stated in every response, never hidden):
- Large transfers are defined as >= max($1,000,000, 0.01% of
  circulating supply) per coin; each coin's threshold is reported in
  its coverage block. For the largest coins the 0.01% leg dominates
  (USDT ~$18M, USDC ~$7M), so their lists are short by design.
- The scan covers the recent-transfers feed only (up to 10 pages of
  50 transfers per coin; at most the latest 10 large transfers per
  coin are kept). For high-volume coins 500 transfers cover only a
  minute or two of activity, so the window is whatever the feed
  covers — the scanned count and the scan window (earliest/latest
  timestamp) are reported per coin. An empty list means "no transfer
  above the threshold in the scanned window", not "no large transfer
  happened".
- Transfers cover the primary chain only (Ethereum for 7 coins,
  Tron for USDD). FDUSD/TUSD hold most of their supply off the
  primary chain (BSC / Tron); large transfers on other chains are
  not shown.
- Concentration denominators are primary-chain supply only
  (Blockscout total_supply on Ethereum; DeFiLlama Tron chain balance
  for USDD) — shares are never presented as shares of all-chain
  supply. (The feed itself carries no concentration figures; this is
  the denominator rule of the /whales evaluation it is built on.)
- Exchange attribution is a lower bound from keyless source name
  tags only (Blockscout address tags / TronScan addressTag): an
  address counts as an exchange wallet only when the source's own
  tag names a known exchange. From/to labels on feed rows are those
  source-provided tags, shown verbatim; an untagged wallet stays
  unlabeled even if its size suggests an exchange.
- If a coin's large transfers cannot be observed keyless in a window
  (its transfer feed fails — status "unavailable" — or the scan
  stops early — status "partial"), its coverage block says so with
  the reason; the feed never returns a silent empty list for it.
"""
import asyncio
import time

from common.clients import UpstreamError, _cache_get, _cache_set
from common.stablecoin import (
    EVM_ZERO_ADDRESS,
    STABLECOINS,
    TRON_ZERO_ADDRESS,
    sc_whales,
)

CACHE_TTL = 300

# All 8 stablecoin scopes, in STABLECOINS order.
ALL_SCOPES = tuple(STABLECOINS.keys())

# Feed record directions (?kind= is not used; direction is the
# filterable dimension only via min_usd / coin / time — the row's
# `kind` field carries the direction so the table can badge it).
ALL_KINDS = ("transfer", "mint", "burn")

MAX_DAYS = 365

METHOD = (
    "Whale feed (phase 3, item 14). Large-transfer events across "
    "all stablecoin scopes as a feed, built on common/stablecoin.py "
    "wholesale: the feature-6 whale & exchange exposure evaluation "
    "(sc_whales — large recent transfers above max($1,000,000, "
    "0.01% of circulating supply), latest 10 per coin, found by "
    "paging the recent-transfers feed, up to 10 pages of 50). One "
    "record type, newest first: large_transfer rows (individual "
    "primary-chain transfers; zero-address counterparty labelled "
    "mint / burn, everything else transfer). No large-transfer "
    "detection, holder/concentration logic, or exchange attribution "
    "is re-implemented here."
)
LIMITS = [
    "Large transfers are defined as >= max($1,000,000, 0.01% of "
    "circulating supply) per coin; each coin's threshold is "
    "reported in its coverage block. For the largest coins the "
    "0.01% leg dominates (USDT ~$18M, USDC ~$7M), so their lists "
    "are short by design.",
    "The scan covers the recent-transfers feed only (up to 10 pages "
    "of 50 transfers per coin; at most the latest 10 large "
    "transfers per coin are kept). For high-volume coins 500 "
    "transfers cover only a minute or two of activity, so the "
    "window is whatever the feed covers — the scanned count and "
    "the scan window (earliest/latest timestamp) are reported per "
    "coin. An empty list means no transfer above the threshold in "
    "the scanned window, not that no large transfer happened.",
    "Transfers cover the primary chain only (Ethereum for 7 coins, "
    "Tron for USDD). FDUSD/TUSD hold most of their supply off the "
    "primary chain (BSC / Tron); large transfers on other chains "
    "are not shown.",
    "Concentration denominators are primary-chain supply only "
    "(Blockscout total_supply on Ethereum; DeFiLlama Tron chain "
    "balance for USDD) — shares are never presented as shares of "
    "all-chain supply. The feed itself carries no concentration "
    "figures; this is the denominator rule of the /whales "
    "evaluation it is built on.",
    "Exchange attribution is a lower bound from keyless source name "
    "tags only (Blockscout address tags / TronScan addressTag): an "
    "address counts as an exchange wallet only when the source's "
    "own tag names a known exchange. From/to labels on feed rows "
    "are those source-provided tags, shown verbatim; an untagged "
    "wallet stays unlabeled even if its size suggests an exchange.",
    "If a coin's large transfers cannot be observed keyless in a "
    "window (its transfer feed fails, or the scan stops early), "
    "its coverage block reports status unavailable / partial with "
    "the reason — the feed never returns a silent empty list for "
    "it.",
    "A ?min_usd below a coin's own threshold cannot surface "
    "smaller transfers: the scan only ever saw transfers above "
    "the threshold. The effective minimum per coin "
    "(max(min_usd, threshold_usd)) is reported in its coverage "
    "block.",
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


def _parse_float(raw, name, default, lo):
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise UpstreamError(f"{name} must be a number", status=400)
    if v < lo:
        raise UpstreamError(f"{name} must be >= {lo}", status=400)
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


def _direction(row):
    """mint / burn / transfer, from the zero-address counterparty.
    A large mint is itself a whale event, so zero-address transfers
    are labelled, not filtered out."""
    frm = (row.get("from") or "").lower()
    to = (row.get("to") or "").lower()
    if frm == EVM_ZERO_ADDRESS.lower() or frm == TRON_ZERO_ADDRESS.lower():
        return "mint"
    if to == EVM_ZERO_ADDRESS.lower() or to == TRON_ZERO_ADDRESS.lower():
        return "burn"
    return "transfer"


async def _coin_feed_bundle(scope):
    """Full (unpaginated) feed data for one coin, cached 300s.
    Returns (rows, coin_block). `rows` are the coin's large transfers
    from /whales (latest 10), newest first, enriched into feed rows.
    Raises UpstreamError when the coin's whales evaluation fails —
    the caller turns that into an error coverage block."""
    cache_key = f"whaleFeed:{scope}"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    cfg = STABLECOINS[scope]
    w = await sc_whales(scope)
    lt = w.get("large_transfers") or {}
    rows = []
    for e in lt.get("items") or []:
        direction = _direction(e)
        rows.append({
            "record_type": "large_transfer",
            "kind": direction,
            "direction": direction,
            "timestamp": e.get("timestamp"),
            "date": _iso(e.get("timestamp")),
            "coin": cfg["symbol"],
            "symbol": cfg["symbol"],
            "scope": scope,
            "issuer": cfg["issuer"],
            "chain": cfg["primary"],
            "amount": e.get("amount"),
            "amount_usd": e.get("amount_usd"),
            "from": e.get("from"),
            "from_label": e.get("from_label"),
            "to": e.get("to"),
            "to_label": e.get("to_label"),
            "tx_hash": e.get("tx_hash"),
            "block": e.get("block"),
            "threshold_usd": lt.get("threshold_usd"),
        })
    rows.sort(key=lambda r: -(r["timestamp"] or 0))
    block = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "supply_usd": w.get("supply_usd"),
        "price_usd": w.get("price_usd"),
        "price_assumed_1_usd": bool(w.get("price_assumed_1_usd")),
        "primary_chain_share_pct": w.get("primary_chain_share_pct"),
        "threshold_usd": lt.get("threshold_usd"),
        "threshold_rule": lt.get("threshold_rule"),
        "cap": lt.get("cap"),
        "events_count": lt.get("count"),
        "scanned_transfers": lt.get("scanned_transfers"),
        "scan_earliest": lt.get("scan_earliest"),
        "scan_latest": lt.get("scan_latest"),
        "status": lt.get("status"),
        "status_note": lt.get("status_note"),
        "chain_scope_note": lt.get("chain_scope_note"),
        "error": None,
    }
    out = (rows, block)
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out


def _page(rows, page, page_size):
    total = len(rows)
    total_pages = max(1, -(-total // page_size))
    if page > total_pages:
        return [], total_pages
    start = (page - 1) * page_size
    return rows[start:start + page_size], total_pages


async def whale_feed(params, scopes=None):
    """Aggregate feed: merge the coins' large-transfer lists, filter,
    paginate.

    params: coin, min_usd, from, to, days, page, page_size. `scopes`
    pins the coin set (used by the per-scope endpoint — a single
    scope). Raises UpstreamError(400) for invalid filters /
    pagination.
    """
    if scopes is None:
        scopes = _resolve_coins(params.get("coin"))
    min_usd = _parse_float(params.get("min_usd"), "min_usd", None, 0)
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
        coin_rows, block = res
        # Effective minimum for this coin: the scan never saw
        # transfers below its own threshold, so a min_usd below the
        # threshold cannot surface smaller transfers.
        threshold = block.get("threshold_usd")
        eff = threshold
        if min_usd is not None and threshold is not None:
            eff = max(min_usd, threshold)
        elif min_usd is not None:
            eff = min_usd
        block["min_usd_requested"] = min_usd
        block["effective_min_usd"] = eff
        coins_blocks.append(block)
        rows.extend(coin_rows)
    # Newest first; per-coin lists are already ordered; re-sort after
    # merge.
    rows.sort(key=lambda r: -(r["timestamp"] or 0))
    # Time window first: total_items is the row count inside the
    # requested window (before the min_usd filter), matching the
    # supply feed's total_items semantics. The min_usd filter then
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
    if min_usd is not None:
        filtered = [r for r in filtered
                    if r.get("amount_usd") is not None
                    and r["amount_usd"] >= min_usd]

    counts_by_coin = {}
    counts_by_kind = {}
    for r in filtered:
        counts_by_coin[r["coin"]] = counts_by_coin.get(r["coin"], 0) + 1
        counts_by_kind[r["kind"]] = counts_by_kind.get(r["kind"], 0) + 1
    page_items, total_pages = _page(filtered, page, page_size)

    order = {s: i for i, s in enumerate(ALL_SCOPES)}
    coins_blocks.sort(key=lambda b: order[b["scope"]])
    largest = max(
        (r for r in filtered if r.get("amount_usd") is not None),
        key=lambda r: r["amount_usd"], default=None)
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
            "total_volume_usd": round(sum(
                r.get("amount_usd") or 0 for r in filtered), 2),
            "largest_transfer": largest,
            "coins_included": sum(
                1 for b in coins_blocks if not b.get("error")),
            "note": (
                "Totals sum only the coins whose whales data loaded, "
                "and only transfers above each coin's threshold in "
                "the scanned window (latest 10 per coin). Not a "
                "total of all large transfers in the window."
            ),
        },
        "filters": {
            "coins": [STABLECOINS[s]["symbol"] for s in scopes],
            "min_usd": min_usd,
            "from": from_ts,
            "to": to_ts,
            "days": days,
        },
        "coins": coins_blocks,
        "method": METHOD,
        "limits": LIMITS,
        "as_of": now,
    }


async def whale_feed_scope(scope, params):
    """Per-scope feed: GET /api/<scope>/whale-feed. The ?coin=
    filter is ignored — the scope IS the coin."""
    return await whale_feed(params, scopes=[scope])
