"""Fold a resumed crawl back into the run it was continuing.

A crawl can lose pincodes part-way through — a dropped connection, a sleeping laptop, an IP block.
`scrape.py` prints a resume command that re-crawls only the missing pincodes under a `-partN` run id.
This merges those snapshots into a single run so the dashboard sees one complete crawl instead of
two partial ones (which would otherwise read as mass delistings in the week-over-week diff).

    python -m db.merge_runs 2026-09-09T2105Z 2026-09-09T2105Z-part2
    python -m db.merge_runs 2026-09-09T2105Z 2026-09-09T2105Z-part2 --dry-run

The merged snapshot replaces the base run's parquet; the part files are moved to
data/snapshots/merged/ rather than deleted. The DuckDB cache is then rebuilt from scratch.
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys

import pandas as pd

from . import SNAPSHOT_DIR

log = logging.getLogger(__name__)


def _paths(run_id: str):
    return SNAPSHOT_DIR / f"{run_id}.parquet", SNAPSHOT_DIR / f"{run_id}_runlog.json"


def merge(base_run: str, parts: list[str], *, dry_run: bool = False) -> dict:
    base_pq, base_log = _paths(base_run)
    if not base_pq.exists():
        raise SystemExit(f"base snapshot not found: {base_pq}")

    frames = [pd.read_parquet(base_pq)]
    logs = [json.loads(base_log.read_text(encoding="utf-8"))] if base_log.exists() else []
    for part in parts:
        pq, lg = _paths(part)
        if not pq.exists():
            raise SystemExit(f"part snapshot not found: {pq}")
        frames.append(pd.read_parquet(pq))
        if lg.exists():
            logs.append(json.loads(lg.read_text(encoding="utf-8")))

    merged = pd.concat(frames, ignore_index=True)
    merged["run_id"] = base_run
    # A pincode re-crawled after a failure appears in both files; keep the newer rows.
    before = len(merged)
    merged = (merged.sort_values("crawled_at")
              .drop_duplicates(subset=["pincode", "subcategory", "product_id"], keep="last")
              .reset_index(drop=True))

    pins = sorted(merged["pincode"].unique())
    summary = {"run_id": base_run, "rows_in": before, "rows_out": len(merged),
               "pincodes": len(pins), "products": int(merged["product_id"].nunique()),
               "brands": int(merged["brand_key"].nunique())}
    if dry_run:
        log.info("dry run — would write %s", summary)
        return summary

    # Merge the run logs so Crawl health reflects the whole run, keeping the last status per pincode.
    if logs:
        by_pin: dict[str, dict] = {}
        for lg in logs:
            for p in lg.get("pincodes", []):
                if p["pincode"] not in by_pin or p["status"] == "ok":
                    by_pin[p["pincode"]] = p
        merged_log = dict(logs[0])
        merged_log.update({
            "run_id": base_run,
            "pincodes": list(by_pin.values()),
            "pincodes_total": len(by_pin),
            "pincodes_ok": sum(p["status"] == "ok" for p in by_pin.values()),
            "pincodes_failed": sum(p["status"] != "ok" for p in by_pin.values()),
            "units_total": sum(len(p["units"]) for p in by_pin.values()),
            "units_ok": sum(u["status"] == "ok" for p in by_pin.values() for u in p["units"]),
            "units_partial": sum(u["status"] == "partial" for p in by_pin.values() for u in p["units"]),
            "units_failed": sum(u["status"] == "failed" for p in by_pin.values() for u in p["units"]),
            "rows": len(merged),
            "requests": sum(p.get("requests", 0) for p in by_pin.values()),
            "retries": sum(p.get("retries", 0) for p in by_pin.values()),
            "finished_at": max((lg.get("finished_at") or "") for lg in logs),
            "notes": f"merged from {base_run} + {', '.join(parts)}",
        })
        base_log.write_text(json.dumps(merged_log, indent=1, default=str), encoding="utf-8")
        summary["pincodes_ok"] = merged_log["pincodes_ok"]
        summary["pincodes_failed"] = merged_log["pincodes_failed"]

    merged.to_parquet(base_pq, index=False)
    archive = SNAPSHOT_DIR / "merged"
    archive.mkdir(exist_ok=True)
    for part in parts:
        for f in _paths(part):
            if f.exists():
                shutil.move(str(f), str(archive / f.name))
    log.info("merged: %s", summary)
    return summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("base_run")
    ap.add_argument("parts", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-rebuild", action="store_true", help="skip rebuilding the DuckDB cache")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    summary = merge(args.base_run, args.parts, dry_run=args.dry_run)
    print(json.dumps(summary, indent=1))
    if not args.dry_run and not args.no_rebuild:
        from .load import rebuild
        rebuild()
    return 0


if __name__ == "__main__":
    sys.exit(main())
