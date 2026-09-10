"""Brand classification: seed heuristics + the brands.csv round-trip."""
from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import BRANDS_CSV, CONFIG_DIR

CLASSES = ("Local", "Global", "Korean", "Unclassified")
CSV_COLUMNS = ["brand_key", "brand_name", "classification", "country_of_origin", "notes",
               "confirmed", "suggested_by", "first_seen_run", "updated_at"]


def normalize_brand(name: str | None) -> str:
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def load_seed(path: Path | None = None) -> dict:
    with open(path or CONFIG_DIR / "brands_seed.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def seed_lookup(seed: dict | None = None) -> dict[str, tuple[str, str]]:
    """brand_key -> (classification, suggested_by). Korean wins over Global wins over Local on collisions."""
    seed = seed or load_seed()
    table: dict[str, tuple[str, str]] = {}
    for cls, key in (("Local", "local"), ("Global", "global"), ("Korean", "korean")):
        for name in seed.get(key) or []:
            table[normalize_brand(str(name))] = (cls, f"seed:{key}")
    return table


def suggest(brand_raw: str, lookup: dict[str, tuple[str, str]]) -> tuple[str, str]:
    key = normalize_brand(brand_raw)
    if key in lookup:
        return lookup[key]
    # Loose match: seed name contained in the brand (e.g. "Innisfree India" -> Innisfree).
    for skey, val in lookup.items():
        if len(skey) >= 5 and skey in key:
            return val
    return "Unclassified", "none"


def read_brands_csv(path: Path = BRANDS_CSV) -> list[dict]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["confirmed"] = str(r.get("confirmed", "")).strip().lower() in ("true", "1", "yes")
    return rows


def write_brands_csv(rows: list[dict], path: Path = BRANDS_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r.get("classification") or "", (r.get("brand_name") or "").lower()))
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            out = {c: r.get(c, "") for c in CSV_COLUMNS}
            out["confirmed"] = "true" if r.get("confirmed") else "false"
            w.writerow(out)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def upsert_csv(updates: list[dict], path: Path = BRANDS_CSV) -> int:
    """Apply review-queue edits to brands.csv only.

    The dashboard opens DuckDB read-only so it can never hold the write lock the crawler needs at the
    end of a run, so brands.csv is the live source of truth for classification; the DB's brands table
    is refreshed by sync_brands on the next crawl.
    """
    rows = {r["brand_key"]: r for r in read_brands_csv(path)}
    n = 0
    for u in updates:
        key = u.get("brand_key")
        if not key or key not in rows:
            continue
        rows[key].update({k: v for k, v in u.items() if k != "brand_key"})
        rows[key]["updated_at"] = now_iso()
        rows[key]["suggested_by"] = "user"
        n += 1
    write_brands_csv(list(rows.values()), path)
    return n


def focus_rows(seed: dict | None = None) -> list[dict]:
    """The focus brand + competitors are pre-confirmed as Korean so the focus view works on day one."""
    seed = seed or load_seed()
    names = [seed.get("focus_brand")] + list(seed.get("competitors") or [])
    return [{
        "brand_key": normalize_brand(n), "brand_name": n, "classification": "Korean",
        "country_of_origin": "South Korea", "notes": "focus brand" if n == seed.get("focus_brand") else "competitor",
        "confirmed": True, "suggested_by": "seed:focus", "first_seen_run": "", "updated_at": now_iso(),
    } for n in names if n]
