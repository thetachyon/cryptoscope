"""Quote & route comparison for WalletScope (phase 2, P1).

Ranked swap/bridge quotes across the chains WalletScope serves. All
keyless, zero new dependencies.

Sources (verified from this sandbox 2026-11-06 — see notes per source):
- LI.FI `POST /v1/advanced/routes` — multiple ranked routes, same-chain
  swaps and cross-chain bridges, EVM chains only. No API key required
  (key only raises the limit); keyless limit is 75 quote/route requests
  per 2h rolling window PER IP, and the sandbox egress IP is shared, so
  the budget is often already spent — expect HTTP 429/1005 here and
  surface it honestly instead of inventing a quote. `GET /v1/routes`
  does NOT exist (1003). LI.FI's own /v1/chains lists 65 chains, all
  EVM: no Solana, no Bitcoin — so no LI.FI routes for those chains.
- Circle CCTP (native burn-and-mint) for USDC — NOT an API quote: a
  computed estimate route (1:1, no bridge fee, gas on both legs,
  ~13-19 min for the attestation) for USDC -> USDC on CCTP chains
  (Ethereum, Optimism, Solana). Labelled estimate throughout.
- Gas: the chain's public RPC `eth_gasPrice` (common.clients.rpc) x a
  typical gas budget, priced with the native coin's live USD price
  (common.pricing.native_quote). Spot prices for the rate-vs-spot
  figure: DeFiLlama `coins.llama.fi/prices/current` (keyless, verified
  live from the sandbox).

NOT used (verified unusable from this sandbox, do not re-add without
re-probing):
- Swap API (swapapi.dev): every path probed returns 404 "Application
  not found" — no working API at that host.
- OpenOcean (open-api.openocean.finance / api.openocean.finance):
  Cloudflare bot challenge / empty responses from the sandbox.
- 1inch: API key required. Jupiter: unreachable from the sandbox
  (established earlier; not used).

Token list: curated from contracts verified live against Blockscout
(Ethereum/Optimism) or on-chain eth_call symbol()/decimals()
(Gnosis) on 2026-11-06. Gnosis USDC/USDT/DAI/GNO could NOT be
verified from the sandbox (Gnosis Blockscout 403s / truncated reads
and the public RPC rejected those calls) — they are deliberately
absent, not forgotten. Sepolia carries native ETH only (testnet).
Solana mints are the canonical USDC/USDT mints; they are listed so
the UI can offer them, but no keyless source quotes Solana routes
from this server, so those requests honestly return zero routes.
"""
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from common.chains import WALLET_CHAINS
from common.clients import (
    HEADERS,
    TIMEOUT,
    _cache_get,
    _cache_set,
    rpc,
)
from common.pricing import native_quote

QUOTE_TTL = 30  # seconds — quotes go stale fast; also conserves the
# shared LI.FI keyless budget (75 requests / 2h per egress IP).

LIFI_ROUTES_URL = "https://li.quest/v1/advanced/routes"
LLAMA_PRICES_URL = "https://coins.llama.fi/prices/current"

# LI.FI chain ids (from GET https://li.quest/v1/chains, 2026-11-06).
# Solana and Bitcoin are NOT in that list, so they have no entry here.
LIFI_CHAIN_IDS = {
    "ethereum": 1,
    "optimism": 10,
    "gnosis": 100,
    "sepolia": 11155111,
}

# CCTP domain ids (Circle). Gnosis is NOT a CCTP chain (unverified /
# not listed by Circle for CCTP) — do not add it without verification.
CCTP_DOMAINS = {"ethereum": 0, "optimism": 2, "solana": 5}

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
NATIVE_ADDRESS = "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"

# LI.FI rejects the zero address as fromAddress, so a quote without a
# user-supplied address uses the canonical burn address as a placeholder.
# It is only a routing parameter for the price quote — nothing is ever
# signed, sent, or checked against it — and the UI lets the user supply
# their own address instead.
DEFAULT_QUOTE_ADDRESS = "0x000000000000000000000000000000000000dEaD"


def _plain(d):
    """Decimal -> plain string, no scientific notation (100, not 1E+2)."""
    return format(d.normalize(), "f")

# ---------------------------------------------------------------- tokens
# address: None = native coin (LI.FI wants the 0xEeee... sentinel for
# native on EVM; Solana native is the wrapped-SOL mint for pricing
# only — quotes never reach LI.FI for Solana anyway).
# Every contract below was verified live 2026-11-06 (Blockscout token
# lookup for Ethereum/Optimism, eth_call symbol()/decimals() for
# Gnosis) except the Solana mints, which are the canonical well-known
# mint addresses. Optimism DAI (0xDA10009c...) returned 422 from
# Blockscout and is excluded; Optimism WETH is the 0x...0006 predeploy
# (0x...0042 is the OP token, not WETH).
QUOTE_TOKENS = {
    "ethereum": [
        {"symbol": "ETH", "name": "Ethereum", "address": None, "decimals": 18},
        {"symbol": "USDC", "name": "USD Coin", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6},
        {"symbol": "USDT", "name": "Tether USD", "address": "0xdAC17F958D2ee523a2206206994597C13D831ec7", "decimals": 6},
        {"symbol": "DAI", "name": "Dai", "address": "0x6B175474E89094C44Da98b954EedeAC495271d0F", "decimals": 18},
        {"symbol": "WETH", "name": "Wrapped Ether", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "decimals": 18},
        {"symbol": "WBTC", "name": "Wrapped Bitcoin", "address": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599", "decimals": 8},
    ],
    "optimism": [
        {"symbol": "ETH", "name": "Ethereum", "address": None, "decimals": 18},
        {"symbol": "USDC", "name": "USD Coin", "address": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "decimals": 6},
        {"symbol": "USDT", "name": "Bridged USDT", "address": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58", "decimals": 6},
        {"symbol": "WETH", "name": "Wrapped Ether", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
        {"symbol": "OP", "name": "Optimism", "address": "0x4200000000000000000000000000000000000042", "decimals": 18},
    ],
    "gnosis": [
        {"symbol": "xDAI", "name": "xDAI", "address": None, "decimals": 18},
        {"symbol": "WETH", "name": "Wrapped Ether", "address": "0x6A023CCd1ff6F2045C3309768eAd9E68F978f6e1", "decimals": 18},
        {"symbol": "wxDAI", "name": "Wrapped xDAI", "address": "0xe91D153E0b41518A2Ce8Dd3D7944Fa863463a97d", "decimals": 18},
    ],
    "sepolia": [
        {"symbol": "ETH", "name": "Ethereum", "address": None, "decimals": 18},
    ],
    "solana": [
        {"symbol": "SOL", "name": "Solana", "address": None, "decimals": 9},
        {"symbol": "USDC", "name": "USD Coin", "address": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "decimals": 6},
        {"symbol": "USDT", "name": "Tether USD", "address": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "decimals": 6},
    ],
    "bitcoin": [
        {"symbol": "BTC", "name": "Bitcoin", "address": None, "decimals": 8},
    ],
}


def quote_tokens_payload():
    """Token list for the quote UI, derived from the registry above."""
    chains = {}
    for cid, toks in QUOTE_TOKENS.items():
        c = WALLET_CHAINS.get(cid, {})
        chains[cid] = {
            "label": c.get("label", cid),
            "native": c.get("native"),
            "tokens": [
                {
                    "symbol": t["symbol"],
                    "name": t["name"],
                    "address": t["address"],
                    "decimals": t["decimals"],
                    "native": t["address"] is None,
                }
                for t in toks
            ],
        }
    return {
        "chains": chains,
        "lifi_chains": sorted(LIFI_CHAIN_IDS.keys()),
        "cctp_chains": sorted(CCTP_DOMAINS.keys()),
        "notes": [
            "LI.FI quotes cover EVM chains only (Ethereum, Optimism, Gnosis, Sepolia).",
            "Solana and Bitcoin have no keyless quote source reachable from this server — requests involving them return zero routes.",
            "CCTP is an estimate route for USDC -> USDC only (native burn-and-mint, no bridge fee).",
            "Gnosis stablecoins are absent: their contracts could not be verified from this server (Gnosis Blockscout unreachable/flaky).",
        ],
    }


def resolve_token(chain, ident):
    """Token dict for a chain by symbol (case-insensitive) or address/mint,
    or None. Native coins also resolve by their symbol (ETH/xDAI/SOL/BTC)."""
    if not ident:
        return None
    ident = str(ident).strip()
    for t in QUOTE_TOKENS.get(chain, []):
        if t["symbol"].lower() == ident.lower():
            return t
        if t["address"] and t["address"].lower() == ident.lower():
            return t
    return None


def _human(raw, decimals):
    try:
        return format(Decimal(str(raw)) / (Decimal(10) ** decimals), "f")
    except (InvalidOperation, ValueError, TypeError):
        return None


def _to_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _fmt_seconds(s):
    if s is None:
        return None
    s = int(s)
    if s < 60:
        return f"~{s}s"
    if s < 3600:
        return f"~{s // 60} min"
    return f"~{s // 3600}h {(s % 3600) // 60}m"


# ---------------------------------------------------------------- pricing
async def _llama_price(chain, token):
    """Spot USD price for one token via DeFiLlama (keyless). None on any
    failure — callers treat a missing spot price as 'unknown', never as 0."""
    if chain == "bitcoin":
        key = "coingecko:bitcoin"
    elif token["address"] is None:
        cg = WALLET_CHAINS[chain].get("coingecko_coin")
        key = f"coingecko:{cg}" if cg else None
    else:
        key = f"{chain}:{token['address']}"
    if not key:
        return None
    ck = f"llamaprice:{key}"
    hit = _cache_get(ck)
    if hit is not None:
        return hit
    price = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(LLAMA_PRICES_URL + "/" + key)
        if r.status_code == 200:
            d = r.json()
            entry = (d.get("coins") or {}).get(key) or {}
            price = _to_float(entry.get("price"))
    except Exception:
        price = None
    _cache_set(ck, price, ttl=60)
    return price


async def _gas_usd(chain, gas_units):
    """Estimated gas cost in USD for `gas_units` on `chain`, from the
    live eth_gasPrice x the native coin's live USD price. (None, note)
    on any failure. Solana/Bitcoin have no eth_gasPrice — caller skips
    them (their fees are sub-cent and stated as such)."""
    if chain not in ("ethereum", "optimism", "gnosis", "sepolia"):
        return None, "no EVM gas price for this chain"
    try:
        gp = await rpc(chain, "eth_gasPrice")
        gas_price_wei = int(gp, 16)
    except Exception as e:
        return None, f"gas price unavailable ({type(e).__name__})"
    native_usd, _ = await native_quote(chain)
    if native_usd is None:
        return None, "native coin USD price unavailable"
    cost = Decimal(gas_price_wei) * Decimal(gas_units) / Decimal(10) ** 18 * Decimal(str(native_usd))
    return float(cost), None


# ---------------------------------------------------------------- LI.FI
def _lifi_token_address(token):
    return NATIVE_ADDRESS if token["address"] is None else token["address"]


def _lifi_route_to_route(r, from_tok, to_tok):
    """Normalise one LI.FI advanced/routes object to the common route shape.

    Shape (verified live 2026-11-06): route-level `toAmount` /
    `toAmountMin` / `toAmountUSD` / `gasCostUSD`, and per-step
    `estimate` objects carrying `feeCosts` (amountUSD, `included` —
    included fees are already deducted from toAmount), `gasCosts` and
    `executionDuration` (seconds). Amounts are human-readable strings;
    USD figures are floats or None. Gas is paid on top in the source
    chain's native coin, so the ranking net is amount_out_usd minus
    gas — the distinction is stated in the fees note."""
    to_dec = to_tok["decimals"]
    amount_out = _human(r.get("toAmount"), to_dec)
    amount_out_min = _human(r.get("toAmountMin"), to_dec)
    amount_out_usd = _to_float(r.get("toAmountUSD"))
    gas_usd = _to_float(r.get("gasCostUSD"))
    out_fee_usd = 0.0
    fee_detail = []
    duration = 0
    steps = []
    for s in r.get("steps") or []:
        est = s.get("estimate") or {}
        for f in est.get("feeCosts") or []:
            v = _to_float(f.get("amountUSD"))
            if v is not None:
                out_fee_usd += v
            fee_detail.append({
                "name": f.get("name") or "fee",
                "amount_usd": v,
                "token": ((f.get("token") or {}).get("symbol")),
            })
        if gas_usd is None:
            for g in est.get("gasCosts") or []:
                v = _to_float(g.get("amountUSD"))
                if v is not None:
                    gas_usd = (gas_usd or 0.0) + v
        d = est.get("executionDuration")
        if d is not None:
            try:
                duration += int(d)
            except (TypeError, ValueError):
                pass
        a = s.get("action") or {}
        f = a.get("fromToken") or {}
        t = a.get("toToken") or {}
        tool = ((s.get("toolDetails") or {}).get("name")) or s.get("tool") or "LI.FI"
        same = a.get("fromChainId") == a.get("toChainId")
        steps.append(
            f"{f.get('symbol', '?')} -> {t.get('symbol', '?')} "
            f"({'same chain' if same else 'cross-chain'}) via {tool}"
        )
    first = (r.get("steps") or [{}])[0]
    via = ((first.get("toolDetails") or {}).get("name")) or first.get("tool") or "LI.FI"
    same_chain = r.get("fromChainId") == r.get("toChainId")
    # Spender(s) for the P4 allowance checker: LI.FI puts the contract
    # to approve in each step's estimate.approvalAddress (docs.li.fi:
    # "estimate.approvalAddress — the contract address to approve for
    # token spending"). The route-level spender is the first step's —
    # the one the source-chain approval must target. A step with no
    # approvalAddress needs no source-chain approval (native input).
    spenders = []
    for i, s in enumerate(r.get("steps") or []):
        ap = (s.get("estimate") or {}).get("approvalAddress")
        if ap:
            spenders.append({
                "step": i + 1,
                "tool": ((s.get("toolDetails") or {}).get("name")) or s.get("tool"),
                "approval_address": ap,
            })
    # Same-chain swaps carry no executionDuration in LI.FI's estimate —
    # one block is the honest fallback (labelled as such).
    block_fallback = False
    if not duration and same_chain:
        duration = {"ethereum": 12, "sepolia": 12, "optimism": 2, "gnosis": 5}.get(
            next((c for c, i in LIFI_CHAIN_IDS.items() if i == r.get("fromChainId")), None)
        )
        block_fallback = duration is not None
    time_label = _fmt_seconds(duration) if duration else None
    if block_fallback:
        time_label += " (1 block, est.)"
    return {
        "source": "LI.FI",
        "kind": "swap" if same_chain else "bridge",
        "via": via,
        "steps": steps,
        "amount_out": amount_out,
        "amount_out_min": amount_out_min,
        "amount_out_usd": amount_out_usd,
        "fees": {
            "gas_usd": round(gas_usd, 4) if gas_usd else 0.0,
            "output_fee_usd": round(out_fee_usd, 4) if out_fee_usd else 0.0,
            "output_fee_detail": fee_detail,
            "note": "Output fee is already deducted from the amount out; gas is paid on top in the source chain's native coin.",
        },
        "net_usd": (amount_out_usd - (gas_usd or 0.0)) if amount_out_usd is not None else None,
        "time_seconds_est": duration or None,
        "time_label": time_label,
        "minimum_received": amount_out_min,
        "spender": spenders[0]["approval_address"] if spenders else None,
        "spenders": spenders,
    }


async def _lifi_routes(from_chain, from_tok, to_chain, to_tok, amount_raw, from_address):
    """POST /v1/advanced/routes. Returns (routes, error). `error` is a
    human-readable string (incl. LI.FI's rate-limit message) or None."""
    body = {
        "fromChainId": LIFI_CHAIN_IDS[from_chain],
        "toChainId": LIFI_CHAIN_IDS[to_chain],
        "fromTokenAddress": _lifi_token_address(from_tok),
        "toTokenAddress": _lifi_token_address(to_tok),
        "fromAmount": str(amount_raw),
        "fromAddress": from_address or DEFAULT_QUOTE_ADDRESS,
        "options": {"allowSwitchChain": False},
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0), headers=HEADERS, follow_redirects=True) as c:
            r = await c.post(LIFI_ROUTES_URL, json=body)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        return [], f"LI.FI unreachable ({type(e).__name__})"
    try:
        d = r.json()
    except Exception:
        return [], f"LI.FI returned invalid JSON (HTTP {r.status_code})"
    if r.status_code == 429 or (isinstance(d, dict) and d.get("code") == 1005):
        msg = (d.get("message") or "Rate limit exceeded") if isinstance(d, dict) else "Rate limit exceeded"
        return [], f"LI.FI rate limit (keyless, 75 requests / 2h per shared IP): {msg}"
    if r.status_code >= 400:
        msg = d.get("message") if isinstance(d, dict) else None
        return [], f"LI.FI returned HTTP {r.status_code}" + (f": {msg}" if msg else "")
    routes = []
    for raw in (d.get("routes") or []):
        try:
            routes.append(_lifi_route_to_route(raw, from_tok, to_tok))
        except Exception:
            continue
    return routes, None


# ---------------------------------------------------------------- CCTP
async def _cctp_route(from_chain, to_chain, amount_human, amount_usd):
    """Computed CCTP estimate route for USDC -> USDC. 1:1 (no bridge
    fee, no slippage), gas on both legs, ~13-19 min for Circle's
    attestation. This is an ESTIMATE, not a live quote — no API returns
    CCTP pricing; the figures are the protocol's known mechanics."""
    # Burn on source (~200k gas est.), mint on destination (~150k gas
    # est., paid by whoever submits the mint). Solana legs use a flat
    # ~$0.01 placeholder only for the fee line — stated as such.
    burn_gas, burn_note = await _gas_usd(from_chain, 200_000)
    mint_gas, mint_note = await _gas_usd(to_chain, 150_000)
    gas_total = 0.0
    gas_notes = []
    if burn_gas is not None:
        gas_total += burn_gas
    elif burn_note:
        gas_notes.append(f"source burn gas: {burn_note}")
    if mint_gas is not None:
        gas_total += mint_gas
    elif mint_note:
        gas_notes.append(f"destination mint gas: {mint_note}")
    if to_chain == "solana" or from_chain == "solana":
        gas_notes.append("Solana leg fee is sub-cent and not included in the gas total.")
    return {
        "source": "Circle CCTP (estimate)",
        "kind": "cctp",
        "via": "Circle CCTP (native burn-and-mint)",
        "steps": [
            f"Burn {amount_human} USDC on {WALLET_CHAINS[from_chain]['label']} (TokenMessenger)",
            f"Circle attests the burn (~13-19 min)",
            f"Mint {amount_human} USDC on {WALLET_CHAINS[to_chain]['label']} (MessageTransmitter)",
        ],
        "amount_out": amount_human,
        "amount_out_min": amount_human,
        "amount_out_usd": amount_usd,
        "fees": {
            "gas_usd": round(gas_total, 4),
            "output_fee_usd": 0.0,
            "output_fee_detail": [],
            "note": "No bridge fee and no slippage by construction (1:1 burn/mint). Gas is an estimate from the live gas price x a typical gas budget (200k burn + 150k mint)."
            + (f" {' '.join(gas_notes)}" if gas_notes else ""),
        },
        "net_usd": (amount_usd - gas_total) if amount_usd is not None else None,
        "time_seconds_est": 780,
        "time_label": "~13-19 min",
        "minimum_received": amount_human,
        "spender": None,
        "spenders": [],
        "approval_note": "CCTP burn requires approving Circle's TokenMessenger contract first — its address is not carried by any keyless quote source, so check it ad-hoc on the allowance page (#/allowance) with the messenger address for your chain.",
    }


# ---------------------------------------------------------------- entry
async def get_quote(from_chain, from_token_id, to_chain, to_token_id, amount_human, from_address=None):
    """Ranked routes for one transfer. Returns the response payload dict
    (never raises for upstream failures — those land in `sources`)."""
    quoted_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    from_tok = resolve_token(from_chain, from_token_id)
    to_tok = resolve_token(to_chain, to_token_id)
    try:
        amount = Decimal(str(amount_human))
    except (InvalidOperation, ValueError, TypeError):
        amount = None
    if from_tok is None or to_tok is None or amount is None or amount <= 0:
        return {"error": "Invalid amount or unknown token for the given chain", "quoted_at": quoted_at}
    amount_raw = int(amount * (Decimal(10) ** from_tok["decimals"]))

    cache_key = (
        f"quote:{from_chain}:{from_tok['symbol']}:{to_chain}:{to_tok['symbol']}:"
        f"{amount_raw}:{from_address or '-'}"
    )
    hit = _cache_get(cache_key)
    if hit is not None:
        return hit

    routes = []
    sources = {}
    notes = []

    # LI.FI: EVM chains only, same-chain swaps and cross-chain bridges.
    if from_chain in LIFI_CHAIN_IDS and to_chain in LIFI_CHAIN_IDS:
        lifi_routes, lifi_err = await _lifi_routes(
            from_chain, from_tok, to_chain, to_tok, amount_raw, from_address
        )
        if lifi_err:
            sources["LI.FI"] = {"status": "error", "detail": lifi_err}
        else:
            sources["LI.FI"] = {"status": "ok", "routes": len(lifi_routes)}
            routes.extend(lifi_routes)
    else:
        sources["LI.FI"] = {
            "status": "unavailable",
            "detail": "LI.FI covers EVM chains only (no Solana / Bitcoin in its chain list).",
        }

    # Spot prices for the rate-vs-spot figure (best effort).
    from_price = await _llama_price(from_chain, from_tok)
    to_price = await _llama_price(to_chain, to_tok)
    amount_usd = float(amount * Decimal(str(from_price))) if from_price else None
    spot_rate = (from_price / to_price) if (from_price and to_price) else None

    # CCTP estimate route for USDC -> USDC across CCTP chains.
    # No spender is carried: the burn requires approving Circle's
    # TokenMessenger, but no keyless source returns its address here —
    # the allowance page takes the messenger address directly.
    if (
        from_tok["symbol"] == "USDC" and to_tok["symbol"] == "USDC"
        and from_chain in CCTP_DOMAINS and to_chain in CCTP_DOMAINS
        and from_chain != to_chain
    ):
        cctp = await _cctp_route(from_chain, to_chain, _plain(amount), amount_usd)
        routes.append(cctp)
        sources["Circle CCTP (estimate)"] = {"status": "ok", "routes": 1}

    # Rate vs spot + ranking (net USD received after gas, best first).
    for r in routes:
        if spot_rate and r.get("amount_out") is not None:
            try:
                implied = Decimal(r["amount_out"]) / amount
                r["vs_spot_pct"] = round(float((implied / Decimal(str(spot_rate)) - 1) * 100), 3)
            except (InvalidOperation, ValueError, ZeroDivisionError):
                r["vs_spot_pct"] = None
        else:
            r["vs_spot_pct"] = None
    routes.sort(key=lambda r: (r.get("net_usd") is None, -(r.get("net_usd") or 0)))
    for i, r in enumerate(routes, 1):
        r["rank"] = i

    if not routes:
        notes.append(
            "No routes returned. "
            + (
                "LI.FI was rate-limited or unreachable — retry in a few minutes; quotes are cached 30s."
                if sources.get("LI.FI", {}).get("status") == "error"
                else "No keyless quote source covers this pair from this server."
            )
        )
    if spot_rate is None:
        notes.append("Spot price unavailable, so the rate-vs-spot figure is omitted (not estimated).")

    payload = {
        "from": {"chain": from_chain, "token": from_tok["symbol"], "amount": _plain(amount), "amount_raw": str(amount_raw), "amount_usd": amount_usd},
        "to": {"chain": to_chain, "token": to_tok["symbol"]},
        "quoted_at": quoted_at,
        "cache_ttl_seconds": QUOTE_TTL,
        "routes": routes,
        "sources": sources,
        "notes": notes,
        "disclaimer": "Estimates only, not executable quotes: amounts, fees and times move with the market and the quote expires quickly. No transaction is created or signed here.",
    }
    _cache_set(cache_key, payload, ttl=QUOTE_TTL)
    return payload
