"""DuckDB storage layer.

Source of truth is the append-only parquet snapshots in data/snapshots/ (one file per crawl run)
plus the human-edited data/brands.csv. The .duckdb file is a derived cache that can always be
rebuilt with `python -m db.load --rebuild`.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
DB_PATH = DATA_DIR / "blinkit_beauty.duckdb"
BRANDS_CSV = DATA_DIR / "brands.csv"
CONFIG_DIR = ROOT / "config"
