"""Shared plumbing: DB access, the focus brand, and the chart house-style.

The DuckDB connection is **read-only**. The crawler needs the write lock at the end of every run, so
an open dashboard must never hold it. Brand classification is therefore joined from `data/brands.csv`
(registered into the connection as `brands_live`), which also makes review-queue edits take effect
immediately instead of after a reload.
"""
from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db import BRANDS_CSV, DB_PATH  # noqa: E402,F401  (BRANDS_CSV re-exported for the pages)
from db.brands import load_seed, normalize_brand  # noqa: E402

CLASSES = ["Korean", "Global", "Local", "Unclassified"]
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
# Blue is reserved for the focus brand and nothing else: the page is about one brand *inside* the
# Korean class, so if both shared a hue a chart coloured by class would paint a rival (The Face Shop)
# in the focus colour. Korean therefore takes violet. Violet / orange / aqua clear every CVD and
# normal-vision gate all-pairs, and violet stays distinct from the focus blue (ΔE 16.3).
# Unclassified takes the neutral "Other" grey, so it never reads as a fourth competing hue.
FOCUS_HUE, PEER_HUE = PALETTE[0], "#b9c6d4"
CLASS_COLORS = {"Korean": PALETTE[6], "Global": PALETTE[1], "Local": PALETTE[2], "Unclassified": "#898781"}
INK = {"primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7"}
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


# ----------------------------------------------------------------------------- connection
@st.cache_resource(show_spinner=False)
def _raw_conn():
    return duckdb.connect(str(DB_PATH), read_only=True)


@st.cache_data(show_spinner=False)
def _brands_cached(mtime: float) -> pd.DataFrame:
    if not BRANDS_CSV.exists():
        return pd.DataFrame(columns=["brand_key", "brand_name", "classification", "confirmed"])
    df = pd.read_csv(BRANDS_CSV, dtype={"brand_key": str})
    df["confirmed"] = df["confirmed"].astype(str).str.lower().isin(["true", "1", "yes"])
    return df


def get_conn():
    """Read-only connection with the *current* brands.csv registered as `brands_live`."""
    conn = _raw_conn()
    mtime = BRANDS_CSV.stat().st_mtime if BRANDS_CSV.exists() else 0.0
    conn.register("brands_live", _brands_cached(mtime))
    return conn


def invalidate() -> None:
    st.cache_data.clear()


@st.cache_data(ttl=600, show_spinner=False)
def q(sql: str, params: tuple = ()) -> pd.DataFrame:
    return get_conn().execute(sql, list(params)).df()


def runs() -> pd.DataFrame:
    return q("SELECT * FROM v_runs")


def run_ids() -> list[str]:
    return runs()["run_id"].tolist()


# ----------------------------------------------------------------------------- focus brand
@st.cache_data(show_spinner=False)
def seed() -> dict:
    return load_seed()


def focus_brand() -> str:
    return seed().get("focus_brand", "")


def focus_key() -> str:
    return normalize_brand(focus_brand())


def competitor_keys() -> list[str]:
    return [normalize_brand(c) for c in seed().get("competitors") or []]


# ----------------------------------------------------------------------------- charts
def style_fig(fig, *, height: int = 360, legend: bool = True, xgrid: bool = False):
    """House style: recessive grid on the value axis only, no chart junk, legend above the plot."""
    fig.update_layout(
        height=height, margin=dict(l=8, r=8, t=34 if legend else 10, b=8),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family=FONT, color=INK["secondary"], size=12),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0, title=None) if legend else None,
        showlegend=legend, bargap=0.35, bargroupgap=0.08,
        hoverlabel=dict(bgcolor="#ffffff", font_color=INK["primary"], bordercolor=INK["axis"], font_family=FONT),
    )
    fig.update_xaxes(showgrid=xgrid, gridcolor=INK["grid"], zeroline=False, linecolor=INK["axis"],
                     tickcolor=INK["axis"], title_font_color=INK["muted"])
    fig.update_yaxes(showgrid=not xgrid, gridcolor=INK["grid"], zeroline=False, showline=False,
                     title_font_color=INK["muted"])
    return fig


def link_col():
    return st.column_config.LinkColumn("Blinkit", display_text="open")
