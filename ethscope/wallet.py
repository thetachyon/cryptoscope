"""Cross-chain wallet aggregation for EthScope.

Fan-out over the 4 live chains (ethereum, sepolia, optimism, gnosis):
per-chain native + token balances with USD values, NFT holdings,
unified activity, portfolio history, and an estimated realized PnL.

Design notes (the user-facing method statements live in README.md and in
the dashboard UI — every figure here is labeled with its method):
- Every chain section is independent: a slow/failed chain returns an
  `error` for that chain only, never fails the whole response.
- Per-chain wall-clock budget is WALLET_CHAIN_TIMEOUT seconds.
- USD pricing reuses pricing.py (Blockscout exchange_rate first,
  CoinGecko fallback, max 5 CoinGecko lookups per chain section).
- History assumes TODAY'S balances held throughout the window — it is
  an estimate, labeled as such in the response and the UI.
- PnL is a FIFO estimate over a sampled window of recent token
  transfers (latest 50 per chain), priced at CoinGecko daily closes.
  It is NOT a tax-accurate realized gain: transfers are treated as
  acquisitions/disposals, self-transfers are ignored, and anything
  unpriced or unmatched is reported, never invented.
- Sepolia ETH has no market value; Blockscout reports no price for it,
  so Sepolia balances are excluded from USD totals.
"""
import asyncio
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

from chains import CHAINS
from clients import CHART_TTL, _cache_get, _cache_set, bs
from formatting import token_amount, tx_short, wei_to_eth
from pricing import (
    _cg_contract_daily_prices,
    _daily_native_prices,
    enrich_token_balances,
    native_quote,
)

WALLET_CHAIN_TIMEOUT = 8.0  # seconds per chain section (fast aggregate)
WALLET_TOKENS_TIMEOUT = 45.0  # seconds per chain for token holdings (matches the 45s bs() timeout; the 3.4MB mainnet list alone can take 30s+ through a slow proxy)
WALLET_TOP_TOKENS = 50  # holdings rows returned per wallet (by USD value)
HISTORY_TOP_TOKENS = 5  # tokens priced into the history chart
PNL_MAX_TOKENS = 8  # distinct tokens priced for the PnL estimate
PNL_DAYS = 90  # price-at-time window (CoinGecko daily closes)


# ---------------------------------------------------------------- adapters
class EVMAdapter:
    """Per-chain data adapter for EVM chains (Blockscout v2).

    Aggregation code talks to this adapter, never to bs() directly, so a
    future non-EVM chain (e.g. Solana) can plug in as another adapter
    implementing the same methods — address, token balances, NFTs,
    transactions, token transfers — without a rewrite of the aggregation.
    Only EVM chains exist today; CHAINS is the EVM chain registry.
    """

    async def address(self, chain, h):
        return await bs(chain, f"/addresses/{h}")

    async def token_balances(self, chain, h):
        return await bs(chain, f"/addresses/{h}/token-balances", timeout=45.0)

    async def nfts(self, chain, h):
        return await bs(chain, f"/addresses/{h}/nft")

    async def transactions(self, chain, h):
        return await bs(chain, f"/addresses/{h}/transactions")

    async def token_transfers(self, chain, h):
        return await bs(chain, f"/addresses/{h}/token-transfers")


ADAPTER = EVMAdapter()


def _empty_chain_section(chain, error=None):
    return {
        "chain": chain,
        "label": CHAINS[chain]["label"],
        "native": CHAINS[chain]["native"],
        "native_balance": "0",
        "native_usd_price": None,
        "native_usd_value": None,
        "native_change_24h_pct": None,
        "tokens": [],
        "tokens_total_usd": None,
        "tokens_count": None,
        "tokens_priced_count": None,
        "chain_total_usd": None,
        "tokens_status": "pending",
        "nft_count": 0,
        "ens": None,
        "error": error,
    }


def _price_on_or_before(daily, date_str, max_back=7):
    """Price for a date from a {YYYY-MM-DD: price} map, falling back to the
    most recent earlier day (up to max_back days). None if nothing found."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None
    for i in range(max_back + 1):
        p = daily.get((d - timedelta(days=i)).isoformat())
        if p is not None:
            return p
    return None


async def _chain_wallet_fast(chain, h):
    """One chain's FAST wallet section: native balance + USD + ENS only.
    Token holdings are deliberately NOT fetched here — Blockscout's
    /token-balances is one giant unpaginated download (vitalik's mainnet
    list is 3.4MB / ~15s through this sandbox's egress proxy), so holdings
    load progressively via _chain_tokens instead. A 404 (address unseen
    on this chain) yields zero balances, not an error."""
    addr_r, quote_r = await asyncio.gather(
        ADAPTER.address(chain, h),
        native_quote(chain),
        return_exceptions=True,
    )
    out = _empty_chain_section(chain)
    errors = []
    addr = addr_r if isinstance(addr_r, dict) else None
    if isinstance(addr_r, Exception):
        errors.append(str(addr_r))
    quote = quote_r if isinstance(quote_r, tuple) else (None, None)
    if addr:
        out["native_balance"] = wei_to_eth(addr.get("coin_balance"))
        out["ens"] = addr.get("ens_domain_name")
    price, change = quote
    out["native_usd_price"] = price
    out["native_change_24h_pct"] = change
    try:
        bal = Decimal(out["native_balance"])
        if price is not None:
            out["native_usd_value"] = float(bal * Decimal(str(price)))
    except InvalidOperation:
        pass
    out["chain_total_usd"] = out["native_usd_value"]
    out["error"] = "; ".join(errors) if errors else None
    return out


async def _chain_tokens(chain, h):
    """One chain's token holdings: full /token-balances download + USD
    enrichment. Slow for token-rich addresses (see _chain_wallet_fast) —
    callers give this a longer budget and load it progressively. The
    Blockscout read itself gets a 45s timeout: vitalik's mainnet list is
    3.4MB and stalls through the sandbox egress proxy."""
    raw_r = await ADAPTER.token_balances(chain, h)
    raw = []
    if isinstance(raw_r, list):
        raw = raw_r
    elif isinstance(raw_r, dict):
        raw = raw_r.get("items", [])
    items, total = await enrich_token_balances(chain, raw)
    priced = [it for it in items if it.get("usd_value") is not None]
    priced.sort(key=lambda it: it["usd_value"], reverse=True)
    return {
        "chain": chain,
        "label": CHAINS[chain]["label"],
        "tokens": priced[:WALLET_TOP_TOKENS],
        "tokens_total_usd": total,
        "tokens_count": len(items),
        "tokens_priced_count": len(priced),
        "nft_count": sum(
            1 for it in items if it.get("type") in ("ERC-721", "ERC-1155")
        ),
        "error": None,
    }


async def _chain_tokens_guarded(chain, h):
    """Token holdings for one chain, cached per (address, chain) so the
    history endpoint and the frontend's per-chain calls share the same
    (slow) download instead of each re-fetching it."""
    key = f"wallet:{h.lower()}:tokens:{chain}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        out = await asyncio.wait_for(
            _chain_tokens(chain, h), timeout=WALLET_TOKENS_TIMEOUT
        )
    except asyncio.TimeoutError:
        out = {
            "chain": chain,
            "label": CHAINS[chain]["label"],
            "tokens": [],
            "tokens_total_usd": None,
            "tokens_count": None,
            "tokens_priced_count": None,
            "nft_count": 0,
            "error": f"{CHAINS[chain]['label']} token holdings timed out after {WALLET_TOKENS_TIMEOUT:g}s",
        }
    except Exception as e:  # never let one chain break the dashboard
        out = {
            "chain": chain,
            "label": CHAINS[chain]["label"],
            "tokens": [],
            "tokens_total_usd": None,
            "tokens_count": None,
            "tokens_priced_count": None,
            "nft_count": 0,
            "error": str(e),
        }
    _cache_set(key, out)
    return out


async def _wallet_tokens_all(h):
    """Token holdings for all chains (slow endpoint — see _chain_tokens).
    Returns {chain: section} plus flattened top tokens and grand total."""
    key = f"wallet:{h.lower()}:tokens"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    sections = await asyncio.gather(
        *[_chain_tokens_guarded(c, h) for c in CHAINS]
    )
    chains_out = {}
    total = Decimal(0)
    top = []
    errors = []
    for sec in sections:
        chain = sec["chain"]
        chains_out[chain] = sec
        if sec.get("error"):
            errors.append(chain)
        if sec.get("tokens_total_usd") is not None:
            total += Decimal(str(sec["tokens_total_usd"]))
        for t in sec.get("tokens", []):
            top.append(
                {
                    "chain": chain,
                    "chain_label": sec["label"],
                    "address": t.get("address"),
                    "name": t.get("name"),
                    "symbol": t.get("symbol"),
                    "decimals": t.get("decimals"),
                    "type": t.get("type"),
                    "icon_url": t.get("icon_url"),
                    "balance": t.get("balance"),
                    "usd_price": t.get("usd_price"),
                    "usd_value": t.get("usd_value"),
                    "usd_change_24h_pct": t.get("usd_change_24h_pct"),
                }
            )
    top.sort(key=lambda t: t.get("usd_value") or 0, reverse=True)
    out = {
        "address": h,
        "chains": chains_out,
        "tokens_total_usd": float(total),
        "top_tokens": top[:WALLET_TOP_TOKENS],
        "errors": errors,
    }
    _cache_set(key, out)
    return out


async def _wallet_tokens(h, chain=None):
    """Token holdings, optionally for one chain only (progressive loading:
    the frontend calls this per chain so each section renders as it
    arrives). Returns the _wallet_tokens_all shape."""
    if chain is None:
        return await _wallet_tokens_all(h)
    if chain not in CHAINS:
        return {"error": "Unknown chain"}
    sec = await _chain_tokens_guarded(chain, h)
    top = []
    for t in sec.get("tokens", []):
        top.append(
            {
                "chain": chain,
                "chain_label": sec["label"],
                "address": t.get("address"),
                "name": t.get("name"),
                "symbol": t.get("symbol"),
                "decimals": t.get("decimals"),
                "type": t.get("type"),
                "icon_url": t.get("icon_url"),
                "balance": t.get("balance"),
                "usd_price": t.get("usd_price"),
                "usd_value": t.get("usd_value"),
                "usd_change_24h_pct": t.get("usd_change_24h_pct"),
            }
        )
    top.sort(key=lambda t: t.get("usd_value") or 0, reverse=True)
    return {
        "address": h,
        "chains": {chain: sec},
        "tokens_total_usd": sec.get("tokens_total_usd") or 0.0,
        "top_tokens": top[:WALLET_TOP_TOKENS],
        "errors": [chain] if sec.get("error") else [],
    }


async def _chain_wallet_guarded(chain, h):
    try:
        return await asyncio.wait_for(
            _chain_wallet_fast(chain, h), timeout=WALLET_CHAIN_TIMEOUT
        )
    except asyncio.TimeoutError:
        return _empty_chain_section(
            chain,
            error=f"{CHAINS[chain]['label']} timed out after {WALLET_CHAIN_TIMEOUT:g}s",
        )
    except Exception as e:  # never let one chain break the dashboard
        return _empty_chain_section(chain, error=str(e))


async def _wallet(h):
    """Fast aggregate wallet: per-chain NATIVE balances + USD, ENS, and
    status. Token holdings are NOT included — they load progressively via
    _wallet_tokens_all (Blockscout's /token-balances is one giant
    unpaginated download). total_usd here is the native-only total; the
    frontend adds token totals as they arrive. 60s cache."""
    key = f"wallet:{h.lower()}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    sections = await asyncio.gather(
        *[_chain_wallet_guarded(c, h) for c in CHAINS]
    )
    chains_out = {}
    total = Decimal(0)
    ens = None
    errors = []
    cur = Decimal(0)
    prior = Decimal(0)
    for sec in sections:
        chain = sec["chain"]
        chains_out[chain] = sec
        if sec.get("error"):
            errors.append(chain)
        if sec.get("native_usd_value") is not None:
            total += Decimal(str(sec["native_usd_value"]))
        if sec.get("ens") and not ens:
            ens = sec["ens"]
        # 24h change over native holdings only (tokens load separately).
        if sec.get("native_usd_value") and sec.get("native_change_24h_pct") is not None:
            v = Decimal(str(sec["native_usd_value"]))
            chg = Decimal(str(sec["native_change_24h_pct"]))
            cur += v
            prior += v / (1 + chg / 100)
    change_pct = None
    if prior > 0 and cur > 0:
        change_pct = float((cur - prior) / prior * 100)
    out = {
        "address": h,
        "ens": ens,
        "total_usd": float(total),
        "total_native_usd": float(total),
        "total_tokens_usd": None,
        "change_24h_pct": change_pct,
        "change_24h_coverage_usd": float(cur),
        "change_24h_note": (
            "Value-weighted 24h change over NATIVE holdings only at this "
            "stage — token holdings load separately and the dashboard "
            "recomputes the combined change once they arrive. Holdings "
            "priced by Blockscout without a 24h change are excluded from "
            "the change figure (but included in the total)."
        ),
        "nft_count_total": 0,
        "tokens_total_count": None,
        "tokens_status": "pending",
        "chains": chains_out,
        "top_tokens": [],
        "errors": errors,
        "testnet_note": (
            "Sepolia ETH has no market value — Blockscout reports no price "
            "for it, so Sepolia balances are excluded from USD totals."
        ),
    }
    _cache_set(key, out)
    return out


async def _wallet_history(h, days):
    """Portfolio value history (estimate). Today's balances — native per
    chain + top HISTORY_TOP_TOKENS tokens by current value — multiplied by
    each day's historical price. Token holdings come from the (slower)
    tokens loader; native balances from the fast aggregate. 10-min cache."""
    key = f"wallet:{h.lower()}:history:{days}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    w = await _wallet(h)
    toks = await _wallet_tokens_all(h)
    # Native series only for chains with a real USD price — Sepolia has
    # none (Blockscout reports no price), so its "ETH" must NOT be valued
    # at mainnet ETH prices here (native_quote already excludes it from
    # the aggregate total; _daily_native_prices would not).
    priced_chains = {
        chain
        for chain, sec in w["chains"].items()
        if sec.get("native_usd_price") is not None
    }
    native_series = {}
    for chain in CHAINS:
        if chain not in priced_chains:
            continue
        try:
            native_series[chain] = await _daily_native_prices(chain, days)
        except Exception:
            native_series[chain] = {}
    cands = [t for t in toks["top_tokens"] if t.get("usd_value")]
    cands.sort(key=lambda t: t["usd_value"], reverse=True)
    top_tokens = cands[:HISTORY_TOP_TOKENS]
    token_series = {}
    for t in top_tokens:
        try:
            token_series[(t["chain"], (t["address"] or "").lower())] = (
                await _cg_contract_daily_prices(
                    CHAINS[t["chain"]]["coingecko_platform"], t["address"], days
                )
            )
        except Exception:
            token_series[(t["chain"], (t["address"] or "").lower())] = {}
    per_day: dict = {}
    for chain, sec in w["chains"].items():
        try:
            bal = Decimal(sec.get("native_balance") or "0")
        except InvalidOperation:
            continue
        if bal == 0:
            continue
        for day, price in native_series.get(chain, {}).items():
            per_day[day] = per_day.get(day, Decimal(0)) + bal * Decimal(str(price))
    for t in top_tokens:
        try:
            bal = Decimal(t["balance"])
        except (InvalidOperation, TypeError):
            continue
        series = token_series.get((t["chain"], (t["address"] or "").lower()), {})
        for day, price in series.items():
            per_day[day] = per_day.get(day, Decimal(0)) + bal * Decimal(str(price))
    points = [
        {"date": d, "value_usd": float(per_day[d])} for d in sorted(per_day)
    ]
    out = {
        "address": h,
        "days": days,
        "points": points,
        "method": (
            "Estimate: today's balances (native coins + top "
            f"{HISTORY_TOP_TOKENS} tokens by current value) multiplied by each "
            "day's historical price. Balances are assumed constant over the "
            "window."
        ),
        "included": [
            {
                "chain": t["chain"],
                "token": t.get("symbol"),
                "address": t.get("address"),
                "value_usd": t.get("usd_value"),
            }
            for t in top_tokens
        ],
        "excluded_tokens": max(0, len(cands) - len(top_tokens)),
        "note": (
            "Not a true historical portfolio value: deposits, withdrawals "
            "and trades during the window are ignored."
        ),
    }
    _cache_set(key, out, ttl=CHART_TTL)
    return out


async def _wallet_pnl(h):
    """Estimated realized PnL (FIFO) over the latest 50 token transfers per
    chain. Every incoming ERC-20 transfer is treated as an acquisition at
    that day's CoinGecko close, every outgoing transfer as a disposal at
    that day's close; disposals match the oldest acquisitions first.
    Self-transfers are ignored. Anything unpriced or unmatched is counted
    and excluded, never invented. 5-min cache."""
    key = f"wallet:{h.lower()}:pnl"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    h_low = h.lower()

    async def _fetch(chain):
        try:
            d = await ADAPTER.token_transfers(chain, h)
        except Exception:
            return chain, []
        if d is None:
            return chain, []
        return chain, d.get("items", [])

    results = await asyncio.gather(*[_fetch(c) for c in CHAINS])
    groups: dict = {}
    for chain, items in results:
        for tt in items or []:
            tok = tt.get("token") or {}
            if (tok.get("type") or "") != "ERC-20":
                continue
            frm = (tt.get("from") or {}).get("hash", "")
            to = (tt.get("to") or {}).get("hash", "")
            if not frm or not to or frm.lower() == to.lower():
                continue
            total = tt.get("total") or {}
            dec = (
                total.get("decimals")
                if total.get("decimals") is not None
                else tok.get("decimals")
            )
            try:
                amt = Decimal(token_amount(total.get("value"), dec))
            except (InvalidOperation, ValueError):
                continue
            if amt <= 0:
                continue
            gkey = (chain, ((tok.get("address_hash") or tok.get("address") or "").lower()))
            g = groups.setdefault(
                gkey, {"chain": chain, "token": tok, "transfers": []}
            )
            g["transfers"].append(
                {
                    "timestamp": tt.get("timestamp"),
                    "amount": amt,
                    "is_in": to.lower() == h_low,
                }
            )
    ranked = sorted(groups.values(), key=lambda g: len(g["transfers"]), reverse=True)
    priced_groups = ranked[:PNL_MAX_TOKENS]
    series = {}
    for g in priced_groups:
        addr = g["token"].get("address_hash") or g["token"].get("address") or ""
        try:
            series[(g["chain"], addr.lower())] = await _cg_contract_daily_prices(
                CHAINS[g["chain"]]["coingecko_platform"], addr, PNL_DAYS
            )
        except Exception:
            series[(g["chain"], addr.lower())] = {}

    rows = []
    tot_proceeds = tot_cost = tot_pnl = Decimal(0)
    tot_priced = tot_unpriced = tot_unmatched = 0
    for g in priced_groups:
        g["transfers"].sort(key=lambda t: t["timestamp"] or "")
        daily = series.get(
            (
                g["chain"],
                (
                    g["token"].get("address_hash")
                    or g["token"].get("address")
                    or ""
                ).lower(),
            ),
            {},
        )
        lots: deque = deque()  # [amount_remaining, unit_cost_usd]
        proceeds = cost = pnl = Decimal(0)
        n_priced = n_unpriced = n_unmatched = 0
        for t in g["transfers"]:
            date = (t["timestamp"] or "")[:10]
            price = _price_on_or_before(daily, date)
            if price is None:
                n_unpriced += 1
                continue
            usd_price = Decimal(str(price))
            if t["is_in"]:
                lots.append([t["amount"], usd_price])
                n_priced += 1
                continue
            n_priced += 1
            out_amt = t["amount"]
            matched_cost = Decimal(0)
            remaining = out_amt
            while remaining > 0 and lots:
                lot_amt, lot_price = lots[0]
                take = min(lot_amt, remaining)
                matched_cost += take * lot_price
                lot_amt -= take
                remaining -= take
                if lot_amt == 0:
                    lots.popleft()
                else:
                    lots[0][0] = lot_amt
            matched_amt = out_amt - remaining
            if remaining > 0:
                n_unmatched += 1
            out_proceeds = matched_amt * usd_price
            proceeds += out_proceeds
            cost += matched_cost
            pnl += out_proceeds - matched_cost
        rows.append(
            {
                "chain": g["chain"],
                "token_address": g["token"].get("address_hash") or g["token"].get("address"),
                "token_symbol": g["token"].get("symbol"),
                "token_name": g["token"].get("name"),
                "proceeds_usd": float(proceeds),
                "cost_usd": float(cost),
                "realized_pnl_usd": float(pnl),
                "transfers_priced": n_priced,
                "transfers_unpriced": n_unpriced,
                "transfers_unmatched": n_unmatched,
            }
        )
        tot_proceeds += proceeds
        tot_cost += cost
        tot_pnl += pnl
        tot_priced += n_priced
        tot_unpriced += n_unpriced
        tot_unmatched += n_unmatched
    rows.sort(key=lambda r: abs(r["realized_pnl_usd"]), reverse=True)
    out = {
        "address": h,
        "realized_pnl_usd": float(tot_pnl),
        "proceeds_usd": float(tot_proceeds),
        "cost_usd": float(tot_cost),
        "rows": rows,
        "transfers_priced": tot_priced,
        "transfers_unpriced": tot_unpriced,
        "transfers_unmatched": tot_unmatched,
        "tokens_priced": len(priced_groups),
        "tokens_skipped": max(0, len(groups) - len(priced_groups)),
        "method": (
            "FIFO estimate over the latest 50 token transfers per chain: "
            "every incoming ERC-20 transfer is treated as an acquisition at "
            "that day's CoinGecko close, every outgoing transfer as a "
            "disposal at that day's close; disposals are matched against "
            "the oldest acquisitions first. Self-transfers are ignored."
        ),
        "limits": [
            "Only transfers in the sampled window (latest 50 per chain) are included; older history is not fetched.",
            "Transfers older than 90 days, or in tokens with no CoinGecko price history, are unpriced and excluded from the PnL.",
            "Disposals with no matching acquisition in the window are unmatched and excluded (no cost basis known).",
            "This is an estimate, not tax advice: transfers to exchanges, gifts and internal moves are all treated as disposals.",
        ],
    }
    _cache_set(key, out, ttl=300)
    return out


async def _wallet_nfts(h):
    """NFT holdings across all chains (Blockscout /addresses/{h}/nft,
    first 50 per chain). 60s cache."""
    key = f"wallet:{h.lower()}:nfts"
    hit = _cache_get(key)
    if hit is not None:
        return hit

    async def _fetch(chain):
        try:
            d = await ADAPTER.nfts(chain, h)
        except Exception as e:
            return chain, None, str(e)
        if d is None:
            return chain, [], None
        return chain, d.get("items", []), None

    results = await asyncio.gather(*[_fetch(c) for c in CHAINS])
    items = []
    per_chain = {}
    errors = {}
    for chain, lst, err in results:
        if err:
            errors[chain] = err
            per_chain[chain] = 0
            continue
        per_chain[chain] = len(lst or [])
        for inst in lst or []:
            tok = inst.get("token") or {}
            meta = inst.get("metadata") or {}
            items.append(
                {
                    "chain": chain,
                    "token_address": tok.get("address_hash") or tok.get("address"),
                    "token_name": tok.get("name"),
                    "token_symbol": tok.get("symbol"),
                    "token_type": inst.get("token_type") or tok.get("type"),
                    "token_id": inst.get("id"),
                    "name": meta.get("name"),
                    "image_url": inst.get("image_url"),
                    "media_url": inst.get("media_url"),
                    "animation_url": inst.get("animation_url"),
                }
            )
    out = {
        "address": h,
        "items": items,
        "count": len(items),
        "per_chain": per_chain,
        "errors": errors,
        "note": "First 50 NFTs per chain (Blockscout /addresses/{h}/nft, first page only).",
    }
    _cache_set(key, out)
    return out


async def _wallet_activity(h):
    """Recent transactions across all chains, merged and sorted by time
    (latest first, top 50). Per-chain next_page_params are returned for
    the frontend's Load-more, which fetches further pages per chain
    directly. 60s cache."""
    key = f"wallet:{h.lower()}:activity"
    hit = _cache_get(key)
    if hit is not None:
        return hit

    async def _fetch(chain):
        try:
            d = await ADAPTER.transactions(chain, h)
        except Exception as e:
            return chain, None, None, str(e)
        if d is None:
            return chain, [], None, None
        return chain, d.get("items", []), d.get("next_page_params"), None

    results = await asyncio.gather(*[_fetch(c) for c in CHAINS])
    items = []
    cursors = {}
    errors = {}
    for chain, lst, npp, err in results:
        if err:
            errors[chain] = err
            continue
        if npp:
            cursors[chain] = npp
        for t in lst or []:
            row = tx_short(t)
            row["chain"] = chain
            items.append(row)
    items.sort(key=lambda t: t.get("timestamp") or "", reverse=True)
    out = {
        "address": h,
        "items": items[:50],
        "next_page_params": cursors,
        "errors": errors,
    }
    _cache_set(key, out)
    return out


async def _wallet_activity_page(h, chain, params):
    """One further page of ONE chain's transactions for the activity
    feed's Load-more, shaped exactly like _wallet_activity items
    (tx_short + chain) so the frontend merges without re-shaping.
    `params` is Blockscout's next_page_params for that chain."""
    if chain not in CHAINS:
        raise ValueError("Unknown chain")
    d = await ADAPTER.transactions(chain, h) if not params else await bs(
        chain, f"/addresses/{h}/transactions", params=params
    )
    items = []
    for t in d.get("items", []):
        row = tx_short(t)
        row["chain"] = chain
        items.append(row)
    return {
        "address": h,
        "chain": chain,
        "items": items,
        "next_page_params": d.get("next_page_params"),
    }
