"""Shared plumbing: DB access, cached metric calls, filters, chart house-style.

The DuckDB connection is **read-only**. The crawler needs the write lock at the end of every run, so
an open dashboard must never hold it. Brand classification is therefore joined from `data/brands.csv`
(registered into the connection as `brands_live`), which also makes review-queue edits take effect
immediately instead of after a reload.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import metrics  # noqa: E402
from db import BRANDS_CSV, DB_PATH  # noqa: E402
from db.brands import load_seed, normalize_brand  # noqa: E402

CLASSES = ["Korean", "Global", "Local", "Unclassified"]
# Validated categorical slots (blue / orange / aqua clear every CVD + normal-vision gate all-pairs).
# Unclassified takes the neutral "Other" gray, so it never reads as a fourth competing hue.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
CLASS_COLORS = {"Korean": PALETTE[0], "Global": PALETTE[1], "Local": PALETTE[2], "Unclassified": "#898781"}
CAT_COLORS = {"Skincare": PALETTE[0], "Makeup": PALETTE[1]}
SEQ = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]
INK = {"primary": "#0b0b0b", "secondary": "#52514e", "muted": "#898781", "grid": "#e1e0d9", "axis": "#c3c2b7"}
FOCUS_HUE, PEER_HUE = PALETTE[0], "#b9c6d4"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

PROXY_NOTE = ("Blinkit publishes no sales volume. *Visibility* and *best-seller* here are proxies built from "
              "position in Blinkit's own bestsellers-sorted listing (which tracks demand: rank correlates −0.43 "
              "with rating count), plus rating volume and pincode coverage.")


# ----------------------------------------------------------------------------- connection
@st.cache_resource(show_spinner=False)
def _raw_conn():
    return duckdb.connect(str(DB_PATH), read_only=True)


def _brands_df() -> pd.DataFrame:
    if not BRANDS_CSV.exists():
        return pd.DataFrame(columns=["brand_key", "brand_name", "classification", "confirmed"])
    df = pd.read_csv(BRANDS_CSV, dtype={"brand_key": str})
    df["confirmed"] = df["confirmed"].astype(str).str.lower().isin(["true", "1", "yes"])
    return df


@st.cache_data(show_spinner=False)
def _brands_cached(mtime: float) -> pd.DataFrame:
    return _brands_df()


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


# ----------------------------------------------------------------------------- cached metrics
@st.cache_data(ttl=600, show_spinner=False)
def brand_table(where: str, params: tuple) -> pd.DataFrame:
    return metrics.brand_table(get_conn(), where, list(params))


@st.cache_data(ttl=600, show_spinner=False)
def scope_totals(where: str, params: tuple) -> dict:
    return metrics.scope_totals(get_conn(), where, list(params))


@st.cache_data(ttl=600, show_spinner=False)
def by_geo(where: str, params: tuple, brand_keys: tuple = ()) -> pd.DataFrame:
    return metrics.by_geo(get_conn(), where, list(params), list(brand_keys) or None)


@st.cache_data(ttl=600, show_spinner=False)
def shelf_share_by(where: str, params: tuple, dim: str) -> pd.DataFrame:
    return metrics.shelf_share_by(get_conn(), where, list(params), dim)


@st.cache_data(ttl=600, show_spinner=False)
def focus_products(where: str, params: tuple, brand_keys: tuple) -> pd.DataFrame:
    return metrics.focus_products(get_conn(), where, list(params), list(brand_keys))


@st.cache_data(ttl=600, show_spinner=False)
def sku_depth(where: str, params: tuple) -> pd.DataFrame:
    return metrics.sku_depth(get_conn(), where, list(params))


@st.cache_data(ttl=600, show_spinner=False)
def attention(where: str, params: tuple, focus_key: str, peer_keys: tuple,
              prev_where: str | None, prev_params: tuple | None) -> pd.DataFrame:
    return metrics.attention(get_conn(), where, list(params), focus_key=focus_key, peer_keys=list(peer_keys),
                             prev_where=prev_where, prev_params=list(prev_params) if prev_params else None)


# ----------------------------------------------------------------------------- seeds / focus brand
@st.cache_data(show_spinner=False)
def seed() -> dict:
    return load_seed()


def focus_brand() -> str:
    return seed().get("focus_brand", "")


def focus_key() -> str:
    return normalize_brand(focus_brand())


def competitor_names() -> list[str]:
    return list(seed().get("competitors") or [])


def competitor_keys() -> list[str]:
    return [normalize_brand(c) for c in competitor_names()]


def runs() -> pd.DataFrame:
    return q("SELECT * FROM v_runs")


def run_ids() -> list[str]:
    return runs()["run_id"].tolist()


# ----------------------------------------------------------------------------- filters
class Filters:
    """Builds the WHERE clause consumed by metrics.SHELF_CTE (tables aliased `l` and `bl`)."""

    def __init__(self, run_id: str, prev_run_id: str | None, cities: list[str], pincodes: list[str],
                 top_categories: list[str], classifications: list[str]):
        self.run_id = run_id
        self.prev_run_id = prev_run_id
        self.cities, self.pincodes = cities, pincodes
        self.top_categories, self.classifications = top_categories, classifications

    def _clauses(self) -> tuple[list[str], list]:
        cl, p = [], []
        if self.pincodes:
            cl.append(f"l.pincode IN ({','.join('?' * len(self.pincodes))})")
            p += self.pincodes
        elif self.cities:
            cl.append(f"l.city IN ({','.join('?' * len(self.cities))})")
            p += self.cities
        if self.top_categories:
            cl.append(f"l.top_category IN ({','.join('?' * len(self.top_categories))})")
            p += self.top_categories
        if self.classifications:
            cl.append("coalesce(bl.classification, 'Unclassified') IN ({})".format(",".join("?" * len(self.classifications))))
            p += self.classifications
        # Deliberately no in-stock filter: availability is a headline metric here, and filtering the
        # rows it is computed from would pin it at 100%. Stock state is shown, never filtered on.
        return cl, p

    def where(self) -> tuple[str, tuple]:
        cl, p = self._clauses()
        return " AND ".join(["l.run_id = ?"] + cl), tuple([self.run_id] + p)

    def where_for_run(self, run_id: str) -> tuple[str, tuple]:
        cl, p = self._clauses()
        return " AND ".join(["l.run_id = ?"] + cl), tuple([run_id] + p)

    def where_all_runs(self) -> tuple[str, tuple]:
        cl, p = self._clauses()
        return (" AND ".join(cl) if cl else "TRUE"), tuple(p)


def sidebar_filters(*, show_class: bool = True, show_stock: bool = False) -> Filters | None:
    ids = run_ids()
    if not ids:
        st.sidebar.warning("No crawl data yet — run `python scrape.py`.")
        return None
    st.sidebar.header("Filters")
    run_id = st.sidebar.selectbox("Crawl", ids, index=0,
                                  format_func=lambda r: f"{r}{'  (latest)' if r == ids[0] else ''}")
    prev = ids[ids.index(run_id) + 1] if ids.index(run_id) + 1 < len(ids) else None
    geo = q("SELECT DISTINCT city, pincode, area FROM listings WHERE run_id = ? ORDER BY city, pincode", (run_id,))
    cities = st.sidebar.multiselect("City", geo["city"].dropna().unique().tolist())
    pool = geo[geo["city"].isin(cities)] if cities else geo
    labels = dict(zip(pool["pincode"], pool["area"].fillna("")))
    pincodes = st.sidebar.multiselect("Pincode", pool["pincode"].tolist(),
                                      format_func=lambda p: f"{p} · {labels.get(p, '')}")
    top = st.sidebar.multiselect("Category", ["Skincare", "Makeup"])
    classes = st.sidebar.multiselect("Classification", CLASSES) if show_class else []
    st.sidebar.divider()
    st.sidebar.caption(f"Focus brand · **{focus_brand()}**")
    st.sidebar.caption("Peers · " + ", ".join(competitor_names()))
    return Filters(run_id, prev, cities, pincodes, top, classes)


# ----------------------------------------------------------------------------- charts
def style_fig(fig, *, height: int = 360, legend: bool = True, xgrid: bool = False):
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


def peer_bar(df: pd.DataFrame, *, value: str, label: str, focus: str, suffix: str = "%",
             height: int | None = None):
    """Horizontal ranked bar with the focus brand highlighted — the workhorse comparison chart."""
    d = df.sort_values(value, ascending=True)
    colors = [FOCUS_HUE if b == focus else PEER_HUE for b in d["brand"]]
    fig = go.Figure(go.Bar(
        x=d[value], y=d["brand"], orientation="h", marker=dict(color=colors, line=dict(width=0)),
        text=[f"{v:,.1f}{suffix}" if pd.notna(v) else "–" for v in d[value]],
        textposition="outside", cliponaxis=False,
        hovertemplate="%{y}<br>" + label + ": %{x:,.1f}" + suffix + "<extra></extra>",
    ))
    fig.update_xaxes(title=label, range=[0, float(d[value].max() or 1) * 1.22])
    fig.update_yaxes(title=None)
    return style_fig(fig, height=height or max(220, 30 * len(d) + 70), legend=False, xgrid=True)


# ----------------------------------------------------------------------------- formatting
_UNIT_RE = re.compile(r"(?:(\d+)\s*[xX]\s*)?(\d+(?:\.\d+)?)\s*(ml|g|gm|kg|l|ltr)\b", re.I)


def parse_unit(unit: str | None) -> tuple[float | None, str | None]:
    if not unit:
        return None, None
    m = _UNIT_RE.search(unit)
    if not m:
        return None, None
    mult = float(m.group(1)) if m.group(1) else 1.0
    qty, uom = float(m.group(2)), m.group(3).lower()
    if uom == "gm":
        uom = "g"
    if uom == "kg":
        qty, uom = qty * 1000, "g"
    if uom in ("l", "ltr"):
        qty, uom = qty * 1000, "ml"
    return mult * qty, uom


def add_unit_price(df: pd.DataFrame) -> pd.DataFrame:
    parsed = df["unit"].map(parse_unit)
    df = df.copy()
    df["qty"] = [p[0] for p in parsed]
    df["uom"] = [p[1] for p in parsed]
    df["price_per_100"] = [round(pr / qt * 100, 1) if qt and pd.notna(pr) else None
                           for pr, qt in zip(df["price"], df["qty"])]
    return df


def link_col():
    return st.column_config.LinkColumn("Blinkit", display_text="open")


def pct_col(label: str):
    return st.column_config.NumberColumn(label, format="%.0f%%")


def inr_col(label: str):
    return st.column_config.NumberColumn(label, format="₹%.0f")
