"""Recurring-payment tracker (phase 2, Q5).

Endpoint: GET /api/walletscope/recurring?address=&direction=out
(direction=out | in | all; default out). Rendered at #/recurring.

What it does: takes one address's transfers and finds repeating
payments — same counterparty, same coin, approximately the same
amount, at a roughly regular interval. Per series it reports payment
count, median interval + cadence label (daily / weekly / fortnightly /
monthly / quarterly / half-yearly / yearly / every ~N days), the
expected next date, missed/late payments (a payment overdue vs the
expected cadence), and amount drift (stddev / CV / max relative
deviation from the median amount).

Chains / coins (Tejas's directive: ALL coins, wherever it applies):
Ethereum / Sepolia / Optimism / Gnosis (native + ERC-20), Solana
(SOL + SPL), Bitcoin (BTC). Every coin the wallet aggregates, not
just stablecoins.

Collection is REUSED, never re-implemented: the transfer collectors
in common/accounting.py (_collect_evm / _collect_solana /
_collect_bitcoin), which themselves fan out over common/wallet.py's
ADAPTER (Blockscout first page per chain) plus the public Solana RPC
and mempool.space. This module only groups, clusters and scores the
rows those collectors return. Solana uses a 25-signature window here
(the accounting export uses 10 — a recurring series needs more than
10 signatures to show up at all); the EVM and Bitcoin windows are
the same as the accounting export.

Detection method (stated in the response, and in the UI):
- Group by (direction, chain, coin, token_address, counterparty).
  Rows with no counterparty are skipped (they cannot be attributed
  to a series).
- Within a group, cluster amounts: sorted amounts split into a new
  cluster whenever an amount exceeds the previous one by more than
  25% — so a $50/month subscription and a $500/month payment to the
  same counterparty stay separate series, while small drift
  ($100, $102, $98) stays one series.
- A series needs >= 3 payments in one amount cluster. Groups with
  exactly 2 payments are reported separately as `candidates` — not
  enough to establish a cadence — with their single interval shown
  and labelled as such.
- Cadence comes from the MEDIAN interval between consecutive
  payments. Regularity is the interval coefficient of variation
  (stddev / mean): <= 0.20 is "regular", <= 0.40 "rough", above that
  "irregular" (and an irregular series gets no cadence label, only
  its median interval). Cadence buckets (median interval in days):
  daily 0.9-1.6, weekly 6-8.5, fortnightly 13-16, monthly 27-33,
  quarterly 85-95, half-yearly 175-190, yearly 350-380; a regular
  series outside those buckets is "every ~N days".
- Expected next date = last payment + median interval. Status:
  on_schedule (next date not yet reached), due (overdue by no more
  than the grace period — max(2 days, 25% of the median interval)),
  overdue (past the grace period). missed_payments = whole expected
  dates that have passed since the last payment. late_payments =
  historical intervals longer than median + grace (a payment that
  arrived late relative to the cadence).
- Amount drift: median / mean / min / max / stddev, CV
  (stddev / mean), and max relative deviation from the median.

Honest limits (also carried in the response `limits` and the UI):
- Sampled window only: first page per EVM chain (50 native txs +
  50 token transfers per chain), latest 25 Solana signatures, first
  page of Bitcoin txs. A series is only as complete as the window — an
  older series whose recent payments fall outside the window is
  invisible, and a series that started before the window looks
  shorter than it is (first_payment is the first payment SEEN, not
  necessarily the first ever).
- The window is newest-first: for a busy address the 50-transfer
  page may cover only days, so a monthly series may show just 1-2
  payments (a candidate, not a series) or nothing at all. Absence
  of a series is not proof there is no recurring payment.
- Counterparty attribution is per-source and imperfect: Solana
  counterparties are best-effort (largest opposite balance change);
  Bitcoin counterparties are the first other address in the tx
  (change outputs cannot be identified from chain data alone), so
  Bitcoin series are weak evidence and are labelled as such.
- A detected series is a pattern, not proof of intent: two
  different obligations to the same counterparty in the same coin
  at similar amounts and spacing will read as one series.
- Cadence, next date and missed counts are estimates from the
  median interval — a series with rough/irregular spacing has a
  weak next-date estimate, and that is stated per series.
"""
import asyncio
import math
import statistics
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from common.accounting import (
    _collect_bitcoin,
    _collect_evm,
    _collect_solana,
    _resolve_spl_symbols,
)
from common.bitcoin import is_bitcoin_address
from common.chains import CHAINS, WALLET_CHAINS
from common.clients import _cache_get, _cache_set
from common.solana import is_solana_address
from common.wallet import _EVM_ADDR_RE

CACHE_TTL = 300  # seconds, like the PnL estimate / accounting export
SOL_SIG_LIMIT = 25  # Solana signatures scanned (accounting uses 10)
MIN_SERIES = 3  # payments needed to call a group a series
AMOUNT_GAP = 1.25  # new amount cluster when amount exceeds previous by >25%
REGULAR_CV = 0.20  # interval CV at/below this is "regular"
ROUGH_CV = 0.40  # interval CV at/below this is "rough", above "irregular"

# (label, low_days, high_days) for the median interval.
CADENCE_BUCKETS = [
    ("daily", 0.9, 1.6),
    ("weekly", 6.0, 8.5),
    ("fortnightly", 13.0, 16.0),
    ("monthly", 27.0, 33.0),
    ("quarterly", 85.0, 95.0),
    ("half-yearly", 175.0, 190.0),
    ("yearly", 350.0, 380.0),
]

HONEST_NOTES = [
    "Sampled window only: first page per EVM chain (50 native txs + 50 token transfers per chain), latest 25 Solana signatures, first page of Bitcoin txs. A series is only as complete as the window — an older series whose recent payments fall outside the window is invisible, and a series that started before the window looks shorter than it is (first_payment is the first payment SEEN, not necessarily the first ever).",
    "The window is newest-first: for a busy address the 50-transfer page may cover only days, so a monthly series may show just 1-2 payments (a candidate, not a series) or nothing at all. Absence of a series is not proof there is no recurring payment.",
    "Counterparty attribution is per-source and imperfect: Solana counterparties are best-effort (largest opposite balance change); Bitcoin counterparties are the first other address in the tx (change outputs cannot be identified from chain data alone), so Bitcoin series are weak evidence and are labelled as such.",
    "A detected series is a pattern, not proof of intent: two different obligations to the same counterparty in the same coin at similar amounts and spacing will read as one series.",
    "Cadence, next date and missed counts are estimates from the median interval — a series with rough/irregular spacing has a weak next-date estimate, and that is stated per series.",
]


def _parse_ts(ts):
    """ISO string or unix int -> unix float, or None."""
    if ts is None:
        return None
    try:
        if isinstance(ts, (int, float)):
            return float(ts)
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _iso(unix):
    return datetime.fromtimestamp(unix, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _dec(s):
    try:
        return Decimal(str(s))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _f(d):
    """Decimal -> float (JSON-safe), or None."""
    if d is None:
        return None
    try:
        return float(d)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _amount_clusters(payments):
    """Split a group's payments into amount clusters: sorted by amount,
    a new cluster starts when an amount exceeds the previous one by
    more than AMOUNT_GAP (25%). Returns a list of payment lists."""
    ordered = sorted(payments, key=lambda p: p["amount_dec"])
    clusters = []
    cur = []
    prev = None
    for p in ordered:
        if prev is not None and p["amount_dec"] > prev * Decimal(str(AMOUNT_GAP)):
            clusters.append(cur)
            cur = []
        cur.append(p)
        prev = p["amount_dec"]
    if cur:
        clusters.append(cur)
    return clusters


def _cadence(median_days, regularity):
    """Cadence label for a median interval, or 'irregular' when the
    spacing is too uneven to name a cadence."""
    if regularity == "irregular":
        return "irregular"
    for label, lo, hi in CADENCE_BUCKETS:
        if lo <= median_days <= hi:
            return label
    return f"every ~{median_days:.0f} days"


def _analyse_cluster(direction, chain, coin, token_address, counterparty,
                     payments, now_unix):
    """One amount cluster (already sorted oldest-first by the caller)
    -> a series dict, or a candidate dict when it has only 2 payments.
    Payments are dicts with ts (unix float), amount_dec (Decimal),
    amount (str), tx_hash."""
    payments = sorted(payments, key=lambda p: p["ts"])
    amounts = [p["amount_dec"] for p in payments]
    median_amt = statistics.median(amounts)
    mean_amt = sum(amounts) / len(amounts)
    std_amt = statistics.pstdev(amounts) if len(amounts) > 1 else Decimal(0)
    cv_amt = (std_amt / mean_amt * 100) if mean_amt else Decimal(0)
    max_rel_dev = (
        max(abs(a - median_amt) for a in amounts) / median_amt * 100
        if median_amt else Decimal(0)
    )
    first_ts = payments[0]["ts"]
    last_ts = payments[-1]["ts"]
    base = {
        "direction": direction,
        "chain": chain,
        "chain_label": WALLET_CHAINS.get(chain, {}).get("label", chain),
        "coin": coin,
        "token_address": token_address or None,
        "counterparty": counterparty,
        "count": len(payments),
        "first_payment": _iso(first_ts),
        "last_payment": _iso(last_ts),
        "amount_median": _f(median_amt),
        "amount_mean": _f(mean_amt),
        "amount_min": _f(min(amounts)),
        "amount_max": _f(max(amounts)),
        "amount_stddev": _f(std_amt),
        "amount_cv_pct": round(float(cv_amt), 2),
        "amount_max_rel_dev_pct": round(float(max_rel_dev), 2),
        "payments": [
            {
                "date": _iso(p["ts"]),
                "amount": p["amount"],
                "tx_hash": p["tx_hash"],
            }
            for p in payments
        ],
    }
    if chain == "bitcoin":
        base["counterparty_note"] = (
            "Bitcoin counterparty is the first other address in the "
            "transaction — change outputs cannot be identified, so "
            "this series is weak evidence."
        )
    if len(payments) == 2:
        interval_days = (payments[1]["ts"] - payments[0]["ts"]) / 86400.0
        nxt = last_ts + interval_days * 86400.0
        if interval_days < 0.25:
            # Two payments minutes apart: a burst, not a cadence.
            base.update(
                {
                    "kind": "candidate",
                    "median_interval_days": round(interval_days, 2),
                    "cadence": "same-day burst",
                    "regularity": "unknown",
                    "expected_next": None,
                    "status": "unknown",
                    "status_note": (
                        "The two payments were minutes apart — a "
                        "same-day burst, not a cadence. No next date "
                        "is estimated."
                    ),
                    "days_until_next": None,
                    "overdue_days": None,
                    "missed_payments": 0,
                    "late_payments": 0,
                }
            )
            return base
        base.update(
            {
                "kind": "candidate",
                "median_interval_days": round(interval_days, 2),
                "cadence": "insufficient data (2 payments)",
                "regularity": "unknown",
                "expected_next": _iso(nxt),
                "status": "unknown",
                "status_note": (
                    "Only 2 payments in the sampled window — one "
                    "interval cannot establish a cadence. The next "
                    "date below just repeats that single interval."
                ),
                "days_until_next": round((nxt - now_unix) / 86400.0, 1),
                "overdue_days": None,
                "missed_payments": 0,
                "late_payments": 0,
            }
        )
        return base
    intervals = [
        (payments[i + 1]["ts"] - payments[i]["ts"]) / 86400.0
        for i in range(len(payments) - 1)
    ]
    median_iv = statistics.median(intervals)
    mean_iv = statistics.fmean(intervals)
    std_iv = statistics.pstdev(intervals) if len(intervals) > 1 else 0.0
    cv_iv = (std_iv / mean_iv) if mean_iv else 0.0
    if median_iv < 0.25:
        # Payments within hours of each other: a same-day burst
        # (spam / airdrop dust), not a recurring cadence. No next
        # date is estimated, and the median interval is never used
        # as a divisor below.
        base.update(
            {
                "kind": "burst",
                "median_interval_days": round(median_iv, 2),
                "cadence": "same-day burst",
                "regularity": "unknown",
                "expected_next": None,
                "status": "unknown",
                "status_note": (
                    "Payments in this group arrived within hours of "
                    "each other — a same-day burst (e.g. spam / "
                    "airdrop dust), not a recurring cadence. No next "
                    "date is estimated."
                ),
                "days_until_next": None,
                "overdue_days": None,
                "missed_payments": 0,
                "late_payments": 0,
            }
        )
        return base
    if cv_iv <= REGULAR_CV:
        regularity = "regular"
    elif cv_iv <= ROUGH_CV:
        regularity = "rough"
    else:
        regularity = "irregular"
    grace_days = max(2.0, 0.25 * median_iv)
    late = sum(1 for iv in intervals if iv > median_iv + grace_days)
    nxt = last_ts + median_iv * 86400.0
    overdue_days = (now_unix - nxt) / 86400.0 if now_unix > nxt else 0.0
    if regularity == "irregular":
        # No reliable cadence, so no overdue/missed assessment: the
        # next date below is a median projection only, not a schedule.
        status = "unknown"
        missed = 0
        overdue_days = 0.0
    elif now_unix <= nxt:
        status = "on_schedule"
        missed = 0
    elif overdue_days <= grace_days:
        status = "due"
        missed = 0
    else:
        status = "overdue"
        missed = max(0, int(math.floor((now_unix - last_ts) / (median_iv * 86400.0))))
    base.update(
        {
            "kind": "series",
            "median_interval_days": round(median_iv, 2),
            "mean_interval_days": round(mean_iv, 2),
            "interval_stddev_days": round(std_iv, 2),
            "interval_cv": round(cv_iv, 3),
            "cadence": _cadence(median_iv, regularity),
            "regularity": regularity,
            "grace_days": round(grace_days, 1),
            "expected_next": _iso(nxt),
            "status": status,
            "days_until_next": round((nxt - now_unix) / 86400.0, 1),
            "overdue_days": round(overdue_days, 1) if overdue_days > 0 else 0.0,
            "missed_payments": missed,
            "late_payments": late,
        }
    )
    if regularity != "regular":
        base["status_note"] = (
            f"Interval spacing is {regularity} (CV {cv_iv:.2f}) — the "
            "expected next date is a weak estimate."
            + (
                " No overdue/missed assessment is made for irregular "
                "series: the next date is a median projection only."
                if regularity == "irregular"
                else ""
            )
        )
    return base


async def detect_recurring(address, direction="out"):
    """GET /api/walletscope/recurring?address=&direction= — detect
    repeating payments for one address. direction is out (sends),
    in (receives) or all. 5-min cache, like the PnL estimate."""
    direction = (direction or "out").lower()
    if direction not in ("out", "in", "all"):
        raise ValueError("direction must be out, in or all")
    key = f"wallet:{address.lower()}:recurring:{direction}"
    hit = _cache_get(key)
    if hit is not None:
        return hit

    is_btc = is_bitcoin_address(address)
    is_sol = is_solana_address(address)
    rows = []
    errors = {}
    if is_btc:
        btc_rows, btc_err = await _collect_bitcoin(address)
        rows.extend(btc_rows)
        errors.update(btc_err)
    else:
        # EVM chains only for EVM addresses: Blockscout 422s on a
        # base58 Solana address, so a Solana wallet must not fan out
        # to the EVM collectors (same guard as the accounting export).
        if _EVM_ADDR_RE.fullmatch(address):
            evm_rows, evm_err = await _collect_evm(address)
            rows.extend(evm_rows)
            errors.update(evm_err)
        if is_sol:
            sol_rows, sol_err = await _collect_solana(address, sig_limit=SOL_SIG_LIMIT)
            rows.extend(sol_rows)
            errors.update(sol_err)

    # SPL symbols, best-effort (same helper as the accounting export):
    # unknown mints keep the SPL:<prefix> coin label, never guessed.
    await _resolve_spl_symbols(rows)

    want = {"send"} if direction == "out" else {"receive"} if direction == "in" else {"send", "receive"}
    now_unix = datetime.now(timezone.utc).timestamp()
    groups = {}
    scanned = 0
    skipped_no_counterparty = 0
    skipped_no_timestamp = 0
    window_start = None
    window_end = None
    for r in rows:
        ts = _parse_ts(r.get("date") or r.get("_ts"))
        if ts is None:
            skipped_no_timestamp += 1
            continue
        window_start = ts if window_start is None else min(window_start, ts)
        window_end = ts if window_end is None else max(window_end, ts)
        if r.get("type") not in want:
            continue
        scanned += 1
        cp = (r.get("counterparty") or "").strip()
        if not cp:
            skipped_no_counterparty += 1
            continue
        amt = _dec(r.get("amount"))
        if amt is None or amt <= 0:
            continue
        gkey = (
            r.get("type"),
            r.get("chain"),
            r.get("coin"),
            r.get("token_address") or "",
            cp.lower() if r.get("chain") != "solana" else cp,
        )
        groups.setdefault(gkey, []).append(
            {
                "ts": ts,
                "amount_dec": amt,
                "amount": r.get("amount"),
                "tx_hash": r.get("tx_hash") or "",
            }
        )

    series = []
    candidates = []
    bursts = []
    for (dtype, chain, coin, tok_addr, cp), pays in groups.items():
        # The grouping key lowercases EVM counterparties; recover the
        # original-case address from the first payment's group — the
        # rows themselves are gone here, so keep the key form: EVM
        # addresses are conventionally shown as stored (lowercase is
        # a valid address form; the UI links by it).
        for cluster in _amount_clusters(pays):
            if len(cluster) < 2:
                continue
            analysed = _analyse_cluster(
                dtype, chain, coin, tok_addr, cp, cluster, now_unix
            )
            if analysed["kind"] == "series":
                series.append(analysed)
            elif analysed["kind"] == "burst":
                bursts.append(analysed)
            else:
                candidates.append(analysed)

    series.sort(key=lambda s: (s["count"], s["last_payment"]), reverse=True)
    candidates.sort(key=lambda c: c["last_payment"], reverse=True)
    bursts.sort(key=lambda b: b["last_payment"], reverse=True)
    out = {
        "address": address,
        "direction": direction,
        "generated_at": _iso(now_unix),
        "series": series,
        "candidates": candidates,
        "bursts": bursts,
        "summary": {
            "transfers_scanned": scanned,
            "transfers_total_in_window": len(rows),
            "groups_considered": len(groups),
            "series_count": len(series),
            "candidates_count": len(candidates),
            "bursts_count": len(bursts),
            "window_start": _iso(window_start) if window_start else None,
            "window_end": _iso(window_end) if window_end else None,
            "chains": sorted({r.get("chain") for r in rows if r.get("chain")}),
            "coins": sorted({r.get("coin") for r in rows if r.get("coin")}),
            "skipped_no_counterparty": skipped_no_counterparty,
            "skipped_no_timestamp": skipped_no_timestamp,
        },
        "method": (
            "Transfers are grouped by (direction, chain, coin, "
            "counterparty), then split into amount clusters (a new "
            "cluster when an amount exceeds the previous one by "
            ">25%). A series needs >= 3 payments in one cluster; "
            "2-payment groups are reported as candidates. Cadence is "
            "the median interval between consecutive payments "
            "(daily 0.9-1.6d, weekly 6-8.5d, fortnightly 13-16d, "
            "monthly 27-33d, quarterly 85-95d, half-yearly 175-190d, "
            "yearly 350-380d); regularity is the interval CV "
            "(<=0.20 regular, <=0.40 rough, above irregular). "
            "Expected next = last payment + median interval; grace "
            "is max(2 days, 25% of the median interval)."
        ),
        "limits": HONEST_NOTES,
        "errors": errors,
    }
    _cache_set(key, out, ttl=CACHE_TTL)
    return out
