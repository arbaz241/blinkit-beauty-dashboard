"""Load parquet snapshots into DuckDB, sync the brands table, rebuild from scratch.

    python -m db.load --snapshot data/snapshots/2026-09-10T0530Z.parquet   # append one run
    python -m db.load --rebuild                                             # drop + reload everything
    python -m db.load --sync-brands                                         # only refresh brands from csv + new sightings
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import pandas as pd

from . import BRANDS_CSV, DB_PATH, SNAPSHOT_DIR
from .brands import (focus_rows, load_seed, now_iso, read_brands_csv, seed_lookup, suggest,
                     write_brands_csv)

log = logging.getLogger(__name__)
SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


def connect(path: Path = DB_PATH, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path), read_only=read_only)
    if not read_only:
        conn.execute(SCHEMA_SQL)
    return conn


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(SCHEMA_SQL)


# ---------------------------------------------------------------------------- listings
def append_snapshot(conn: duckdb.DuckDBPyConnection, parquet_path: Path) -> int:
    """Insert one run's parquet; idempotent per run_id."""
    df = pd.read_parquet(parquet_path)
    if df.empty:
        log.warning("snapshot %s is empty", parquet_path)
        return 0
    run_ids = df["run_id"].unique().tolist()
    existing = conn.execute(
        "SELECT DISTINCT run_id FROM listings WHERE run_id IN (SELECT unnest(?::VARCHAR[]))", [run_ids]
    ).fetchall()
    if existing:
        log.info("run(s) %s already loaded, skipping", [r[0] for r in existing])
        return 0
    cols = [c[0] for c in conn.execute("DESCRIBE listings").fetchall()]
    for c in cols:
        if c not in df.columns:
            df[c] = None
    df = df[cols]
    conn.register("snapshot_df", df)
    conn.execute("INSERT INTO listings SELECT * FROM snapshot_df")
    conn.unregister("snapshot_df")
    log.info("loaded %d rows from %s", len(df), parquet_path.name)
    return len(df)


def record_run(conn: duckdb.DuckDBPyConnection, run_log: dict) -> None:
    conn.execute("DELETE FROM crawl_runs WHERE run_id = ?", [run_log["run_id"]])
    conn.execute(
        """INSERT INTO crawl_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            run_log["run_id"], run_log.get("started_at"), run_log.get("finished_at"),
            run_log.get("pincodes_total"), run_log.get("pincodes_ok"), run_log.get("pincodes_failed"),
            run_log.get("units_total"), run_log.get("units_ok"), run_log.get("units_partial"), run_log.get("units_failed"),
            run_log.get("rows"), run_log.get("requests"), run_log.get("retries"), run_log.get("notes"),
            json.dumps(run_log),
        ],
    )


# ---------------------------------------------------------------------------- brands
def sync_brands(conn: duckdb.DuckDBPyConnection, *, run_id: str | None = None) -> dict:
    """brands.csv  ->  brands table  ->  add newly-seen brands with a suggested class  ->  brands.csv.

    Confirmed rows in the CSV always win. Unconfirmed rows keep whatever the CSV says (the user may
    have edited it by hand) and new sightings get a seed-based suggestion.
    """
    seed = load_seed()
    lookup = seed_lookup(seed)
    by_key: dict[str, dict] = {}
    for r in focus_rows(seed):
        by_key[r["brand_key"]] = r
    for r in read_brands_csv():
        key = r.get("brand_key") or ""
        if not key:
            continue
        if key in by_key and by_key[key]["confirmed"] and not r["confirmed"]:
            continue  # keep the pre-confirmed focus row
        by_key[key] = r

    seen = conn.execute(
        """SELECT brand_key, any_value(brand_raw) AS brand_raw, min(run_id) AS first_run
           FROM listings WHERE brand_key IS NOT NULL AND brand_key <> '' GROUP BY brand_key"""
    ).fetchall()
    added = 0
    for key, raw, first_run in seen:
        if key in by_key:
            if not by_key[key].get("first_seen_run"):
                by_key[key]["first_seen_run"] = first_run
            continue
        cls, why = suggest(raw, lookup)
        by_key[key] = {
            "brand_key": key, "brand_name": raw, "classification": cls, "country_of_origin": "",
            "notes": "", "confirmed": False, "suggested_by": why, "first_seen_run": first_run or run_id or "",
            "updated_at": now_iso(),
        }
        added += 1

    rows = list(by_key.values())
    write_brands_csv(rows)
    df = pd.DataFrame(rows, columns=["brand_key", "brand_name", "classification", "country_of_origin", "notes",
                                     "confirmed", "suggested_by", "first_seen_run", "updated_at"])
    df["confirmed"] = df["confirmed"].astype(bool)
    df["updated_at"] = pd.to_datetime(df["updated_at"], utc=True, errors="coerce")
    conn.execute("DELETE FROM brands")
    conn.register("brands_df", df)
    conn.execute("INSERT INTO brands SELECT * FROM brands_df")
    conn.unregister("brands_df")
    summary = {"brands_total": len(rows), "brands_added": added,
               "unconfirmed": int((~df["confirmed"]).sum())}
    log.info("brands synced: %s", summary)
    return summary


def update_brand(conn: duckdb.DuckDBPyConnection, brand_key: str, *, classification: str,
                 country_of_origin: str = "", notes: str = "", confirmed: bool = True) -> None:
    """Used by the dashboard's review page. Writes DuckDB and brands.csv together."""
    conn.execute(
        """UPDATE brands SET classification=?, country_of_origin=?, notes=?, confirmed=?, suggested_by='user', updated_at=now()
           WHERE brand_key=?""",
        [classification, country_of_origin, notes, confirmed, brand_key],
    )
    rows = conn.execute("SELECT * FROM brands").df().to_dict("records")
    for r in rows:
        r["updated_at"] = r["updated_at"].isoformat(timespec="seconds") if pd.notna(r["updated_at"]) else ""
    write_brands_csv(rows)


# ---------------------------------------------------------------------------- rebuild
def rebuild(db_path: Path = DB_PATH) -> None:
    if db_path.exists():
        db_path.unlink()
    for extra in (db_path.with_suffix(".duckdb.wal"),):
        if extra.exists():
            extra.unlink()
    conn = connect(db_path)
    total = 0
    for pq in sorted(SNAPSHOT_DIR.glob("*.parquet")):
        total += append_snapshot(conn, pq)
        runlog = pq.with_name(pq.stem + "_runlog.json")
        if runlog.exists():
            record_run(conn, json.loads(runlog.read_text(encoding="utf-8")))
    sync_brands(conn)
    conn.close()
    log.info("rebuilt %s with %d rows", db_path, total)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", type=Path, help="parquet snapshot to append")
    ap.add_argument("--rebuild", action="store_true", help="drop the .duckdb and reload all snapshots")
    ap.add_argument("--sync-brands", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.rebuild:
        rebuild()
        return
    conn = connect()
    if args.snapshot:
        append_snapshot(conn, args.snapshot)
        runlog = args.snapshot.with_name(args.snapshot.stem + "_runlog.json")
        if runlog.exists():
            record_run(conn, json.loads(runlog.read_text(encoding="utf-8")))
    if args.snapshot or args.sync_brands:
        sync_brands(conn)
    conn.close()


if __name__ == "__main__":
    main()
