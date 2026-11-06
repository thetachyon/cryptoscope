"""Peg guard at quote time (phase 2, P3) — keyless.

One guard component, two modes, same ok / warn / block framing with
thresholds stated in every response (never hidden):

  - PEG mode — the quoted coin is one of the 8 stablecoins in
    common/stablecoin.py. Reuses the phase-1 depeg early-warning
    score (common/stablecoin_score.sc_depeg_score) — the scoring
    itself is NOT re-implemented here, only the verdict on top of
    it, plus the coin's live peg state (current deviation in bps).
      block: |deviation| > 50 bps (currently off-peg, the same
             50 bps threshold the depeg history uses) OR depeg-risk
             score >= 75 (high band).
      warn:  |deviation| > 10 bps OR depeg-risk score >= 50
             (elevated band).
      ok:    otherwise.
      unknown: live price unavailable, so the peg cannot be
             verified at all (never reported ok on missing data).
    When the verdict is warn/block, the response suggests the
    nearest stable alternative: the lowest-depeg-score coin among
    the other 7 stablecoin scopes, with its own peg state shown.
    The alternative is computed only on the warn/block path (it
    costs one depeg-score evaluation per other coin; each is
    cached 300s by sc_depeg_score).

  - VOLATILITY mode — every other quotable coin (the counterpart
    Tejas directed for non-stablecoins). Rolling price moves from
    CoinGecko (keyless) — 24h change plus the change over the last
    ~7 daily closes — plus the quote's own slippage/price-impact
    figure (the best route's rate-vs-spot %, passed in as
    ?slippage_pct= by the quote screen; the guard never invents a
    slippage number — without it that input is reported unknown).
      block: |24h change| >= 15% OR |7d change| >= 30% OR
             |slippage| >= 5%.
      warn:  |24h change| >= 5% OR |7d change| >= 10% OR
             |slippage| >= 1%.
      ok:    otherwise.
      unknown: no price move could be measured and no slippage was
             supplied.

Nothing here creates or signs a transaction. A guard verdict is a
screening signal at quote time, not investment advice and not a
guarantee: the peg score is the Par v0 provisional heuristic
(UNCONFIRMED weights), volatility is backward-looking (daily
closes), and the slippage figure is the quote's own estimate.
"""
import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from common.chains import WALLET_CHAINS
from common.clients import UpstreamError, _cache_get, _cache_set, coingecko
from common.pricing import (
    _cg_contract_daily_prices,
    _daily_native_prices,
    cg_token_price_full,
)
from common.quotes import resolve_token
from common.stablecoin import DEPEG_BPS, STABLECOINS, _dev_bps, _peg_health
from common.stablecoin_score import sc_depeg_score

# ---------------------------------------------------------------- thresholds
# Peg mode (bps / 0-100 score). Stated in every peg response and in
# the UI; change them here and both update together.
PEG_BLOCK_DEVIATION_BPS = 50
PEG_BLOCK_SCORE = 75
PEG_WARN_DEVIATION_BPS = 10
PEG_WARN_SCORE = 50

# Volatility mode (percent). Stated in every volatility response.
VOL_BLOCK_CHANGE_24H_PCT = 15.0
VOL_BLOCK_CHANGE_7D_PCT = 30.0
VOL_BLOCK_SLIPPAGE_PCT = 5.0
VOL_WARN_CHANGE_24H_PCT = 5.0
VOL_WARN_CHANGE_7D_PCT = 10.0
VOL_WARN_SLIPPAGE_PCT = 1.0

METHOD = (
    "Peg guard at quote time (phase 2, P3). Peg mode (stablecoins): "
    "live peg deviation from DeFiLlama plus the phase-1 depeg "
    "early-warning score (Par v0 provisional weights, UNCONFIRMED) — "
    "the score is reused from common/stablecoin_score, not "
    "re-implemented. Volatility mode (all other coins): CoinGecko "
    "24h change plus the change over the last ~7 daily closes, plus "
    "the quote's own rate-vs-spot figure when the quote screen "
    "passes it as slippage_pct. Thresholds are stated in the "
    "response. Screening signal only — not investment advice, not a "
    "guarantee."
)
LIMITS = [
    "A guard verdict is a screening signal at quote time, not a "
    "guarantee: the peg score is a heuristic (Par v0 provisional "
    "weights, UNCONFIRMED), volatility is backward-looking daily "
    "closes, and slippage is the quote's own estimate.",
    "The peg price is chain-independent (DeFiLlama global price for "
    "the coin); the chain parameter is context only for stablecoins.",
    "The alternative suggestion is the lowest depeg-risk score "
    "among the other stablecoin scopes at check time — it is a "
    "suggestion, not a recommendation to switch, and its own peg "
    "state is shown so a bad alternative is visible.",
]


def _thresholds_text(mode):
    if mode == "peg":
        return {
            "block": (
                f"Block when |deviation| > {PEG_BLOCK_DEVIATION_BPS} bps "
                f"(currently off-peg) or depeg-risk score >= "
                f"{PEG_BLOCK_SCORE}"
            ),
            "warn": (
                f"Warn when |deviation| > {PEG_WARN_DEVIATION_BPS} bps or "
                f"depeg-risk score >= {PEG_WARN_SCORE}"
            ),
            "ok": (
                f"Ok when |deviation| <= {PEG_WARN_DEVIATION_BPS} bps and "
                f"depeg-risk score < {PEG_WARN_SCORE}"
            ),
            "block_deviation_bps": PEG_BLOCK_DEVIATION_BPS,
            "block_score": PEG_BLOCK_SCORE,
            "warn_deviation_bps": PEG_WARN_DEVIATION_BPS,
            "warn_score": PEG_WARN_SCORE,
        }
    return {
        "block": (
            f"Block when |24h change| >= {VOL_BLOCK_CHANGE_24H_PCT}% or "
            f"|7d change| >= {VOL_BLOCK_CHANGE_7D_PCT}% or |slippage| "
            f">= {VOL_BLOCK_SLIPPAGE_PCT}%"
        ),
        "warn": (
            f"Warn when |24h change| >= {VOL_WARN_CHANGE_24H_PCT}% or "
            f"|7d change| >= {VOL_WARN_CHANGE_7D_PCT}% or |slippage| "
            f">= {VOL_WARN_SLIPPAGE_PCT}%"
        ),
        "ok": (
            f"Ok when |24h change| < {VOL_WARN_CHANGE_24H_PCT}%, |7d "
            f"change| < {VOL_WARN_CHANGE_7D_PCT}% and |slippage| < "
            f"{VOL_WARN_SLIPPAGE_PCT}%"
        ),
        "block_change_24h_pct": VOL_BLOCK_CHANGE_24H_PCT,
        "block_change_7d_pct": VOL_BLOCK_CHANGE_7D_PCT,
        "block_slippage_pct": VOL_BLOCK_SLIPPAGE_PCT,
        "warn_change_24h_pct": VOL_WARN_CHANGE_24H_PCT,
        "warn_change_7d_pct": VOL_WARN_CHANGE_7D_PCT,
        "warn_slippage_pct": VOL_WARN_SLIPPAGE_PCT,
    }


def _stablecoin_scope(symbol):
    """(scope_key, cfg) for a stablecoin symbol, or (None, None)."""
    if not symbol:
        return None, None
    for key, cfg in STABLECOINS.items():
        if cfg["symbol"].lower() == str(symbol).strip().lower():
            return key, cfg
    return None, None


def _parse_amount(amount):
    if amount is None or str(amount).strip() == "":
        return None
    try:
        v = Decimal(str(amount).strip())
    except InvalidOperation:
        raise UpstreamError("amount must be a number", status=400)
    if v <= 0:
        raise UpstreamError("amount must be greater than 0", status=400)
    return v


def _parse_slippage(slippage_pct):
    if slippage_pct is None or str(slippage_pct).strip() == "":
        return None
    try:
        return float(str(slippage_pct).strip())
    except (TypeError, ValueError):
        raise UpstreamError("slippage_pct must be a number", status=400)


def _pct_change(new, old):
    try:
        if new is None or old in (None, 0):
            return None
        return round((float(new) - float(old)) / float(old) * 100.0, 2)
    except (TypeError, ValueError):
        return None


async def _native_24h_change(chain):
    """24h % change for a wallet chain's native coin, from CoinGecko
    simple/price (include_24hr_change). None on any failure."""
    coin = WALLET_CHAINS[chain].get("coingecko_coin")
    if not coin:
        return None, None
    d = await coingecko(
        "/simple/price",
        params={"ids": coin, "vs_currencies": "usd",
                "include_24hr_change": "true"},
    )
    if not isinstance(d, dict):
        return None, None
    entry = d.get(coin) or {}
    price = entry.get("usd")
    change = entry.get("usd_24h_change")
    try:
        price = float(price) if price is not None else None
    except (TypeError, ValueError):
        price = None
    try:
        change = float(change) if change is not None else None
    except (TypeError, ValueError):
        change = None
    return price, change


async def _volatility(chain, token):
    """(24h % change, 7d % change, notes) for a quote token.

    24h: CoinGecko 24h change (token_price for ERC-20/SPL, simple
    price for natives). 7d: change between the latest daily close
    and the close ~7 days earlier, from the CoinGecko daily series
    (10-min cache in pricing.py) — labelled as daily closes, not
    intraday. Any leg that fails is None with a note, never invented.
    """
    notes = []
    is_native = token["address"] is None
    if is_native:
        price, chg24 = await _native_24h_change(chain)
        series = await _daily_native_prices(chain, 8)
        series_note = (
            f"Daily closes for {WALLET_CHAINS[chain].get('coingecko_coin')} "
            "(CoinGecko market_chart)."
        )
    else:
        platform = WALLET_CHAINS[chain].get("coingecko_platform")
        if not platform:
            return None, None, ["No CoinGecko platform for this chain."], None
        price, chg24 = await cg_token_price_full(platform, token["address"])
        series = await _cg_contract_daily_prices(
            platform, token["address"], 8)
        series_note = (
            f"Daily closes for {token['symbol']} on {platform} "
            "(CoinGecko contract market_chart)."
        )
    if chg24 is None:
        notes.append("24h change unavailable (CoinGecko 24h figure missing).")
    chg7 = None
    window_days = None
    if series:
        days = sorted(series.keys())
        if len(days) >= 2:
            first, last = days[0], days[-1]
            try:
                d0 = datetime.strptime(first, "%Y-%m-%d").date()
                d1 = datetime.strptime(last, "%Y-%m-%d").date()
                window_days = (d1 - d0).days
            except ValueError:
                window_days = None
            chg7 = _pct_change(series[last], series[first])
            notes.append(
                f"{window_days}-day change from daily closes "
                f"({first} → {last}). {series_note}"
            )
        else:
            notes.append(
                "Too few daily closes to compute a 7d change "
                f"(only {len(days)} day(s) in the series).")
    else:
        notes.append("Daily price series unavailable (CoinGecko market_chart failed).")
    return chg24, chg7, notes, window_days


def _vol_verdict(chg24, chg7, slippage):
    """(verdict, reasons list) for volatility mode."""
    reasons = []
    worst = None  # None < ok < warn < block
    rank = {"ok": 0, "warn": 1, "block": 2}

    def _hit(value, block_at, warn_at, label):
        nonlocal worst
        if value is None:
            return
        a = abs(value)
        if a >= block_at:
            worst = "block" if rank.get(worst or "ok", 0) < 2 else worst
            reasons.append(
                f"BLOCK trigger — {label} {value:+.2f}% (|.| >= {block_at}%).")
        elif a >= warn_at:
            if worst != "block":
                worst = "warn"
            reasons.append(
                f"Warn trigger — {label} {value:+.2f}% (|.| >= {warn_at}%).")

    _hit(chg24, VOL_BLOCK_CHANGE_24H_PCT, VOL_WARN_CHANGE_24H_PCT, "24h change")
    _hit(chg7, VOL_BLOCK_CHANGE_7D_PCT, VOL_WARN_CHANGE_7D_PCT, "7d change (daily closes)")
    _hit(slippage, VOL_BLOCK_SLIPPAGE_PCT, VOL_WARN_SLIPPAGE_PCT,
         "quote slippage (rate vs spot)")
    if chg24 is None and chg7 is None and slippage is None:
        return "unknown", ["No volatility input could be measured."]
    if worst is None:
        bits = []
        if chg24 is not None:
            bits.append(f"24h {chg24:+.2f}%")
        if chg7 is not None:
            bits.append(f"7d {chg7:+.2f}%")
        if slippage is not None:
            bits.append(f"slippage {slippage:+.2f}%")
        reasons.append(
            "All measured moves inside the warn bands"
            + (f" ({', '.join(bits)})" if bits else "") + ".")
        return "ok", reasons
    return worst, reasons


async def _best_alternative(exclude_scope):
    """Lowest-depeg-score coin among the other 7 scopes, with its own
    peg state. (None, note) when no other coin could be scored."""
    others = [s for s in STABLECOINS if s != exclude_scope]
    results = await asyncio.gather(
        *[sc_depeg_score(s) for s in others], return_exceptions=True)
    scored = []
    failed = []
    for scope, r in zip(others, results):
        if isinstance(r, Exception):
            failed.append(STABLECOINS[scope]["symbol"])
            continue
        if r.get("score") is None:
            failed.append(STABLECOINS[scope]["symbol"])
            continue
        scored.append(r)
    if not scored:
        note = "No alternative could be scored live."
        if failed:
            note += f" (failed/unknown: {', '.join(failed)})."
        return None, note
    best = min(scored, key=lambda r: r["score"])
    return {
        "symbol": best["symbol"],
        "scope": best["scope"],
        "name": best["name"],
        "score": best["score"],
        "band": best["band"],
        "price_usd": best.get("price_usd"),
        "price_source": best.get("price_source"),
        "deviation_bps": best.get("deviation_bps"),
        "peg_health": _peg_health(best.get("deviation_bps")),
        "inputs_unknown": best.get("inputs_unknown"),
        "note": (
            f"Lowest depeg-risk score among the other "
            f"{len(others)} stablecoin scopes at check time."
        ),
    }, None


async def _peg_guard(scope, cfg, chain, amount_dec):
    """Peg-mode payload for one stablecoin scope."""
    try:
        score = await sc_depeg_score(scope)
    except Exception as e:
        score = None
        score_error = str(e)
    else:
        score_error = None
    if score is not None:
        price = score.get("price_usd")
        price_source = score.get("price_source")
        bps = score.get("deviation_bps")
        depeg = {
            "score": score.get("score"),
            "band": score.get("band"),
            "inputs_unknown": score.get("inputs_unknown"),
            "weights_used": score.get("weights_used"),
            "sub_scores": {
                k: (v or {}).get("score")
                for k, v in (score.get("inputs") or {}).items()
            },
            "error": None,
        }
    else:
        price = bps = price_source = None
        depeg = {
            "score": None, "band": "unknown", "inputs_unknown": None,
            "weights_used": {}, "sub_scores": {},
            "error": score_error or "Depeg score unavailable.",
        }
    peg = {
        "price_usd": price,
        "price_source": price_source,
        "deviation_bps": bps,
        "peg_health": _peg_health(bps),
        "depeg_threshold_bps": DEPEG_BPS,
    }
    if price is None:
        verdict = "unknown"
        reasons = ["Live price unavailable — the peg cannot be verified."]
    else:
        reasons = []
        if abs(bps) > PEG_BLOCK_DEVIATION_BPS:
            verdict = "block"
            reasons.append(
                f"BLOCK trigger — {cfg['symbol']} is currently off-peg: "
                f"{bps:+.2f} bps from $1.00 (|dev| > "
                f"{PEG_BLOCK_DEVIATION_BPS} bps).")
        elif depeg["score"] is not None and depeg["score"] >= PEG_BLOCK_SCORE:
            verdict = "block"
            reasons.append(
                f"BLOCK trigger — depeg-risk score {depeg['score']} "
                f"(high band, >= {PEG_BLOCK_SCORE}).")
        elif abs(bps) > PEG_WARN_DEVIATION_BPS:
            verdict = "warn"
            reasons.append(
                f"Warn trigger — {cfg['symbol']} is "
                f"{bps:+.2f} bps off $1.00 (|dev| > "
                f"{PEG_WARN_DEVIATION_BPS} bps).")
        elif depeg["score"] is not None and depeg["score"] >= PEG_WARN_SCORE:
            verdict = "warn"
            reasons.append(
                f"Warn trigger — depeg-risk score {depeg['score']} "
                f"(elevated band, >= {PEG_WARN_SCORE}).")
        else:
            verdict = "ok"
            bits = [f"deviation {bps:+.2f} bps"]
            if depeg["score"] is not None:
                bits.append(f"depeg-risk score {depeg['score']} ({depeg['band']})")
            else:
                bits.append("depeg-risk score unavailable")
            reasons.append("Peg holds: " + ", ".join(bits) + ".")
    if depeg["score"] is None and verdict in ("ok", "warn"):
        reasons.append(
            "Note: the depeg-risk score itself was unavailable, so the "
            "verdict rests on the live deviation alone.")
    alternative = None
    alt_note = None
    if verdict in ("warn", "block"):
        alternative, alt_note = await _best_alternative(scope)
    return {
        "mode": "peg",
        "verdict": verdict,
        "verdict_reasons": reasons,
        "peg": peg,
        "depeg_score": depeg,
        "volatility": None,
        "slippage": None,
        "alternative": alternative,
        "alternative_note": alt_note,
        "thresholds": _thresholds_text("peg"),
    }


async def quote_guard(coin, chain=None, amount=None, slippage_pct=None):
    """Guard for one quoted coin.

    coin: stablecoin symbol (peg mode) or a quote-token symbol /
    contract address on `chain` (volatility mode). chain must be a
    wallet chain when given; stablecoins ignore it for pricing (the
    peg is chain-independent) but it is echoed for context. amount
    is context only. slippage_pct is the quote's own best-route
    rate-vs-spot % (volatility mode only; never invented here).
    Raises UpstreamError(400) for bad input."""
    amount_dec = _parse_amount(amount)
    slip = _parse_slippage(slippage_pct)
    if chain is not None and chain not in WALLET_CHAINS:
        raise UpstreamError(
            f"Unknown chain '{chain}' (wallet chains: "
            f"{', '.join(WALLET_CHAINS)})", status=400)
    sym = str(coin or "").strip()
    if not sym:
        raise UpstreamError("coin is required", status=400)

    scope, cfg = _stablecoin_scope(sym)
    cache_key = (
        f"guard:{sym.lower()}:{chain or '-'}:"
        f"{amount_dec if amount_dec is not None else '-'}:"
        f"{slip if slip is not None else '-'}"
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    chain_label = WALLET_CHAINS[chain]["label"] if chain else None
    base = {
        "coin": sym.upper(),
        "symbol": sym.upper(),
        "chain": chain,
        "chain_label": chain_label,
        "amount": str(amount_dec) if amount_dec is not None else None,
        "method": METHOD,
        "limits": LIMITS,
        "as_of": int(time.time()),
    }
    if scope is not None:
        base["scope"] = scope
        base["name"] = cfg["name"]
        base["issuer"] = cfg["issuer"]
        base.update(await _peg_guard(scope, cfg, chain, amount_dec))
        out = base
    else:
        if chain is None:
            raise UpstreamError(
                "chain is required for a non-stablecoin (the coin is "
                "resolved per chain)", status=400)
        token = resolve_token(chain, sym)
        if token is None:
            raise UpstreamError(
                f"Unknown coin '{sym}' — use a stablecoin symbol "
                "(USDT, USDC, DAI, USDe, FDUSD, PYUSD, TUSD, USDD) or "
                "a token listed for that chain (see /quote/tokens)",
                status=400)
        chg24, chg7, notes, window_days = await _volatility(chain, token)
        verdict, reasons = _vol_verdict(chg24, chg7, slip)
        out = dict(base)
        out.update({
            "mode": "volatility",
            "verdict": verdict,
            "verdict_reasons": reasons + notes,
            "peg": None,
            "depeg_score": None,
            "volatility": {
                "change_24h_pct": chg24,
                "change_7d_pct": chg7,
                "window_days": window_days,
                "price_basis": (
                    "24h: CoinGecko 24h change. 7d: change between "
                    "daily closes from the CoinGecko market_chart "
                    "(daily closes, not intraday)."
                ),
            },
            "slippage": (
                {
                    "slippage_pct": slip,
                    "note": (
                        "Best route's rate vs DeFiLlama spot, as "
                        "reported by the quote (negative = worse than "
                        "spot)."
                    ),
                } if slip is not None else {
                    "slippage_pct": None,
                    "note": (
                        "No slippage figure was supplied with this "
                        "guard check — pass ?slippage_pct= from the "
                        "quote's best route to include it."
                    ),
                }
            ),
            "alternative": None,
            "alternative_note": (
                "No stable alternative applies: this is a volatility "
                "guard, not a peg guard — the alternative for a "
                "volatile coin is to wait or to size down, not to "
                "swap the coin."
            ),
            "thresholds": _thresholds_text("volatility"),
        })
    _cache_set(cache_key, out, ttl=60)
    return out


async def stablecoin_guard(scope, amount=None):
    """Guard for a stablecoin scope page (its own coin, peg mode).
    `scope` is the scope key (e.g. "usdcscope")."""
    cfg = STABLECOINS.get(scope)
    if cfg is None:
        raise UpstreamError(f"Unknown stablecoin scope: {scope}", status=404)
    return await quote_guard(cfg["symbol"], chain=cfg["primary"],
                             amount=amount)
