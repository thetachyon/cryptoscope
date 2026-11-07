"""Accounting API (phase 3, item 10) — keyless.

The phase 2, Q2 accounting export as a documented, productized API:
one address in, the cross-chain send/receive ledger out — USD value
at time of transaction, FIFO cost basis and realized gain per send,
and QuickBooks/Xero general-journal columns — in CSV or JSON form.

Nothing is rebuilt here — the export is reused wholesale:

- ``common/accounting.py`` ``accounting_export`` (phase 2, Q2):
  the rows, the USD-at-time pricing (one CoinGecko daily-close
  series per coin), the FIFO cost basis (the shared ``_fifo_run``
  engine from ``common/wallet.py``), the journal columns, the
  summary, the per-coin FIFO lots remaining, the column mapping and
  the honest-limit notes.
- ``common/accounting.py`` ``accounting_csv``: the CSV rendering of
  the same payload (21 columns).

Endpoint home: ``GET /api/walletscope/accounting?address=&format=``
— a new dedicated endpoint, not a rename of ``/accounting/export``.
The Q2 export endpoint stays exactly as it is: it is the download
backing (the wallet dashboard's CSV/JSON download links used to
point at it, and the single-chain explorer CSV link lives beside
it), while ``/accounting`` is the productized paid-API contract —
the full export payload plus the contract fields (``method``,
``limits``, ``coverage``, ``price_basis_counts``, ``api_url`` /
``csv_url`` / ``json_url``), so an API caller never has to infer
completeness, price basis, or journal shape from the raw rows.
``format=csv`` returns the same CSV bytes as the export endpoint;
the contract fields are JSON-only (the CSV header is the 21 export
columns, unchanged).

Contract fields added here (everything else is the export payload
verbatim):
  - ``accounting: True`` — marks the productized response.
  - ``method`` / ``limits`` — what this endpoint is and its limits.
  - ``coverage`` — the sampled window, the chains/coins present in
    the rows, the per-chain collection errors (surfaced, never
    silently dropped), and ``complete`` (True only when no chain
    errored — a chain that errored contributes no rows, so the
    export is partial for that address until the error clears).
  - ``price_basis_counts`` — rows priced ``at-time`` (daily close
    on the tx date) vs ``estimate-current`` (live-price fallback)
    vs ``unavailable`` (null USD fields, never invented).

Honest limits (also stated in every response, via the export's
``notes`` plus the API-level additions in ``LIMITS``):
  - Sampled window: first page per chain (50 native txs + 50 token
    transfers per EVM chain, 10 Solana signatures, first page of
    Bitcoin txs — mempool.space page size, 25 at phase-2
    verification, 50 observed 2026-11-06; the collector takes the
    whole first page).
  - Rows with price_basis ``estimate-current`` are priced at today's
    live price, not at transaction time; for old transactions that
    is a weak estimate, and their FIFO lots inherit the same
    weakness.
  - Gnosis (and any chain) can error under the sandbox egress proxy
    (disconnects / 403s) — the failure is reported in ``errors`` /
    ``coverage`` and the export still succeeds with the other
    chains; it is never presented as complete.
  - Token-transfer rows carry no fee_usd (the fee is paid in the
    native coin on the parent transaction and Blockscout's
    token-transfer endpoint does not return it). Native rows carry
    fee_usd.
  - Sepolia ETH has no market value: its USD fields are always null.
  - Bitcoin rows are NET flow per transaction; change outputs cannot
    be identified from chain data alone.
  - This is an estimate, not tax advice: transfers to exchanges,
    gifts and internal moves are all treated as disposals.
"""
from common.accounting import HONEST_NOTES, accounting_csv, accounting_export

METHOD = (
    "Accounting API (phase 3, item 10). The phase 2, Q2 accounting "
    "export (common/accounting.py accounting_export, USD value at "
    "time of transaction + FIFO cost basis + QuickBooks/Xero "
    "general-journal columns) reused wholesale, plus the paid-API "
    "contract fields: method, limits, coverage (sampled window, "
    "chains/coins present, per-chain errors surfaced, complete flag), "
    "price_basis_counts, and api/csv/json URLs. format=csv returns "
    "the same CSV bytes as GET /api/walletscope/accounting/export; "
    "the contract fields are JSON-only."
)

LIMITS = HONEST_NOTES + [
    "Per-chain collection errors are reported in `errors` and `coverage` — a chain that errored contributes no rows, so the export is partial for that address until the error clears. `coverage.complete` is False whenever any chain errored; never treat a partial export as a complete ledger.",
    "CSV form carries the same rows as the JSON form; the contract fields (method, limits, coverage, price_basis_counts) are JSON-only — the CSV header is the 21 export columns, unchanged.",
    "This is an estimate, not tax advice: transfers to exchanges, gifts and internal moves are all treated as disposals. FIFO lots are built only from the sampled window — a disposal whose acquisition predates the window is unmatched (cost_basis_usd null), never guessed.",
]


async def accounting_api(address, fmt="json"):
    """Productized accounting export for one address.

    Returns the full ``accounting_export`` payload plus the contract
    fields above. ``fmt`` is "json" (the payload dict) or "csv" (the
    same rows rendered by ``accounting_csv``); anything else is a
    caller error and is rejected by the route, not here.
    """
    payload = await accounting_export(address)
    out = dict(payload)
    summary = payload.get("summary") or {}
    rows = payload.get("rows") or []
    errors = payload.get("errors") or {}
    out.update(
        {
            "accounting": True,
            "method": METHOD,
            "limits": LIMITS,
            "coverage": {
                "window": (
                    "first page per chain (50 native txs + 50 token "
                    "transfers per EVM chain, 10 Solana signatures, "
                    "first page of Bitcoin txs — mempool.space page "
                    "size, 25 at phase-2 verification, 50 observed "
                    "2026-11-06)"
                ),
                "chains": summary.get("chains") or [],
                "coins": summary.get("coins") or [],
                "errors": errors,
                "chains_with_errors": sorted(errors.keys()),
                "complete": not bool(errors),
            },
            "price_basis_counts": {
                "at-time": sum(
                    1 for r in rows if r.get("price_basis") == "at-time"
                ),
                "estimate-current": sum(
                    1 for r in rows
                    if r.get("price_basis") == "estimate-current"
                ),
                "unavailable": sum(
                    1 for r in rows
                    if r.get("price_basis") == "unavailable"
                ),
            },
            "api_url": (
                f"/api/walletscope/accounting?address={address}"
                "&format=json"
            ),
            "csv_url": (
                f"/api/walletscope/accounting?address={address}"
                "&format=csv"
            ),
            "json_url": (
                f"/api/walletscope/accounting?address={address}"
                "&format=json"
            ),
            "export_url": (
                f"/api/walletscope/accounting/export?address={address}"
                "&format=json"
            ),
        }
    )
    if fmt == "csv":
        return accounting_csv(payload)
    return out
