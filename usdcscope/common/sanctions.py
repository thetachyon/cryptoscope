"""Sanctions list API (phase 3, item 3) — keyless.

Serves the parsed OFAC SDN crypto-address set as a queryable list:
filter by program (e.g. DPRK3 / CYBER2), by chain family
(EVM / Bitcoin / Solana / other), and by SDN-name substring, with
pagination. Nothing is re-parsed here — the label set is reused
wholesale from common/risk.py (vendored OFAC SDN list, 514
addresses, snapshot 2026-11-05), including its `is_mixer` helper.

Change tracking (honest version)
---------------------------------
The response carries a `changes` block diffing the live set against
a committed baseline snapshot file,
``common/data/ofac_sdn_snapshot_2026-11-05.json`` — the same 514
entries, generated from risk.SDN at build time. That snapshot IS the
baseline: there is no stored prior snapshot in the repo, so no
newly-listed / delisted history exists before 2026-11-05 and none is
invented. On a future rebuild (fresh sdn.csv -> new risk.SDN), run
``python3 scripts/sdn_diff.py`` BEFORE regenerating the snapshot:
it prints added / removed addresses against the committed file.
Only after those diffs have been reviewed should the snapshot be
rewritten (``scripts/sdn_diff.py --update-snapshot``); rewriting it
first would silently erase the diff. Until a rebuild happens,
`added` / `removed` are empty and the response says so plainly.

Honest limits (also stated in every response):
  - The list is a dated vendored snapshot (2026-11-05), not the live
    OFAC SDN list. Listings / delistings after that date are not
    reflected until the set is rebuilt from a fresh sdn.csv.
  - "Other" family covers the 158 addresses on chains Cryptoscope
    does not score (Tron, Monero, Litecoin, ...). They are listed
    for reference; the screen endpoints only score EVM, Bitcoin
    and Solana.
  - Program counts count each program tag separately: an entry
    listed under two programs (e.g. "[DPRK3] [CYBER2]") is counted
    once in the total but once under each program, so program
    counts sum to more than the total.
  - Tornado Cash is NOT in the vendored set: OFAC removed it from
    the SDN list in March 2025.
  - This list is a screening reference, not a legal determination.
"""
import json
import re
from pathlib import Path

from common.clients import UpstreamError
from common.risk import (
    OFAC_SDN_COUNT,
    OFAC_SDN_DATE,
    OFAC_SDN_SOURCE,
    SDN,
    is_mixer,
)

METHOD = (
    "Sanctions list (phase 3, item 3). Queryable OFAC SDN "
    "crypto-address list from common/risk.py (vendored OFAC SDN list, "
    "514 addresses, 2026-11-05). Screening reference, not a legal "
    "determination."
)
LIMITS = [
    "The list is a dated vendored snapshot (2026-11-05), not the "
    "live OFAC SDN list. Listings / delistings after that date are "
    "not reflected until the set is rebuilt from a fresh sdn.csv.",
    "Family 'other' covers the 158 addresses on chains Cryptoscope "
    "does not score (Tron, Monero, Litecoin, ...). They are listed "
    "for reference; the screen endpoints only score EVM, Bitcoin "
    "and Solana.",
    "Program counts count each program tag separately: an entry "
    "listed under two programs is counted once in the total but "
    "once under each program, so program counts sum to more than "
    "the total.",
    "Tornado Cash is not in the vendored set — OFAC removed it from "
    "the SDN list in March 2025.",
    "This list is a screening reference, not a legal determination.",
]

SNAPSHOT_FILE = "ofac_sdn_snapshot_2026-11-05.json"
_SNAPSHOT_PATH = Path(__file__).resolve().parent / "data" / SNAPSHOT_FILE

_PROGRAM_RE = re.compile(r"\[([^\]]+)\]")

FAMILIES = ("evm", "bitcoin", "solana", "other")


def _family(address, currency):
    if address.startswith("0x"):
        return "evm"
    if currency == "XBT":
        return "bitcoin"
    if currency == "SOL":
        return "solana"
    return "other"


def _programs(program_raw):
    return _PROGRAM_RE.findall(program_raw or "")


def _build_entries():
    entries = []
    for address, (currency, name, program_raw) in SDN.items():
        entry = {
            "address": address,
            "currency": currency,
            "family": _family(address, currency),
            "name": name,
            "programs": _programs(program_raw),
            "program": program_raw,
            "mixer": is_mixer({"name": name}),
        }
        entries.append(entry)
    # Deterministic order: name, then address.
    entries.sort(key=lambda e: (e["name"], e["address"]))
    return entries


ENTRIES = _build_entries()

_BY_FAMILY = {}
_BY_CURRENCY = {}
_BY_PROGRAM = {}
for _e in ENTRIES:
    _BY_FAMILY[_e["family"]] = _BY_FAMILY.get(_e["family"], 0) + 1
    _BY_CURRENCY[_e["currency"]] = _BY_CURRENCY.get(_e["currency"], 0) + 1
    for _p in _e["programs"]:
        _BY_PROGRAM[_p] = _BY_PROGRAM.get(_p, 0) + 1
COUNTS = {
    "total": len(ENTRIES),
    "by_family": dict(sorted(_BY_FAMILY.items())),
    "by_currency": dict(sorted(_BY_CURRENCY.items())),
    "by_program": dict(sorted(_BY_PROGRAM.items())),
    "program_count_note": (
        "Program counts count each program tag separately: an entry "
        "listed under two programs is counted once in the total but "
        "once under each program, so program counts sum to more than "
        "the total."
    ),
}
PROGRAMS = sorted(_BY_PROGRAM)


def _load_snapshot():
    """Committed baseline snapshot, or None if the file is absent.

    The snapshot maps address -> {currency, name, program}, same
    shape as risk.SDN. It is loaded once at import; a missing file
    is not an error — the endpoint then reports changes as
    unavailable instead of inventing history.
    """
    try:
        with open(_SNAPSHOT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and data.get("entries"):
            return data
    except (OSError, ValueError):
        pass
    return None


_SNAPSHOT = _load_snapshot()


def _changes():
    """Diff of the live set against the committed baseline snapshot.

    The snapshot shipped with this build was generated from this
    same set, so added / removed are empty and `baseline` is True:
    this snapshot IS the baseline. A future rebuild that changes
    risk.SDN without rewriting the snapshot first will surface real
    added / removed entries here.
    """
    if _SNAPSHOT is None:
        return {
            "available": False,
            "baseline": False,
            "added": [],
            "removed": [],
            "added_count": 0,
            "removed_count": 0,
            "note": (
                "No baseline snapshot file is present in this build "
                f"({SNAPSHOT_FILE} not found), so newly-listed / "
                "delisted addresses cannot be computed. The list "
                "below is the current vendored set only."
            ),
        }
    snap = _SNAPSHOT.get("entries") or {}
    snap_date = _SNAPSHOT.get("date") or OFAC_SDN_DATE
    live = set(SDN)
    old = set(snap)
    added = [
        {"address": a, "currency": SDN[a][0], "name": SDN[a][1],
         "program": SDN[a][2]}
        for a in sorted(live - old)
    ]
    removed = [
        {"address": a, "currency": snap[a].get("currency"),
         "name": snap[a].get("name"), "program": snap[a].get("program")}
        for a in sorted(old - live)
    ]
    return {
        "available": True,
        "baseline": not added and not removed,
        "baseline_date": snap_date,
        "snapshot_file": SNAPSHOT_FILE,
        "snapshot_entries": len(old),
        "added": added,
        "removed": removed,
        "added_count": len(added),
        "removed_count": len(removed),
        "note": (
            "Diffed against the committed baseline snapshot "
            f"({snap_date}). This snapshot was generated from the "
            "same set it ships with, so it IS the baseline: no "
            "newly-listed / delisted history exists before "
            f"{snap_date} and none is claimed. On a rebuild from a "
            "fresh sdn.csv, run scripts/sdn_diff.py before "
            "rewriting the snapshot — added / removed entries will "
            "appear here until the snapshot is updated."
            if not added and not removed
            else
            "Diffed against the committed baseline snapshot "
            f"({snap_date}). Added / removed are the addresses that "
            "changed since that snapshot; the snapshot has not been "
            "rewritten yet."
        ),
    }


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


def list_sanctions(params):
    """List SDN crypto addresses, filtered + paginated.

    params: program (one program tag, brackets optional,
    case-insensitive), family (evm / bitcoin / solana / other),
    name (case-insensitive substring of the SDN name), page
    (1-based, default 1), page_size (1-100, default 50).
    Raises UpstreamError(400) for invalid filters / pagination.
    """
    program = (params.get("program") or "").strip()
    family = (params.get("family") or "").strip().lower()
    name_q = (params.get("name") or "").strip()
    if family and family not in FAMILIES:
        raise UpstreamError(
            f"Unknown family '{family}' (evm / bitcoin / solana / other)",
            status=400)
    program_norm = None
    if program:
        program_norm = program.strip("[]").strip().upper()
        if program_norm not in _BY_PROGRAM:
            raise UpstreamError(
                f"Unknown program '{program}' (programs: "
                f"{', '.join(PROGRAMS)})",
                status=400)
    page = _parse_int(params.get("page"), "page", 1, 1, 100000)
    page_size = _parse_int(params.get("page_size"), "page_size", 50, 1, 100)

    filtered = ENTRIES
    if program_norm:
        filtered = [e for e in filtered if program_norm in e["programs"]]
    if family:
        filtered = [e for e in filtered if e["family"] == family]
    if name_q:
        needle = name_q.lower()
        filtered = [e for e in filtered if needle in e["name"].lower()]

    total_filtered = len(filtered)
    total_pages = max(1, -(-total_filtered // page_size))
    if page > total_pages:
        page_items = []
    else:
        start = (page - 1) * page_size
        page_items = filtered[start:start + page_size]

    return {
        "entries": page_items,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "total_filtered": total_filtered,
        "total": len(ENTRIES),
        "filters": {
            "program": program_norm,
            "family": family or None,
            "name": name_q or None,
        },
        "counts": COUNTS,
        "programs": PROGRAMS,
        "families": list(FAMILIES),
        "sdn": {
            "source": OFAC_SDN_SOURCE,
            "date": OFAC_SDN_DATE,
            "labels_total": OFAC_SDN_COUNT,
        },
        "changes": _changes(),
        "method": METHOD,
        "limits": LIMITS,
    }
