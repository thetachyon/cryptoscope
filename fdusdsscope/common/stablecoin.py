"""Stablecoin dashboards for the *Scope apps — peg, supply, holders,
transfers, depeg history, reserves.

Sources (all probed 2026-11-05 from this sandbox, all keyless):
- DeFiLlama stablecoins https://stablecoins.llama.fi/stablecoins?includePrices=true
  (list of peggedAssets: numeric id, symbol, gecko_id, pegType,
  pegMechanism, circulating.peggedUSD, chainCirculating).
- DeFiLlama https://stablecoins.llama.fi/stablecoin/{numeric-id}
  (description, address, auditLinks, price, currentChainBalances
  {chain: {peggedUSD}}, and — despite the name — `tokens`, a daily
  total-supply history [{date, circulating: {peggedUSD}, ...}]).
- DeFiLlama https://stablecoins.llama.fi/stablecoinprices (list of
  {date, prices: {gecko_id: price}}, ~2132 daily entries; tether in all
  of them, ethena-usde in ~1052). /stablecoincharts/* 404s — not used.
- CoinGecko keyless /coins/{gecko_id} (price / market cap / supply
  fallback + homepage/whitepaper links).
- Blockscout v2 (eth.blockscout.com) /tokens/{addr},
  /tokens/{addr}/holders, /tokens/{addr}/transfers for the Ethereum
  primary chain of USDT/USDC/DAI/USDe/FDUSD/PYUSD/TUSD.
- TronScan keyless https://apilist.tronscan.org/api/token_trc20
  (token info), /token_trc20/holders?contract_address=...,
  /token_trc20/transfers?contract_address=... for USDD (Tron-native;
  Blockscout does not cover Tron). NOTE: the holders/transfers
  endpoints need the `contract_address=` parameter name — `contract=`
  is silently ignored (transfers) or 400s (holders). The holders list
  is capped at 10,000 by TronScan.

USDD contract (verified): TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz — confirmed
a live contract via api.trongrid.io /wallet/getcontract AND TronScan
token info (symbol USDD, "Decentralized USD"). The address
TPYmHEhy5nXmKtc5f6KgQVPSMURkCPSoW9 is NOT a contract (empty
getcontract response) — do not use it. TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t
is USDT on Tron, not USDD.

Reserves: no keyless machine-readable API exposes live reserve /
collateral figures for any of these coins (probed: Ethena app API 307,
api.ethena.fi 403, daistats.com/oasis/makerdao API hosts unreachable or
404). The /reserves endpoint therefore returns issuer attestation /
transparency links plus market figures (market cap, supply) clearly
labelled as market figures — never invented reserve numbers.
"""
import asyncio
import calendar
import math
import statistics
import time
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

from common.chains import CHAINS
from common.clients import (
    HEADERS,
    UpstreamError,
    _cache_get,
    _cache_set,
    bs,
    coingecko,
)
from common.formatting import token_amount, token_transfer_short

LLAMA_BASE = "https://stablecoins.llama.fi"
TRONSCAN_BASE = "https://apilist.tronscan.org/api"

# Depeg threshold: |price - $1| > 50 bps (0.5%).
DEPEG_BPS = 50

# ---------------------------------------------------------------- config
# llama_id: numeric DeFiLlama stablecoin id (USDe = 146 — resolve by
# symbol, case-sensitive "USDe"; a filter for "USDE" misses it).
# contract: primary-chain token contract (Ethereum 0x... / Tron base58).
STABLECOINS = {
    "usdtscope": {
        "symbol": "USDT",
        "name": "Tether USD",
        "llama_id": 1,
        "gecko_id": "tether",
        "primary": "ethereum",
        "contract": "0xdac17f958d2ee523a2206206994597c13d831ec7",
        "decimals": 6,
        "issuer": "Tether",
        "links": {
            "website": "https://tether.to/",
            "attestations": [
                {"label": "Tether Transparency — attestation reports", "url": "https://tether.to/en/transparency/#reports"},
            ],
            "whitepaper": "https://tether.to/wp-content/uploads/2016/06/TetherWhitePaper.pdf",
        },
        "reserves_note": "Tether publishes quarterly attestation reports (BDO) on its transparency page. Reserve figures are not available via a keyless machine-readable API — the linked page is the source.",
        # Attestation block (feature 1; feature 3 builds the full
        # tracker on it). last_attestation is the reporting date of
        # the latest report verified from the issuer's public
        # transparency page — never invented. USDT: Q3 2026 report
        # (BDO), reporting date 2026-09-30, published 2026-10-30
        # (tether.io news). KPMG's Dec 31, 2025 full audit is a
        # separate, annual document, not the quarterly attestation.
        "attestation": {
            "cadence": "quarterly",
            "auditor": "BDO",
            "last_attestation": "2026-09-30",
            "source_url": "https://tether.to/en/transparency/#reports",
            "note": "Quarterly BDO attestation; latest verified reporting date 2026-09-30 (published Oct 30, 2026).",
        },
        # Dated published reserve figures (feature 3). Every figure
        # is from the attestation named in `source`, as of `as_of` —
        # never a live or estimated number. Totals are verbatim from
        # Tether's Oct 30, 2026 release; the mix breakdown is per the
        # BDO attestation as reported in secondary summaries (the
        # release states totals + $132.4bn direct/indirect T-bills).
        "reserve_figures": {
            "as_of": "2026-09-30",
            "source": "Tether Q3 2026 Financial Figures and Reserves Report — BDO attestation, published Oct 30, 2026",
            "source_url": "https://tether.io/news/tether-generates-1-5-billion-in-q3-net-operating-profit-as-reserve-buffer-doubles-to-almost-8-billion/",
            "reserve_total_usd": 191451091374,
            "tokens_outstanding": 183469787261,
            "excess_usd": 7954630470,
            "reserve_mix": [
                {"label": "Cash & cash equivalents", "amount_usd": 139580000000, "share_pct": 72.9, "detail": "incl. US Treasury bills $122.84bn + overnight reverse repo $9.52bn"},
                {"label": "Gold (precious metals)", "amount_usd": 19080000000, "share_pct": 10.0},
                {"label": "Secured loans", "amount_usd": 14180000000, "share_pct": 7.4},
                {"label": "Bitcoin", "amount_usd": 8370000000, "share_pct": 4.4},
                {"label": "Other investments", "amount_usd": 6000000000, "share_pct": 3.1},
                {"label": "Publicly traded equities", "amount_usd": 4180000000, "share_pct": 2.2},
            ],
            "mix_note": "Mix amounts rounded to $0.01bn in the secondary summaries, so the mix sums to $191.39bn vs the $191.45bn attested total. Direct + indirect US Treasury bill holdings $132.4bn (Tether release).",
        },
    },
    "usdcscope": {
        "symbol": "USDC",
        "name": "USD Coin",
        "llama_id": 2,
        "gecko_id": "usd-coin",
        "primary": "ethereum",
        "contract": "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        "decimals": 6,
        "issuer": "Circle",
        "links": {
            "website": "https://www.circle.com/usdc/",
            "attestations": [
                {"label": "Circle USDC Transparency — attestations & reserve reports", "url": "https://www.centre.io/usdc-transparency"},
            ],
            "whitepaper": "https://www.circle.com/legal/mica-usdc-whitepaper",
        },
        "reserves_note": "Circle publishes monthly attestations and weekly reserve reports on its transparency page. Reserve figures are not available via a keyless machine-readable API — the linked page is the source.",
        # USDC: monthly Deloitte examinations. Re-verified Nov 6,
        # 2026 (feature 3): the September 2026 report IS posted on
        # Circle's transparency page — reporting dates Sep 4 and
        # Sep 30, 2026, Deloitte examination signed Oct 26, 2026.
        # The Oct 25 secondary review that saw only August was
        # simply before the September report was signed/posted.
        # last_attestation is the latest reporting date, 2026-09-30.
        "attestation": {
            "cadence": "monthly",
            "auditor": "Deloitte",
            "last_attestation": "2026-09-30",
            "source_url": "https://6778953.fs1.hubspotusercontent-na1.net/hubfs/6778953/USDCAttestationReports/2026/2026%20USDC_Examination%20Report%20September%2026.pdf",
            "note": "Monthly Deloitte examinations; latest verified report Sep 2026 (reporting date 2026-09-30, signed Oct 26, 2026) — re-verified Nov 6, 2026 from Circle's transparency page.",
        },
        # Dated published reserve figures (feature 3): exact figures
        # from Circle's Deloitte-examined September 2026 report
        # (PDF linked in source_url), as of 2026-09-30.
        "reserve_figures": {
            "as_of": "2026-09-30",
            "source": "Circle USDC Reserve Report as of Sep 30, 2026 — Deloitte & Touche LLP examination, signed Oct 26, 2026",
            "source_url": "https://6778953.fs1.hubspotusercontent-na1.net/hubfs/6778953/USDCAttestationReports/2026/2026%20USDC_Examination%20Report%20September%2026.pdf",
            "reserve_total_usd": 73956687701,
            "tokens_outstanding": 73878861314,
            "reserve_mix": [
                {"label": "Circle Reserve Fund — U.S. Treasury repo", "amount_usd": 36654000000, "share_pct": 49.6},
                {"label": "Circle Reserve Fund — U.S. Treasury securities", "amount_usd": 25361578416, "share_pct": 34.3},
                {"label": "Cash at regulated financial institutions", "amount_usd": 9502194346, "share_pct": 12.8},
                {"label": "U.S. Treasury securities held directly", "amount_usd": 1757678272, "share_pct": 2.4},
                {"label": "Circle Reserve Fund — cash", "amount_usd": 1003847741, "share_pct": 1.4},
                {"label": "Settlement differences (net)", "amount_usd": -322611074, "share_pct": -0.4, "detail": "Circle Reserve Fund −$552,000,382 + other reserves +$229,389,308"},
            ],
            "mix_note": "Exact figures from the Deloitte-examined report; the mix sums to the $73,956,687,701 total. $62.47bn of the reserve sits in the BlackRock-managed Circle Reserve Fund.",
        },
    },
    "daiscope": {
        "symbol": "DAI",
        "name": "Dai",
        "llama_id": 5,
        "gecko_id": "dai",
        "primary": "ethereum",
        "contract": "0x6b175474e89094c44da98b954eedeac495271d0f",
        "decimals": 18,
        "issuer": "MakerDAO / Sky",
        "links": {
            "website": "https://app.sky.money/",
            "attestations": [
                {"label": "Sky (MakerDAO) app — vaults & collateral", "url": "https://app.sky.money/"},
                {"label": "DAI Stats — live collateral breakdown", "url": "https://daistats.com"},
                {"label": "MakerDAO", "url": "https://makerdao.com/"},
            ],
            "whitepaper": "https://makerdao.com/en/whitepaper/",
        },
        "reserves_note": "DAI is crypto-collateralised via MakerDAO/Sky vaults. No keyless machine-readable API exposes the live collateral composition (MakerDAO/oasis API hosts probed 2026-11-05 are unreachable or 404) — see DAI Stats for the current breakdown.",
        # DAI: no dated reserve attestation — backing is
        # crypto-collateral in on-chain vaults, verifiable on-chain
        # in principle (feature 3), so last_attestation stays null.
        "attestation": {
            "cadence": "on-chain",
            "auditor": None,
            "last_attestation": None,
            "source_url": "https://daistats.com",
            "note": "Crypto-collateralised via MakerDAO/Sky vaults; no dated reserve attestation exists — collateral is on-chain, not in dated reports.",
        },
    },
    "usdescope": {
        "symbol": "USDe",
        "name": "Ethena USDe",
        "llama_id": 146,
        "gecko_id": "ethena-usde",
        "primary": "ethereum",
        "contract": "0x4c9EDD5852cd905f086C759E8383e09bff1E68B3",
        "decimals": 18,
        "issuer": "Ethena Labs",
        "links": {
            "website": "https://www.ethena.fi",
            "attestations": [
                {"label": "Ethena audits (Quantstamp, v2 contracts)", "url": "https://ethena-labs.gitbook.io/ethena-labs/resources/audits#quantstamp-audit-on-v2-of-contracts"},
                {"label": "Ethena app — backing & yield", "url": "https://app.ethena.fi/"},
            ],
            "whitepaper": "https://ethena-labs.gitbook.io/",
        },
        "reserves_note": "USDe is backed by delta-neutral hedged positions (staked ETH plus perpetual shorts). No keyless machine-readable API exposes live backing figures (Ethena app API redirects; api.ethena.fi returns 403) — see the Ethena app for the current backing.",
        # USDe: no scheduled reserve attestation is published.
        # Quantstamp audited the smart contracts — that is not a
        # reserve attestation, so last_attestation stays null and
        # the reserve-liquidity input scores unknown.
        "attestation": {
            "cadence": "none",
            "auditor": "Quantstamp",
            "last_attestation": None,
            "source_url": "https://ethena-labs.gitbook.io/ethena-labs/resources/audits#quantstamp-audit-on-v2-of-contracts",
            "note": "No scheduled reserve attestation published; Quantstamp is a smart-contract auditor, not a reserve attestor.",
        },
    },
    "fdusdsscope": {
        "symbol": "FDUSD",
        "name": "First Digital USD",
        "llama_id": 119,
        "gecko_id": "first-digital-usd",
        "primary": "ethereum",
        "contract": "0xc5f0f7b66764f6ec8c8dff7ba683102295e16409",
        "decimals": 18,
        "issuer": "First Digital Labs",
        "links": {
            "website": "https://firstdigitallabs.com",
            "attestations": [
                {"label": "First Digital Labs — attestation reports", "url": "https://firstdigitallabs.com"},
            ],
            "whitepaper": "https://firstdigitallabs.com/workspace/uploads/FDUSD-Whitepaper-25216064ca0cc8.pdf",
        },
        "reserves_note": "First Digital publishes monthly attestation reports on its site. Reserve figures are not available via a keyless machine-readable API — the linked site is the source. Note: most FDUSD supply lives on BSC, not Ethereum.",
        # FDUSD: monthly attestations, but the auditor has changed
        # repeatedly (Prescient to Feb 2025, Prism Mar–Dec 2025,
        # AOGB CPA from Jan 2026) and the latest report retrievable
        # in an Oct 2026 review was 2026-02-28 (AOGB, signed
        # 2026-03-13); the issuer site blocks automated access, so
        # later reports could not be verified — do not assume one.
        # Re-checked Nov 6, 2026 (feature 3): an independent
        # secondary review (Oct 2026) likewise could retrieve
        # nothing newer than Feb 2026 — the date stands.
        "attestation": {
            "cadence": "monthly",
            "auditor": "AOGB CPA Limited",
            "last_attestation": "2026-02-28",
            "source_url": "https://firstdigitallabs.com",
            "note": "Monthly attestations; auditor changed 3x in ~1 yr (Prescient, Prism, AOGB from Jan 2026). Latest report retrievable Oct 2026 was Feb 2026 — issuer site blocks automated access; re-checked Nov 6, 2026, still nothing newer retrievable.",
        },
        # Dated published reserve figures (feature 3): AOGB
        # attestation as of 2026-02-28, signed 2026-03-13, per an
        # independent secondary summary (issuer site blocks
        # automated access, so the report PDF itself could not be
        # fetched from this sandbox).
        "reserve_figures": {
            "as_of": "2026-02-28",
            "source": "First Digital FDUSD reserve attestation as of Feb 28, 2026 — AOGB CPA Limited (ISAE 3000, limited assurance), signed Mar 13, 2026; figures per secondary summary",
            "source_url": "https://www.firstdigitallabs.com/transparency",
            "reserve_total_usd": 381700000,
            "tokens_outstanding": None,
            "reserve_mix": [
                {"label": "US Treasury bills (maturing Mar–Apr 2026)", "amount_usd": 334700000, "share_pct": 87.7},
                {"label": "US dollar cash in custody accounts", "amount_usd": 39500000, "share_pct": 10.3},
                {"label": "Fixed deposits", "amount_usd": 7500000, "share_pct": 2.0},
            ],
            "mix_note": "Excess over circulation ~$1.3m (~0.35%). Amounts rounded to $0.1m in the secondary summary. Latest report retrievable as of Oct 2026; later reports may exist but could not be verified.",
        },
    },
    "pyusdsscope": {
        "symbol": "PYUSD",
        "name": "PayPal USD",
        "llama_id": 120,
        "gecko_id": "paypal-usd",
        "primary": "ethereum",
        "contract": "0x6c3ea9036406852006290770BEdFcAbA0e23A0e8",
        "decimals": 6,
        "issuer": "PayPal (issued by Paxos)",
        "links": {
            "website": "https://www.paypal.com/pyusd",
            "attestations": [
                {"label": "PayPal PYUSD — issuer reports", "url": "https://www.paypal.com/pyusd"},
            ],
            "whitepaper": "https://developer.paypal.com/dev-center/pyusd/",
        },
        "reserves_note": "PYUSD is issued by Paxos; monthly attestation reports are published by the issuer. Reserve figures are not available via a keyless machine-readable API — the linked page is the source.",
        # PYUSD: monthly KPMG LLP attestations (KPMG since Feb 28,
        # 2025; WithumSmith+Brown before). Latest verified reporting
        # date 2026-09-30 (Paxos PYUSD Redemption Assets Report,
        # Sep 17 & Sep 30, 2026, KPMG examination signed Oct 23,
        # 2026; $2.75B reserves vs 2,747,625,868 outstanding).
        "attestation": {
            "cadence": "monthly",
            "auditor": "KPMG LLP",
            "last_attestation": "2026-09-30",
            "source_url": "https://www.paxos.com/pyusd-transparency",
            "note": "Monthly KPMG attestations (since Feb 28, 2025; Withum before); latest verified reporting date 2026-09-30.",
        },
        # Dated published reserve figures (feature 3): Paxos PYUSD
        # Redemption Assets Report as of Sep 30, 2026, KPMG
        # examination signed Oct 23, 2026 — per a secondary summary
        # of the report (the issuer page is JS-rendered; the report
        # PDF itself could not be fetched directly from this
        # sandbox, so the total is rounded to $0.01bn as stated).
        "reserve_figures": {
            "as_of": "2026-09-30",
            "source": "Paxos PYUSD Redemption Assets Report as of Sep 17 & Sep 30, 2026 — KPMG LLP examination, signed Oct 23, 2026; figures per secondary summary of the report",
            "source_url": "https://www.paxos.com/pyusd-transparency",
            "reserve_total_usd": 2750000000,
            "reserve_total_approx": True,
            "tokens_outstanding": 2747625868,
            "reserve_mix": [
                {"label": "Overnight repurchase agreements (U.S. Treasury collateral)", "amount_usd": 2160000000, "share_pct": 78.2},
                {"label": "U.S. Treasury bills", "amount_usd": 499000000, "share_pct": 18.1},
                {"label": "Cash at banks", "amount_usd": 100000000, "share_pct": 3.6},
                {"label": "Government money market funds", "amount_usd": 0, "share_pct": 0.0},
            ],
            "mix_note": "Reserve total rounded to $0.01bn in the secondary summary, so the mix ($2.16bn + $0.499bn + $0.100bn = $2.759bn) slightly exceeds the rounded $2.75bn total. Cash includes $100,160,089 in uninsured bank deposits.",
        },
    },
    "tusdscope": {
        "symbol": "TUSD",
        "name": "TrueUSD",
        "llama_id": 7,
        "gecko_id": "true-usd",
        "primary": "ethereum",
        "contract": "0x0000000000085d4780b73119b644ae5ecd22b376",
        "decimals": 18,
        "issuer": "TrueUSD (Techteryx)",
        "links": {
            "website": "https://trueusd.com/",
            "attestations": [
                {"label": "TrueUSD real-time attestation (TrustExplorer)", "url": "https://real-time-attest.trustexplorer.io/truecurrencies"},
            ],
            "whitepaper": "https://tusd.io/docs/trueusd-white-paper-202602.pdf",
        },
        "reserves_note": "TrueUSD publishes a real-time attestation feed (linked). Reserve figures are not available via a keyless machine-readable API — the linked feed is the source. Note: most TUSD supply lives on Tron, not Ethereum.",
        # TUSD: real-time attestation feed (TrustExplorer), not
        # dated reports — no last_attestation date applies.
        "attestation": {
            "cadence": "real-time",
            "auditor": "The Network Firm",
            "last_attestation": None,
            "source_url": "https://real-time-attest.trustexplorer.io/truecurrencies",
            "note": "Real-time attestation feed, not dated reports — no attestation date applies. Note: TUSD supply is frozen (no new mint/burn).",
        },
    },
    "usddscope": {
        "symbol": "USDD",
        "name": "Decentralized USD",
        "llama_id": 14,
        "gecko_id": "usdd",
        "primary": "tron",
        "contract": "TXDk8mbtRbXeYuMNS83CfKPaYYT8XWv9Hz",
        "decimals": 18,
        "issuer": "TRON DAO Reserve",
        "links": {
            "website": "https://usdd.io/",
            "attestations": [
                {"label": "USDD — collateral & transparency dashboard", "url": "https://usdd.io/"},
                {"label": "SlowMist audit report (USDD TRC20)", "url": "https://usdd.io/SlowMistAuditReport-USDDTRC20.pdf"},
            ],
            "whitepaper": "https://docs.usdd.io/",
        },
        "reserves_note": "USDD is over-collateralised by design (TRON DAO Reserve). The live collateral ratio is shown on usdd.io, not via a public API — no reserve figures are available keyless here. On-chain holders/transfers below are TronScan data (Tron-native coin; Blockscout does not cover Tron).",
        # USDD: no dated reserve attestation — collateral ratio is
        # published live on-chain / on the usdd.io dashboard.
        # SlowMist audited the TRC20 contract, not the reserves.
        "attestation": {
            "cadence": "on-chain",
            "auditor": "SlowMist",
            "last_attestation": None,
            "source_url": "https://usdd.io/",
            "note": "Over-collateralised by design; collateral ratio shown live on usdd.io, not in dated attestations. SlowMist is a contract auditor, not a reserve attestor.",
        },
    },
}


def _cfg(scope):
    c = STABLECOINS.get(scope)
    if c is None:
        raise UpstreamError(f"Unknown stablecoin scope: {scope}", status=404)
    return c


# ---------------------------------------------------------------- helpers
def _dev_bps(price):
    """Deviation from $1 in basis points (None if no price)."""
    try:
        return round((float(price) - 1.0) * 10000, 2)
    except (TypeError, ValueError):
        return None


def _peg_health(bps):
    if bps is None:
        return "unknown"
    a = abs(bps)
    if a <= 5:
        return "pegged"
    if a <= DEPEG_BPS:
        return "watch"
    return "depegged"


def _pct_change(new, old):
    try:
        if new is None or old in (None, 0):
            return None
        return round((float(new) - float(old)) / float(old) * 100.0, 2)
    except (TypeError, ValueError):
        return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def _llama(path):
    """GET DeFiLlama stablecoins API. Returns parsed JSON; raises
    UpstreamError on transport/upstream failure."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0), headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(LLAMA_BASE + path)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        raise UpstreamError(f"DeFiLlama unreachable ({type(e).__name__})")
    if r.status_code >= 400:
        raise UpstreamError(f"DeFiLlama returned HTTP {r.status_code}")
    try:
        return r.json()
    except Exception:
        raise UpstreamError("DeFiLlama returned invalid JSON")


async def _coin_detail(cfg):
    """DeFiLlama /stablecoin/{id} (cached 120s)."""
    key = f"scDetail:{cfg['llama_id']}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await _llama(f"/stablecoin/{cfg['llama_id']}")
    if not isinstance(d, dict):
        raise UpstreamError("DeFiLlama returned unexpected stablecoin data")
    _cache_set(key, d, ttl=120)
    return d


async def _price_history(cfg):
    """DeFiLlama /stablecoinprices, filtered to this coin's gecko_id.
    Cached 600s (6MB+ download). Returns [{date, price}] ascending."""
    key = f"scPrices:{cfg['gecko_id']}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await _llama("/stablecoinprices")
    if not isinstance(d, list):
        raise UpstreamError("DeFiLlama returned unexpected price data")
    gid = cfg["gecko_id"]
    out = []
    for e in d:
        try:
            ts = int(e.get("date") or 0)
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        p = (e.get("prices") or {}).get(gid)
        if p is None:
            continue
        out.append({"date": ts, "price": float(p)})
    out.sort(key=lambda x: x["date"])
    _cache_set(key, out, ttl=600)
    return out


async def _gecko(cfg):
    """CoinGecko /coins/{gecko_id} (cached 300s). None on any failure."""
    key = f"scGecko:{cfg['gecko_id']}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await coingecko(
        f"/coins/{cfg['gecko_id']}",
        {"localization": "false", "tickers": "false", "community_data": "false",
         "developer_data": "false", "sparkline": "false"},
    )
    if isinstance(d, dict):
        _cache_set(key, d, ttl=300)
        return d
    return None


def _supply_history(detail):
    """Daily total-supply history from /stablecoin/{id} `tokens`
    (despite the name, a daily [{date, circulating:{peggedUSD}}] series).
    Returns [{date, supply_usd}] ascending."""
    out = []
    for e in detail.get("tokens") or []:
        if not isinstance(e, dict):
            continue
        try:
            ts = int(e.get("date") or 0)
        except (TypeError, ValueError):
            continue
        v = (e.get("circulating") or {}).get("peggedUSD")
        if ts <= 0 or v is None:
            continue
        out.append({"date": ts, "supply_usd": float(v)})
    out.sort(key=lambda x: x["date"])
    return out


def _chain_breakdown(detail):
    """currentChainBalances -> [{chain, supply_usd, share_pct}] desc."""
    cb = detail.get("currentChainBalances") or {}
    rows = []
    total = 0.0
    for chain, v in cb.items():
        amt = _num((v or {}).get("peggedUSD")) or 0.0
        if amt > 0:
            rows.append({"chain": chain, "supply_usd": amt})
            total += amt
    rows.sort(key=lambda r: -r["supply_usd"])
    for r in rows:
        r["share_pct"] = round(r["supply_usd"] / total * 100.0, 2) if total else None
    return rows, total


def _depeg_events(hist):
    """Days where |price - $1| > 50 bps, newest first, with summary."""
    evs = []
    for p in hist:
        bps = _dev_bps(p["price"])
        if bps is not None and abs(bps) > DEPEG_BPS:
            evs.append({
                "date": p["date"],
                "price": p["price"],
                "deviation_bps": bps,
            })
    evs.sort(key=lambda e: -e["date"])
    worst = max(evs, key=lambda e: abs(e["deviation_bps"])) if evs else None
    return evs, worst


async def _current_price(cfg, detail):
    """Live price: DeFiLlama detail price first, CoinGecko fallback.
    Returns (price, source)."""
    p = _num(detail.get("price"))
    if p:
        return p, "defillama"
    g = await _gecko(cfg)
    md = (g or {}).get("market_data") or {}
    p = _num((md.get("current_price") or {}).get("usd"))
    if p:
        return p, "coingecko"
    return None, None


async def _token_overview(cfg):
    """Blockscout token overview for EVM coins (holders, on-chain
    supply, exchange rate). None for Tron / on failure."""
    if cfg["primary"] != "ethereum":
        return None
    try:
        d = await bs("ethereum", f"/tokens/{cfg['contract']}")
    except UpstreamError:
        return None
    if not isinstance(d, dict):
        return None
    return {
        "holders_count": d.get("holders_count"),
        "total_supply": token_amount(d.get("total_supply"), d.get("decimals")),
        "exchange_rate_usd": d.get("exchange_rate"),
        "name": d.get("name"),
        "symbol": d.get("symbol"),
        "decimals": d.get("decimals"),
    }


async def _tronscan(path, params):
    """GET TronScan keyless API. Returns parsed JSON or None on 4xx."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0), headers=HEADERS, follow_redirects=True) as c:
            r = await c.get(TRONSCAN_BASE + path, params=params)
    except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
        raise UpstreamError(f"TronScan unreachable ({type(e).__name__})")
    if r.status_code == 404:
        return None
    if r.status_code >= 400:
        raise UpstreamError(f"TronScan returned HTTP {r.status_code}")
    try:
        return r.json()
    except Exception:
        raise UpstreamError("TronScan returned invalid JSON")


async def _tron_token_info(cfg):
    """TronScan token info for USDD (market cap, 24h volume)."""
    key = f"scTronTok:{cfg['contract']}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    d = await _tronscan("/token_trc20", {"contract": cfg["contract"]})
    toks = (d or {}).get("trc20_tokens") or []
    out = None
    if toks and isinstance(toks[0], dict):
        t = toks[0]
        out = {
            "market_cap_usd": _num(t.get("market_cap_usd")),
            "volume_24h_usd": _num(t.get("volume24h")),
            "name": t.get("name"),
            "symbol": t.get("symbol"),
            "decimals": t.get("decimals"),
        }
    if out is not None:
        _cache_set(key, out, ttl=300)
    return out


def _holder_label(addr_obj):
    """Blockscout address object -> display label (first name tag)."""
    if not isinstance(addr_obj, dict):
        return None
    meta = addr_obj.get("metadata") or {}
    for tag in meta.get("tags") or []:
        if isinstance(tag, dict) and tag.get("name"):
            return tag["name"]
    return addr_obj.get("ens_domain_name")


def _amount(raw, decimals):
    """Raw base-unit string -> Decimal amount."""
    try:
        return Decimal(str(raw)) / (Decimal(10) ** int(decimals))
    except (InvalidOperation, ValueError, TypeError):
        return None


# ---------------------------------------------------------------- stats
async def sc_stats(scope):
    cfg = _cfg(scope)
    detail, gecko = await asyncio.gather(
        _coin_detail(cfg), _gecko(cfg), return_exceptions=True
    )
    if isinstance(detail, Exception):
        raise detail if isinstance(detail, UpstreamError) else UpstreamError(str(detail))
    if isinstance(gecko, Exception):
        gecko = None
    price, price_source = await _current_price(cfg, detail)
    bps = _dev_bps(price)
    hist = _supply_history(detail)
    chains, chain_total = _chain_breakdown(detail)
    supply = chain_total or (hist[-1]["supply_usd"] if hist else None)
    # 7d / 30d supply change from daily history.
    chg7 = chg30 = None
    if len(hist) >= 2:
        last = hist[-1]["supply_usd"]
        for days, slot in ((7, "chg7"), (30, "chg30")):
            past = [h for h in hist if h["date"] <= hist[-1]["date"] - days * 86400]
            v = _pct_change(last, past[-1]["supply_usd"]) if past else None
            if slot == "chg7":
                chg7 = v
            else:
                chg30 = v
    md = (gecko or {}).get("market_data") or {}
    if cfg["primary"] == "ethereum":
        tok = await _token_overview(cfg)
        tron = None
    else:
        tok = None
        try:
            tron = await _tron_token_info(cfg)
        except UpstreamError:
            tron = None
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "peg_mechanism": detail.get("pegMechanism") or cfg.get("peg_mechanism"),
        "peg_type": detail.get("pegType"),
        "description": detail.get("description"),
        "price_usd": price,
        "price_source": price_source,
        "deviation_bps": bps,
        "peg_health": _peg_health(bps),
        "supply_usd": supply,
        "supply_change_7d_pct": chg7,
        "supply_change_30d_pct": chg30,
        "supply_history_points": len(hist),
        "supply_history_first": hist[0]["date"] if hist else None,
        "supply_history_last": hist[-1]["date"] if hist else None,
        "chains": chains[:10],
        "chain_count": len(chains),
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "decimals": cfg["decimals"],
        "market_cap_usd": _num((md.get("market_cap") or {}).get("usd")),
        "gecko_total_supply": _num(md.get("total_supply")),
        "gecko_circulating_supply": _num(md.get("circulating_supply")),
        "token_on_primary": tok,
        "tron_token": tron,
        "links": cfg["links"],
        "sources": ["DeFiLlama stablecoins API", "CoinGecko keyless API"]
        + (["Blockscout v2 (Ethereum)"] if cfg["primary"] == "ethereum" else ["TronScan keyless API", "TronGrid keyless API"]),
    }


# ---------------------------------------------------------------- peg
async def sc_peg(scope, days=30):
    cfg = _cfg(scope)
    detail, hist = await asyncio.gather(
        _coin_detail(cfg), _price_history(cfg), return_exceptions=True
    )
    if isinstance(detail, Exception):
        raise detail if isinstance(detail, UpstreamError) else UpstreamError(str(detail))
    if isinstance(hist, Exception):
        raise hist if isinstance(hist, UpstreamError) else UpstreamError(str(hist))
    price, price_source = await _current_price(cfg, detail)
    bps = _dev_bps(price)
    now = int(time.time())
    cutoff = now - max(1, days) * 86400
    window = [p for p in hist if p["date"] >= cutoff]
    evs, worst = _depeg_events(window)
    # Daily points for the chart (already daily; cap at 365 points).
    pts = window[-365:]
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "price_usd": price,
        "price_source": price_source,
        "deviation_bps": bps,
        "peg_health": _peg_health(bps),
        "days": days,
        "points": [{"date": p["date"], "price": p["price"],
                    "deviation_bps": _dev_bps(p["price"])} for p in pts],
        "points_count": len(pts),
        "history_first": hist[0]["date"] if hist else None,
        "history_last": hist[-1]["date"] if hist else None,
        "depeg_days_in_window": len(evs),
        "worst_depeg_in_window": worst,
        "depeg_threshold_bps": DEPEG_BPS,
    }


# ---------------------------------------------------------------- supply
async def sc_supply(scope, days=90):
    cfg = _cfg(scope)
    detail = await _coin_detail(cfg)
    hist = _supply_history(detail)
    chains, chain_total = _chain_breakdown(detail)
    now = int(time.time())
    cutoff = now - max(1, days) * 86400
    window = [h for h in hist if h["date"] >= cutoff]
    # Recent mint/burn: day-over-day deltas for the last 7 days.
    recent = []
    for i in range(max(1, len(hist) - 7), len(hist)):
        prev, cur = hist[i - 1], hist[i]
        recent.append({
            "date": cur["date"],
            "supply_usd": cur["supply_usd"],
            "delta_usd": round(cur["supply_usd"] - prev["supply_usd"], 2),
            "delta_pct": _pct_change(cur["supply_usd"], prev["supply_usd"]),
        })
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "days": days,
        "points": window[-365:],
        "points_count": len(window),
        "history_first": hist[0]["date"] if hist else None,
        "history_last": hist[-1]["date"] if hist else None,
        "supply_usd": chain_total or (hist[-1]["supply_usd"] if hist else None),
        "chains": chains,
        "chain_count": len(chains),
        "recent_daily_change": recent,
        "note": "Supply is DeFiLlama circulating peggedUSD across all chains; per-chain figures are currentChainBalances.",
    }


# ---------------------------------------------------------------- mint/burn
# Feature 4 — mint/burn monitor.
#
# Net issuance: day-over-day change in the DeFiLlama daily total-supply
# series (_supply_history). This is NET supply change, not gross
# mint/burn — a day's net mint is (new issuance minus redemptions) and
# a single day's figure can hide large offsetting gross flows.
#
# Large on-chain events: individual Transfer events with the zero
# address as counterparty (mint: from 0x0; burn: to 0x0) on the coin's
# PRIMARY chain only. EVM coins use Blockscout's Etherscan-compatible
# `tokentx` endpoint for the zero address: the v2 REST token-transfers
# endpoint has no working mint/burn filter (`?type=mint` returns
# nothing even for DAI, which mints constantly), and paging the
# unfiltered feed cannot reach mints for high-volume tokens (50
# transfers cover seconds of USDT/USDC activity). Tron (USDD) uses
# TronScan transfers filtered client-side for the black-hole address
# T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb.
#
# Threshold rule (uniform, stated in every response): a single event
# counts as "large" when its USD value is >= max($1,000,000, 0.1% of
# circulating supply). For the largest coins the 0.1% leg dominates
# (USDT ~$183M, USDC ~$71M), so their lists are short by design — an
# empty list means "no event above the threshold in the scanned
# window", not "no mint/burn happened". `scanned` and
# `scan_earliest` show how far back the scan reached.
#
# Known limits (stated in the response, not hidden):
# - USDT: Blockscout's zero-address tokentx query times out
#   consistently for USDT (probed 2026-11-06: 25s and 55s timeouts),
#   so its on-chain events are reported unavailable. Separately,
#   Tether's issue()/redeem() emit Issue/Redeem events, not
#   zero-address Transfers, so a zero-address scan would undercount
#   USDT mint/burn even if the query succeeded.
# - TUSD: supply is frozen (no new mint/burn); the zero-address query
#   also timed out in probing — reported unavailable, with the flat
#   supply history shown instead.
# - FDUSD/TUSD: most supply lives off the primary chain (BSC / Tron);
#   on-chain events cover the primary chain only. The primary chain's
#   share of total supply is reported alongside the events.
MINT_BURN_CACHE_TTL = 300
MINT_BURN_FLOOR_USD = 1_000_000.0
MINT_BURN_SUPPLY_SHARE = 0.001  # 0.1% of circulating supply
MINT_BURN_MAX_EVENTS = 20
MINT_BURN_EVM_PAGES = 5
MINT_BURN_EVM_PAGE_SIZE = 100
MINT_BURN_TRON_PAGES = 5
MINT_BURN_TRON_PAGE_SIZE = 50
EVM_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
TRON_ZERO_ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
MINT_BURN_METHODOLOGY = (
    "Net issuance is the day-over-day change in DeFiLlama's daily "
    "total circulating supply (peggedUSD) — net supply change, NOT "
    "gross mint/burn. Largest mint/burn day = largest single-day net "
    "change in the 90d window; daily changes spanning a gap of more "
    "than 2 days in the series are excluded from the largest-day "
    "figures. Windows whose covered span differs from the requested "
    "days (short history, or a gap in the series — USDD's has an "
    "83-day gap) are flagged partial and their actual dates shown. "
    "Large on-chain events are individual "
    "Transfer events with the zero address as counterparty (mint: "
    "from the zero address; burn: to the zero address) on the coin's "
    "primary chain only, above max($1,000,000, 0.1% of circulating "
    "supply), latest 20. EVM events come from Blockscout's "
    "Etherscan-compatible tokentx endpoint for the zero address; "
    "USDD events from TronScan transfers filtered for the black-hole "
    "address. USD values use the live coin price (or $1.00 if no "
    "live price is available)."
)
MINT_BURN_DEMAND_CAVEAT = (
    "Net issuance is a PROXY for demand, not a direct measure of it. "
    "Redemptions, chain migrations (supply moving between chains), "
    "and issuer treasury moves also change total supply, so growing "
    "supply can reflect issuer inventory moves rather than end-user "
    "demand, and shrinking supply can reflect redemptions "
    "concentrated in a few holders. Read issuance alongside the peg, "
    "holder and reserve views, not on its own."
)


def _signed_usd(v):
    """Signed compact USD: +$1.23B / -$456.78M / +$12.34."""
    if v is None:
        return "—"
    a = abs(float(v))
    if a >= 1e9:
        s = f"${a / 1e9:.2f}B"
    elif a >= 1e6:
        s = f"${a / 1e6:.2f}M"
    elif a >= 1e3:
        s = f"${a / 1e3:.2f}K"
    else:
        s = f"${a:,.2f}"
    return ("+" if v >= 0 else "-") + s


def _net_window(hist, days, supply_now):
    """Net supply change over the last `days` days of the daily history.
    Returns {net_usd, pct_of_supply, start_date, end_date, window_days,
    window_days_requested, partial} — pct_of_supply is net / current
    supply. None fields when the history is too short."""
    empty = {
        "net_usd": None, "pct_of_supply": None, "start_date": None,
        "end_date": None, "window_days": 0,
        "window_days_requested": days, "partial": True,
    }
    if len(hist) < 2:
        return empty
    end = hist[-1]
    cutoff = end["date"] - days * 86400
    past = [h for h in hist if h["date"] <= cutoff]
    start = past[-1] if past else hist[0]
    net = end["supply_usd"] - start["supply_usd"]
    actual = (end["date"] - start["date"]) // 86400
    return {
        "net_usd": round(net, 2),
        "pct_of_supply": round(net / supply_now * 100.0, 2) if supply_now else None,
        "start_date": start["date"],
        "end_date": end["date"],
        "window_days": int(actual),
        "window_days_requested": days,
        # True whenever the covered span differs from the requested
        # window — a short history, or a gap in the daily series
        # (USDD's series has an 83-day gap, so its 90d window covers
        # 122 days). The dates are always shown alongside.
        "partial": actual != days,
    }


def _daily_deltas(hist, days):
    """Day-over-day net supply changes within the last `days` days:
    [{date, delta_usd}] ascending. Deltas spanning a gap of more than
    2 days in the daily series are excluded — they are multi-day jumps,
    not single-day mint/burn (USDD's DeFiLlama series has an 83-day
    gap, 2026-07-07 → 2026-09-28)."""
    if len(hist) < 2:
        return []
    cutoff = hist[-1]["date"] - days * 86400
    out = []
    for prev, cur in zip(hist, hist[1:]):
        if cur["date"] >= cutoff and cur["date"] - prev["date"] <= 2 * 86400:
            out.append({
                "date": cur["date"],
                "delta_usd": round(cur["supply_usd"] - prev["supply_usd"], 2),
            })
    return out


def _trend_30d(hist):
    """Net issuance in the last 30d vs the prior 30d (days 31–60 ago).
    Returns {net_30d_usd, net_prior_30d_usd, change_usd, label} with
    None values when the history is too short."""
    empty = {"net_30d_usd": None, "net_prior_30d_usd": None,
             "change_usd": None, "label": None}
    if len(hist) < 2:
        return empty

    def _at(ts):
        past = [h for h in hist if h["date"] <= ts]
        return past[-1] if past else None

    end = hist[-1]
    s30 = _at(end["date"] - 30 * 86400)
    s60 = _at(end["date"] - 60 * 86400)
    if s30 is None or s60 is None:
        return empty
    net30 = end["supply_usd"] - s30["supply_usd"]
    net_prior = s30["supply_usd"] - s60["supply_usd"]
    change = net30 - net_prior
    if change > 0:
        label = "Net issuance higher than the prior 30 days"
    elif change < 0:
        label = "Net issuance lower than the prior 30 days"
    else:
        label = "Net issuance unchanged vs the prior 30 days"
    return {
        "net_30d_usd": round(net30, 2),
        "net_prior_30d_usd": round(net_prior, 2),
        "change_usd": round(change, 2),
        "label": label,
    }


def _demand_signal(w7, w30, w90, flat_supply, supply_now):
    """Short issuance-as-demand read. Pure function of the window nets;
    the caveat is always attached — the read is a proxy, never a
    direct demand measurement."""
    if flat_supply:
        summary = (
            "Supply unchanged over the 90d window — no net mint/burn "
            "(frozen supply). No demand read is possible from issuance."
        )
        return {"summary": summary, "caveat": MINT_BURN_DEMAND_CAVEAT,
                "direction": "flat", "net_30d_usd": w30.get("net_usd"),
                "net_90d_usd": w90.get("net_usd")}
    n30 = w30.get("net_usd")
    n90 = w90.get("net_usd")
    if n30 is None or n90 is None:
        return {"summary": "Supply history too short for a demand read.",
                "caveat": MINT_BURN_DEMAND_CAVEAT, "direction": None,
                "net_30d_usd": n30, "net_90d_usd": n90}
    p30 = w30.get("pct_of_supply")
    p90 = w90.get("pct_of_supply")
    head = (
        f"Net issuance {_signed_usd(n30)}"
        f"{f' ({p30:+.2f}% of supply)' if p30 is not None else ''} over 30d; "
        f"{_signed_usd(n90)}"
        f"{f' ({p90:+.2f}% of supply)' if p90 is not None else ''} over 90d."
    )
    near_zero = (
        (p30 is not None and abs(p30) < 0.05)
        and (p90 is not None and abs(p90) < 0.05)
    )
    if near_zero:
        direction = "flat"
        read = "Net issuance near zero on both windows — supply roughly flat; no clear demand direction from issuance."
    elif n30 > 0 and n90 > 0:
        direction = "growing"
        read = "Supply is growing on both windows — consistent with net inflows / rising demand for the coin."
    elif n30 < 0 and n90 < 0:
        direction = "shrinking"
        read = "Supply is shrinking on both windows — redemptions exceed new issuance; consistent with net outflows."
    else:
        direction = "mixed"
        read = "Mixed: the recent 30d direction differs from the 90d window — the trend has turned within the last month."
    return {"summary": head + " " + read,
            "caveat": MINT_BURN_DEMAND_CAVEAT, "direction": direction,
            "net_30d_usd": n30, "net_90d_usd": n90}


async def _evm_zero_address_events(cfg, price_usd, threshold_usd):
    """Large mint/burn events on Ethereum via Blockscout's
    Etherscan-compatible tokentx endpoint for the zero address
    (address=0x0, contractaddress=<token>, sort=desc). Scans up to
    MINT_BURN_EVM_PAGES pages of MINT_BURN_EVM_PAGE_SIZE, stopping
    early at MINT_BURN_MAX_EVENTS large events. Returns
    (events, scanned, scan_earliest, status, status_note)."""
    root = CHAINS["ethereum"]["blockscout"].removesuffix("/api/v2") + "/api"
    events = []
    scanned = 0
    earliest = None
    pages_ok = 0
    stopped_early = False
    dec = cfg["decimals"]
    for page in range(1, MINT_BURN_EVM_PAGES + 1):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20.0), headers=HEADERS,
                follow_redirects=True,
            ) as c:
                r = await c.get(root, params={
                    "module": "account", "action": "tokentx",
                    "address": EVM_ZERO_ADDRESS,
                    "contractaddress": cfg["contract"],
                    "sort": "desc", "page": page,
                    "offset": MINT_BURN_EVM_PAGE_SIZE,
                })
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError):
            stopped_early = True
            break
        if r.status_code != 200:
            stopped_early = True
            break
        try:
            d = r.json()
        except Exception:
            stopped_early = True
            break
        res = d.get("result")
        if not isinstance(res, list):
            stopped_early = True
            break
        pages_ok += 1
        if not res:
            break
        for x in res:
            scanned += 1
            try:
                ts = int(x.get("timeStamp") or 0)
            except (TypeError, ValueError):
                ts = 0
            if ts and (earliest is None or ts < earliest):
                earliest = ts
            amt = _amount(x.get("value"), dec)
            if amt is None:
                continue
            usd = float(amt) * price_usd
            if usd < threshold_usd:
                continue
            frm = (x.get("from") or "").lower()
            to = (x.get("to") or "").lower()
            if frm == EVM_ZERO_ADDRESS:
                direction = "mint"
            elif to == EVM_ZERO_ADDRESS:
                direction = "burn"
            else:
                continue
            events.append({
                "timestamp": ts if ts else None,
                "direction": direction,
                "amount": float(amt),
                "amount_usd": round(usd, 2),
                "tx_hash": x.get("hash"),
                "block": _num(x.get("blockNumber")),
                "from": x.get("from"),
                "to": x.get("to"),
            })
            if len(events) >= MINT_BURN_MAX_EVENTS:
                break
        if len(events) >= MINT_BURN_MAX_EVENTS:
            break
        if len(res) < MINT_BURN_EVM_PAGE_SIZE:
            break
    if pages_ok == 0:
        return events, scanned, earliest, "unavailable", (
            "Blockscout's zero-address transfer query timed out or "
            "failed for this token — no on-chain mint/burn events "
            "could be retrieved. Net issuance above is unaffected "
            "(it comes from the DeFiLlama supply series)."
        )
    if stopped_early:
        return events, scanned, earliest, "partial", (
            "The on-chain scan stopped early (upstream error) after "
            f"{scanned} zero-address transfers — the list below may "
            "be incomplete."
        )
    return events, scanned, earliest, "ok", None


async def _tron_zero_address_events(cfg, price_usd, threshold_usd):
    """Large mint/burn events on Tron via TronScan transfers, paged by
    start offset and filtered client-side for the black-hole address.
    Returns (events, scanned, scan_earliest, status, status_note)."""
    events = []
    scanned = 0
    earliest = None
    pages_ok = 0
    stopped_early = False
    dec = cfg["decimals"]
    for page in range(MINT_BURN_TRON_PAGES):
        start = page * MINT_BURN_TRON_PAGE_SIZE
        try:
            d = await _tronscan("/token_trc20/transfers", {
                "contract_address": cfg["contract"], "start": start,
                "limit": MINT_BURN_TRON_PAGE_SIZE, "sort": "-timestamp",
            })
        except UpstreamError:
            stopped_early = True
            break
        items = (d or {}).get("token_transfers") or []
        pages_ok += 1
        if not items:
            break
        for t in items:
            scanned += 1
            ts_ms = t.get("block_ts")
            ts = int(ts_ms // 1000) if isinstance(ts_ms, (int, float)) else None
            if ts and (earliest is None or ts < earliest):
                earliest = ts
            amt = _amount(t.get("quant"), dec)
            if amt is None:
                continue
            usd = float(amt) * price_usd
            if usd < threshold_usd:
                continue
            frm = t.get("from_address") or ""
            to = t.get("to_address") or ""
            if frm == TRON_ZERO_ADDRESS:
                direction = "mint"
            elif to == TRON_ZERO_ADDRESS:
                direction = "burn"
            else:
                continue
            events.append({
                "timestamp": ts,
                "direction": direction,
                "amount": float(amt),
                "amount_usd": round(usd, 2),
                "tx_hash": t.get("transaction_id"),
                "block": _num(t.get("block")),
                "from": frm,
                "to": to,
            })
            if len(events) >= MINT_BURN_MAX_EVENTS:
                break
        if len(events) >= MINT_BURN_MAX_EVENTS:
            break
        if len(items) < MINT_BURN_TRON_PAGE_SIZE:
            break
    if pages_ok == 0:
        return events, scanned, earliest, "unavailable", (
            "TronScan's transfer query failed for this token — no "
            "on-chain mint/burn events could be retrieved. Net "
            "issuance above is unaffected (DeFiLlama supply series)."
        )
    if stopped_early:
        return events, scanned, earliest, "partial", (
            "The on-chain scan stopped early (upstream error) after "
            f"{scanned} transfers — the list below may be incomplete."
        )
    return events, scanned, earliest, "ok", None


async def sc_mint_burn(scope):
    """GET /api/<scope>/mint-burn — feature 4: net issuance from the
    DeFiLlama daily supply series (7/30/90d, largest single-day net
    mint/burn in 90d, 30d vs prior 30d trend), large on-chain mint/burn
    events on the primary chain, and an issuance-as-demand read with
    its caveat. Cached 300s."""
    cfg = _cfg(scope)
    cache_key = f"scMintBurn:{scope}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    detail = await _coin_detail(cfg)
    price, price_source = await _current_price(cfg, detail)
    price_usd = price if price else 1.0
    price_assumed = price is None
    hist = _supply_history(detail)
    chains, chain_total = _chain_breakdown(detail)
    supply = chain_total or (hist[-1]["supply_usd"] if hist else None)
    w7 = _net_window(hist, 7, supply)
    w30 = _net_window(hist, 30, supply)
    w90 = _net_window(hist, 90, supply)
    deltas90 = _daily_deltas(hist, 90)
    largest_mint = max(deltas90, key=lambda d: d["delta_usd"]) if deltas90 else None
    largest_burn = min(deltas90, key=lambda d: d["delta_usd"]) if deltas90 else None
    flat_supply = bool(
        deltas90 and all(abs(d["delta_usd"]) < 1.0 for d in deltas90)
    )
    trend = _trend_30d(hist)
    threshold = (
        max(MINT_BURN_FLOOR_USD, MINT_BURN_SUPPLY_SHARE * supply)
        if supply else MINT_BURN_FLOOR_USD
    )
    primary_label = "Tron" if cfg["primary"] == "tron" else "Ethereum"
    primary_share = None
    for row in chains:
        if row["chain"] == primary_label:
            primary_share = row["share_pct"]
            break
    if cfg["primary"] == "tron":
        evs, scanned, earliest, status, status_note = \
            await _tron_zero_address_events(cfg, price_usd, threshold)
    else:
        evs, scanned, earliest, status, status_note = \
            await _evm_zero_address_events(cfg, price_usd, threshold)
    if status == "unavailable" and scope == "usdtscope":
        status_note = (status_note or "") + (
            " Note: Tether's issue()/redeem() emit Issue/Redeem "
            "events, not zero-address Transfers, so a zero-address "
            "scan would undercount USDT mint/burn even if the query "
            "succeeded."
        )
    if flat_supply:
        status_note = (status_note or "") + (
            " Supply is frozen for this coin — no new mint/burn is "
            "expected, so an empty event list is consistent with the "
            "flat supply history above."
        )
    if status_note == "":
        status_note = None
    chain_note = (
        f"On-chain events cover the primary chain only ({primary_label})."
        + (
            f" {primary_label} holds {primary_share}% of total supply;"
            if primary_share is not None else ""
        )
        + " mint/burn events on other chains are not shown."
    )
    out = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "price_usd": price,
        "price_source": price_source,
        "price_assumed_1_usd": price_assumed,
        "supply_usd": supply,
        "supply_history_points": len(hist),
        "supply_history_first": hist[0]["date"] if hist else None,
        "supply_history_last": hist[-1]["date"] if hist else None,
        "threshold_usd": round(threshold, 2),
        "threshold_rule": (
            "A single on-chain mint/burn event counts as large when "
            "its USD value is >= max($1,000,000, 0.1% of circulating "
            "supply), applied uniformly to every coin."
        ),
        "net_issuance": {
            "window_7d": w7,
            "window_30d": w30,
            "window_90d": w90,
            "largest_mint_day": largest_mint,
            "largest_burn_day": largest_burn,
            "trend_30d_vs_prior_30d": trend,
            "flat_supply": flat_supply,
            "note": (
                "Net change in total circulating supply (DeFiLlama "
                "daily series) — net issuance, NOT gross mint/burn. "
                "A day's net mint is new issuance minus redemptions; "
                "large offsetting gross flows can hide inside a small "
                "net figure."
            ),
        },
        "large_events": {
            "events": evs,
            "count": len(evs),
            "cap": MINT_BURN_MAX_EVENTS,
            "primary_chain": cfg["primary"],
            "primary_chain_share_pct": primary_share,
            "chain_scope_note": chain_note,
            "scanned_zero_address_transfers": scanned,
            "scan_earliest": earliest,
            "status": status,
            "status_note": status_note,
        },
        "demand_signal": _demand_signal(w7, w30, w90, flat_supply, supply),
        "methodology": MINT_BURN_METHODOLOGY,
        "sources": ["DeFiLlama stablecoins API (/stablecoin/{id} daily supply series)"]
        + (["TronScan keyless API"] if cfg["primary"] == "tron"
           else ["Blockscout Etherscan-compatible API (zero-address token transfers)"]),
    }
    _cache_set(cache_key, out, ttl=MINT_BURN_CACHE_TTL)
    return out


# ---------------------------------------------------------------- holders
async def sc_holders(scope, params):
    cfg = _cfg(scope)
    detail = await _coin_detail(cfg)
    price, _ = await _current_price(cfg, detail)
    if cfg["primary"] == "tron":
        start = 0
        try:
            start = max(0, int(params.get("start") or 0))
        except (TypeError, ValueError):
            start = 0
        d = await _tronscan("/token_trc20/holders", {
            "contract_address": cfg["contract"], "start": start, "limit": 50,
        })
        items = []
        for t in (d or {}).get("trc20_tokens") or []:
            amt = _amount(t.get("balance"), cfg["decimals"])
            items.append({
                "address": t.get("holder_address"),
                "label": t.get("addressTag") or None,
                "balance": float(amt) if amt is not None else None,
                "balance_usd": (float(amt) * price) if amt is not None and price else None,
                "balance_usd_tronscan": _num(t.get("balanceInUsd")),
            })
        total = (d or {}).get("total")
        return {
            "scope": scope,
            "symbol": cfg["symbol"],
            "primary_chain": "tron",
            "items": items,
            "start": start,
            "next_start": (start + len(items)) if len(items) == 50 else None,
            "holders_total": total,
            "holders_total_note": "TronScan caps the holder list at 10,000 — the total shown is the capped list size, not necessarily the true holder count.",
            "price_usd": price,
        }
    # Ethereum via Blockscout.
    d = await bs("ethereum", f"/tokens/{cfg['contract']}/holders", params=params)
    if d is None:
        raise UpstreamError("Token not found", status=404)
    tok = await _token_overview(cfg)
    items = []
    for it in d.get("items") or []:
        addr = it.get("address") or {}
        amt = _amount(it.get("value"), cfg["decimals"])
        items.append({
            "address": addr.get("hash"),
            "label": _holder_label(addr),
            "is_contract": addr.get("is_contract"),
            "balance": float(amt) if amt is not None else None,
            "balance_usd": (float(amt) * price) if amt is not None and price else None,
        })
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "primary_chain": "ethereum",
        "items": items,
        "next_page_params": d.get("next_page_params"),
        "holders_total": (tok or {}).get("holders_count"),
        "price_usd": price,
    }


# ---------------------------------------------------------------- transfers
async def sc_transfers(scope, params):
    cfg = _cfg(scope)
    detail = await _coin_detail(cfg)
    price, _ = await _current_price(cfg, detail)
    if cfg["primary"] == "tron":
        start = 0
        try:
            start = max(0, int(params.get("start") or 0))
        except (TypeError, ValueError):
            start = 0
        d = await _tronscan("/token_trc20/transfers", {
            "contract_address": cfg["contract"], "start": start, "limit": 50,
            "sort": "-timestamp",
        })
        items = []
        for t in (d or {}).get("token_transfers") or []:
            amt = _amount(t.get("quant"), cfg["decimals"])
            items.append({
                "tx_hash": t.get("transaction_id"),
                "block": t.get("block"),
                "timestamp": t.get("block_ts"),
                "from": t.get("from_address"),
                "to": t.get("to_address"),
                "amount": float(amt) if amt is not None else None,
                "amount_usd": (float(amt) * price) if amt is not None and price else None,
            })
        return {
            "scope": scope,
            "symbol": cfg["symbol"],
            "primary_chain": "tron",
            "items": items,
            "start": start,
            "next_start": (start + len(items)) if len(items) == 50 else None,
            "price_usd": price,
        }
    d = await bs("ethereum", f"/tokens/{cfg['contract']}/transfers", params=params)
    if d is None:
        raise UpstreamError("Token not found", status=404)
    items = []
    for tt in d.get("items") or []:
        row = token_transfer_short(tt)
        amt = _num(row.get("amount"))
        row["amount_usd"] = (amt * price) if amt is not None and price else None
        items.append(row)
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "primary_chain": "ethereum",
        "items": items,
        "next_page_params": d.get("next_page_params"),
        "price_usd": price,
    }


# ---------------------------------------------------------------- whales
# Feature 6 — whale & exchange exposure.
#
# Reuses the /holders and /transfers feeds (same upstream calls, no new
# sources) and adds the three things those endpoints do not provide:
# concentration metrics, exchange attribution, and large-transfer
# alerts.
#
# Concentration: top-1 / top-10 / top-50 share of PRIMARY-CHAIN supply
# only — Blockscout total_supply on Ethereum; DeFiLlama's Tron chain
# balance for USDD (TronScan reports no token total supply). The
# denominator and its source are always stated; shares are never
# presented as shares of all-chain supply. The holder list is the
# first page of the primary-chain holder list (Blockscout default page
# = 50 holders — do NOT pass page_size, which returns an empty list;
# TronScan limit 50), so "top 50" means the 50 largest holders the
# source returns. A coin with fewer holders than 50 reports the count
# actually seen.
#
# Exchange attribution: an address counts as an exchange wallet ONLY
# when a keyless source's own name tag for it names a known exchange
# (Blockscout address metadata tags on Ethereum, TronScan addressTag
# on Tron), matched case-insensitively against EXCHANGE_NAMES. The
# matched tag is returned verbatim with its source, so the attribution
# can be checked. No address is labelled an exchange from memory, from
# its balance, or from a third-party list — an untagged wallet stays
# unattributed even if its size suggests an exchange. Exchange totals
# cover identified wallets WITHIN the top 50 on the primary chain
# only: exchanges also hold this coin in wallets outside the top 50,
# on other chains, and in untagged wallets, so the total is a LOWER
# BOUND on exchange-held supply, never a total — stated in every
# response. A tag can also name infrastructure a third party uses
# (e.g. "Ethena: Coinbase Web3 Wallet 4" is Ethena's wallet on
# Coinbase infrastructure); the tag is shown verbatim so that is
# visible.
#
# Large transfers: individual transfers on the primary chain whose USD
# value is >= max($1,000,000, 0.01% of circulating supply), latest 10,
# found by paging the recent-transfers feed (up to 10 pages x 50
# transfers). For high-volume coins 500 transfers cover only a minute
# or two of activity, so an empty list means "no transfer above the
# threshold in the scanned window" — the scanned count and the window
# (earliest/latest timestamp) are always reported. Mint/burn (zero
# address) transfers are included if they clear the threshold; they
# are not filtered out, since a large mint is itself a whale event.
WHALES_CACHE_TTL = 300
WHALE_TOP_N = 50
WHALE_TOP_LISTED = 10
WHALE_TRANSFER_FLOOR_USD = 1_000_000.0
WHALE_TRANSFER_SUPPLY_SHARE = 0.0001  # 0.01% of circulating supply
WHALE_TRANSFER_MAX = 10
WHALE_TRANSFER_PAGES = 10
WHALE_TRANSFER_PAGE_SIZE = 50
# (substring to match in a source-provided name tag, canonical display
# name). Order matters only where one name contains another; longer /
# more specific names come first.
EXCHANGE_NAMES = [
    ("crypto.com", "Crypto.com"),
    ("binance us", "Binance US"),
    ("binance", "Binance"),
    ("coinbase", "Coinbase"),
    ("kraken", "Kraken"),
    ("okx", "OKX"),
    ("okex", "OKX"),
    ("bybit", "Bybit"),
    ("bitfinex", "Bitfinex"),
    ("huobi", "Huobi"),
    ("htx", "HTX"),
    ("gemini", "Gemini"),
    ("bitstamp", "Bitstamp"),
    ("kucoin", "KuCoin"),
    ("gate.io", "Gate.io"),
    ("bitmex", "BitMEX"),
    ("deribit", "Deribit"),
    ("upbit", "Upbit"),
    ("bithumb", "Bithumb"),
    ("bitso", "Bitso"),
    ("luno", "Luno"),
    ("bitvavo", "Bitvavo"),
    ("cex.io", "CEX.IO"),
]
WHALES_METHODOLOGY = (
    "Top holders are the first page of the primary-chain holder list "
    "(Blockscout on Ethereum, TronScan on Tron; 50 holders). "
    "Concentration is the top-1 / top-10 / top-50 share of "
    "PRIMARY-CHAIN supply (Blockscout total_supply on Ethereum; "
    "DeFiLlama Tron chain balance for USDD) — not of all-chain supply. "
    "Exchange attribution comes only from the name tag the keyless "
    "source itself attaches to an address (Blockscout address tags, "
    "TronScan addressTag); an address is labelled an exchange only "
    "when its tag names a known exchange, and the matched tag is shown "
    "verbatim. Exchange totals cover identified wallets within the top "
    "50 on the primary chain only and are a lower bound on "
    "exchange-held supply, not a total. Large transfers are individual "
    "primary-chain transfers >= max($1,000,000, 0.01% of circulating "
    "supply), latest 10, found by paging the recent-transfers feed "
    "(up to 10 pages of 50); the scanned count and window are always "
    "reported, and an empty list means no transfer above the threshold "
    "in that window."
)


def _holder_tags(addr_obj):
    """All name tags Blockscout attaches to an address object."""
    if not isinstance(addr_obj, dict):
        return []
    meta = addr_obj.get("metadata") or {}
    out = []
    for tag in meta.get("tags") or []:
        if isinstance(tag, dict) and tag.get("name"):
            out.append(tag["name"])
    return out


def _match_exchange(tags):
    """First (tag, exchange) whose tag names a known exchange, or None.
    `tags` is a list of source-provided name-tag strings."""
    for tag in tags or []:
        low = str(tag).lower()
        for needle, name in EXCHANGE_NAMES:
            if needle in low:
                return tag, name
    return None


def _ts_epoch(v):
    """ISO-8601 string / seconds / ms epoch -> epoch seconds (int)."""
    if isinstance(v, (int, float)):
        return int(v // 1000) if v > 1e12 else int(v)
    if isinstance(v, str):
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None


async def _evm_top_holders(cfg):
    """First page of Blockscout holders (default page = 50). Returns
    [{address, label, tags, is_contract, balance}] in rank order."""
    d = await bs("ethereum", f"/tokens/{cfg['contract']}/holders",
                 timeout=30.0)
    if d is None:
        raise UpstreamError("Token not found", status=404)
    out = []
    for it in d.get("items") or []:
        addr = it.get("address") or {}
        amt = _amount(it.get("value"), cfg["decimals"])
        out.append({
            "address": addr.get("hash"),
            "label": _holder_label(addr),
            "tags": _holder_tags(addr),
            "is_contract": bool(addr.get("is_contract")),
            "balance": float(amt) if amt is not None else None,
        })
    return out


async def _tron_top_holders(cfg, price_usd):
    """First 50 TronScan holders. Returns
    ([holders], total) — total is TronScan's capped list size."""
    d = await _tronscan("/token_trc20/holders", {
        "contract_address": cfg["contract"], "start": 0, "limit": 50,
    })
    out = []
    for t in (d or {}).get("trc20_tokens") or []:
        amt = _amount(t.get("balance"), cfg["decimals"])
        tag = t.get("addressTag") or None
        out.append({
            "address": t.get("holder_address"),
            "label": tag,
            "tags": [tag] if tag else [],
            "is_contract": None,
            "balance": float(amt) if amt is not None else None,
        })
    return out, (d or {}).get("total")


async def _evm_large_transfers(cfg, price_usd, threshold_usd):
    """Scan recent token transfers (up to WHALE_TRANSFER_PAGES pages)
    for transfers >= threshold. Returns (items, scanned, earliest,
    latest, status, status_note)."""
    items = []
    scanned = 0
    earliest = None
    latest = None
    pages_ok = 0
    stopped_early = False
    params = None
    for _ in range(WHALE_TRANSFER_PAGES):
        try:
            d = await bs("ethereum",
                         f"/tokens/{cfg['contract']}/transfers",
                         params=params)
        except UpstreamError:
            stopped_early = True
            break
        if d is None:
            stopped_early = True
            break
        rows = d.get("items") or []
        pages_ok += 1
        if not rows:
            break
        for tt in rows:
            scanned += 1
            row = token_transfer_short(tt)
            ts = _ts_epoch(row.get("timestamp"))
            if ts is not None:
                earliest = ts if earliest is None or ts < earliest else earliest
                latest = ts if latest is None or ts > latest else latest
            amt = _num(row.get("amount"))
            if amt is None:
                continue
            usd = amt * price_usd
            if usd < threshold_usd:
                continue
            frm = tt.get("from") or {}
            to = tt.get("to") or {}
            items.append({
                "timestamp": ts,
                "amount": amt,
                "amount_usd": round(usd, 2),
                "from": frm.get("hash"),
                "from_label": _holder_label(frm),
                "to": to.get("hash"),
                "to_label": _holder_label(to),
                "tx_hash": row.get("tx_hash"),
                "block": row.get("block"),
            })
            if len(items) >= WHALE_TRANSFER_MAX:
                break
        if len(items) >= WHALE_TRANSFER_MAX:
            break
        npp = d.get("next_page_params")
        if not npp:
            break
        params = npp
    if pages_ok == 0:
        return items, scanned, earliest, latest, "unavailable", (
            "Blockscout's token-transfers feed could not be read for "
            "this coin — no large transfers could be scanned."
        )
    if stopped_early:
        return items, scanned, earliest, latest, "partial", (
            "The transfer scan stopped early (upstream error) after "
            f"{scanned} transfers — the list below may be incomplete."
        )
    return items, scanned, earliest, latest, "ok", None


async def _tron_large_transfers(cfg, price_usd, threshold_usd):
    """Scan recent TronScan transfers (up to WHALE_TRANSFER_PAGES
    pages of 50) for transfers >= threshold."""
    items = []
    scanned = 0
    earliest = None
    latest = None
    pages_ok = 0
    stopped_early = False
    for page in range(WHALE_TRANSFER_PAGES):
        try:
            d = await _tronscan("/token_trc20/transfers", {
                "contract_address": cfg["contract"],
                "start": page * WHALE_TRANSFER_PAGE_SIZE,
                "limit": WHALE_TRANSFER_PAGE_SIZE,
                "sort": "-timestamp",
            })
        except UpstreamError:
            stopped_early = True
            break
        if d is None:
            stopped_early = True
            break
        rows = (d or {}).get("token_transfers") or []
        pages_ok += 1
        if not rows:
            break
        for t in rows:
            scanned += 1
            amt = _amount(t.get("quant"), cfg["decimals"])
            ts = _ts_epoch(t.get("block_ts"))
            if ts is not None:
                earliest = ts if earliest is None or ts < earliest else earliest
                latest = ts if latest is None or ts > latest else latest
            if amt is None:
                continue
            usd = float(amt) * price_usd
            if usd < threshold_usd:
                continue
            items.append({
                "timestamp": ts,
                "amount": float(amt),
                "amount_usd": round(usd, 2),
                "from": t.get("from_address"),
                "from_label": None,
                "to": t.get("to_address"),
                "to_label": None,
                "tx_hash": t.get("transaction_id"),
                "block": t.get("block"),
            })
            if len(items) >= WHALE_TRANSFER_MAX:
                break
        if len(items) >= WHALE_TRANSFER_MAX:
            break
        if len(rows) < WHALE_TRANSFER_PAGE_SIZE:
            break
    if pages_ok == 0:
        return items, scanned, earliest, latest, "unavailable", (
            "TronScan's transfers feed could not be read for this "
            "coin — no large transfers could be scanned."
        )
    if stopped_early:
        return items, scanned, earliest, latest, "partial", (
            "The transfer scan stopped early (upstream error) after "
            f"{scanned} transfers — the list below may be incomplete."
        )
    return items, scanned, earliest, latest, "ok", None


async def sc_whales(scope):
    """GET /api/<scope>/whales — feature 6: holder concentration
    (top-1/10/50 share of primary-chain supply), exchange exposure
    (identified exchange wallets within the top 50, lower bound), and
    large recent transfers above a stated threshold. Cached 300s."""
    cfg = _cfg(scope)
    cache_key = f"scWhales:{scope}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    detail = await _coin_detail(cfg)
    price, price_source = await _current_price(cfg, detail)
    price_usd = price if price else 1.0
    chains, chain_total = _chain_breakdown(detail)
    supply = chain_total or None
    primary_label = "Tron" if cfg["primary"] == "tron" else "Ethereum"
    primary_share = None
    primary_supply_usd = None
    for row in chains:
        if row["chain"] == primary_label:
            primary_share = row["share_pct"]
            primary_supply_usd = row["supply_usd"]
            break
    # Primary-chain supply in token units (the concentration
    # denominator) + holder count.
    if cfg["primary"] == "tron":
        holders, holders_total = await _tron_top_holders(cfg, price_usd)
        holders_total_note = (
            "TronScan caps the holder list at 10,000 — the total "
            "shown is the capped list size, not necessarily the true "
            "holder count."
        )
        primary_supply = (
            primary_supply_usd / price_usd
            if primary_supply_usd and price_usd else None
        )
        denom_source = (
            "DeFiLlama Tron chain balance (USD) / live price — "
            "TronScan reports no token total supply"
        )
    else:
        holders = await _evm_top_holders(cfg)
        tok = await _token_overview(cfg)
        holders_total = (tok or {}).get("holders_count")
        holders_total_note = None
        ts = (tok or {}).get("total_supply")
        try:
            primary_supply = float(str(ts).replace(",", "")) if ts is not None else None
        except (TypeError, ValueError):
            primary_supply = None
        denom_source = "Blockscout token total_supply (Ethereum)"
    counted = len(holders)
    balances = [h["balance"] for h in holders if h["balance"] is not None]

    def _share(n):
        if not primary_supply or not balances:
            return None
        return round(sum(balances[:n]) / primary_supply * 100.0, 3)

    concentration = {
        "top_1_share_pct": _share(1),
        "top_10_share_pct": _share(10),
        "top_50_share_pct": _share(min(WHALE_TOP_N, counted)),
        "holders_counted": counted,
        "holders_counted_note": (
            f"Top {counted} holders on the primary chain "
            f"({primary_label}) — the first page of the holder list. "
            + (
                f"Fewer than {WHALE_TOP_N} holders returned."
                if counted < WHALE_TOP_N else ""
            )
        ),
        "denominator": "primary_chain_supply",
        "denominator_note": (
            "Shares are of PRIMARY-CHAIN supply "
            f"({primary_label}), not of all-chain supply. Denominator: "
            f"{denom_source}."
        ),
        "primary_chain_supply": primary_supply,
        "primary_chain_supply_usd": (
            primary_supply * price_usd if primary_supply and price_usd else None
        ),
    }
    top_listed = []
    for i, h in enumerate(holders[:WHALE_TOP_LISTED]):
        bal = h["balance"]
        ex = _match_exchange(h["tags"])
        top_listed.append({
            "rank": i + 1,
            "address": h["address"],
            "label": h["label"],
            "is_contract": h["is_contract"],
            "balance": bal,
            "balance_usd": (bal * price_usd) if bal is not None and price_usd else None,
            "share_pct": (
                round(bal / primary_supply * 100.0, 3)
                if bal is not None and primary_supply else None
            ),
            "exchange": ex[1] if ex else None,
            "exchange_tag": ex[0] if ex else None,
        })
    # Exchange exposure: identified wallets within the top 50 only.
    identified = []
    for i, h in enumerate(holders):
        ex = _match_exchange(h["tags"])
        if not ex:
            continue
        bal = h["balance"]
        identified.append({
            "rank": i + 1,
            "exchange": ex[1],
            "matched_tag": ex[0],
            "attribution_source": (
                "TronScan addressTag" if cfg["primary"] == "tron"
                else "Blockscout address tag"
            ),
            "address": h["address"],
            "balance": bal,
            "balance_usd": (bal * price_usd) if bal is not None and price_usd else None,
            "share_pct": (
                round(bal / primary_supply * 100.0, 3)
                if bal is not None and primary_supply else None
            ),
        })
    by_exchange = {}
    for row in identified:
        agg = by_exchange.setdefault(row["exchange"], {
            "exchange": row["exchange"], "wallets": 0,
            "balance_usd": 0.0, "share_pct": 0.0,
        })
        agg["wallets"] += 1
        if row["balance_usd"]:
            agg["balance_usd"] += row["balance_usd"]
        if row["share_pct"]:
            agg["share_pct"] += row["share_pct"]
    by_exchange_rows = sorted(
        by_exchange.values(), key=lambda r: -r["balance_usd"]
    )
    for r in by_exchange_rows:
        r["balance_usd"] = round(r["balance_usd"], 2)
        r["share_pct"] = round(r["share_pct"], 3)
    ex_total_usd = round(sum(r["balance_usd"] for r in by_exchange_rows), 2)
    ex_total_share = round(sum(r["share_pct"] for r in by_exchange_rows), 3)
    exchanges = {
        "identified": identified,
        "by_exchange": by_exchange_rows,
        "total_balance_usd": ex_total_usd,
        "total_share_pct": ex_total_share,
        "wallets_identified": len(identified),
        "scope_note": (
            "Exchange wallets identified ONLY among the top "
            f"{counted} holders on the primary chain ({primary_label}) "
            "by the name tag the source attaches to the address. "
            "Exchanges also hold this coin in wallets outside the top "
            "50, on other chains, and in untagged wallets — so this "
            "total is a LOWER BOUND on exchange-held supply, not a "
            "total. A tag names the exchange infrastructure a wallet "
            "uses; the wallet itself may belong to a third party "
            "(e.g. a protocol's Coinbase Web3 Wallet)."
        ),
        "empty_note": (
            "No top-50 holder carries a name tag naming a known "
            "exchange — exchange exposure cannot be identified from "
            "keyless sources for this coin, not necessarily zero."
            if not identified else None
        ),
    }
    # Large transfers.
    threshold = (
        max(WHALE_TRANSFER_FLOOR_USD, WHALE_TRANSFER_SUPPLY_SHARE * supply)
        if supply else WHALE_TRANSFER_FLOOR_USD
    )
    if cfg["primary"] == "tron":
        lt, scanned, earliest, latest, lt_status, lt_note = \
            await _tron_large_transfers(cfg, price_usd, threshold)
    else:
        lt, scanned, earliest, latest, lt_status, lt_note = \
            await _evm_large_transfers(cfg, price_usd, threshold)
    large_transfers = {
        "items": lt,
        "count": len(lt),
        "cap": WHALE_TRANSFER_MAX,
        "threshold_usd": round(threshold, 2),
        "threshold_rule": (
            "A transfer counts as large when its USD value is >= "
            "max($1,000,000, 0.01% of circulating supply), applied "
            "uniformly to every coin."
        ),
        "primary_chain": cfg["primary"],
        "primary_chain_share_pct": primary_share,
        "chain_scope_note": (
            f"Transfers cover the primary chain only ({primary_label})."
            + (
                f" {primary_label} holds {primary_share}% of total "
                "supply;" if primary_share is not None else ""
            )
            + " large transfers on other chains are not shown."
        ),
        "scanned_transfers": scanned,
        "scan_earliest": earliest,
        "scan_latest": latest,
        "status": lt_status,
        "status_note": lt_note,
    }
    out = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "price_usd": price,
        "price_source": price_source,
        "price_assumed_1_usd": price is None,
        "supply_usd": supply,
        "primary_chain_share_pct": primary_share,
        "holders_total": holders_total,
        "holders_total_note": holders_total_note,
        "concentration": concentration,
        "top_holders": top_listed,
        "exchanges": exchanges,
        "large_transfers": large_transfers,
        "methodology": WHALES_METHODOLOGY,
        "sources": ["DeFiLlama stablecoins API (supply by chain)"]
        + (["TronScan keyless API (holders, transfers, address tags)"]
           if cfg["primary"] == "tron"
           else ["Blockscout v2 (Ethereum holders, transfers, address tags)"]),
    }
    _cache_set(cache_key, out, ttl=WHALES_CACHE_TTL)
    return out


# ---------------------------------------------------------------- activity & velocity
# Feature 7 — on-chain activity & velocity.
#
# What exists already (reused, not rebuilt): the raw recent-transfers
# feed (/transfers: Blockscout token transfers on Ethereum, TronScan
# token_trc20/transfers on Tron) and the holder count (Blockscout
# holders_count / TronScan holders total). What this feature adds:
# active addresses, transaction counts, transfer volume, median
# transfer size, turnover/velocity and period-over-period trends —
# all computed from a scan of the recent-transfers feed.
#
# THE CENTRAL HONESTY CONSTRAINT: the transfer feed is a paginated
# RECENT feed (50/page, newest first). For high-volume coins 500
# transfers cover minutes of activity; for low-volume coins they can
# cover days or weeks. There is NO keyless source for all-chain,
# all-time transfer counts or volumes (probed 2026-11-06: DeFiLlama
# has no transfer/volume series; CoinGecko total_volume is EXCHANGE
# trading volume, not on-chain transfers — it is reported separately,
# clearly labelled, and never mixed into on-chain volume). So:
#
# - Every window (24h / 7d / 30d) carries `complete`: true only when
#   the scan provably reached the start of the window (scan_earliest
#   at or before the window start, or the feed itself was exhausted).
#   An incomplete window's counts/volume/active addresses are LOWER
#   BOUNDS, labelled as such — never presented as the window total.
# - When no window is complete, daily rates come from the scanned
#   sample (sample volume / sample window) and are labelled
#   EXTRAPOLATED, with the sample window length stated. A sample
#   spanning minutes is a noisy basis for a daily rate and is flagged
#   as such; the raw sample figures are always shown alongside.
# - Trends (24h vs prior 24h, 7d vs prior 7d) are reported ONLY when
#   both halves are complete; otherwise null with the reason (usually
#   "scan covers only X — prior period not observable"). For USDT/USDC
#   the trend fields will normally be null. That is the honest answer,
#   not a gap to paper over.
# - Everything is PRIMARY-CHAIN only (Ethereum for 7 coins, Tron for
#   USDD). Velocity divides primary-chain transfer volume by
#   PRIMARY-CHAIN supply (DeFiLlama chain balance), so numerator and
#   denominator cover the same chain; the primary chain's share of
#   total supply is stated. All-chain velocity would be higher (other
#   chains' transfers are unobserved) — a vs-total-supply figure is
#   included as an explicit lower bound.
# - Active addresses = unique senders + unique receivers (union) in
#   the window/sample. A transfer counts one address if sender ==
#   receiver (self-transfer). Mint/burn (zero-address) transfers are
#   included — they are real transfers.
ACTIVITY_CACHE_TTL = 300
ACTIVITY_PAGES = 10
ACTIVITY_PAGE_SIZE = 50
ACTIVITY_METHODOLOGY = (
    "Active addresses, transaction counts, transfer volume and median "
    "transfer size are computed from a scan of the recent-transfers "
    "feed on the coin's PRIMARY chain only (Blockscout token transfers "
    "on Ethereum, TronScan token_trc20/transfers on Tron; up to 10 "
    "pages of 50 transfers, newest first). A 24h/7d/30d window is "
    "marked complete only when the scan provably reached the start of "
    "the window (or the feed was exhausted); an incomplete window's "
    "figures are LOWER BOUNDS, not totals. Daily rates use the 24h "
    "window when complete, else the 7d, else the 30d, else the scanned "
    "sample extrapolated (sample volume / sample window) — the basis "
    "is always stated and a sample spanning under an hour is flagged "
    "as a noisy basis. Velocity (annualised) = daily transfer volume "
    "x 365 / PRIMARY-CHAIN supply (DeFiLlama chain balance), so "
    "numerator and denominator cover the same chain; the "
    "vs-total-supply figure divides the same volume by all-chain "
    "supply and is a lower bound on all-chain velocity. Trends compare "
    "the last 24h with the prior 24h and the last 7d with the prior "
    "7d, and are reported only when both halves are complete. "
    "CoinGecko 24h trading volume is EXCHANGE trading volume, not "
    "on-chain transfer volume, and is reported separately. USD values "
    "use the live coin price (or $1.00 if no live price is available)."
)


async def _activity_scan_evm(cfg, price_usd):
    """Scan recent token transfers on Ethereum (up to ACTIVITY_PAGES
    pages of 50, newest first). Returns (transfers, scanned, earliest,
    latest, exhausted, status, status_note). Each transfer is
    {timestamp, from, to, amount, amount_usd}."""
    items = []
    scanned = 0
    earliest = None
    latest = None
    exhausted = False
    pages_ok = 0
    stopped_early = False
    params = None
    for _ in range(ACTIVITY_PAGES):
        try:
            d = await bs("ethereum",
                         f"/tokens/{cfg['contract']}/transfers",
                         params=params)
        except UpstreamError:
            stopped_early = True
            break
        if d is None:
            stopped_early = True
            break
        rows = d.get("items") or []
        pages_ok += 1
        if not rows:
            exhausted = True
            break
        for tt in rows:
            scanned += 1
            row = token_transfer_short(tt)
            ts = _ts_epoch(row.get("timestamp"))
            if ts is not None:
                earliest = ts if earliest is None or ts < earliest else earliest
                latest = ts if latest is None or ts > latest else latest
            amt = _num(row.get("amount"))
            if amt is None:
                continue
            frm = tt.get("from") or {}
            to = tt.get("to") or {}
            items.append({
                "timestamp": ts,
                "from": frm.get("hash") if isinstance(frm, dict) else frm,
                "to": to.get("hash") if isinstance(to, dict) else to,
                "amount": amt,
                "amount_usd": round(amt * price_usd, 2),
                "tx_hash": row.get("tx_hash"),
                "block": row.get("block"),
            })
        if len(rows) < ACTIVITY_PAGE_SIZE:
            exhausted = True
            break
        npp = d.get("next_page_params")
        if not npp:
            exhausted = True
            break
        params = npp
    if pages_ok == 0:
        return items, scanned, earliest, latest, exhausted, "unavailable", (
            "Blockscout's token-transfers feed could not be read for "
            "this coin — no activity figures could be computed."
        )
    if stopped_early:
        return items, scanned, earliest, latest, exhausted, "partial", (
            "The transfer scan stopped early (upstream error) after "
            f"{scanned} transfers — figures below cover only the "
            "transfers actually scanned and may be incomplete."
        )
    return items, scanned, earliest, latest, exhausted, "ok", None


async def _activity_scan_tron(cfg, price_usd):
    """Scan recent TronScan transfers (up to ACTIVITY_PAGES pages of
    50, newest first). Same return shape as _activity_scan_evm."""
    items = []
    scanned = 0
    earliest = None
    latest = None
    exhausted = False
    pages_ok = 0
    stopped_early = False
    for page in range(ACTIVITY_PAGES):
        try:
            d = await _tronscan("/token_trc20/transfers", {
                "contract_address": cfg["contract"],
                "start": page * ACTIVITY_PAGE_SIZE,
                "limit": ACTIVITY_PAGE_SIZE,
                "sort": "-timestamp",
            })
        except UpstreamError:
            stopped_early = True
            break
        if d is None:
            stopped_early = True
            break
        rows = (d or {}).get("token_transfers") or []
        pages_ok += 1
        if not rows:
            exhausted = True
            break
        for t in rows:
            scanned += 1
            ts = _ts_epoch(t.get("block_ts"))
            if ts is not None:
                earliest = ts if earliest is None or ts < earliest else earliest
                latest = ts if latest is None or ts > latest else latest
            amt = _amount(t.get("quant"), cfg["decimals"])
            if amt is None:
                continue
            items.append({
                "timestamp": ts,
                "from": t.get("from_address"),
                "to": t.get("to_address"),
                "amount": float(amt),
                "amount_usd": round(float(amt) * price_usd, 2),
                "tx_hash": t.get("transaction_id"),
                "block": t.get("block"),
            })
        if len(rows) < ACTIVITY_PAGE_SIZE:
            exhausted = True
            break
    if pages_ok == 0:
        return items, scanned, earliest, latest, exhausted, "unavailable", (
            "TronScan's token-transfers feed could not be read for "
            "this coin — no activity figures could be computed."
        )
    if stopped_early:
        return items, scanned, earliest, latest, exhausted, "partial", (
            "The transfer scan stopped early (upstream error) after "
            f"{scanned} transfers — figures below cover only the "
            "transfers actually scanned and may be incomplete."
        )
    return items, scanned, earliest, latest, exhausted, "ok", None


def _window_stats(items, now, days):
    """Stats for transfers with age < `days` days: counts, unique
    senders/receivers/addresses, volume, median/mean/largest transfer.
    `items` are normalised scan rows (amount_usd set, timestamp epoch
    seconds or None — a row with no timestamp is counted in the sample
    but in NO window)."""
    cutoff = now - days * 86400
    rows = [t for t in items
            if t.get("timestamp") is not None and t["timestamp"] >= cutoff]
    senders = {t["from"] for t in rows if t.get("from")}
    receivers = {t["to"] for t in rows if t.get("to")}
    vols = [t["amount_usd"] for t in rows if t.get("amount_usd") is not None]
    total = sum(vols) if vols else 0.0
    n = len(rows)
    return {
        "window_days": days,
        "tx_count": n,
        "unique_senders": len(senders),
        "unique_receivers": len(receivers),
        "unique_addresses": len(senders | receivers),
        "volume_usd": round(total, 2),
        "median_transfer_usd": (
            round(statistics.median(vols), 2) if vols else None
        ),
        "mean_transfer_usd": round(total / n, 2) if n else None,
        "largest_transfer_usd": round(max(vols), 2) if vols else None,
        "tx_per_day": round(n / days, 2) if n else 0.0,
        "volume_per_day_usd": round(total / days, 2) if n else 0.0,
    }


def _window_complete(now, days, scan_earliest, exhausted, scan_ok):
    """True when the window [now-days, now] is provably fully covered:
    the scan reached at or before the window start, or the feed itself
    was exhausted (no older transfers exist). False when the scan
    stopped at the page cap (or early) inside the window — figures for
    that window are then lower bounds."""
    if not scan_ok:
        return False
    if exhausted:
        return True
    if scan_earliest is None:
        return False
    return scan_earliest <= now - days * 86400


def _trend_entry(current, prior, label):
    """Period-over-period change for one metric. Both halves must be
    complete; otherwise comparable=False with the reason. `current` /
    `prior` are (value, complete) pairs."""
    cur, cur_ok = current
    pri, pri_ok = prior
    if not (cur_ok and pri_ok):
        missing = "current period" if not cur_ok else "prior period"
        return {
            "current": cur, "prior": pri, "change_pct": None,
            "comparable": False,
            "reason": (
                f"The {missing} is not fully covered by the transfer "
                f"scan, so no {label} trend can be computed — see "
                "coverage."
            ),
        }
    return {
        "current": cur, "prior": pri,
        "change_pct": _pct_change(cur, pri),
        "comparable": True, "reason": None,
    }


def _supply_change(hist, days):
    """Net supply change over the last `days` days of the daily
    DeFiLlama history (same rule as sc_stats): pct or None."""
    if len(hist) < 2:
        return None
    last = hist[-1]["supply_usd"]
    past = [h for h in hist if h["date"] <= hist[-1]["date"] - days * 86400]
    return _pct_change(last, past[-1]["supply_usd"]) if past else None


def _scan_window_label(seconds):
    if seconds is None:
        return None
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f} minutes"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} hours"
    return f"{seconds / 86400:.1f} days"


async def sc_activity(scope):
    """GET /api/<scope>/activity — feature 7: on-chain activity &
    velocity on the primary chain. Cached 300s. See the feature-7
    header above for the coverage rules — windows are marked complete
    only when provably covered, incomplete windows are lower bounds,
    daily rates fall back to a labelled sample extrapolation, and
    trends are null unless both halves are complete."""
    cfg = _cfg(scope)
    cache_key = f"scActivity:{scope}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached
    detail = await _coin_detail(cfg)
    price, price_source = await _current_price(cfg, detail)
    price_usd = price if price else 1.0
    chains, chain_total = _chain_breakdown(detail)
    supply = chain_total or None
    hist = _supply_history(detail)
    primary_label = "Tron" if cfg["primary"] == "tron" else "Ethereum"
    primary_share = None
    primary_supply_usd = None
    for row in chains:
        if row["chain"] == primary_label:
            primary_share = row["share_pct"]
            primary_supply_usd = row["supply_usd"]
            break
    # Holder count (total holders on the primary chain) for the
    # active-share read. EVM: Blockscout holders_count. Tron: the
    # TronScan holders total (capped at 10,000 — see note).
    holders_total = None
    holders_total_note = None
    if cfg["primary"] == "tron":
        try:
            d = await _tronscan("/token_trc20/holders", {
                "contract_address": cfg["contract"], "start": 0, "limit": 1,
            })
            holders_total = (d or {}).get("total")
        except UpstreamError:
            holders_total = None
        holders_total_note = (
            "TronScan caps the holder list at 10,000 — the total "
            "shown is the capped list size, not necessarily the true "
            "holder count."
        )
    else:
        tok = await _token_overview(cfg)
        # Blockscout returns holders_count as a string — coerce so the
        # active-share division below cannot TypeError on it.
        holders_total = _num((tok or {}).get("holders_count"))
    # CoinGecko 24h trading volume — EXCHANGE trading volume, kept
    # strictly separate from on-chain transfer volume.
    gecko = await _gecko(cfg)
    md = (gecko or {}).get("market_data") or {}
    trading_vol_24h = _num((md.get("total_volume") or {}).get("usd"))
    # The scan: up to 10 pages of 50 recent transfers, newest first.
    if cfg["primary"] == "tron":
        items, scanned, earliest, latest, exhausted, status, status_note = \
            await _activity_scan_tron(cfg, price_usd)
    else:
        items, scanned, earliest, latest, exhausted, status, status_note = \
            await _activity_scan_evm(cfg, price_usd)
    now = int(time.time())
    scan_ok = status in ("ok", "partial")
    span_s = (latest - earliest) if (earliest is not None and latest is not None and latest > earliest) else 0
    # Window stats + completeness. Prior windows (for trends) are the
    # same length immediately before the current window.
    windows = {}
    for days in (1, 7, 30):
        w = _window_stats(items, now, days)
        complete = _window_complete(now, days, earliest, exhausted, scan_ok)
        w["complete"] = complete
        if not complete:
            # Per-day rates inside an incomplete window would divide
            # a minutes-long slice by whole days — null them; the
            # `daily` object carries the proper (labelled) daily rate.
            w["tx_per_day"] = None
            w["volume_per_day_usd"] = None
        w["coverage_note"] = (
            "Complete — the scan reached the start of this window, "
            "so the figures are the window totals on the primary chain."
            if complete else
            "INCOMPLETE — the scan ended before the start of this "
            "window, so the figures are LOWER BOUNDS (only the "
            "transfers actually scanned), not the window totals."
        )
        if holders_total and complete and w["unique_addresses"]:
            w["active_share_of_holders_pct"] = round(
                w["unique_addresses"] / holders_total * 100.0, 3)
        else:
            w["active_share_of_holders_pct"] = None
        windows[f"{days}d"] = w
    # Prior-window stats for the trend comparisons.
    def _prior_stats(days):
        cutoff_lo = now - 2 * days * 86400
        cutoff_hi = now - days * 86400
        rows = [t for t in items
                if t.get("timestamp") is not None
                and cutoff_lo <= t["timestamp"] < cutoff_hi]
        senders = {t["from"] for t in rows if t.get("from")}
        receivers = {t["to"] for t in rows if t.get("to")}
        vols = [t["amount_usd"] for t in rows if t.get("amount_usd") is not None]
        return {
            "tx_count": len(rows),
            "unique_addresses": len(senders | receivers),
            "volume_usd": round(sum(vols), 2) if vols else 0.0,
        }

    prior = {}
    for days in (1, 7):
        p = _prior_stats(days)
        p["complete"] = _window_complete(now, 2 * days, earliest, exhausted, scan_ok)
        prior[f"{days}d"] = p
    # Sample stats (everything actually scanned).
    sample = _window_stats(items, now, 3650)  # 10y cutoff = all scanned
    sample["window_days"] = None
    sample["tx_per_day"] = None
    sample["volume_per_day_usd"] = None
    sample["tx_count"] = len(items)
    sample["sample_window_seconds"] = span_s
    sample["sample_window_label"] = _scan_window_label(span_s)
    sample["sample_window_days"] = round(span_s / 86400.0, 6) if span_s else 0.0
    sample["median_transfer_usd"] = (
        round(statistics.median(
            [t["amount_usd"] for t in items if t.get("amount_usd") is not None]), 2)
        if any(t.get("amount_usd") is not None for t in items) else None
    )
    sample["mean_transfer_usd"] = (
        round(sum(t["amount_usd"] for t in items if t.get("amount_usd") is not None) / len(items), 2)
        if items else None
    )
    sample["largest_transfer_usd"] = (
        round(max(t["amount_usd"] for t in items if t.get("amount_usd") is not None), 2)
        if any(t.get("amount_usd") is not None for t in items) else None
    )
    # Daily rates + velocity. Prefer the shortest complete window;
    # otherwise extrapolate from the scanned sample (labelled).
    if windows["1d"]["complete"]:
        daily_vol = windows["1d"]["volume_usd"]
        daily_tx = windows["1d"]["tx_count"]
        daily_basis = "observed_24h"
        basis_note = (
            "Daily rates are the observed 24h window totals on the "
            "primary chain (the scan covered the full 24h)."
        )
    elif windows["7d"]["complete"]:
        daily_vol = windows["7d"]["volume_usd"] / 7.0
        daily_tx = windows["7d"]["tx_count"] / 7.0
        daily_basis = "observed_7d"
        basis_note = (
            "Daily rates are the observed 7d window totals divided by "
            "7 (the 24h window is not fully covered by the scan)."
        )
    elif windows["30d"]["complete"]:
        daily_vol = windows["30d"]["volume_usd"] / 30.0
        daily_tx = windows["30d"]["tx_count"] / 30.0
        daily_basis = "observed_30d"
        basis_note = (
            "Daily rates are the observed 30d window totals divided "
            "by 30 (shorter windows are not fully covered by the scan)."
        )
    elif span_s >= 1 and items:
        days_span = span_s / 86400.0
        daily_vol = sample["volume_usd"] / days_span
        daily_tx = len(items) / days_span
        daily_basis = "extrapolated_sample"
        basis_note = (
            "EXTRAPOLATED: no window is fully covered, so daily rates "
            f"are the scanned sample ({len(items)} transfers over "
            f"{_scan_window_label(span_s)}) scaled to a day. "
            + (
                "The sample spans under an hour, so this is a noisy "
                "basis — treat the daily figures as an order-of-"
                "magnitude read, not a measurement."
                if span_s < 3600 else
                "The sample window is short, so treat the daily "
                "figures as an estimate, not a measurement."
            )
        )
    else:
        daily_vol = None
        daily_tx = None
        daily_basis = "unavailable"
        basis_note = "No usable transfer sample — daily rates unavailable."
    velocity = None
    velocity_total = None
    if daily_vol is not None:
        if primary_supply_usd:
            velocity = round(daily_vol * 365.0 / primary_supply_usd, 3)
        if supply:
            velocity_total = round(daily_vol * 365.0 / supply, 3)
    # Trends: 24h vs prior 24h, 7d vs prior 7d — only when both
    # halves are complete.
    trends = {}
    for days in (1, 7):
        key = f"{days}d_vs_prior_{days}d"
        cur = windows[f"{days}d"]
        pri = prior[f"{days}d"]
        trends[key] = {
            "tx_count": _trend_entry(
                (cur["tx_count"], cur["complete"]),
                (pri["tx_count"], pri["complete"]), "transaction-count"),
            "volume_usd": _trend_entry(
                (cur["volume_usd"], cur["complete"]),
                (pri["volume_usd"], pri["complete"]), "volume"),
            "active_addresses": _trend_entry(
                (cur["unique_addresses"], cur["complete"]),
                (pri["unique_addresses"], pri["complete"]),
                "active-address"),
        }
    # Median transfer size: 24h window when complete, else the sample.
    if windows["1d"]["complete"] and windows["1d"]["median_transfer_usd"] is not None:
        median_basis = "observed_24h"
        median_n = windows["1d"]["tx_count"]
        median_usd = windows["1d"]["median_transfer_usd"]
    else:
        median_basis = "sample"
        median_n = len(items)
        median_usd = sample["median_transfer_usd"]
    largest = sorted(
        (t for t in items if t.get("amount_usd") is not None),
        key=lambda t: -t["amount_usd"],
    )[:5]
    out = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "price_usd": price,
        "price_source": price_source,
        "price_assumed_1_usd": price is None,
        "supply_usd": supply,
        "supply_change_7d_pct": _supply_change(hist, 7),
        "supply_change_30d_pct": _supply_change(hist, 30),
        "primary_chain_supply_usd": primary_supply_usd,
        "primary_chain_share_pct": primary_share,
        "holders_total": holders_total,
        "holders_total_note": holders_total_note,
        "coverage": {
            "chain": primary_label,
            "chain_scope_note": (
                f"All activity figures cover the primary chain only "
                f"({primary_label})."
                + (
                    f" {primary_label} holds {primary_share}% of total "
                    "supply; activity on other chains is not shown."
                    if primary_share is not None else ""
                )
            ),
            "scanned_transfers": scanned,
            "transfers_with_amount": len(items),
            "scan_earliest": earliest,
            "scan_latest": latest,
            "scan_window_seconds": span_s,
            "scan_window_label": _scan_window_label(span_s),
            "scan_exhausted_feed": exhausted,
            "scan_pages": ACTIVITY_PAGES,
            "scan_page_size": ACTIVITY_PAGE_SIZE,
            "status": status,
            "status_note": status_note,
        },
        "windows": windows,
        "sample": sample,
        "daily": {
            "tx_per_day": round(daily_tx, 2) if daily_tx is not None else None,
            "volume_per_day_usd": round(daily_vol, 2) if daily_vol is not None else None,
            "basis": daily_basis,
            "basis_note": basis_note,
        },
        "velocity": {
            "annualized": velocity,
            "annualized_vs_total_supply": velocity_total,
            "basis": daily_basis,
            "denominator": "primary_chain_supply",
            "denominator_usd": primary_supply_usd,
            "interpretation": (
                f"At the current daily rate, each unit of "
                f"{primary_label}-chain supply changes hands about "
                f"{velocity:.1f} times per year on the primary chain."
                if velocity is not None else None
            ),
            "note": (
                "Velocity = daily transfer volume x 365 / primary-chain "
                "supply. The vs-total-supply figure divides the same "
                "primary-chain volume by all-chain supply and is a "
                "LOWER BOUND on all-chain velocity (other chains' "
                "transfers are unobserved)."
            ),
        },
        "median_transfer": {
            "median_transfer_usd": median_usd,
            "basis": median_basis,
            "transfers_counted": median_n,
            "note": (
                "Median transfer size from the observed 24h window."
                if median_basis == "observed_24h" else
                "Median transfer size from the scanned sample (the 24h "
                "window is not fully covered) — the sample skews to "
                "the most recent transfers."
            ),
        },
        "trends": trends,
        "largest_transfers_in_sample": largest,
        "market": {
            "trading_volume_24h_usd": trading_vol_24h,
            "trading_volume_source": "CoinGecko market_data.total_volume (keyless)",
            "note": (
                "CoinGecko 24h trading volume is EXCHANGE trading "
                "volume, NOT on-chain transfer volume — shown for "
                "context only, never mixed into the on-chain figures "
                "above."
            ),
        },
        "methodology": ACTIVITY_METHODOLOGY,
        "sources": ["DeFiLlama stablecoins API (supply by chain)"]
        + (["TronScan keyless API (transfers, holders)"]
           if cfg["primary"] == "tron"
           else ["Blockscout v2 (Ethereum transfers, holders)"])
        + ["CoinGecko keyless API (24h trading volume, context only)"],
    }
    _cache_set(cache_key, out, ttl=ACTIVITY_CACHE_TTL)
    return out


# ---------------------------------------------------------------- peg leaderboard
# Feature 2 — cross-coin peg-stability leaderboard (all 8 scopes).
#
# Episode rule (stated in the response and the UI): an episode is a
# maximal run of consecutive daily closes in the 365-day window with
# |deviation| > 50 bps. "Consecutive" means adjacent entries in the
# sorted daily history with at most a 2-day gap (one missing day is
# tolerated); a longer gap breaks the run, since consecutiveness can
# no longer be established. Recovery days for an episode = whole days
# from its last off-peg close to the first subsequent daily close back
# within 5 bps of $1.00. An episode with no such close yet (still
# off-peg at the end of the history, or never back within 5 bps) is
# "ongoing": it counts in the episode count but NOT in the median/max
# recovery figures, which cover completed episodes only. If no episode
# completed, median/max recovery are null (not zero).
#
# Volatility: sample standard deviation (ddof=1) of daily log returns
# ln(p_t / p_{t-1}) of the DeFiLlama daily closes in the last 30 days,
# in % per day (not annualised). If fewer than 30 daily closes exist
# in the 30-day window, volatility is null — never zero.
#
# Ranking rule: fewest off-peg days in 365d, then smallest max
# |deviation| in 365d, then smallest max |deviation| all-time, then
# symbol. A coin whose history could not be fetched gets an error row
# and ranks last (rank null).
LEADERBOARD_CACHE_TTL = 300
LEADERBOARD_METHODOLOGY = (
    "Per-coin peg stability from DeFiLlama /stablecoinprices daily "
    "closes. Off-peg day: |price - $1| > 50 bps. Ranking: fewest "
    "off-peg days in 365d, then smallest max |deviation| in 365d, "
    "then smallest max |deviation| all-time, then symbol. Episode: a "
    "maximal run of consecutive off-peg daily closes in the 365d "
    "window; recovery days = days from the episode's last off-peg "
    "close to the first subsequent close back within 5 bps of $1.00. "
    "Ongoing episodes (no recovery close yet) count in the episode "
    "count but not in median/max recovery. 30d volatility is the "
    "sample standard deviation (ddof=1) of daily log returns of the "
    "daily closes, in % per day, not annualised; null when fewer "
    "than 30 daily closes exist in the window."
)
LEADERBOARD_CAVEAT = (
    "All figures are from DeFiLlama DAILY CLOSES. Intraday depegs "
    "larger than the daily close are not counted, so every figure "
    "here understates intraday peg instability."
)


def _worst_dev(entries):
    """entries: [{date, price}] -> worst {date, price, deviation_bps}
    by |deviation|, or None."""
    worst = None
    for p in entries:
        bps = _dev_bps(p["price"])
        if bps is None:
            continue
        if worst is None or abs(bps) > abs(worst["deviation_bps"]):
            worst = {"date": p["date"], "price": p["price"], "deviation_bps": bps}
    return worst


def _off_peg_window(entries):
    """Count of off-peg days in `entries` + share of days with data."""
    total = len(entries)
    n = sum(
        1 for p in entries
        if (lambda b: b is not None and abs(b) > DEPEG_BPS)(_dev_bps(p["price"]))
    )
    return {
        "days": n,
        "total_days": total,
        "share_pct": round(n / total * 100.0, 2) if total else None,
    }


def _episodes_365d(window):
    """Depeg episodes in the 365d window (ascending entries).
    Returns {count, ongoing, completed, median_recovery_days,
    max_recovery_days} — see the module comment for the rule."""
    # bps per entry, ascending by date.
    pts = []
    for p in window:
        bps = _dev_bps(p["price"])
        if bps is not None:
            pts.append((p["date"], bps))
    pts.sort(key=lambda t: t[0])
    # Group into episodes: consecutive off-peg entries (gap <= 2 days).
    episodes = []  # each: [first_date, last_date, last_idx]
    cur = None
    for i, (ts, bps) in enumerate(pts):
        if abs(bps) <= DEPEG_BPS:
            cur = None
            continue
        if cur is not None and ts - pts[cur[2]][0] <= 2 * 86400:
            cur[1] = ts
            cur[2] = i
        else:
            cur = [ts, ts, i]
            episodes.append(cur)
    recoveries = []
    ongoing = 0
    for _first, last_ts, last_idx in episodes:
        rec = None
        for ts, bps in pts[last_idx + 1:]:
            if abs(bps) <= 5:
                rec = (ts - last_ts) // 86400
                break
        if rec is None:
            ongoing += 1
        else:
            recoveries.append(rec)
    return {
        "count": len(episodes),
        "ongoing": ongoing,
        "completed": len(recoveries),
        "median_recovery_days": (
            statistics.median(recoveries) if recoveries else None
        ),
        "max_recovery_days": max(recoveries) if recoveries else None,
    }


def _vol_30d(window):
    """Sample std (ddof=1) of daily log returns in the 30d window,
    in % per day. None when fewer than 30 closes in the window."""
    if len(window) < 30:
        return None, len(window), 0
    rets = []
    for prev, cur in zip(window, window[1:]):
        try:
            if prev["price"] > 0 and cur["price"] > 0:
                rets.append(math.log(cur["price"] / prev["price"]))
        except (TypeError, ValueError):
            continue
    if len(rets) < 2:
        return None, len(window), len(rets)
    return round(statistics.stdev(rets) * 100.0, 4), len(window), len(rets)


def _leaderboard_row(scope, cfg, detail, hist, price, price_source, now):
    """One leaderboard row for a coin whose detail + history loaded."""
    hist = sorted(hist, key=lambda p: p["date"])
    bps = _dev_bps(price)
    win = {}
    for days in (30, 90, 365):
        win[days] = [p for p in hist if p["date"] >= now - days * 86400]
    worst30 = _worst_dev(win[30])
    worst365 = _worst_dev(win[365])
    worst_all = _worst_dev(hist)
    vol, vol_points, vol_rets = _vol_30d(win[30])
    eps = _episodes_365d(win[365])
    off30 = _off_peg_window(win[30])
    off90 = _off_peg_window(win[90])
    off365 = _off_peg_window(win[365])
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "rank": None,  # assigned after sorting
        "error": None,
        "price_usd": price,
        "price_source": price_source,
        "deviation_bps": bps,
        "peg_health": _peg_health(bps),
        "max_dev_30d_bps": worst30["deviation_bps"] if worst30 else None,
        "max_dev_30d_date": worst30["date"] if worst30 else None,
        "max_dev_30d_price": worst30["price"] if worst30 else None,
        "max_dev_365d_bps": worst365["deviation_bps"] if worst365 else None,
        "max_dev_365d_date": worst365["date"] if worst365 else None,
        "max_dev_365d_price": worst365["price"] if worst365 else None,
        "max_dev_alltime_bps": worst_all["deviation_bps"] if worst_all else None,
        "max_dev_alltime_date": worst_all["date"] if worst_all else None,
        "max_dev_alltime_price": worst_all["price"] if worst_all else None,
        "off_peg_30d": off30,
        "off_peg_90d": off90,
        "off_peg_365d": off365,
        "episodes_365d": eps,
        "volatility_30d_pct": vol,
        "volatility_30d_points": vol_points,
        "volatility_30d_returns": vol_rets,
        "history_days": len(hist),
        "history_first": hist[0]["date"] if hist else None,
        "history_last": hist[-1]["date"] if hist else None,
        "is_this_scope": False,
    }


def _leaderboard_error_row(scope, err):
    cfg = STABLECOINS[scope]
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "rank": None,
        "error": str(err),
        "price_usd": None,
        "price_source": None,
        "deviation_bps": None,
        "peg_health": None,
        "max_dev_30d_bps": None,
        "max_dev_30d_date": None,
        "max_dev_30d_price": None,
        "max_dev_365d_bps": None,
        "max_dev_365d_date": None,
        "max_dev_365d_price": None,
        "max_dev_alltime_bps": None,
        "max_dev_alltime_date": None,
        "max_dev_alltime_price": None,
        "off_peg_30d": {"days": None, "total_days": 0, "share_pct": None},
        "off_peg_90d": {"days": None, "total_days": 0, "share_pct": None},
        "off_peg_365d": {"days": None, "total_days": 0, "share_pct": None},
        "episodes_365d": {
            "count": None, "ongoing": None, "completed": None,
            "median_recovery_days": None, "max_recovery_days": None,
        },
        "volatility_30d_pct": None,
        "volatility_30d_points": 0,
        "volatility_30d_returns": 0,
        "history_days": 0,
        "history_first": None,
        "history_last": None,
        "is_this_scope": False,
    }


async def sc_peg_leaderboard(this_scope=None):
    """GET /api/<scope>/peg-leaderboard and
    /api/overview/stablecoin-leaderboard — the peg-stability table
    across all 8 stablecoin scopes. A failed coin gets an error row,
    never a failed response. Cached 300s (per-coin price histories
    are themselves cached 600s by _price_history)."""
    cache_key = "scPegLeaderboard"
    cached = _cache_get(cache_key)
    if cached is not None:
        rows = [dict(r) for r in cached["rows"]]
    else:
        now = int(time.time())
        scopes = list(STABLECOINS.keys())
        results = await asyncio.gather(
            *[
                asyncio.gather(
                    _coin_detail(STABLECOINS[s]), _price_history(STABLECOINS[s]),
                    return_exceptions=True,
                )
                for s in scopes
            ],
            return_exceptions=True,
        )
        # Current prices (detail price first; CoinGecko fallback only
        # when the detail carries no price — no extra call otherwise).
        price_jobs = []
        for s, res in zip(scopes, results):
            if isinstance(res, Exception) or isinstance(res[0], Exception):
                price_jobs.append(None)
            else:
                price_jobs.append(_current_price(STABLECOINS[s], res[0]))
        prices = await asyncio.gather(
            *[j for j in price_jobs if j is not None],
            return_exceptions=True,
        )
        price_iter = iter(prices)
        rows = []
        for s, res in zip(scopes, results):
            if isinstance(res, Exception):
                rows.append(_leaderboard_error_row(s, res))
                continue
            detail, hist = res
            if isinstance(detail, Exception):
                rows.append(_leaderboard_error_row(s, detail))
                continue
            if isinstance(hist, Exception):
                rows.append(_leaderboard_error_row(s, hist))
                continue
            pr = next(price_iter)
            price, price_source = pr if not isinstance(pr, Exception) else (None, None)
            rows.append(_leaderboard_row(
                s, STABLECOINS[s], detail, hist, price, price_source, now
            ))
        # Rank: fewest off-peg days 365d, then smallest max |dev| 365d,
        # then smallest max |dev| all-time, then symbol. Error rows last.
        def sort_key(r):
            if r["error"] is not None:
                return (1, 0, 0.0, 0.0, r["symbol"])
            return (
                0,
                r["off_peg_365d"]["days"] if r["off_peg_365d"]["days"] is not None else 10**9,
                abs(r["max_dev_365d_bps"]) if r["max_dev_365d_bps"] is not None else 10**9,
                abs(r["max_dev_alltime_bps"]) if r["max_dev_alltime_bps"] is not None else 10**9,
                r["symbol"],
            )
        rows.sort(key=sort_key)
        for i, r in enumerate(rows):
            if r["error"] is None:
                r["rank"] = i + 1
        _cache_set(cache_key, {"rows": rows}, ttl=LEADERBOARD_CACHE_TTL)
    now = int(time.time())
    for r in rows:
        r["is_this_scope"] = (r["scope"] == this_scope)
    return {
        "rows": rows,
        "this_scope": this_scope,
        "ranking_rule": (
            "Ranked by fewest off-peg days in 365d, then smallest max "
            "|deviation| in 365d, then smallest max |deviation| "
            "all-time, then symbol. Error rows rank last."
        ),
        "methodology": LEADERBOARD_METHODOLOGY,
        "episode_rule": (
            "An episode is a maximal run of consecutive daily closes "
            "in the 365-day window with |deviation| > 50 bps "
            "(consecutive = adjacent daily closes, gap at most 2 "
            "days). Recovery days = whole days from the episode's "
            "last off-peg close to the first subsequent close back "
            "within 5 bps of $1.00. Episodes with no recovery close "
            "yet are ongoing: counted in the episode count, excluded "
            "from median/max recovery."
        ),
        "volatility_note": (
            "30d volatility is the sample standard deviation "
            "(ddof=1) of daily log returns of DeFiLlama daily closes "
            "in the last 30 days, in % per day (not annualised); null "
            "when fewer than 30 daily closes exist in the window."
        ),
        "depeg_threshold_bps": DEPEG_BPS,
        "recovery_threshold_bps": 5,
        "windows_days": [30, 90, 365],
        "caveat": LEADERBOARD_CAVEAT,
        "as_of": now,
        "sources": ["DeFiLlama stablecoins API (/stablecoinprices daily closes, /stablecoin/{id} live price)"],
    }


# ---------------------------------------------------------------- depegs
async def sc_depegs(scope):
    cfg = _cfg(scope)
    hist = await _price_history(cfg)
    evs, worst = _depeg_events(hist)
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "depeg_threshold_bps": DEPEG_BPS,
        "depeg_days_total": len(evs),
        "history_days": len(hist),
        "history_first": hist[0]["date"] if hist else None,
        "history_last": hist[-1]["date"] if hist else None,
        "worst_depeg": worst,
        "most_recent_depeg": evs[0] if evs else None,
        "events": evs,
    }


# ---------------------------------------------------------------- reserves
# Feature 3 — reserve & attestation tracker.
#
# Staleness rule (stated in the response and the UI): days since the
# last verified attestation reporting date vs the coin's cadence.
# next_expected = last_attestation + one cadence interval (monthly:
# +1 calendar month; quarterly: +3). Status: "current" while the next
# reporting date is more than 7 days away, "due_soon" within 7 days
# of it, "overdue" once it has passed. Coins with no dated
# attestation get a non-staleness status instead and are never scored:
# "live" (real-time feed, TUSD), "on_chain" (collateral verifiable
# on-chain, no dated reports — DAI, USDD), "no_attestation" (none
# published — USDe).
#
# Caveat on "overdue": issuers typically publish a report several
# weeks AFTER its reporting date (e.g. Circle's Aug 2026 report was
# signed Sep 29), so a monthly coin reads "overdue" as soon as the
# next reporting date passes even though the report for that date
# cannot yet have been published. "Overdue" here means past the next
# reporting date, not past a publication deadline — the alerts say
# so. FDUSD is the genuinely stale case: 8 months since the last
# retrievable report.
#
# Reserve figures are dated published figures from the config only
# (see the `reserve_figures` blocks above) — never live, never
# estimated. reserve_to_supply_ratio = dated reserve total /
# current DeFiLlama circulating supply, with BOTH dates stated;
# reserve_to_outstanding_ratio = dated reserve total / tokens
# outstanding per the SAME attestation (same-date denominator),
# present only where the attestation states outstanding exactly.
TRACKER_METHODOLOGY = (
    "Staleness = days since the last verified attestation reporting "
    "date vs the coin's cadence; next expected = last attestation + "
    "one cadence interval (monthly +1 month, quarterly +3 months). "
    "Current: next reporting date more than 7 days away. Due soon: "
    "within 7 days. Overdue: past the next reporting date. Issuers "
    "typically publish a report several weeks after its reporting "
    "date, so 'overdue' means past the next reporting date, not "
    "past a publication deadline. Coins with no dated attestation "
    "(real-time feed, on-chain collateral, or none published) are "
    "not scored. Reserve figures are dated published figures from "
    "the named attestation only — never live or estimated. "
    "Reserve-to-supply ratio = dated reserve total / current "
    "DeFiLlama circulating supply, with both dates stated; "
    "reserve-to-outstanding ratio uses the tokens outstanding "
    "stated in the same attestation."
)
_CADENCE_MONTHS = {"monthly": 1, "quarterly": 3}
_DUE_SOON_DAYS = 7


def _parse_iso_date(s):
    try:
        y, m, d = (int(x) for x in str(s).split("-"))
        return date(y, m, d)
    except (TypeError, ValueError):
        return None


def _add_months(d, months):
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _attestation_tracker(cfg, current_supply_usd):
    """Feature 3 tracker for one coin. Pure function of the config
    (+ current supply for the ratio); no network access."""
    att = cfg.get("attestation") or {}
    figs = cfg.get("reserve_figures")
    today = date.today()
    cadence = att.get("cadence")
    auditor = att.get("auditor")
    last_s = att.get("last_attestation")
    last = _parse_iso_date(last_s)
    t = {
        "auditor": auditor,
        "cadence": cadence,
        "last_attestation": last_s,
        "last_attestation_source": att.get("source_url"),
        "last_attestation_note": att.get("note"),
        "as_of": today.isoformat(),
        "days_since": None,
        "status": None,
        "status_label": None,
        "next_expected": None,
        "overdue_by_days": None,
        "reserve_figures": None,
        "alerts": [],
        "methodology": TRACKER_METHODOLOGY,
    }
    if cadence == "real-time":
        t["status"] = "live"
        t["status_label"] = "Live — real-time attestation feed"
    elif cadence == "on-chain":
        t["status"] = "on_chain"
        t["status_label"] = "On-chain collateral — no dated attestation"
    elif cadence == "none" or last is None:
        t["status"] = "no_attestation"
        t["status_label"] = "No reserve attestation published"
        t["alerts"].append(
            f"No reserve attestation is published for {cfg['symbol']} — "
            "backing cannot be checked against a dated report. "
            f"({att.get('note') or 'See the attestation links.'})"
        )
    else:
        months = _CADENCE_MONTHS.get(cadence)
        t["days_since"] = (today - last).days
        if months:
            nxt = _add_months(last, months)
            t["next_expected"] = nxt.isoformat()
            gap = (nxt - today).days
            if gap < 0:
                t["status"] = "overdue"
                t["status_label"] = f"Overdue by {-gap} days"
                t["overdue_by_days"] = -gap
                t["alerts"].append(
                    f"Attestation overdue by {-gap} days vs {cadence} cadence — "
                    f"latest verified report {last_s}; next reporting date was "
                    f"{nxt.isoformat()}. Issuers typically publish several weeks "
                    "after the reporting date, so this means the next report has "
                    "not been verified yet, not necessarily that it is late."
                )
            elif gap <= _DUE_SOON_DAYS:
                t["status"] = "due_soon"
                t["status_label"] = f"Due in {gap} days"
                t["alerts"].append(
                    f"Next attestation (reporting date {nxt.isoformat()}) due in "
                    f"{gap} days."
                )
            else:
                t["status"] = "current"
                t["status_label"] = "Current"
        else:
            t["status"] = "current"
            t["status_label"] = "Current"
    if figs:
        total = figs.get("reserve_total_usd")
        outstanding = figs.get("tokens_outstanding")
        rf = {
            "as_of": figs.get("as_of"),
            "source": figs.get("source"),
            "source_url": figs.get("source_url"),
            "reserve_total_usd": total,
            "reserve_total_approx": bool(figs.get("reserve_total_approx")),
            "tokens_outstanding": outstanding,
            "excess_usd": figs.get("excess_usd"),
            "reserve_mix": figs.get("reserve_mix"),
            "mix_note": figs.get("mix_note"),
            "reserve_to_supply_ratio": None,
            "ratio_supply_usd": current_supply_usd,
            "ratio_supply_as_of": today.isoformat() if current_supply_usd else None,
            "reserve_to_outstanding_ratio": None,
        }
        if total and current_supply_usd:
            rf["reserve_to_supply_ratio"] = round(total / current_supply_usd, 4)
        if total and outstanding:
            rf["reserve_to_outstanding_ratio"] = round(total / outstanding, 4)
        t["reserve_figures"] = rf
    return t


async def sc_reserves(scope):
    cfg = _cfg(scope)
    detail, gecko = await asyncio.gather(
        _coin_detail(cfg), _gecko(cfg), return_exceptions=True
    )
    if isinstance(detail, Exception):
        raise detail if isinstance(detail, UpstreamError) else UpstreamError(str(detail))
    if isinstance(gecko, Exception):
        gecko = None
    md = (gecko or {}).get("market_data") or {}
    chains, chain_total = _chain_breakdown(detail)
    hist = _supply_history(detail)
    current_supply = chain_total or (hist[-1]["supply_usd"] if hist else None)
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "issuer": cfg["issuer"],
        "peg_mechanism": detail.get("pegMechanism"),
        "description": detail.get("description"),
        "mint_redeem": detail.get("mintRedeemDescription"),
        "attestations": cfg["links"]["attestations"],
        "website": cfg["links"]["website"],
        "whitepaper": cfg["links"].get("whitepaper"),
        "attestation_tracker": _attestation_tracker(cfg, current_supply),
        "reserve_figures": None,
        "reserve_figures_note": "No keyless machine-readable API exposes live reserve / collateral figures for this coin (probed 2026-11-05). The figures below are market figures, not reserves.",
        "market_figures": {
            "market_cap_usd": _num((md.get("market_cap") or {}).get("usd")),
            "total_supply": _num(md.get("total_supply")),
            "circulating_supply": _num(md.get("circulating_supply")),
            "source": "CoinGecko keyless API",
        },
        "note": cfg["reserves_note"],
    }


# ---------------------------------------------------------------- chains
async def sc_chains(scope):
    cfg = _cfg(scope)
    detail = await _coin_detail(cfg)
    chains, total = _chain_breakdown(detail)
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "chains": chains,
        "chain_count": len(chains),
        "total_supply_usd": total,
    }


# ---------------------------------------------------------------- supply map
# Feature 5 — cross-chain supply map.
#
# Supply by chain (DeFiLlama currentChainBalances, same source as
# /chains) enriched with, per chain:
# - net supply change over 7d / 30d, from the per-chain daily history
#   in /stablecoin/{id} `chainBalances` ({chain: {tokens: [{date,
#   circulating: {peggedUSD}, bridgedTo: {...}}]}}). This is NET
#   supply change on that chain — bridges moving supply in/out plus
#   local mint/burn combined. The two cannot be separated from supply
#   data alone, so the response never calls it "bridge flow".
# - bridged-in stock: the latest history entry's `bridgedTo` says how
#   much of the chain's current supply arrived via each bridge, and
#   from which source chain (amounts are token units; converted to USD
#   at the live price, ~$1 for a pegged coin, and labelled approximate).
#   This is a STOCK (how much sits bridged-in now), not a flow — no
#   keyless source gives per-coin bridge flows over a window:
#   Wormholescan's /transactions ignores the `symbol` filter (probed
#   2026-11-06: symbol=USDC returned SOL transfers), and DeFiLlama's
#   bridges API is paywalled (probed 2026-11-06).
#
# Covered share: currentChainBalances sums to slightly less than the
# latest daily total (different snapshot times / chains DeFiLlama
# does not break out). The covered share is shown explicitly and the
# remainder is labelled unknown — a partial sum is never presented as
# the total supply.
SUPPLY_MAP_CACHE_TTL = 300
SUPPLY_MAP_METHODOLOGY = (
    "Per-chain supply is DeFiLlama currentChainBalances. Per-chain "
    "7d/30d change is the net change in that chain's own daily "
    "circulating series (chainBalances) — net supply change on the "
    "chain, i.e. bridge moves plus local mint/burn combined, NOT a "
    "bridge-flow measurement. Bridged-in amounts are the latest "
    "daily entry's bridgedTo stock per bridge/source chain (token "
    "units converted to USD at the live price, approximate) — how "
    "much supply currently sits bridged-in, not how much moved in a "
    "window. No keyless source reports per-coin bridge flows over a "
    "window (Wormholescan ignores the symbol filter; DeFiLlama "
    "bridges is paywalled). Covered share = chain-mapped supply / "
    "latest daily total supply; the remainder is unattributed, never "
    "folded into a chain."
)


def _chain_series(detail, chain):
    """Per-chain daily supply series from chainBalances:
    [{date, supply_usd, bridged}] ascending. Empty list when the
    chain has no history (or the shape is unexpected)."""
    cb = detail.get("chainBalances") or {}
    entry = cb.get(chain)
    toks = (entry or {}).get("tokens") if isinstance(entry, dict) else None
    out = []
    if not isinstance(toks, list):
        return out
    for e in toks:
        if not isinstance(e, dict):
            continue
        try:
            ts = int(e.get("date") or 0)
        except (TypeError, ValueError):
            continue
        v = _num((e.get("circulating") or {}).get("peggedUSD"))
        if ts <= 0 or v is None:
            continue
        out.append({"date": ts, "supply_usd": v, "bridged": e.get("bridgedTo")})
    out.sort(key=lambda x: x["date"])
    return out


def _series_change(series, days):
    """Net change over the last `days` days of a per-chain series.
    Returns {net_usd, pct, start_date, end_date, window_days,
    partial} — partial when the series starts inside the window (or
    has a gap, so the covered span differs from the request). None
    values when the series is too short."""
    empty = {"net_usd": None, "pct": None, "start_date": None,
             "end_date": None, "window_days": 0, "partial": True}
    if len(series) < 2:
        return empty
    end = series[-1]
    cutoff = end["date"] - days * 86400
    past = [s for s in series if s["date"] <= cutoff]
    start = past[-1] if past else series[0]
    net = end["supply_usd"] - start["supply_usd"]
    actual = (end["date"] - start["date"]) // 86400
    return {
        "net_usd": round(net, 2),
        "pct": round(net / start["supply_usd"] * 100.0, 2) if start["supply_usd"] else None,
        "start_date": start["date"],
        "end_date": end["date"],
        "window_days": int(actual),
        "partial": actual != days,
    }


def _bridge_label(key):
    """Display label for a DeFiLlama bridgedTo bridge key. Most keys
    are readable bridge names (wormhole, bsc, polygon, allbridge).
    Opaque keys are labelled, never renamed to a guessed bridge:
    'not-found' and UUID/hex/factory-style keys are DeFiLlama's
    unmapped entries, and 'ibc/<hash>' keys are IBC denominations."""
    if key == "not-found":
        return "Unlabelled bridge"
    if key.startswith("ibc/"):
        return "IBC"
    if key.startswith("factory/"):
        return "Unlabelled bridge"
    if len(key) >= 32 and all(ch in "0123456789abcdef-" for ch in key.lower()):
        return "Unlabelled bridge"
    return key


def _bridged_breakdown(bridged, price_usd):
    """Latest entry's bridgedTo -> ({bridge: {total_usd, sources:
    [{from_chain, amount_usd}]}}, bridged_total_usd). Amounts are
    token units; converted at the live price (or $1.00 if none)."""
    total = 0.0
    by_bridge = {}
    if not isinstance(bridged, dict):
        return by_bridge, total
    bridges = bridged.get("bridges") or {}
    if not isinstance(bridges, dict):
        return by_bridge, total
    px = price_usd or 1.0
    for bridge, srcs in bridges.items():
        if not isinstance(srcs, dict):
            continue
        b_total = 0.0
        sources = []
        for from_chain, v in srcs.items():
            amt = _num((v or {}).get("amount")) if isinstance(v, dict) else _num(v)
            if amt is None or amt <= 0:
                continue
            usd = amt * px
            b_total += usd
            sources.append({"from_chain": from_chain, "amount_usd": round(usd, 2)})
        if b_total > 0:
            sources.sort(key=lambda s: -s["amount_usd"])
            by_bridge[bridge] = {"total_usd": round(b_total, 2), "sources": sources}
            total += b_total
    return by_bridge, round(total, 2)


async def sc_supply_map(scope):
    cfg = _cfg(scope)
    key = f"scSupplyMap:{scope}"
    hit = _cache_get(key)
    if hit is not None:
        return hit
    detail = await _coin_detail(cfg)
    price, _src = await _current_price(cfg, detail)
    hist = _supply_history(detail)
    chains, chain_total = _chain_breakdown(detail)
    total_supply = hist[-1]["supply_usd"] if hist else chain_total
    as_of = hist[-1]["date"] if hist else None
    covered_share = (
        round(chain_total / total_supply * 100.0, 2)
        if total_supply else None
    )
    unknown = (total_supply - chain_total) if total_supply else None
    timing_note = None
    if unknown is not None and unknown < 0:
        # Chain snapshot is marginally newer than the daily total.
        timing_note = (
            "Chain-mapped supply slightly exceeds the latest daily total "
            "($%.2fM) — the two come from different snapshot times; "
            "unattributed supply is shown as $0." % (abs(unknown) / 1e6)
        )
        unknown = 0.0
    rows = []
    bridge_totals = {}
    bridged_grand = 0.0
    for c in chains:
        series = _chain_series(detail, c["chain"])
        ch7 = _series_change(series, 7)
        ch30 = _series_change(series, 30)
        by_bridge, bridged = _bridged_breakdown(
            series[-1]["bridged"] if series else None, price
        )
        bridged_grand += bridged
        bridges = []
        for name, b in sorted(by_bridge.items(), key=lambda kv: -kv[1]["total_usd"]):
            bridges.append({
                "bridge": name,
                "bridge_label": _bridge_label(name),
                "amount_usd": b["total_usd"],
                "share_of_chain_pct": round(b["total_usd"] / c["supply_usd"] * 100.0, 2) if c["supply_usd"] else None,
                "sources": b["sources"],
            })
            bt = bridge_totals.setdefault(name, {"total_usd": 0.0, "chains": set()})
            bt["total_usd"] += b["total_usd"]
            bt["chains"].add(c["chain"])
        rows.append({
            "chain": c["chain"],
            "supply_usd": c["supply_usd"],
            "share_pct": c["share_pct"],
            "share_of_total_pct": round(c["supply_usd"] / total_supply * 100.0, 2) if total_supply else None,
            "change_7d": ch7,
            "change_30d": ch30,
            "bridged_usd": bridged,
            "bridged_share_pct": round(bridged / c["supply_usd"] * 100.0, 2) if c["supply_usd"] else None,
            "bridges": bridges,
        })
    bridges_summary = [
        {
            "bridge": name,
            "bridge_label": _bridge_label(name),
            "total_usd": round(b["total_usd"], 2),
            "share_of_bridged_pct": round(b["total_usd"] / bridged_grand * 100.0, 2) if bridged_grand else None,
            "chains": len(b["chains"]),
        }
        for name, b in sorted(bridge_totals.items(), key=lambda kv: -kv[1]["total_usd"])
    ]
    out = {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "primary_chain": cfg["primary"],
        "contract": cfg["contract"],
        "as_of": as_of,
        "total_supply_usd": total_supply,
        "covered_supply_usd": round(chain_total, 2),
        "covered_share_pct": covered_share,
        "unknown_supply_usd": round(unknown, 2) if unknown is not None else None,
        "timing_note": timing_note,
        "chain_count": len(rows),
        "chains": rows,
        "bridged_total_usd": round(bridged_grand, 2),
        "bridges_summary": bridges_summary,
        "methodology": SUPPLY_MAP_METHODOLOGY,
        "sources": ["DeFiLlama stablecoins API (currentChainBalances + chainBalances)"],
    }
    _cache_set(key, out, ttl=SUPPLY_MAP_CACHE_TTL)
    return out


# ---------------------------------------------------------------- search
async def sc_search(scope, q):
    cfg = _cfg(scope)
    q = (q or "").strip()
    if not q:
        raise UpstreamError("Empty search", status=400)
    if cfg["primary"] == "tron":
        if q.startswith("T") and 33 <= len(q) <= 35:
            return {"type": "holder", "address": q, "chain": "tron"}
        if len(q) == 64 and all(c in "0123456789abcdefABCDEF" for c in q):
            return {"type": "transaction", "txid": q, "chain": "tron"}
        raise UpstreamError("No match: expected a Tron address (T...) or a transaction id", status=404)
    if q.startswith("0x") and len(q) == 42 and all(c in "0123456789abcdefABCDEF" for c in q[2:]):
        return {"type": "holder", "address": q, "chain": "ethereum"}
    if q.startswith("0x") and len(q) == 66 and all(c in "0123456789abcdefABCDEF" for c in q[2:]):
        return {"type": "transaction", "hash": q, "chain": "ethereum"}
    raise UpstreamError("No match: expected an Ethereum address (0x + 40 hex) or a transaction hash (0x + 64 hex)", status=404)


# ---------------------------------------------------------------- DeFi deployment & yield
# Feature 9 — DeFi deployment & yield.
#
# Source: DeFiLlama yields API, https://yields.llama.fi/pools
# (keyless; one ~11MB JSON snapshot of ~15,700 pools, fetched once
# and cached 300s server-side — the same snapshot serves all 8 coins,
# so the overview rollup does not re-download it per coin). Each pool
# carries symbol, project, chain, tvlUsd, apy / apyBase / apyReward /
# apyMean30d, poolMeta (fee tier / market variant), stablecoin flag,
# exposure ("single" vs multi-asset), ilRisk, outlier flag, and the
# pool id. There is NO borrow-rate field in this API: DeFiLlama's
# borrow dataset (/poolsBorrow) answers HTTP 402 "Upgrade to the paid
# API plan" (verified 2026-11-06), so borrow APYs are reported
# unavailable, never estimated.
#
# Matching (symbol-based — the yields API has no token-address
# index): the coin's yields symbol is its ticker, except USDe, which
# the yields API lists as "USDE" (verified 2026-11-06: 27 USDE pools,
# 0 "USDe" pools). A lending market is a pool with exposure "single"
# and symbol == the coin's yields symbol whose project is in
# LENDING_PROJECTS (lending markets only — yield vaults/aggregators
# such as Yearn, Beefy, Harvest, Midas RWA and Centrifuge are
# excluded; their APYs are vault yields, not lending rates). An LP
# position is a pool whose hyphen-split symbol contains the coin as
# one leg (e.g. DAI-USDC-USDT, WETH-USDC). `outlier` pools are
# excluded everywhere (DeFiLlama flags them as suspicious).
#
# Honest limits, stated in every response:
# - APYs are a snapshot: they move constantly (utilisation-driven).
#   Every response carries as_of (fetch time) and the source.
# - No single pool's APY is "the" yield: the lending section always
#   shows the RANGE (min–max) across venues plus median and
#   TVL-weighted mean.
# - LP pool TVL counts the WHOLE pool (both sides), not just this
#   coin's side, so LP TVL and total DeFi TVL are exposure measures
#   (upper bounds on the coin's own deployed amount), never the
#   coin's own balance in DeFi. Concentrated-liquidity DEXes report
#   one yields entry per position range with the full pool TVL, so LP
#   rows are deduplicated to one row per (project, chain, pair) —
#   the largest entry — before summing or ranking.
# - Collateral: the yields API shows where the coin is SUPPLIED, not
#   whether a market accepts it as collateral, and no keyless source
#   exposes live per-market collateral eligibility, LTV or
#   liquidation thresholds. The collateral section therefore lists
#   the lending markets the coin is supplied in, states the general
#   per-protocol mechanics (Aave: supplied assets can generally be
#   enabled as collateral; Compound V3: the supplied base asset is
#   NOT collateral — only listed collateral assets can be borrowed
#   against; Morpho Blue: collateral depends on the individual
#   market), and says plainly that live LTVs and the borrowable-asset
#   list per market are unavailable keyless.
YIELDS_BASE = "https://yields.llama.fi"
DEFI_CACHE_TTL = 300
DEFI_LENDING_TOP = 10
DEFI_LP_TOP = 10
DEFI_MIN_TVL_USD = 100000.0  # pools below this are too small for their APY to be a meaningful venue rate — excluded from APY range stats
_YIELDS_LOCK = asyncio.Lock()
# Yields-API symbol per scope. USDe is "USDE" in the yields API.
DEFI_YIELDS_SYMBOL = {
    "usdtscope": "USDT", "usdcscope": "USDC", "daiscope": "DAI",
    "usdescope": "USDE", "fdusdsscope": "FDUSD", "pyusdsscope": "PYUSD",
    "tusdscope": "TUSD", "usddscope": "USDD",
}
# Lending markets only (project slugs as listed by the yields API).
# Vaults / aggregators / RWA vaults are deliberately NOT in this set:
# beefy, yearn-finance, harvest-finance, midas-rwa,
# centrifuge-protocol, ember, accountable, symbiosis, stargate-v1,
# ipor-fusion, zerobase-cedefi, evaa-protocol, bitway-earn,
# native-credit-pool, sentora-curator, project-0, across, vesper,
# kiloex, kinza-finance, spark-savings (Spark's savings-rate vault —
# a savings product, not the SparkLend lending market), etc. Their
# APYs are vault yields, not lending rates. Slugs that do not occur in
# the yields data simply never match — the set is safe to extend.
LENDING_PROJECTS = {
    "aave-v3", "aave-v4", "compound-v2", "compound-v3", "morpho-blue",
    "sparklend", "euler-v2", "fluid-lending",
    "kamino-lend", "jupiter-lend", "venus-core-pool", "moonwell-lending",
    "justlend-v1", "save", "silo-v2", "gearbox", "radiant-v2",
    "marginfi", "solend", "benqi", "tectonic", "scallop-lend",
    "navi-lending", "lista-lending", "exactly", "flux-finance",
    "rhea-lend", "hyperlend-pooled", "hypurrfi-pooled", "seamless",
}
# The named majors whose absence for a coin is reported explicitly
# (a missing major is stated, never silently omitted).
DEFI_MAJOR_LENDING = [
    "aave-v3", "aave-v4", "compound-v2", "compound-v3", "morpho-blue",
    "sparklend",
]
LENDING_LABELS = {
    "aave-v3": "Aave V3", "aave-v4": "Aave V4",
    "compound-v2": "Compound V2", "compound-v3": "Compound V3",
    "morpho-blue": "Morpho Blue", "sparklend": "SparkLend",
    "euler-v2": "Euler V2",
    "fluid-lending": "Fluid", "kamino-lend": "Kamino Lend",
    "jupiter-lend": "Jupiter Lend", "venus-core-pool": "Venus",
    "moonwell-lending": "Moonwell", "justlend-v1": "JustLend",
    "save": "Save", "silo-v2": "Silo V2", "gearbox": "Gearbox",
    "radiant-v2": "Radiant V2", "marginfi": "Marginfi",
    "solend": "Solend", "benqi": "BENQI", "tectonic": "Tectonic",
    "scallop-lend": "Scallop", "navi-lending": "NAVI",
    "lista-lending": "Lista Lending", "exactly": "Exactly",
    "flux-finance": "Flux Finance", "rhea-lend": "Rhea",
    "hyperlend-pooled": "HyperLend", "hypurrfi-pooled": "HypurrFi",
    "seamless": "Seamless",
}
DEX_LABELS = {
    "curve-dex": "Curve", "uniswap-v3": "Uniswap V3",
    "uniswap-v2": "Uniswap V2", "uniswap-v4": "Uniswap V4",
    "raydium-amm": "Raydium", "orca": "Orca",
    "balancer-v2": "Balancer V2", "pancakeswap-amm": "PancakeSwap",
    "pancakeswap-amm-v3": "PancakeSwap V3", "sushiswap": "SushiSwap",
    "sunswap": "SunSwap", "sunswap-v2": "SunSwap V2",
    "sunswap-v3": "SunSwap V3", "aerodrome-slipstream": "Aerodrome",
    "velodrome-v2": "Velodrome", "camelot-v3": "Camelot",
    "trader-joe-dex": "Trader Joe", "spookyswap": "SpookySwap",
    "quickswap-dex": "QuickSwap", "maverick-amm": "Maverick",
    "ramses": "Ramses", "equalizer": "Equalizer",
    "stellar-dex": "Stellar DEX", "phoenix": "Phoenix",
    "meteora-amm": "Meteora", "lifinity": "Lifinity",
    "gmx-evm": "GMX", "fluid-dex": "Fluid DEX",
}
# General per-protocol collateral mechanics. These are protocol
# mechanics, NOT live per-market data — no keyless source exposes
# live collateral eligibility / LTV / liquidation thresholds.
COLLATERAL_MECHANICS = [
    {
        "protocol": "Aave V3 / V4",
        "rule": "Supplied assets can generally be enabled as collateral (borrow against them), except assets in isolation mode or siloed markets — eligibility is per-market, not visible in the yields data.",
    },
    {
        "protocol": "Compound V3 (Comet)",
        "rule": "The supplied BASE asset is NOT collateral: supplying USDC to a USDC Comet market earns yield but cannot be borrowed against. Only the market's listed collateral assets (e.g. ETH, WBTC) can be borrowed against.",
    },
    {
        "protocol": "Morpho Blue",
        "rule": "Each market is one collateral-asset / loan-asset pair — the coin is collateral only in markets where it is the collateral asset, and a loan asset in markets where it is borrowed. The yields data does not say which side a pool is.",
    },
    {
        "protocol": "SparkLend",
        "rule": "Aave V3 fork — same mechanics as Aave: supplied assets can generally be enabled as collateral, per-market exceptions apply.",
    },
]
DEFI_METHODOLOGY = (
    "Lending rates are DeFiLlama yields supply APYs (base + reward) "
    "for single-asset pools on lending markets only — vaults and "
    "aggregators are excluded. LP positions are the largest "
    "DeFiLlama yields pools whose pair contains the coin, deduplicated "
    "to one row per (DEX, chain, pair) by largest TVL. Pool TVL for LP "
    "pools counts the whole pool (both sides), so LP TVL and total "
    "DeFi TVL are exposure measures (upper bounds), not the coin's own "
    "deployed balance. Outlier pools (DeFiLlama-flagged) are excluded "
    "everywhere, and pools below $100,000 TVL are excluded from the "
    "APY range — a $2k pool quoting 37% is not a venue rate. APYs are a "
    "snapshot at as_of — they move constantly with utilisation; the "
    "range across venues is the yield, never one pool's APY. Borrow "
    "APYs are unavailable keyless (DeFiLlama /poolsBorrow requires a "
    "paid plan, HTTP 402 verified 2026-11-06). Live per-market "
    "collateral eligibility, LTV and liquidation thresholds are not "
    "exposed by any keyless API."
)


def _apy(v):
    """APY percent rounded to 2dp, or None."""
    x = _num(v)
    return round(x, 2) if x is not None else None


def _tvl(v):
    x = _num(v)
    return round(x, 2) if x is not None else None


async def _yields_pools():
    """DeFiLlama yields /pools snapshot (cached 300s, shared by all 8
    coins). Returns (pools, fetched_at_epoch). A module-level lock
    keeps a cold overview rollup from downloading the ~11MB snapshot
    8 times concurrently."""
    key = "yieldsPools"
    hit = _cache_get(key)
    if hit is not None:
        return hit["pools"], hit["fetched_at"]
    async with _YIELDS_LOCK:
        hit = _cache_get(key)
        if hit is not None:
            return hit["pools"], hit["fetched_at"]
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0), headers=HEADERS,
                follow_redirects=True,
            ) as c:
                r = await c.get(YIELDS_BASE + "/pools")
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as e:
            raise UpstreamError(f"DeFiLlama yields unreachable ({type(e).__name__})")
        if r.status_code >= 400:
            raise UpstreamError(f"DeFiLlama yields returned HTTP {r.status_code}")
        try:
            d = r.json()
        except Exception:
            raise UpstreamError("DeFiLlama yields returned invalid JSON")
        pools = (d or {}).get("data")
        if not isinstance(pools, list):
            raise UpstreamError("DeFiLlama yields returned unexpected data")
        fetched_at = int(time.time())
        _cache_set(key, {"pools": pools, "fetched_at": fetched_at}, ttl=DEFI_CACHE_TTL)
        return pools, fetched_at


def _lending_row(p):
    return {
        "protocol": p.get("project"),
        "protocol_label": LENDING_LABELS.get(p.get("project"), p.get("project")),
        "chain": p.get("chain"),
        "pool_id": p.get("pool"),
        "pool_meta": p.get("poolMeta"),
        "supply_apy_pct": _apy(p.get("apy")),
        "base_apy_pct": _apy(p.get("apyBase")),
        "reward_apy_pct": _apy(p.get("apyReward")),
        "apy_30d_mean_pct": _apy(p.get("apyMean30d")),
        "tvl_usd": _tvl(p.get("tvlUsd")),
    }


def _lp_row(p):
    return {
        "pair": p.get("symbol"),
        "project": p.get("project"),
        "project_label": DEX_LABELS.get(p.get("project"), p.get("project")),
        "chain": p.get("chain"),
        "pool_id": p.get("pool"),
        "fee_tier": p.get("poolMeta"),
        "tvl_usd": _tvl(p.get("tvlUsd")),
        "apy_pct": _apy(p.get("apy")),
        "base_apy_pct": _apy(p.get("apyBase")),
        "reward_apy_pct": _apy(p.get("apyReward")),
        "volume_24h_usd": _tvl(p.get("volumeUsd1d")),
        "stable_pair": bool(p.get("stablecoin")),
        "il_risk": p.get("ilRisk"),
    }


async def sc_defi(scope):
    """GET /api/<scope>/defi — feature 9: DeFi deployment & yield.
    Lending rates (supply APY range across major lending markets),
    largest LP pools holding the coin, and collateral usage (where the
    coin is supplied, with the honest limits on what is not knowable
    keyless). Cached 300s (the yields snapshot is shared across
    coins)."""
    cfg = _cfg(scope)
    ysym = DEFI_YIELDS_SYMBOL[scope]
    pools, fetched_at = await _yields_pools()
    detail = await _coin_detail(cfg)
    _chains, supply = _chain_breakdown(detail)

    # --- single-asset pools for this coin (lending candidates) ---
    single = [
        p for p in pools
        if isinstance(p, dict) and not p.get("outlier")
        and (p.get("symbol") or "").upper() == ysym
        and p.get("exposure") == "single"
    ]
    lending_pools = [p for p in single if (p.get("project") or "") in LENDING_PROJECTS]
    lending_rows = sorted(
        (_lending_row(p) for p in lending_pools),
        key=lambda r: -(r["tvl_usd"] or 0),
    )
    lending_tvl = round(sum(r["tvl_usd"] or 0 for r in lending_rows), 2)
    # APY range stats over markets with a reported APY and >= $1k TVL.
    ranged = [r for r in lending_rows
              if r["supply_apy_pct"] is not None and (r["tvl_usd"] or 0) >= DEFI_MIN_TVL_USD]
    if ranged:
        apys = [r["supply_apy_pct"] for r in ranged]
        tvls = [r["tvl_usd"] or 0 for r in ranged]
        wsum = sum(a * t for a, t in zip(apys, tvls))
        tsum = sum(tvls)
        best = max(ranged, key=lambda r: (r["supply_apy_pct"], r["tvl_usd"] or 0))
        apy_range = {
            "min_pct": min(apys),
            "max_pct": max(apys),
            "median_pct": round(statistics.median(apys), 2),
            "tvl_weighted_pct": round(wsum / tsum, 2) if tsum else None,
            "markets_counted": len(ranged),
        }
        best_row = {
            "protocol": best["protocol"], "protocol_label": best["protocol_label"],
            "chain": best["chain"], "supply_apy_pct": best["supply_apy_pct"],
            "tvl_usd": best["tvl_usd"],
        }
    else:
        apy_range = {"min_pct": None, "max_pct": None, "median_pct": None,
                     "tvl_weighted_pct": None, "markets_counted": 0}
        best_row = None
    present = sorted({r["protocol"] for r in lending_rows})
    absent = [p for p in DEFI_MAJOR_LENDING if p not in present]
    # Top protocols by lending TVL (for the collateral summary).
    by_proto = {}
    for r in lending_rows:
        by_proto[r["protocol_label"]] = by_proto.get(r["protocol_label"], 0) + (r["tvl_usd"] or 0)
    top_protocols = sorted(
        ({"protocol_label": k, "tvl_usd": round(v, 2)} for k, v in by_proto.items()),
        key=lambda x: -x["tvl_usd"],
    )[:5]

    # --- LP pools holding the coin (dedupe one row per pair) ---
    lp_all = [
        p for p in pools
        if isinstance(p, dict) and not p.get("outlier")
        and (p.get("symbol") or "").upper() != ysym
        and ysym in (p.get("symbol") or "").upper().split("-")
    ]
    deduped = {}
    for p in lp_all:
        # Normalise the pair key: "CC-USDC" and "USDC-CC" are the same
        # pair listed in opposite leg order — sort the legs so they
        # dedupe to one row (the largest entry wins).
        legs = tuple(sorted((p.get("symbol") or "").upper().split("-")))
        k = (p.get("project"), p.get("chain"), legs)
        cur = deduped.get(k)
        if cur is None or (_num(p.get("tvlUsd")) or 0) > (_num(cur.get("tvlUsd")) or 0):
            deduped[k] = p
    lp_rows = sorted(
        (_lp_row(p) for p in deduped.values()),
        key=lambda r: -(r["tvl_usd"] or 0),
    )
    lp_tvl = round(sum(r["tvl_usd"] or 0 for r in lp_rows), 2)
    top_lp = lp_rows[0] if lp_rows else None

    defi_tvl = round(lending_tvl + lp_tvl, 2)
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "as_of": fetched_at,
        "supply_usd": supply,
        "lending": {
            "markets": lending_rows[:DEFI_LENDING_TOP],
            "markets_shown": min(len(lending_rows), DEFI_LENDING_TOP),
            "markets_total": len(lending_rows),
            "tvl_usd": lending_tvl,
            "supply_apy_range_pct": apy_range,
            "best": best_row,
            "majors_present": [LENDING_LABELS.get(p, p) for p in present if p in DEFI_MAJOR_LENDING],
            "majors_absent": [LENDING_LABELS.get(p, p) for p in absent],
            "single_asset_pools_total": len(single),
            "non_lending_single_asset_pools": len(single) - len(lending_pools),
            "coverage_note": (
                "Lending table covers lending markets only (Aave, Compound, "
                "Morpho, Spark, Euler, Fluid, Kamino, Jupiter Lend, Venus, "
                "Moonwell, JustLend, Save, Silo, Gearbox and similar). "
                f"{len(single) - len(lending_pools)} single-asset pools for "
                "this coin are yield vaults / aggregators / RWA vaults, not "
                "lending markets, and are excluded from the lending table "
                "and lending TVL."
            ),
        },
        "lp": {
            "pools": lp_rows[:DEFI_LP_TOP],
            "pools_shown": min(len(lp_rows), DEFI_LP_TOP),
            "pairs_total": len(lp_rows),
            "tvl_usd": lp_tvl,
            "top_pair": (
                {"pair": top_lp["pair"], "project_label": top_lp["project_label"],
                 "chain": top_lp["chain"], "tvl_usd": top_lp["tvl_usd"],
                 "apy_pct": top_lp["apy_pct"]} if top_lp else None
            ),
            "note": (
                "One row per (DEX, chain, pair) — the largest pool for that "
                "pair. Pool TVL counts the whole pool (both sides), not just "
                "this coin's side. APY is mostly trading fees; for volatile "
                "pairs it comes with impermanent-loss risk (il_risk shown)."
            ),
        },
        "collateral": {
            "supplied_in_lending_markets": len(lending_rows),
            "supplied_tvl_usd": lending_tvl,
            "top_protocols_by_supplied_tvl": top_protocols,
            "mechanics": COLLATERAL_MECHANICS,
            "borrow_apy_status": (
                "Unavailable keyless — DeFiLlama's borrow dataset "
                "(/poolsBorrow) requires a paid plan (HTTP 402, verified "
                "2026-11-06). No borrow APY is shown or estimated."
            ),
            "borrowable_against_note": (
                "What can be borrowed against the coin depends on the "
                "market: on Aave/Spark, supplied collateral can generally "
                "borrow any borrowable asset listed in that market; on "
                "Compound V3, supplying the base asset earns yield but "
                "cannot be borrowed against at all. No keyless API lists "
                "the borrowable assets, LTV, or liquidation threshold per "
                "market live — check the protocol's own market page for "
                "the live list (Aave: app.aave.com; Compound: "
                "app.compound.finance; Morpho: app.morpho.org)."
            ),
        },
        "totals": {
            "defi_tvl_usd": defi_tvl,
            "lending_tvl_usd": lending_tvl,
            "lp_tvl_usd": lp_tvl,
            "defi_tvl_share_of_supply_pct": (
                round(defi_tvl / supply * 100.0, 2) if supply else None
            ),
        },
        "methodology": DEFI_METHODOLOGY,
        "sources": [
            "DeFiLlama yields API (yields.llama.fi/pools)",
            "DeFiLlama stablecoins API (total supply)",
        ],
    }


# ---------------------------------------------------------------- docs
SC_DOCS = [
    {"method": "GET", "suffix": "/stats", "description": "Coin overview: live price vs $1, deviation in bps, peg-health badge, total supply (DeFiLlama circulating), 7d/30d supply change, per-chain breakdown, holder count on the primary chain, market-cap figures.", "params": [], "example": "/stats"},
    {"method": "GET", "suffix": "/peg", "description": "Peg status + daily price history (?days=7|30|90, default 30): price points with deviation in bps, depeg days in window, worst depeg. Source: DeFiLlama /stablecoinprices.", "params": ["days"], "example": "/peg?days=90"},
    {"method": "GET", "suffix": "/supply", "description": "Supply history (?days=90, default 90): daily circulating supply (DeFiLlama), per-chain breakdown, recent daily mint/burn deltas (last 7 days).", "params": ["days"], "example": "/supply?days=90"},
    {"method": "GET", "suffix": "/holders", "description": "Top holders on the primary chain with balances + USD values (Ethereum: Blockscout; USDD: TronScan, capped at 10,000). Paginate with the returned next_page_params (EVM) or ?start= (Tron).", "params": ["start"], "example": "/holders"},
    {"method": "GET", "suffix": "/transfers", "description": "Recent transfers on the primary chain with amounts + USD values (Ethereum: Blockscout; USDD: TronScan, newest first). Paginate with next_page_params (EVM) or ?start= (Tron).", "params": ["start"], "example": "/transfers"},
    {"method": "GET", "suffix": "/depegs", "description": "Every day in the full price history where |price - $1| > 50 bps, newest first, with worst and most-recent depeg.", "params": [], "example": "/depegs"},
    {"method": "GET", "suffix": "/depeg-score", "description": "Depeg early-warning score (0-100, higher = higher depeg risk) with the four Par v0 input sub-scores and their evidence: redemption quality 40% (current deviation + 365d depeg history proxy), reserve liquidity 25% (attestation-staleness proxy — NOT a direct reserve measurement; unknown where no attestation date could be verified), secondary-market depth 20% (DexScreener near-par liquidity / circulating supply + venue-concentration penalty), issuer/operational risk 15% (attestation cadence/auditor + full-history depeg incidents). Weights are Par v0 provisional (UNCONFIRMED); heuristic screening signal, not investment advice or a rating.", "params": [], "example": "/depeg-score"},
    {"method": "GET", "suffix": "/quote-guard", "description": "Peg guard at quote time (phase 2, P3) for this coin: ok / warn / block (+ unknown when the peg cannot be verified) with thresholds stated in the response — block when |deviation| > 50 bps or depeg-risk score >= 75, warn when |deviation| > 10 bps or score >= 50. Reuses the /depeg-score evaluation (not re-implemented). On warn/block the lowest-depeg-score coin among the other 7 scopes is suggested as the nearest stable alternative, with its own peg state. Screening signal only, not investment advice. Cached 60s.", "params": ["amount (optional)"], "example": "/quote-guard?amount=100"},
    {"method": "GET", "suffix": "/peg-leaderboard", "description": "Peg-stability leaderboard across all 8 stablecoin scopes (this coin flagged): current deviation, max |deviation| in 30d and all-time (with dates), off-peg days (>50 bps) in 30/90/365d with shares, depeg episodes in 365d with median/max recovery days (recovery = first subsequent close back within 5 bps; ongoing episodes counted but excluded from median/max), and 30d volatility (sample std of daily log returns, %/day; null if <30 closes). Ranked by fewest off-peg days in 365d, then smallest max |deviation|. All figures are DeFiLlama daily closes — intraday depegs are not counted. Cached 300s.", "params": [], "example": "/peg-leaderboard"},
    {"method": "GET", "suffix": "/reserves", "description": "Reserves/attestations + attestation tracker (feature 3): auditor, cadence, last verified attestation (+ source), days since, status (current / due soon / overdue / live / on_chain / no_attestation), next expected reporting date, alerts, and — where a dated published figure could be verified — the reserve total and reserve mix as of that date with its source, plus reserve-to-supply ratio (dated reserves / current DeFiLlama supply, both dates stated) and reserve-to-outstanding ratio (same-attestation denominator). No reserve numbers are invented — no keyless API exposes live figures; coins with no dated attestation carry reserve_figures null. Market figures (market cap, supply) are labelled as market figures.", "params": [], "example": "/reserves"},
    {"method": "GET", "suffix": "/mint-burn", "description": "Mint/burn monitor (feature 4): net issuance over 7/30/90d from the DeFiLlama daily supply series (net supply change, NOT gross mint/burn) with largest single-day net mint/burn in 90d and 30d-vs-prior-30d trend; large on-chain mint/burn events on the primary chain only (Transfer from/to the zero address, >= max($1M, 0.1% of circulating supply), latest 20, with scanned count and scan window); and an issuance-as-demand-signal read with its caveat (redemptions, chain migrations and issuer treasury moves also move supply). Cached 300s. On-chain events may be unavailable for a coin (e.g. USDT — Blockscout's zero-address query times out; USDT also mints via Issue/Redeem events, not zero-address Transfers).", "params": [], "example": "/mint-burn"},
    {"method": "GET", "suffix": "/whales", "description": "Whale & exchange exposure (feature 6): top-1 / top-10 / top-50 holder share of PRIMARY-CHAIN supply (Blockscout total_supply on Ethereum; DeFiLlama Tron chain balance for USDD), top-10 holder list, exchange wallets identified ONLY among the top 50 by the name tag the keyless source itself attaches to the address (Blockscout address tags / TronScan addressTag; matched tag shown verbatim) — the exchange total is a lower bound, not a total — and large recent transfers >= max($1M, 0.01% of circulating supply), latest 10, found by paging the recent-transfers feed (up to 10 pages of 50; scanned count and window always reported). Cached 300s.", "params": [], "example": "/whales"},
    {"method": "GET", "suffix": "/activity", "description": "On-chain activity & velocity (feature 7): active addresses (unique senders/receivers), transaction counts, transfer volume, median/mean/largest transfer and active share of holders for 24h/7d/30d windows on the PRIMARY chain only, from a scan of the recent-transfers feed (up to 10 pages of 50, newest first). Each window carries complete=true only when the scan provably reached the window start (or the feed was exhausted); incomplete windows are LOWER BOUNDS, not totals. Daily rates use the shortest complete window, else the scanned sample extrapolated (basis stated; a sub-hour sample is flagged noisy). Velocity (annualised) = daily volume x 365 / primary-chain supply, with a vs-total-supply lower bound. Trends (24h vs prior 24h, 7d vs prior 7d) are reported only when both halves are complete, else null with the reason. CoinGecko 24h trading volume is reported separately as exchange trading volume, never mixed into on-chain volume. Cached 300s.", "params": [], "example": "/activity"},
    {"method": "GET", "suffix": "/payments-readiness", "description": "Payments readiness score (feature 8): one 0-100 score per coin (higher = more ready for a company to use for payments) with the five input sub-scores and their evidence — peg stability 30% (100 minus the feature-1 depeg risk score), liquidity 25% (DexScreener near-par liquidity / supply + feature-7 daily transfer volume / supply; DexScreener 24h volume fallback, labelled), chain coverage 20% (chains holding >= 1% of total supply, feature-5 supply map; 1 chain -> 25, 2 -> 45, 3 -> 60, 4 -> 75, 5 -> 85, 6 -> 92, 7+ -> 100), fees/finality 15% (PRIMARY-CHAIN figures only: live gas/energy price x a typical transfer — 65,000 gas EVM / 14,000 energy Tron, both ESTIMATES — x the live native-coin USD price; finality is a protocol figure, Ethereum 2 epochs ~13 min, Tron ~19 blocks ~1 min), freeze risk 10% (live eth_call probes for freeze/blacklist/pause functions on the primary-chain contract; TronScan method map for USDD — capability only; observed freeze events are tracked in /freeze, feature 10). Unknown inputs are excluded and the remaining weights renormalised. Weights are this implementation's stated choice (no Par spec exists); heuristic screening signal, not investment advice or a rating. Cached 300s.", "params": [], "example": "/payments-readiness"},
    {"method": "GET", "suffix": "/defi", "description": "DeFi deployment & yield (feature 9): lending rates from DeFiLlama yields (single-asset pools on lending markets only — Aave, Compound, Morpho, Spark, Euler, Fluid, Kamino, Jupiter Lend, Venus, Moonwell, JustLend, Save, Silo, Gearbox and similar; vaults/aggregators excluded), with the supply-APY range (min/max/median/TVL-weighted) across venues — never one pool's APY as 'the' yield — plus best market, majors present/absent, and lending TVL; largest LP pools holding the coin (one row per DEX/chain/pair, top 10 by TVL; pool TVL counts the whole pool, both sides); collateral usage (markets the coin is supplied in + general per-protocol mechanics — live per-market collateral eligibility, LTV and liquidation thresholds are not exposed keyless; borrow APYs unavailable — DeFiLlama /poolsBorrow is paywalled, HTTP 402); total DeFi TVL (lending + LP) and its share of supply. APYs are a snapshot at as_of. Cached 300s (yields snapshot shared across coins).", "params": [], "example": "/defi"},
    {"method": "GET", "suffix": "/freeze", "description": "Freeze & blacklist monitor (feature 10): issuer freeze/blacklist capability (feature 8's live eth_call probes reused, cross-checked against the live contract ABI — implementation contract for proxies; TUSD's write-only setBlacklisted is caught by the ABI even though the read probe misses it), frozen supply as a LOWER-BOUND ESTIMATE (sum of balanceOf over addresses the scanned window's freeze events show as currently frozen, batched via Multicall3 — the blacklist is not enumerable on-chain, so addresses frozen before the window are missed), freeze/blacklist/pause events on the primary chain in the last 365 days (5-year lookback only when a coin has freeze signatures but zero events in 365d — TUSD), destroyed black funds (burned out of blacklisted addresses — seized, never added to frozen supply), and sanctions exposure (vendored OFAC SDN set, 514 addresses 2026-11-05, checked against currently-frozen addresses, top-50 holders, and counterparties of the latest 250 transfers). DAI/USDe have no freeze function (not applicable, with ABI evidence); USDD has none observed on Tron and its sanctions check is not applicable (SDN set holds no Tron-format addresses). Cached 300s.", "params": [], "example": "/freeze"},
    {"method": "GET", "suffix": "/freeze/watch", "description": "Freeze / blacklist watch (phase 2, Q6): is this address currently frozen/blacklisted for this coin, and does any freeze event name it? Live read probe via eth_call (USDC isBlacklisted, USDT isBlackListed, PYUSD isFrozen, FDUSD frozen — pre-flight probes reused) plus the freeze/blacklist events naming the address from the same window as /freeze (365d, 5y extended lookback for silent coins), the address's current balance in the coin, and the verdict frozen / not_frozen / unknown / not_freezable / not_applicable. TUSD has no on-chain read function, so its state is derived from its last freeze event, labelled event-derived, never live-verified. DAI/USDe have no freeze function (not_freezable); USDD is Tron-native (not_applicable). The blacklist is NOT enumerable on-chain — 'not frozen' means the live probe says not frozen and no event in the scanned window names the address; it cannot prove the address was never frozen before the window. Cached 300s per address. Rendered as the 'Check an address' form in the Freeze & blacklist card.", "params": ["address"], "example": "/freeze/watch?address=0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"},
    {"method": "GET", "suffix": "/issuer-risk", "description": "Issuer risk score (feature 11): one 0-100 score per coin (higher = riskier) composed of six documented inputs reused wholesale from earlier features — depeg risk 25% (feature-1 score as-is), reserve transparency 25% (feature-3 attestation tracker: status, days since last verified attestation, auditor tier, dated reserve-to-outstanding ratio where verifiable — transparency, NOT a live reserve measurement), issuance direction 15% (feature-4 net issuance, 30d primary / 90d confirmation; frozen supply scores high), concentration 15% (feature-6 top-10 holder share of primary-chain supply + identified-exchange share, a lower bound), freeze exposure 15% (feature-10 capability + observed use: freeze events, frozen supply share as a LOWER-BOUND ESTIMATE, destroyed funds, sanctions matches), adoption 5% (feature-8 chain-coverage + fees/finality legs only, inverted — the peg/liquidity/freeze legs are excluded to avoid double-counting). Unknown inputs are excluded and the remaining weights renormalised. Weights are this implementation's stated choice (no Par spec exists); heuristic screening signal, not investment advice or a rating. Cached 300s.", "params": [], "example": "/issuer-risk"},
    {"method": "GET", "suffix": "/chains", "description": "Per-chain supply breakdown (DeFiLlama currentChainBalances).", "params": [], "example": "/chains"},
    {"method": "GET", "suffix": "/supply-map", "description": "Cross-chain supply map (feature 5): per-chain supply + share, net supply change per chain over 7d/30d (bridges + local mint/burn combined — NOT a bridge-flow measurement), and bridged-in stock per bridge/source chain (latest daily bridgedTo snapshot, token units converted to USD at the live price, approximate). Covered share vs latest daily total is shown explicitly; the remainder is unattributed, never folded into a chain. No keyless source reports per-coin bridge flows over a window (Wormholescan ignores the symbol filter; DeFiLlama bridges is paywalled). Cached 300s.", "params": [], "example": "/supply-map"},
    {"method": "GET", "suffix": "/search", "description": "Search: an address (holder) or a transaction hash/id on the coin's primary chain.", "params": ["q"], "example": "/search?q=0x..."},
]


def sc_docs_payload(api_prefix, scope):
    cfg = _cfg(scope)
    return {
        "scope": scope,
        "symbol": cfg["symbol"],
        "name": cfg["name"],
        "primary_chain": cfg["primary"],
        "endpoints": [
            {
                "method": e["method"],
                "path": api_prefix + e["suffix"],
                "description": e["description"],
                "params": e["params"],
                "example": api_prefix + e["suffix"].split("?")[0] + ("?days=90" if "days" in e["params"] else ""),
            }
            for e in SC_DOCS
        ]
        + [
            {"method": "GET", "path": api_prefix + "/chains", "description": "List the chains this app serves (primary chain only).", "params": [], "example": api_prefix + "/chains"},
            {"method": "GET", "path": api_prefix + "/docs", "description": "This endpoint list.", "params": [], "example": api_prefix + "/docs"},
        ],
    }
