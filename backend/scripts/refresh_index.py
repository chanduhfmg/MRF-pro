"""
Refresh the locally stored payer index URLs.

    cd backend
    .venv/Scripts/python -m scripts.refresh_index          # stale entries only
    .venv/Scripts/python -m scripts.refresh_index --force  # every payer

Runs automatically on API startup for stale entries; this is for cron, or for
when a payer re-publishes early. Safe to run any time - a payer that cannot be
reached keeps whatever URL is already stored.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(name)s | %(message)s")


def main() -> int:
    from app.index_store import STORE_FILE, get, refresh_all, staleness
    from app.payers import PAYER_SOURCES

    force = "--force" in sys.argv
    print(f"store: {STORE_FILE}")
    print(f"mode:  {'force (all payers)' if force else 'stale entries only'}\n")

    results = refresh_all(force=force, only_stale=not force)

    width = max(len(p) for p in PAYER_SOURCES)
    for r in results:
        entry = get(r["payer"])
        when = (
            datetime.fromtimestamp(entry.resolved_at, timezone.utc).strftime("%Y-%m-%d")
            if entry and entry.resolved_at else "seed"
        )
        mark = {"refreshed": "refreshed", "kept": "kept     ", "failed": "FAILED   "}[r["action"]]
        url = (r["url"] or entry.url if entry else None) or "-"
        print(f"  {r['payer']:<{width}}  {mark}  [{when}]  {url.split('?')[0][:88]}")
        if r["action"] == "failed":
            print(f"  {'':<{width}}             {r['error']}")
        elif r["action"] == "kept" and entry:
            s = staleness(entry)
            if s:
                print(f"  {'':<{width}}             (stale: {s})")

    failed = sum(1 for r in results if r["action"] == "failed")
    usable = sum(1 for r in results if (get(r["payer"]) or None) and get(r["payer"]).url)
    print(f"\n{usable}/{len(results)} payers have a usable index URL stored; {failed} refresh failure(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
