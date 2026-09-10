"""Market structure — who actually owns the beauty shelf on Blinkit, and where.

Merges the old "Brand landscape" and "Pincode coverage" pages. The organising metric is share of
shelf rather than brand count: in the data the top 5 brands hold 58-75% of a sub-category's listings,
so counting brands says almost nothing about who is winning.
"""
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from common import (CLASS_COLORS, CLASSES, SEQ, brand_table, competitor_keys, focus_brand, focus_key,
                    q, scope_totals, shelf_share_by, sidebar_filters, style_fig)

st.set_page_config(page_title="Market", page_icon="🧴", layout="wide")
st.title("Market structure")
st.caption("Who owns the Skincare + Makeup shelf on Blinkit, split Local / Global / Korean.")

f = sidebar_filters()
if f is None:
    st.stop()
where, params = f.where()
fb, fk = focus_brand(), focus_key()

bt = brand_table(where, params)
sc = scope_totals(where, params)
if bt.empty:
    st.info("No listings match these filters.")
    st.stop()

cls = (bt.groupby("classification", as_index=False)
       .agg(brands=("brand_key", "nunique"), products=("products", "sum"),
            cards=("cards", "sum"), share=("share_of_shelf_pct", "sum"),
            availability=("availability_pct", "mean")))
cls["classification"] = pd.Categorical(cls["classification"], CLASSES, ordered=True)
cls = cls.sort_values("classification")

c = st.columns(4)
c[0].metric("Shelf listings", f"{sc['cards']:,}", help="Primary product cards; pack-size variants excluded.")
c[1].metric("Brands", sc["brands"])
c[2].metric("Products", f"{sc['products']:,}")
kb = cls[cls["classification"] == "Korean"]["share"].sum()
unc = cls[cls["classification"] == "Unclassified"]["share"].sum()
c[3].metric("Korean share of shelf", f"{kb:.2f}%",
            help=f"A floor, not a final number: {unc:.0f}% of the shelf is still unclassified.")
if unc >= 5:
    st.info(f"**{unc:.0f}% of shelf listings belong to unclassified brands.** Every split on this page is "
            "provisional until those are triaged on **Data & brands**.")

left, right = st.columns([2, 3])
with left:
    st.subheader("Share of shelf")
    fig = go.Figure(go.Bar(x=cls["share"], y=cls["classification"].astype(str), orientation="h",
                           marker=dict(color=[CLASS_COLORS[c_] for c_ in cls["classification"].astype(str)], line=dict(width=0)),
                           text=[f"{v:.1f}%" for v in cls["share"]], textposition="outside", cliponaxis=False,
                           customdata=cls[["brands", "cards"]].values,
                           hovertemplate="%{y}<br>share %{x:.2f}%<br>%{customdata[0]} brands · %{customdata[1]} cards<extra></extra>"))
    fig.update_xaxes(title="% of shelf listings", range=[0, float(cls["share"].max()) * 1.25])
    fig.update_yaxes(title=None, autorange="reversed")
    st.plotly_chart(style_fig(fig, height=260, legend=False, xgrid=True), width="stretch")
    st.caption("Brand *count* by classification is in the table below — it tells a different and much "
               "weaker story than share of shelf.")
    st.dataframe(cls.rename(columns={"share": "shelf %", "availability": "in stock %"}), hide_index=True,
                 width="stretch",
                 column_config={"shelf %": st.column_config.NumberColumn(format="%.2f%%"),
                                "in stock %": st.column_config.NumberColumn(format="%.0f%%")})
with right:
    st.subheader("Top brands")
    n = st.slider("Brands shown", 5, min(40, len(bt)), min(15, len(bt)), key="topn")
    top = bt.head(n).sort_values("share_of_shelf_pct")
    fig = px.bar(top, x="share_of_shelf_pct", y="brand", color="classification", orientation="h",
                 color_discrete_map=CLASS_COLORS, category_orders={"classification": CLASSES},
                 hover_data={"products": True, "pincodes": True, "availability_pct": ":.0f"})
    fig.update_traces(marker_line_width=0)
    fig.update_xaxes(title="share of shelf %")
    fig.update_yaxes(title=None)
    st.plotly_chart(style_fig(fig, height=max(300, 24 * n + 80), xgrid=True), width="stretch")

st.divider()
st.subheader("Classification mix")
dim = st.radio("Split by", ["subcategory", "city"], horizontal=True, format_func=str.title)
ss = shelf_share_by(where, params, dim)
if not ss.empty:
    order = (ss[ss["classification"] == "Korean"].set_index("dim")["share_pct"].reindex(
        ss["dim"].unique()).fillna(0).sort_values(ascending=False).index.tolist())
    fig = px.bar(ss, x="share_pct", y="dim", color="classification", orientation="h",
                 color_discrete_map=CLASS_COLORS, category_orders={"classification": CLASSES, "dim": order[::-1]},
                 hover_data={"cards": True})
    fig.update_traces(marker_line_color="#ffffff", marker_line_width=2)
    fig.update_xaxes(title="% of shelf", range=[0, 100])
    fig.update_yaxes(title=None)
    st.plotly_chart(style_fig(fig, height=max(280, 26 * ss["dim"].nunique() + 80), xgrid=True), width="stretch")
    st.caption(f"Sorted by Korean share, highest first — the top rows are where K-beauty already has a foothold.")

st.divider()
st.subheader("Brand × pincode coverage")
st.caption("Dark cells = more listings. Gaps in a row are distribution opportunities (or genuine store-assortment differences).")
measure = st.radio("Cell value", ["Listings", "Listings in stock"], horizontal=True)
val = "count(*)" if measure == "Listings" else "count(CASE WHEN in_stock THEN 1 END)"
grid = q(f"""
    WITH shelf AS (SELECT l.*, coalesce(bl.brand_name, l.brand_raw) AS brand,
                          coalesce(bl.classification,'Unclassified') AS classification
                   FROM listings l LEFT JOIN brands_live bl USING (brand_key)
                   WHERE {where} AND NOT l.is_variant)
    SELECT brand, brand_key, city, pincode, {val} AS n FROM shelf GROUP BY 1,2,3,4""", params)
if grid.empty:
    st.info("Nothing to map.")
else:
    pins = grid[["city", "pincode"]].drop_duplicates().sort_values(["city", "pincode"])
    pins["label"] = pins["city"].str.slice(0, 9) + " " + pins["pincode"]
    pinned = set([fk] + competitor_keys())
    cov = (grid.groupby(["brand", "brand_key"], as_index=False)
           .agg(pincodes=("pincode", "nunique"), total=("n", "sum")))
    cov["pinned"] = cov["brand_key"].isin(pinned)
    n2 = st.slider("Brands shown (K-beauty peers always included)", 10, min(60, len(cov)), min(25, len(cov)), key="covn")
    shown = pd.concat([cov[cov["pinned"]], cov[~cov["pinned"]].sort_values(["pincodes", "total"], ascending=False).head(n2)]
                      ).drop_duplicates("brand_key")
    order = shown.sort_values(["pinned", "pincodes", "total"], ascending=[False, False, False])["brand"].tolist()
    mat = (grid[grid["brand_key"].isin(shown["brand_key"])]
           .pivot_table(index="brand", columns="pincode", values="n", aggfunc="sum", fill_value=0)
           .reindex(index=order, columns=pins["pincode"], fill_value=0))
    fig = px.imshow(mat.values, x=pins["label"].tolist(), y=mat.index.tolist(), color_continuous_scale=SEQ,
                    aspect="auto", zmin=0, labels=dict(color=measure))
    fig.update_traces(xgap=2, ygap=2, hovertemplate="%{y}<br>%{x}<br>" + measure + ": %{z}<extra></extra>")
    fig.update_xaxes(side="top", tickangle=-45, title=None, showgrid=False)
    fig.update_yaxes(title=None, showgrid=False)
    fig.update_layout(coloraxis_colorbar=dict(title=None, thickness=10, len=0.55))
    st.plotly_chart(style_fig(fig, height=max(360, 19 * len(mat) + 190), legend=False), width="stretch")

st.divider()
st.subheader("Trend across crawls")
aw, ap = f.where_all_runs()
tr = q(f"""
    WITH shelf AS (SELECT l.*, coalesce(bl.classification,'Unclassified') AS classification
                   FROM listings l LEFT JOIN brands_live bl USING (brand_key)
                   WHERE {aw} AND NOT l.is_variant)
    SELECT run_id, max(crawled_at)::DATE AS crawl_date, classification,
           count(*) AS cards, count(*) * 100.0 / sum(count(*)) OVER (PARTITION BY run_id) AS share_pct,
           count(DISTINCT brand_key) AS brands
    FROM shelf GROUP BY 1, 3 ORDER BY 2""", ap)
if tr["run_id"].nunique() < 2:
    st.info("Needs two crawls. This populates after the next weekly run.")
else:
    fig = px.line(tr, x="crawl_date", y="share_pct", color="classification", markers=True,
                  color_discrete_map=CLASS_COLORS, category_orders={"classification": CLASSES},
                  hover_data={"brands": True, "cards": True})
    fig.update_traces(line_width=2, marker_size=8)
    fig.update_xaxes(title=None)
    fig.update_yaxes(title="share of shelf %")
    st.plotly_chart(style_fig(fig, height=320), width="stretch")
