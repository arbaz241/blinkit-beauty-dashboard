#!/usr/bin/env python3
"""Run a Blinkit beauty crawl and load it into DuckDB.

    python scrape.py                                   # all enabled pincodes x groupings
    python scrape.py --pincodes 400001,110001          # subset of pincodes
    python scrape.py --groups sunscreen,11672          # subset of groupings (name or id)
    python scrape.py --max-pages 3 --no-load           # quick smoke test, don't touch the DB
    python scrape.py --headed                          # watch the browser

Each run writes data/snapshots/<run_id>.parquet + <run_id>_runlog.json, then appends to
data/blinkit_beauty.duckdb and refreshes data/brands.csv with any newly-seen brands.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

from db import CONFIG_DIR, SNAPSHOT_DIR
from scraper.client import BlinkitBrowser, is_network_error, wait_for_network
from scraper.crawl import crawl_pincode, enabled_groupings

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"


def load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def setup_logging(run_id: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt, handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / f"scrape_{run_id}.log", encoding="utf-8"),
    ])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pincodes", help="comma-separated subset of pincodes")
    ap.add_argument("--groups", help="comma-separated grouping names or ids")
    ap.add_argument("--max-pages", type=int, default=80, help="max pages per grouping (15 cards each)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--no-load", action="store_true", help="write parquet only, skip DuckDB load")
    ap.add_argument("--run-id", help="override run id (default: UTC timestamp)")
    ap.add_argument("--network-wait", type=float, default=1800,
                    help="seconds to wait for the connection to return before giving up (default 1800)")
    args = ap.parse_args(argv)

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%MZ")
    setup_logging(run_id)
    log = logging.getLogger("scrape")

    pin_cfg = [p for p in load_yaml("pincodes.yaml").get("pincodes", []) if p.get("enabled", True)]
    if args.pincodes:
        want = {p.strip() for p in args.pincodes.split(",") if p.strip()}
        pin_cfg = [p for p in pin_cfg if str(p["pincode"]) in want]
    only = {g.strip().lower() for g in args.groups.split(",")} if args.groups else None
    groupings = enabled_groupings(load_yaml("categories.yaml"), only)
    if not pin_cfg or not groupings:
        log.error("nothing to crawl: %d pincodes, %d groupings", len(pin_cfg), len(groupings))
        return 2

    log.info("run %s: %d pincodes x %d groupings (max %d pages each)", run_id, len(pin_cfg), len(groupings), args.max_pages)
    started = datetime.now(timezone.utc)
    all_rows: list[dict] = []
    pin_logs = []
    with BlinkitBrowser(headless=not args.headed) as browser:
        aborted = False
        for i, p in enumerate(pin_cfg, 1):
            log.info("[%d/%d] pincode %s (%s, %s)", i, len(pin_cfg), p["pincode"], p.get("city"), p.get("area"))
            rows, plog = crawl_pincode(browser, p, groupings, run_id=run_id, max_pages=args.max_pages)

            # A dropped connection is not a failed pincode. Without this, one outage marks every
            # remaining pincode failed within seconds and throws away hours of crawling.
            if plog.status != "ok" and is_network_error(plog.error):
                log.warning("pincode %s failed on a network error — pausing the crawl", p["pincode"])
                if wait_for_network(args.network_wait):
                    log.info("connection restored, retrying pincode %s", p["pincode"])
                    rows, plog = crawl_pincode(browser, p, groupings, run_id=run_id, max_pages=args.max_pages)
                else:
                    log.error("no connection after %.0fs — stopping so the remaining pincodes stay "
                              "un-attempted rather than being recorded as failures", args.network_wait)
                    all_rows.extend(rows)
                    pin_logs.append(plog)
                    aborted = True
                    break

            all_rows.extend(rows)
            pin_logs.append(plog)
            log.info("pincode %s: %s, %d rows, %d requests (%d retries)", p["pincode"], plog.status, len(rows), plog.requests, plog.retries)
            time.sleep(2)
        if aborted:
            missed = [q["pincode"] for q in pin_cfg[len(pin_logs):]]
            if missed:
                log.error("not attempted: %s", ",".join(missed))
    finished = datetime.now(timezone.utc)

    units = [u for pl in pin_logs for u in pl.units]
    run_log = {
        "run_id": run_id,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "pincodes_total": len(pin_logs),
        "pincodes_ok": sum(pl.status == "ok" for pl in pin_logs),
        "pincodes_failed": sum(pl.status == "failed" for pl in pin_logs),
        "units_total": len(units),
        "units_ok": sum(u.status == "ok" for u in units),
        "units_partial": sum(u.status == "partial" for u in units),
        "units_failed": sum(u.status == "failed" for u in units),
        "rows": len(all_rows),
        "requests": sum(pl.requests for pl in pin_logs),
        "retries": sum(pl.retries for pl in pin_logs),
        "notes": f"args={vars(args)}",
        "pincodes": [pl.to_dict() for pl in pin_logs],
    }

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    runlog_path = SNAPSHOT_DIR / f"{run_id}_runlog.json"
    runlog_path.write_text(json.dumps(run_log, indent=1, default=str), encoding="utf-8")
    snapshot_path = SNAPSHOT_DIR / f"{run_id}.parquet"
    if all_rows:
        df = pd.DataFrame(all_rows)
        df["crawled_at"] = pd.to_datetime(df["crawled_at"], utc=True)
        df.to_parquet(snapshot_path, index=False)
        log.info("wrote %s (%d rows, %d products, %d brands)", snapshot_path.name, len(df),
                 df["product_id"].nunique(), df["brand_key"].nunique())
    else:
        log.error("no rows collected; not writing a snapshot")

    log.info("summary: pincodes ok=%d failed=%d | units ok=%d partial=%d failed=%d | rows=%d | requests=%d retries=%d | %.0fs",
             run_log["pincodes_ok"], run_log["pincodes_failed"], run_log["units_ok"], run_log["units_partial"],
             run_log["units_failed"], run_log["rows"], run_log["requests"], run_log["retries"],
             (finished - started).total_seconds())
    for pl in pin_logs:
        if pl.status != "ok":
            log.error("FAILED pincode %s: %s", pl.pincode, pl.error)
        for u in pl.units:
            if u.status != "ok":
                log.warning("%s unit %s/%s: %s %s", u.status.upper(), pl.pincode, u.subcategory, u.error or "", f"({u.rows} rows)")

    if all_rows and not args.no_load:
        try:
            from db.load import append_snapshot, connect, record_run, sync_brands
            conn = connect()
            append_snapshot(conn, snapshot_path)
            record_run(conn, run_log)
            sync_brands(conn, run_id=run_id)
            conn.close()
            log.info("loaded into DuckDB")
        except Exception as e:  # noqa: BLE001 - the crawl itself succeeded; never lose it to a load error
            # Most likely cause: a running Streamlit dashboard holds the DuckDB file lock. DuckDB
            # allows one read-write process OR several read-only ones, so even the read-only app
            # blocks this write. The snapshot is already on disk, so nothing is lost.
            log.error("crawl succeeded but the DuckDB load failed: %s", e)
            log.error("Snapshot is safe at %s — close any running dashboard, then run:", snapshot_path)
            log.error("    python -m db.load --snapshot '%s'", snapshot_path)
            return 3

    if not all_rows:
        return 1
    # A partial crawl must not look like a success. Without this, a run that lost most of its
    # pincodes (network drop, IP block) still exits 0, and the weekly GitHub Action commits
    # truncated data with a green tick — the diff pages then read the missing pincodes as mass
    # delistings. Anything worse than a couple of stragglers fails the run.
    failed = run_log["pincodes_failed"]
    if failed:
        share = failed / max(run_log["pincodes_total"], 1)
        log.error("%d of %d pincodes failed (%.0f%%). Failed: %s", failed, run_log["pincodes_total"],
                  share * 100, ", ".join(pl.pincode for pl in pin_logs if pl.status != "ok"))
        if share > 0.1:
            log.error("Treating this run as FAILED. Re-crawl the missing pincodes with:")
            log.error("    python scrape.py --pincodes %s --run-id %s-part2 --no-load",
                      ",".join(pl.pincode for pl in pin_logs if pl.status != "ok"), run_id)
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
