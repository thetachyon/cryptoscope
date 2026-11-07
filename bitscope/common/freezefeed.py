"""Freeze event feed (phase 3, item 4) — keyless.

A polling feed endpoint for freeze/blacklist events per issuer:
  GET /api/walletscope/freeze/feed   (all freezable coins, filterable)
  GET /api/<scope>/freeze/feed      (one stablecoin scope)

Built wholesale on common/freeze.py (feature 10) — its capability
probe, event fetching (`_scan_window` / `_scan_freeze_events` /
`_parse_log`) and frozen-supply logic are reused, never
re-implemented. This module only merges the per-coin event lists,
filters them, and paginates.

Filters: ?coin= (symbol or scope, comma-separated; default all
freezable coins), ?kind= (event type), ?address= (0x holder),
?from= / ?to= (unix epoch seconds), ?days= (last N days, 1-1825).
Pagination: ?page= (1-based, default 1), ?page_size= (1-100,
default 50).

Coins covered (all 8 stablecoin scopes):
  - Freeze functions exist: USDC, USDT, PYUSD, FDUSD, TUSD
    (Ethereum primary chain; events from Blockscout getLogs).
  - No freeze function: DAI, USDe — their token ABIs have no
    freeze/blacklist/pause function or event, so there is nothing to
    enumerate and no feed to return. They are reported as
    not_applicable with the ABI evidence, never as an empty feed.
  - USDD (Tron): no freeze/blacklist function appears in TronScan's
    published method map (11 observed functions, partial list), so
    there are no freeze events to scan — also not_applicable, with
    the evidence stated.

Honest limits (stated in every response, never hidden):
- Frozen supply / currently-frozen state is a LOWER-BOUND
  ESTIMATE: the issuer's blacklist is NOT enumerable on-chain, so
  the currently-frozen set is rebuilt from the freeze events inside
  the scanned window only (365 days, extended to 5 years ONLY for a
  coin whose freeze signatures exist but produced zero holder-state
  events in 365d — TUSD). Addresses frozen BEFORE the window are
  missed.
- Event windows are limited by log-scan depth: Blockscout getLogs
  is paged (offset=1000, up to 3 pages per event signature), so a
  very chatty signature could in principle be truncated — the
  per-coin scan_status says ok / partial / unavailable when that
  happens.
- Primary chain only (Ethereum for the 5 freezable coins).
  FDUSD/TUSD hold most of their supply off the primary chain
  (BSC / Tron); freezes on other chains are not covered.
- Destroyed black funds (USDT/TUSD `DestroyedBlackFunds`) are funds
  the issuer BURNED out of a blacklisted address — seized, not
  merely frozen. They appear as events (kind `destroyed`) and are
  never counted as frozen.
- Pause/unpause events are global (they halt ALL transfers, not
  one holder) and carry no address.
- Amounts in USD use the live price at feed time (DeFiLlama, same
  as /freeze); for destroyed funds that is the value destroyed, not
  a current balance.
"""
import re
import time

from common.clients import UpstreamError, _cache_get, _cache_set
from common.freeze import (
    FREEZE_EVENT_CONFIG,
    _scan_window,
    ABI_NOTES,
)
from common.stablecoin import (
    STABLECOINS,
    _amount,
    _cfg,
    _coin_detail,
    _current_price,
)

CACHE_TTL = 300

# Scopes with freeze/blacklist event signatures (feature 10).
FREEZABLE_SCOPES = tuple(
    s for s, specs in FREEZE_EVENT_CONFIG.items() if specs
)
# All 8 stablecoin scopes, in STABLECOINS order.
ALL_SCOPES = tuple(STABLECOINS.keys())

# Event kinds, unified across issuers (feature 10's `kind` values).
ALL_KINDS = (
    "blacklisted", "unblacklisted", "destroyed", "paused", "unpaused",
    "frozen", "unfrozen", "wiped", "blacklist_set",
)
HOLDER_STATE_KINDS = (
    "blacklisted", "unblacklisted", "frozen", "unfrozen", "blacklist_set",
)

METHOD = (
    "Freeze event feed (phase 3, item 4). Freeze/blacklist/pause "
    "events per issuer, merged across the freezable stablecoins "
    "(USDC, USDT, PYUSD, FDUSD, TUSD — Ethereum primary chain) and "
    "paginated. Built on common/freeze.py (feature 10) wholesale: "
    "the same Blockscout getLogs scan over the same window (365 "
    "days; 5-year lookback only for a coin with freeze signatures "
    "but zero holder-state events in 365d — TUSD), the same "
    "currently-frozen set rebuilt by applying holder-state events "
    "in order (latest event per address wins), and the same "
    "capability evidence. DAI/USDe have no freeze function in their "
    "token ABI and USDD has none observed on Tron, so those coins "
    "are reported not_applicable with the evidence, never as empty "
    "feeds. A feed event is a screening signal, not a legal "
    "determination; it does not say why the issuer froze the "
    "address."
)
LIMITS = [
    "Frozen supply / currently-frozen state is a LOWER-BOUND "
    "ESTIMATE: the issuer blacklist is not enumerable on-chain, so "
    "the currently-frozen set is rebuilt from the freeze events "
    "inside the scanned window only (365 days, 5-year lookback only "
    "for a coin with freeze signatures but zero holder-state events "
    "in 365d — TUSD). Addresses frozen before the window are missed.",
    "Event windows are limited by log-scan depth: Blockscout "
    "getLogs is paged (offset=1000, up to 3 pages per event "
    "signature), so a very chatty signature could be truncated — "
    "the per-coin scan_status says ok / partial / unavailable when "
    "that happens.",
    "Primary chain only (Ethereum for the 5 freezable coins). "
    "FDUSD/TUSD hold most of their supply off the primary chain "
    "(BSC / Tron); freezes on other chains are not covered.",
    "Destroyed black funds (USDT/TUSD DestroyedBlackFunds) are "
    "funds the issuer BURNED out of a blacklisted address — seized, "
    "not merely frozen — and are never counted as frozen.",
    "Pause/unpause events are global (they halt ALL transfers, not "
    "one holder) and carry no address.",
    "USD amounts use the live price at feed time (DeFiLlama, same "
    "as /freeze); for destroyed funds that is the value destroyed, "
    "not a current balance.",
    "DAI and USDe have no freeze/blacklist/pause function in their "
    "token ABIs, so there are no freeze events to scan for them. "
    "USDD (Tron) has no freeze function in TronScan's published "
    "method map (11 observed functions, partial list).",
]

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


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
    all freezable scopes. Raises UpstreamError(400) on unknown."""
    if not raw or not raw.strip():
        return list(FREEZABLE_SCOPES)
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
        return list(FREEZABLE_SCOPES)
    return out


def _no_freeze_reason(scope):
    """Why a coin has no freeze feed — the same ABI evidence
    feature 10 reports, in one line."""
    if scope == "usddscope":
        return (
            "No freeze/blacklist function appears in TronScan's "
            "published method map for the USDD contract (11 observed "
            "functions — a PARTIAL list built from observed "
            "transactions), so there are no freeze events to scan."
        )
    return (
        "No freeze/blacklist/pause function or event in the token "
        "ABI, so there are no freeze events to scan — reported as "
        "not applicable, not as zero."
    )


async def _coin_feed_events(scope):
    """Full (unpaginated) event list for one freezable coin, cached
    300s. Returns (events_newest_first, coin_block) where each event
    carries issuer/coin, event type, address, amount where known,
    tx hash and timestamp. `currently_frozen` is True/False for
    holder-state events (from the window's frozen set) and None for
    global/destroyed events."""
    cache_key = f"freezeFeed:{scope}"
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit
    cfg = dict(_cfg(scope))
    cfg["_scope"] = scope
    detail = await _coin_detail(cfg)
    price, _ = await _current_price(cfg, detail)
    (events, counts, ev_status, ev_note, frozen_state,
     window) = await _scan_window(cfg)
    rows = []
    for e in events:
        addr = e["address"]
        if e["kind"] in HOLDER_STATE_KINDS and addr:
            currently_frozen = addr.lower() in frozen_state
        else:
            currently_frozen = None
        amount = (
            float(_amount(e["amount_raw"], cfg["decimals"] or 0) or 0)
            if e["amount_raw"] is not None else None
        )
        rows.append({
            "timestamp": e["timestamp"],
            "date": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(e["timestamp"]))
                if e["timestamp"] else None
            ),
            "coin": cfg["symbol"],
            "symbol": cfg["symbol"],
            "scope": scope,
            "issuer": cfg["issuer"],
            "chain": cfg["primary"],
            "event_type": e["kind"],
            "kind": e["kind"],
            "event": e["event"],
            "address": addr,
            "currently_frozen": currently_frozen,
            "amount": amount,
            "amount_usd": (
                round(amount * price, 2)
                if amount is not None and price else None
            ),
            "tx_hash": e["tx_hash"],
            "block": e["block"],
        })
    coin_block = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "status": "freezable",
        "reason": ABI_NOTES.get(scope, ""),
        "events_in_window": len(rows),
        "scan_status": ev_status,
        "scan_note": ev_note,
        "window_days": window["window_days"],
        "window_start_block": window["window_start_block"],
        "window_end_block": window["window_end_block"],
        "extended_lookback": window["extended_lookback"],
        "extended_reason": window["extended_reason"],
        "currently_frozen_addresses": len(frozen_state),
    }
    out = (rows, coin_block)
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out


def _coin_block_not_applicable(scope):
    cfg = STABLECOINS[scope]
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "status": "not_applicable",
        "reason": ABI_NOTES.get(scope, "") + " " + _no_freeze_reason(scope),
        "events_in_window": 0,
        "scan_status": "not_applicable",
        "scan_note": _no_freeze_reason(scope),
        "window_days": None,
        "window_start_block": None,
        "window_end_block": None,
        "extended_lookback": False,
        "extended_reason": None,
        "currently_frozen_addresses": 0,
    }


def _page(rows, page, page_size):
    total = len(rows)
    total_pages = max(1, -(-total // page_size))
    if page > total_pages:
        return [], total_pages
    start = (page - 1) * page_size
    return rows[start:start + page_size], total_pages


async def freeze_feed(params, scopes=None):
    """Aggregate feed (walletscope): merge the freezable coins'
    event lists, filter, paginate.

    params: coin, kind/event_type, address, from, to, days, page,
    page_size. `scopes` pins the coin set (used by the per-scope
    endpoint — a single scope, which may be non-freezable).
    Raises UpstreamError(400) for invalid filters / pagination.
    """
    if scopes is None:
        scopes = _resolve_coins(params.get("coin"))
    kind = (params.get("kind") or params.get("event_type") or "").strip().lower()
    if kind and kind not in ALL_KINDS:
        raise UpstreamError(
            f"Unknown kind '{kind}' (kinds: {', '.join(ALL_KINDS)})",
            status=400)
    address = (params.get("address") or "").strip()
    if address and not _ADDR_RE.fullmatch(address):
        raise UpstreamError(
            "address must be a 0x address (40 hex chars)", status=400)
    address_l = address.lower() if address else None
    now = int(time.time())
    days = _parse_int(params.get("days"), "days", None, 1, 1825)
    from_ts = _parse_int(params.get("from"), "from", None, 0, now + 86400)
    to_ts = _parse_int(params.get("to"), "to", None, 0, now + 86400)
    if days is not None:
        from_ts = now - days * 86400 if from_ts is None else from_ts
    if from_ts is not None and to_ts is not None and from_ts > to_ts:
        raise UpstreamError("from must not be after to", status=400)
    page = _parse_int(params.get("page"), "page", 1, 1, 100000)
    page_size = _parse_int(params.get("page_size"), "page_size", 50, 1, 100)

    coins_blocks = []
    rows = []
    for scope in scopes:
        if scope in FREEZABLE_SCOPES:
            events, block = await _coin_feed_events(scope)
            coins_blocks.append(block)
            rows.extend(events)
        else:
            coins_blocks.append(_coin_block_not_applicable(scope))
    # Newest first (per-coin lists already are; re-sort after merge).
    rows.sort(key=lambda e: (-(e["timestamp"] or 0), -(e["block"] or 0)))
    total_events = len(rows)

    filtered = rows
    if kind:
        filtered = [e for e in filtered if e["kind"] == kind]
    if address_l:
        filtered = [e for e in filtered
                    if (e["address"] or "").lower() == address_l]
    if from_ts is not None:
        filtered = [e for e in filtered
                    if e["timestamp"] is not None and e["timestamp"] >= from_ts]
    if to_ts is not None:
        filtered = [e for e in filtered
                    if e["timestamp"] is not None and e["timestamp"] <= to_ts]

    counts_by_coin = {}
    counts_by_kind = {}
    for e in filtered:
        counts_by_coin[e["coin"]] = counts_by_coin.get(e["coin"], 0) + 1
        counts_by_kind[e["kind"]] = counts_by_kind.get(e["kind"], 0) + 1
    page_items, total_pages = _page(filtered, page, page_size)

    # Coverage: the requested coins' blocks, plus the
    # not-applicable blocks for DAI/USDe/USDD (cheap — no scan), so
    # a caller filtering to one coin still sees plainly which coins
    # have no freeze function at all. Non-requested freezable coins
    # are NOT scanned for coverage — that would defeat the filter.
    coverage = list(coins_blocks)
    seen = {b["scope"] for b in coins_blocks}
    for scope in ALL_SCOPES:
        if scope not in seen and scope not in FREEZABLE_SCOPES:
            coverage.append(_coin_block_not_applicable(scope))
    # Keep coverage in the canonical 8-coin order.
    order = {s: i for i, s in enumerate(ALL_SCOPES)}
    coverage.sort(key=lambda b: order[b["scope"]])

    return {
        "events": page_items,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "total_filtered": len(filtered),
        "total_events": total_events,
        "counts_by_coin": counts_by_coin,
        "counts_by_kind": counts_by_kind,
        "filters": {
            "coins": [STABLECOINS[s]["symbol"] for s in scopes],
            "kind": kind or None,
            "address": address or None,
            "from": from_ts,
            "to": to_ts,
            "days": days,
        },
        "coins": coverage,
        "method": METHOD,
        "limits": LIMITS,
        "as_of": now,
    }


async def freeze_feed_scope(scope, params):
    """Per-scope feed: GET /api/<scope>/freeze/feed. For a coin with
    no freeze function the response is status not_applicable with
    the evidence — an empty feed with no explanation would be a
    lie by omission."""
    if scope not in FREEZABLE_SCOPES:
        cfg = STABLECOINS[scope]
        return {
            "events": [],
            "page": 1,
            "page_size": _parse_int(
                params.get("page_size"), "page_size", 50, 1, 100),
            "total_pages": 1,
            "total_filtered": 0,
            "total_events": 0,
            "status": "not_applicable",
            "summary": (
                f"{cfg['symbol']} has no freeze/blacklist function, "
                "so there is no freeze event feed for it. "
                + _no_freeze_reason(scope)
            ),
            "counts_by_coin": {},
            "counts_by_kind": {},
            "filters": {
                "coins": [cfg["symbol"]],
                "kind": (params.get("kind") or params.get("event_type") or "").strip().lower() or None,
                "address": (params.get("address") or "").strip() or None,
                "from": None,
                "to": None,
                "days": None,
            },
            "coins": [_coin_block_not_applicable(scope)],
            "method": METHOD,
            "limits": LIMITS,
            "as_of": int(time.time()),
        }
    return await freeze_feed(params, scopes=[scope])
