#!/usr/bin/env python3
"""Diff the live OFAC SDN set against the committed baseline snapshot.

Used when rebuilding the vendored set in common/risk.py from a fresh
sdn.csv: run this BEFORE rewriting the snapshot file, so the
added / removed addresses are visible (the /sanctions endpoint
reports the same diff live).

  python3 scripts/sdn_diff.py                # print added / removed
  python3 scripts/sdn_diff.py --update-snapshot
      # rewrite common/data/ofac_sdn_snapshot_<date>.json from the
      # live set — only after the diff above has been reviewed.
      # Rewriting the snapshot erases the diff, by design: the new
      # file becomes the new baseline.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from common.risk import OFAC_SDN_DATE, OFAC_SDN_SOURCE, SDN  # noqa: E402
from common.sanctions import (  # noqa: E402
    SNAPSHOT_FILE,
    _SNAPSHOT_PATH,
    _changes,
)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--update-snapshot", action="store_true",
                    help="rewrite the snapshot file from the live set")
    args = ap.parse_args(argv)

    ch = _changes()
    if not ch["available"]:
        print("No baseline snapshot file present; nothing to diff against.")
        print("Use --update-snapshot to create one from the live set.")
    else:
        print(f"baseline snapshot: {_SNAPSHOT_PATH.name} "
              f"({ch['snapshot_entries']} entries, "
              f"{ch.get('baseline_date')})")
        print(f"live set: {len(SDN)} entries ({OFAC_SDN_DATE})")
        print(f"added: {ch['added_count']}")
        for e in ch["added"]:
            print(f"  + {e['address']}  {e['currency']}  {e['name']}  {e['program']}")
        print(f"removed: {ch['removed_count']}")
        for e in ch["removed"]:
            print(f"  - {e['address']}  {e['currency']}  {e['name']}  {e['program']}")
        if ch["baseline"]:
            print("No changes — the snapshot is the baseline.")

    if args.update_snapshot:
        data = {
            "source": OFAC_SDN_SOURCE,
            "date": OFAC_SDN_DATE,
            "note": (
                "Baseline snapshot of the vendored OFAC SDN "
                "crypto-address set in common/risk.py. Generated from "
                "risk.SDN; do NOT regenerate casually — on a rebuild "
                "from a fresh sdn.csv, run scripts/sdn_diff.py first "
                "so added/removed addresses are recorded, and only "
                "rewrite this file after review."
            ),
            "entries": {
                a: {"currency": v[0], "name": v[1], "program": v[2]}
                for a, v in sorted(SDN.items())
            },
        }
        name = f"ofac_sdn_snapshot_{OFAC_SDN_DATE}.json"
        path = _SNAPSHOT_PATH.parent / name
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, sort_keys=True)
        if path.name != SNAPSHOT_FILE:
            print(f"NOTE: wrote {name}; common/sanctions.py still "
                  f"points at {SNAPSHOT_FILE} — update SNAPSHOT_FILE "
                  f"and remove the old file.")
        else:
            print(f"snapshot rewritten: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
