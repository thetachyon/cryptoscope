"""Payments readiness score for the *Scope stablecoin apps — keyless.

Feature 8 of the stablecoin analytics set. One score per coin (0-100,
higher = more ready for a company to use for payments), from five
documented inputs with fixed weights (no Par spec exists for this
score — the weights below are this implementation's stated choice,
not a confirmed standard):

  1. Peg stability           30%
  2. Liquidity               25%
  3. Chain coverage          20%
  4. Fees / finality         15%
  5. Freeze risk             10%

Each input scores 0-100 (higher = better for payments) and carries
its measured value alongside the sub-score — the headline is never a
bare number:

  1. Peg stability — 100 minus the feature-1 depeg early-warning
     risk score (reuses sc_depeg_score wholesale: current deviation
     + 365d depeg history). A coin that cannot hold $1 is not
     payable in.
  2. Liquidity — 50% DEX depth (DexScreener near-par tracked
     liquidity / circulating supply, same log scale as feature 1,
     inverted so deeper = higher) + 50% on-chain turnover (feature-7
     daily transfer volume / circulating supply). If the feature-7
     scan fails, the turnover leg falls back to DexScreener 24h
     volume in near-par pairs, labelled as DEX volume (not on-chain
     transfer volume). If neither leg is measurable the input is
     unknown.
  3. Chain coverage — number of chains holding a material share of
     supply (share >= 1% of total, from the feature-5 supply map),
     scored on a fixed table (1 chain -> 25, 2 -> 45, 3 -> 60,
     4 -> 75, 5 -> 85, 6 -> 92, 7+ -> 100). A payments coin needs to
     be where the counterparties are.
  4. Fees / finality — PRIMARY-CHAIN figures only, always labelled
     as such. Fee: live gas/energy price x a typical transfer cost
     (65,000 gas for an ERC-20 transfer — labelled ESTIMATE, actual
     gas varies by token and wallet; 14,000 energy for a plain
     TRC20 transfer to an existing holder — labelled ESTIMATE,
     accounts with staked energy pay less, often zero) x the native
     coin's live USD price (CoinGecko keyless). Finality: Ethereum
     2 epochs (64 slots x measured block time, ~13 min — a protocol
     figure, labelled as such); Tron ~19 blocks x 3 s (~1 min —
     a protocol figure, labelled as such). Fee leg 70%, finality
     leg 30%; fee unknown -> finality alone, with a note.
  5. Freeze risk — whether the issuer can freeze/blacklist holder
     balances. EVM: live eth_call probes for isBlacklisted /
     isBlackListed / blacklisted / isFrozen / frozen (address arg)
     and paused() (no arg) on the primary-chain contract. A revert
     or empty return means the function is absent; a transport
     failure means the input is unknown, never assumed absent.
     Scoring: no freeze/blacklist function -> 100; pause-only
     (issuer can halt all transfers but cannot freeze individual
     balances) -> 70; freeze/blacklist function present -> 40.
     Tron (USDD): no ABI endpoint exists keyless, so the check uses
     TronScan's published method map for the contract — a partial
     list of observed functions, labelled as such. Observed
     freeze events ARE tracked now (feature 10, /freeze): this input
     still scores issuer CAPABILITY, not observed incidents — see
     /freeze for the events.

An input that cannot be measured scores `null` ("unknown"); the
overall score is the weight-renormalised mean of the known inputs,
and the response lists which inputs were unknown and the weights
actually used — the same rule as feature 1.

This is a heuristic screening signal for comparing coins, not
investment advice, not a credit rating, and not a guarantee. The
weights are uncalibrated — no backtest against payments-adoption
outcomes has been run.
"""
import asyncio
import math
import time
from datetime import datetime, timezone

import httpx

from common.abi import _decode_args, _encode_args, _function_selector
from common.clients import (
    HEADERS,
    UpstreamError,
    _cache_get,
    _cache_set,
    bs,
    coingecko,
    rpc,
)
from common.stablecoin import (
    STABLECOINS,
    _cfg,
    _num,
    _tronscan,
    sc_activity,
    sc_supply_map,
)
from common.stablecoin_score import _dexscreener_near_par, sc_depeg_score

WEIGHTS = {
    "peg_stability": 0.30,
    "liquidity": 0.25,
    "chain_coverage": 0.20,
    "fees_finality": 0.15,
    "freeze_risk": 0.10,
}

METHODOLOGY = (
    "Payments readiness score (0-100, higher = more ready for a "
    "company to use for payments). Weights (this implementation's "
    "stated choice — no Par spec exists for this score): peg "
    "stability 30%, liquidity 25%, chain coverage 20%, fees/finality "
    "15%, freeze risk 10%. Peg stability is 100 minus the feature-1 "
    "depeg risk score. Liquidity is 50% DexScreener near-par tracked "
    "liquidity / circulating supply (log scale) + 50% daily on-chain "
    "transfer volume / supply (feature 7; DexScreener 24h volume "
    "fallback, labelled as DEX volume when used). Chain coverage "
    "counts chains holding >= 1% of total supply (feature-5 supply "
    "map): 1 chain -> 25, 2 -> 45, 3 -> 60, 4 -> 75, 5 -> 85, "
    "6 -> 92, 7+ -> 100. Fees/finality are PRIMARY-CHAIN figures "
    "only: the fee is the live gas/energy price x a typical transfer "
    "cost (65,000 gas EVM / 14,000 energy Tron — both ESTIMATES, see "
    "the input evidence) x the live native-coin USD price; finality "
    "is a protocol figure (Ethereum 2 epochs, ~13 min; Tron ~19 "
    "blocks, ~1 min). Freeze risk is a capability check (live "
    "eth_call probes on EVM; TronScan method map for USDD, partial): "
    "no freeze function -> 100, pause-only -> 70, freeze/blacklist "
    "function present -> 40. Observed freeze events ARE tracked "
    "now (feature 10, /freeze) — this input still scores issuer "
    "CAPABILITY, not observed incidents. Unknown inputs are excluded and the remaining "
    "weights renormalised. Heuristic screening signal — not "
    "investment advice, not a rating, not a guarantee."
)

CACHE_TTL = 300

# A chain holds a "material" share of supply at >= 1% of total.
MATERIAL_SHARE_PCT = 1.0

# Chain-coverage score table, keyed by material-chain count.
_COVERAGE_TABLE = {1: 25.0, 2: 45.0, 3: 60.0, 4: 75.0, 5: 85.0, 6: 92.0}

# Typical transfer cost assumptions (ESTIMATES — labelled in evidence).
EVM_TRANSFER_GAS = 65000
TRON_TRANSFER_ENERGY = 14000

# Freeze/blacklist read functions probed by eth_call (address arg).
_FREEZE_FNS = [
    "isBlacklisted",
    "isBlackListed",
    "blacklisted",
    "isFrozen",
    "frozen",
]
# TronScan method-map names that would indicate holder-freeze power.
_TRON_FREEZE_NAMES = ("freeze", "blacklist", "black", "pause", "paused")

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def _clamp(v, lo=0.0, hi=100.0):
    return max(lo, min(hi, v))


def _band(score):
    if score is None:
        return "unknown"
    if score >= 75:
        return "ready"
    if score >= 50:
        return "usable"
    if score >= 25:
        return "limited"
    return "not_ready"


def _log_scale(ratio, ref=0.001):
    """Log scale used for depth and turnover: ratio == ref -> 50,
    each 10x up adds 25, each 10x down subtracts 25, clamped 0-100."""
    if ratio is None or ratio <= 0:
        return None
    return _clamp(50.0 + 25.0 * math.log10(ratio / ref))


# ---------------------------------------------------------------- peg
def _peg_input(depeg):
    """100 minus the feature-1 depeg risk score. None when the depeg
    score itself is unknown."""
    if depeg is None:
        return None, {"note": "Depeg score unavailable — peg stability unknown."}
    risk = depeg.get("score")
    if risk is None:
        return None, {
            "note": "Depeg risk score is unknown — peg stability unknown.",
            "depeg_inputs_unknown": depeg.get("inputs_unknown"),
        }
    red = (depeg.get("inputs") or {}).get("redemption_quality") or {}
    ev = red.get("evidence") or {}
    return round(100.0 - risk, 1), {
        "depeg_risk_score": risk,
        "current_deviation_bps": depeg.get("deviation_bps"),
        "price_usd": depeg.get("price_usd"),
        "depeg_days_365d": ev.get("depeg_days_365d"),
        "worst_depeg_365d_bps": ev.get("worst_depeg_365d_bps"),
        "note": (
            "100 minus the feature-1 depeg early-warning risk score "
            "(current deviation + 365d depeg history)."
        ),
    }


# ---------------------------------------------------------------- liquidity
async def _dex_near_par_volume(cfg):
    """24h volume (USD) summed over near-par DexScreener pairs on the
    primary chain. Returns (volume_usd, pairs_count) or (None, 0) on
    failure. Fallback leg only — labelled DEX volume, not on-chain
    transfer volume."""
    cid = {"ethereum": "ethereum", "tron": "tron"}.get(cfg["primary"])
    if cid is None:
        return None, 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), headers=HEADERS) as c:
            r = await c.get(
                f"https://api.dexscreener.com/token-pairs/v1/{cid}/{cfg['contract']}"
            )
    except Exception:
        return None, 0
    if r.status_code != 200:
        return None, 0
    try:
        d = r.json()
    except Exception:
        return None, 0
    pairs = d if isinstance(d, list) else (d.get("pairs") or [])
    total = 0.0
    n = 0
    for p in pairs:
        if p.get("chainId") != cid:
            continue
        price = _num(p.get("priceUsd"))
        if price is None or abs(price - 1.0) > 0.005:
            continue
        v = _num((p.get("volume") or {}).get("h24"))
        if v is not None:
            total += v
            n += 1
    return (total if n else None), n


def _liquidity_input(near_liq, near_pairs, supply_usd, daily_vol_usd,
                     daily_basis, dex_vol24_usd, dex_pairs):
    """0-100: 50% depth (near-par liquidity / supply, log scale) +
    50% turnover (daily transfer volume / supply, log scale). The
    turnover leg uses feature-7 daily volume when available, else
    DexScreener 24h volume (labelled). None when neither leg works."""
    legs = {}
    ev = {}
    if near_pairs and supply_usd:
        depth_ratio = near_liq / supply_usd if supply_usd else None
        depth = _log_scale(depth_ratio)
        if depth is not None:
            legs["depth"] = depth
            ev.update({
                "near_par_liquidity_usd": round(near_liq, 2),
                "tracked_pairs_near_par": len(near_pairs),
                "supply_usd": supply_usd,
                "liquidity_to_supply_ratio": depth_ratio,
                "liquidity_to_supply_pct": round(depth_ratio * 100, 4),
                "depth_component": round(depth, 1),
            })
    else:
        ev["depth_note"] = (
            "No near-par DexScreener pairs on the primary chain — "
            "the depth leg is unknown, not zero."
        )
    if daily_vol_usd is not None and supply_usd:
        turnover = daily_vol_usd / supply_usd if supply_usd else None
        t = _log_scale(turnover)
        if t is not None:
            legs["turnover"] = t
            ev.update({
                "daily_transfer_volume_usd": round(daily_vol_usd, 2),
                "daily_volume_basis": daily_basis,
                "turnover_ratio": turnover,
                "turnover_pct_per_day": round(turnover * 100, 4),
                "turnover_component": round(t, 1),
                "turnover_source": "feature-7 on-chain activity (primary chain)",
            })
    elif dex_vol24_usd is not None and supply_usd:
        turnover = dex_vol24_usd / supply_usd if supply_usd else None
        t = _log_scale(turnover)
        if t is not None:
            legs["turnover"] = t
            ev.update({
                "daily_transfer_volume_usd": round(dex_vol24_usd, 2),
                "daily_volume_basis": "dexscreener_h24_fallback",
                "turnover_ratio": turnover,
                "turnover_pct_per_day": round(turnover * 100, 4),
                "turnover_component": round(t, 1),
                "turnover_source": (
                    "FALLBACK: DexScreener 24h volume in near-par "
                    f"pairs ({dex_pairs} pairs) — DEX trading volume, "
                    "NOT on-chain transfer volume. Used because the "
                    "feature-7 activity scan was unavailable."
                ),
            })
    else:
        ev["turnover_note"] = (
            "No daily transfer volume (feature 7) and no DexScreener "
            "24h volume — the turnover leg is unknown, not zero."
        )
    if not legs:
        ev["note"] = (
            "Liquidity unknown: neither the depth leg (DexScreener "
            "near-par liquidity) nor the turnover leg (daily transfer "
            "volume) could be measured."
        )
        return None, ev
    score = round(sum(legs.values()) / len(legs), 1)
    notes = []
    if "depth" in legs:
        notes.append(
            "DexScreener-tracked pairs on the primary chain only (at "
            "most 30 pairs per token) — a lower bound on tracked DEX "
            "liquidity, not total market depth; CEX depth is not covered."
        )
    if "turnover" in legs:
        if ev.get("daily_volume_basis") == "dexscreener_h24_fallback":
            notes.append(ev.get("turnover_source", ""))
        else:
            notes.append(
                "On-chain turnover is primary-chain only (feature 7); "
                f"daily-volume basis: {ev.get('daily_volume_basis')}."
            )
    ev["note"] = " ".join(notes)
    return score, ev


# ---------------------------------------------------------------- chain coverage
def _coverage_input(supply_map):
    """Material chains (>= 1% of total supply) -> fixed-table score."""
    if supply_map is None:
        return None, {"note": "Supply map unavailable — chain coverage unknown."}
    chains = supply_map.get("chains") or []
    total = supply_map.get("total_supply_usd")
    material = [
        c for c in chains
        if (c.get("share_of_total_pct") or 0) >= MATERIAL_SHARE_PCT
    ]
    n = len(material)
    if n == 0:
        return None, {
            "note": "No chain holds >= 1% of total supply — coverage unknown.",
            "chain_count": len(chains),
            "total_supply_usd": total,
        }
    score = _COVERAGE_TABLE.get(n, 100.0) if n < 7 else 100.0
    covered = round(sum(c.get("share_of_total_pct") or 0 for c in material), 2)
    return round(score, 1), {
        "material_chains": n,
        "material_threshold_pct": MATERIAL_SHARE_PCT,
        "chain_count": len(chains),
        "material_share_of_total_pct": covered,
        "top_chains": [
            {"chain": c.get("chain"), "supply_usd": c.get("supply_usd"),
             "share_of_total_pct": c.get("share_of_total_pct")}
            for c in material[:3]
        ],
        "total_supply_usd": total,
        "note": (
            f"{n} chain(s) hold >= {MATERIAL_SHARE_PCT}% of total supply "
            f"each ({covered}% combined). Score table: 1 chain -> 25, "
            "2 -> 45, 3 -> 60, 4 -> 75, 5 -> 85, 6 -> 92, 7+ -> 100."
        ),
    }


# ---------------------------------------------------------------- fees / finality
def _fee_component(fee_usd):
    """0-100 from a typical transfer fee in USD (log scale):
    $0.10 -> 70, $0.01 -> 95, $1 -> 45, $10 -> 20."""
    if fee_usd is None or fee_usd <= 0:
        return None
    return _clamp(70.0 - 25.0 * math.log10(fee_usd / 0.1))


def _finality_component(seconds):
    if seconds is None:
        return None
    if seconds <= 60:
        return 100.0
    if seconds <= 300:
        return 85.0
    if seconds <= 900:
        return 70.0
    if seconds <= 3600:
        return 50.0
    return 25.0


async def _eth_block_time():
    """Measured MEDIAN block interval (s) from Blockscout's latest
    blocks page (cached 60s). Median, not mean: Blockscout's page
    skips blocks at missed slots, so consecutive listed blocks are
    sometimes 24/48/72 s apart and the mean is unstable (observed
    12.0 s one minute, 24.0 s the next); the median over the full
    50-block page is robustly 12 s. None on failure."""
    key = "prBlockTime:ethereum"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    try:
        d = await bs("ethereum", "/blocks")
    except UpstreamError:
        return None
    items = (d or {}).get("items") or []
    ts = []
    for b in items:
        raw = b.get("timestamp")
        if not raw:
            continue
        try:
            ts.append(int(datetime.fromisoformat(
                str(raw).replace("Z", "+00:00")).timestamp()))
        except (ValueError, TypeError):
            continue
    if len(ts) < 2:
        return None
    import statistics

    diffs = [ts[i] - ts[i + 1] for i in range(len(ts) - 1)]
    diffs = [x for x in diffs if 0 < x <= 300]
    if not diffs:
        return None
    out = round(float(statistics.median(diffs)), 2)
    _cache_set(key, out, ttl=60)
    return out


async def _native_price_usd(gecko_id):
    """CoinGecko simple price (USD) for a native coin, cached 120s —
    the same price serves all 7 EVM coins, so the overview rollup's
    parallel per-coin calls share one fetch instead of seven."""
    key = f"prNativePrice:{gecko_id}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    v = None
    for attempt in range(2):
        px = await coingecko("/simple/price",
                             {"ids": gecko_id, "vs_currencies": "usd"})
        v = _num((px or {}).get(gecko_id, {}).get("usd"))
        if v is not None or attempt == 1:
            break
        await asyncio.sleep(1.5)
    if v is not None:
        _cache_set(key, v, ttl=120)
    return v


async def _eth_gas_price_gwei():
    """Live gas price (gwei) from the public Ethereum RPC, cached
    60s — gas moves slowly relative to a fee estimate, and the cache
    keeps the overview rollup's parallel calls from bursting the
    sandbox egress proxy (see AGENTS.md). One retry on a transport
    failure: the sandbox proxy stalls transiently under parallel
    bursts. None on failure."""
    key = "prGasPrice:ethereum"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    v = None
    for attempt in range(2):
        try:
            gp = await rpc("ethereum", "eth_gasPrice")
            v = int(gp, 16) / 1e9 if gp else None
            break
        except UpstreamError:
            if attempt == 0:
                await asyncio.sleep(1.5)
    if v is not None:
        _cache_set(key, v, ttl=60)
    return v


async def _tron_energy_price_sun():
    """Latest Tron energy unit price (sun) from TronGrid's
    getenergyprices history (\"ts:price,...\"), cached 120s. None on
    failure."""
    key = "prEnergyPrice:tron"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    prices = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0), headers=HEADERS) as c:
                r = await c.post("https://api.trongrid.io/wallet/getenergyprices")
            if r.status_code == 200:
                prices = (r.json() or {}).get("prices") or ""
                break
        except Exception:
            pass
        if attempt == 0:
            await asyncio.sleep(1.5)
    if not prices:
        return None
    best = None
    for part in str(prices).split(","):
        try:
            ts_s, price_s = part.split(":")
            ts, price = int(ts_s), int(price_s)
        except (ValueError, TypeError):
            continue
        if best is None or ts > best[0]:
            best = (ts, price)
    v = best[1] if best else None
    if v is not None:
        _cache_set(key, v, ttl=120)
    return v


async def _fees_input(cfg):
    """Fee + finality on the primary chain. Returns (score, evidence).
    Score None only when neither leg is measurable."""
    ev = {"primary_chain": cfg["primary"]}
    fee_usd = fee_native = gas_price = energy_price = None
    native_usd = None
    block_time_s = None
    finality_s = None
    if cfg["primary"] == "ethereum":
        # Live gas price + ETH price; fee = gas x 65k (ESTIMATE).
        gas_price = await _eth_gas_price_gwei()
        native_usd = await _native_price_usd("ethereum")
        if gas_price is not None and native_usd:
            fee_native = gas_price * 1e-9 * EVM_TRANSFER_GAS  # ETH
            fee_usd = fee_native * native_usd
        block_time_s = await _eth_block_time()
        if block_time_s:
            # Finality: 2 epochs = 64 slots x measured block time.
            finality_s = round(64 * block_time_s)
        ev.update({
            "fee_basis": "live gas price x 65,000 gas (ESTIMATE)",
            "fee_note": (
                "65,000 gas is a typical ERC-20 transfer allowance — "
                "an ESTIMATE, not a measurement: actual gas varies by "
                "token and wallet (roughly 45k-100k+)."
            ),
            "finality_basis": (
                "Protocol figure: Ethereum finality is 2 epochs "
                "(64 slots); seconds = 64 x measured block time."
            ),
        })
    elif cfg["primary"] == "tron":
        energy_price = await _tron_energy_price_sun()
        native_usd = await _native_price_usd("tron")
        if energy_price is not None and native_usd:
            # sun -> TRX (1 TRX = 1,000,000 sun).
            fee_native = TRON_TRANSFER_ENERGY * energy_price / 1e6
            fee_usd = fee_native * native_usd
        block_time_s = 3.0  # Tron protocol block time (labelled).
        finality_s = 57  # ~19 blocks to irreversibility (protocol figure).
        ev.update({
            "fee_basis": "live Tron energy price x 14,000 energy (ESTIMATE)",
            "fee_note": (
                "14,000 energy is a typical plain TRC20 transfer to "
                "an existing holder — an ESTIMATE: accounts with "
                "staked energy pay less (often zero) and new-account "
                "or complex transfers cost more."
            ),
            "finality_basis": (
                "Protocol figures: Tron block time 3 s; irreversibility "
                "after ~19 blocks (~1 min)."
            ),
        })
    else:
        ev["note"] = f"No fee/finality model for primary chain {cfg['primary']}."
        return None, ev
    ev.update({
        "native_symbol": "ETH" if cfg["primary"] == "ethereum" else "TRX",
        "native_price_usd": native_usd,
        "gas_price_gwei": gas_price,
        "energy_price_sun": energy_price,
        "typical_transfer_fee_native": fee_native,
        "typical_transfer_fee_usd": round(fee_usd, 4) if fee_usd is not None else None,
        "block_time_seconds": block_time_s,
        "finality_seconds": finality_s,
    })
    fee_c = _fee_component(fee_usd)
    fin_c = _finality_component(finality_s)
    ev["fee_component"] = round(fee_c, 1) if fee_c is not None else None
    ev["finality_component"] = fin_c
    if fee_c is not None and fin_c is not None:
        score = round(0.7 * fee_c + 0.3 * fin_c, 1)
    elif fin_c is not None:
        score = round(fin_c, 1)
        ev["note"] = (
            "Fee unknown (gas/energy price or native-coin price "
            "unavailable) — scored on finality alone."
        )
    elif fee_c is not None:
        score = round(fee_c, 1)
        ev["note"] = "Finality unknown — scored on the fee alone."
    else:
        ev["note"] = (
            "Neither the transfer fee nor finality could be measured "
            "— fees/finality unknown."
        )
        return None, ev
    return score, ev


# ---------------------------------------------------------------- freeze risk
async def _evm_freeze_probe(cfg):
    """eth_call probes for freeze/blacklist/pause functions on the
    primary-chain contract. A revert (400) or empty return means the
    function is absent; a transport failure raises UpstreamError and
    makes the input unknown. Returns (score, evidence)."""
    present_freeze = []
    present_pause = False
    transport_failed = False

    async def _probe(data):
        """One eth_call probe with a single retry on transport
        failure (the sandbox proxy stalls transiently under parallel
        bursts). Returns ("ok", result) / ("absent", None) /
        ("transport_failed", None). A revert (400) is "absent"."""
        for attempt in range(2):
            try:
                res = await rpc("ethereum", "eth_call",
                                [{"to": cfg["contract"], "data": data}, "latest"])
                if not res or res == "0x":
                    return "absent", None
                return "ok", res
            except UpstreamError as e:
                if e.status == 400:
                    return "absent", None
                if attempt == 0:
                    await asyncio.sleep(1.5)
        return "transport_failed", None

    for fn in _FREEZE_FNS:
        sel = _function_selector(fn, ["address"])
        data = "0x" + (sel + _encode_args(["address"], [ZERO_ADDRESS])).hex()
        status, res = await _probe(data)
        if status == "transport_failed":
            transport_failed = True
            continue
        if status != "ok":
            continue
        try:
            _decode_args(["bool"], bytes.fromhex(res[2:]))
        except Exception:
            continue
        present_freeze.append(fn)
    # paused(): no-arg read.
    sel = _function_selector("paused", [])
    status, res = await _probe("0x" + sel.hex())
    if status == "transport_failed":
        transport_failed = True
    elif status == "ok":
        try:
            _decode_args(["bool"], bytes.fromhex(res[2:]))
            present_pause = True
        except Exception:
            pass
    ev = {
        "method": "live eth_call probes on the primary-chain contract",
        "freeze_functions_found": present_freeze,
        "pause_function_found": present_pause,
        "probed_functions": _FREEZE_FNS + ["paused()"],
        "freeze_events": "tracked_in_freeze",
        "freeze_events_note": (
            "Observed freeze/blacklist events ARE tracked now "
            "(feature 10, /freeze) — this input still scores issuer "
            "CAPABILITY, not observed incidents."
        ),
    }
    if transport_failed and not present_freeze and not present_pause:
        ev["note"] = (
            "The primary-chain RPC failed during the freeze probe — "
            "freeze risk is unknown, not assumed absent."
        )
        return None, ev
    if present_freeze:
        return 40.0, {**ev, "note": (
            f"Issuer CAN freeze/blacklist holder balances "
            f"({', '.join(present_freeze)} present on-chain) — "
            "scores 40/100. Whether freezes have actually happened "
            "is tracked in /freeze (feature 10)."
        )}
    if present_pause:
        return 70.0, {**ev, "note": (
            "No per-holder freeze/blacklist function, but the issuer "
            "can pause all transfers (paused() present) — scores "
            "70/100."
        )}
    return 100.0, {**ev, "note": (
        "No freeze/blacklist or pause function found on the "
        "primary-chain contract — the issuer cannot freeze holder "
        "balances on-chain."
    )}


async def _tron_freeze_probe(cfg):
    """USDD: no keyless ABI endpoint exists for Tron, so the check
    uses TronScan's published method map (observed functions). The
    map is partial — labelled as such. rely/deny are admin
    authorisation functions (DAI-style), not holder freezes."""
    try:
        d = await _tronscan("/contract", {"contract": cfg["contract"]})
    except UpstreamError:
        return None, {
            "method": "TronScan method map",
            "freeze_events": "not_tracked",
            "note": "TronScan contract lookup failed — freeze risk unknown.",
        }
    data = ((d or {}).get("data") or [{}])[0]
    mm = data.get("methodMap") or {}
    names = [str(v).split("(")[0] for v in mm.values()]
    hits = [n for n in names if any(k in n.lower() for k in _TRON_FREEZE_NAMES)]
    ev = {
        "method": "TronScan published method map (observed functions)",
        "functions_observed": sorted(names),
        "freeze_functions_found": hits,
        "pause_function_found": any("pause" in n.lower() for n in names),
        "admin_functions_noted": [
            n for n in names if n in ("rely", "deny")
        ],
        "freeze_events": "tracked_in_freeze",
        "freeze_events_note": (
            "Observed freeze/blacklist events ARE tracked now "
            "(feature 10, /freeze) — this input scores issuer "
            "CAPABILITY, not observed incidents."
        ),
    }
    if hits:
        return 40.0, {**ev, "note": (
            f"Method map shows freeze/pause capability ({', '.join(hits)}) "
            "— scores 40/100."
        )}
    return 100.0, {**ev, "note": (
        "No freeze/blacklist/pause function in TronScan's published "
        "method map (11 observed functions — PARTIAL list, built from "
        "observed transactions, so rarely-called functions may be "
        "missing). rely/deny are admin authorisation functions "
        "(DAI-style wards), not holder freezes."
    )}


async def _freeze_input(cfg):
    if cfg["primary"] == "tron":
        return await _tron_freeze_probe(cfg)
    return await _evm_freeze_probe(cfg)


# ---------------------------------------------------------------- entry
async def sc_payments_readiness(scope):
    """GET /api/<scope>/payments-readiness — feature 8: one 0-100
    payments-readiness score per coin, with all five input sub-scores
    and their evidence. Cached 300s."""
    cfg = _cfg(scope)
    cache_key = f"scPaymentsReadiness:{scope}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    # Depeg score (peg leg), supply map (coverage leg) and activity
    # (liquidity turnover leg) run in parallel; the freeze probe and
    # fee/finality legs run alongside them.
    depeg, sup_map, activity, fees, freeze = await asyncio.gather(
        sc_depeg_score(scope),
        sc_supply_map(scope),
        sc_activity(scope),
        _fees_input(cfg),
        _freeze_input(cfg),
        return_exceptions=True,
    )
    if isinstance(depeg, Exception):
        depeg = None
    if isinstance(sup_map, Exception):
        sup_map = None
    activity_data = None if isinstance(activity, Exception) else activity
    if isinstance(fees, Exception):
        fees = (None, {"note": f"Fees/finality probe failed: {fees}"})
    if isinstance(freeze, Exception):
        freeze = (None, {"note": f"Freeze probe failed: {freeze}"})
    fees_score, fees_ev = fees
    freeze_score, freeze_ev = freeze

    peg_score, peg_ev = _peg_input(depeg)
    cov_score, cov_ev = _coverage_input(sup_map)

    # Liquidity: DexScreener depth + feature-7 daily volume, with a
    # DexScreener-h24 fallback for the turnover leg.
    supply_usd = None
    if depeg is not None:
        supply_usd = depeg.get("supply_usd")
    near_liq, _total_liq, near_pairs, ds_err, _ds_meta = await _dexscreener_near_par(cfg)
    if ds_err:
        near_liq, near_pairs = None, []
    daily_vol = daily_basis = None
    if activity_data is not None:
        daily = activity_data.get("daily") or {}
        daily_vol = daily.get("volume_per_day_usd")
        daily_basis = daily.get("basis")
    dex_vol24 = dex_pairs = None
    if daily_vol is None:
        dex_vol24, dex_pairs = await _dex_near_par_volume(cfg)
    liq_score, liq_ev = _liquidity_input(
        near_liq, near_pairs, supply_usd, daily_vol, daily_basis,
        dex_vol24, dex_pairs,
    )
    if ds_err and liq_score is None:
        liq_ev["dexscreener_error"] = ds_err

    inputs = {
        "peg_stability": {
            "weight": WEIGHTS["peg_stability"],
            "score": peg_score,
            "evidence": peg_ev,
        },
        "liquidity": {
            "weight": WEIGHTS["liquidity"],
            "score": liq_score,
            "evidence": liq_ev,
        },
        "chain_coverage": {
            "weight": WEIGHTS["chain_coverage"],
            "score": cov_score,
            "evidence": cov_ev,
        },
        "fees_finality": {
            "weight": WEIGHTS["fees_finality"],
            "score": fees_score,
            "evidence": fees_ev,
        },
        "freeze_risk": {
            "weight": WEIGHTS["freeze_risk"],
            "score": freeze_score,
            "evidence": freeze_ev,
        },
    }
    known = {k: v for k, v in inputs.items() if v["score"] is not None}
    unknown = [k for k, v in inputs.items() if v["score"] is None]
    if known:
        wsum = sum(v["weight"] for v in known.values())
        score = round(
            sum(v["score"] * v["weight"] for v in known.values()) / wsum, 1
        )
        weights_used = {k: round(v["weight"] / wsum, 3) for k, v in known.items()}
    else:
        score = None
        weights_used = {}
    out = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "score": score,
        "band": _band(score),
        "weights": dict(WEIGHTS),
        "weights_used": weights_used,
        "inputs": inputs,
        "inputs_unknown": unknown,
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "supply_usd": supply_usd,
        "methodology": METHODOLOGY,
        "as_of": int(time.time()),
        "sources": [
            "DeFiLlama stablecoins API (price, supply, depeg history)",
            "DexScreener keyless API (near-par liquidity, 24h volume fallback)",
            "Feature-7 on-chain activity (daily transfer volume, primary chain)",
            "Feature-5 supply map (chain coverage)",
            "Public Ethereum RPC + Blockscout (gas price, block time)",
            "TronGrid keyless API (Tron energy price)",
            "CoinGecko keyless API (native-coin USD prices)",
            "Live eth_call probes on the primary-chain contract (freeze/blacklist/pause) / TronScan method map (USDD)",
        ],
    }
    _cache_set(cache_key, out, ttl=CACHE_TTL)
    return out
