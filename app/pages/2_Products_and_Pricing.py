"""Product-level drill-down: every SKU for a chosen brand set, and how it is priced against the shelf.

Merges the old "SKU tracker" and "Price positioning" pages — they asked the same question (how does
this SKU sit against its shelf?) from two directions, and split the evidence across two screens.
"""
import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from common import (CLASS_COLORS, CLASSES, FOCUS_HUE, PEER_HUE, PROXY_NOTE, add_unit_price, brand_table,
                    competitor_keys, focus_brand, focus_key, focus_products, inr_col, link_col, q,
                    sidebar_filters, sku_depth, style_fig)

st.set_page_config(page_title="Products & pricing", page_icon="🧴", layout="wide")
st.title("Products & pricing")

f = sidebar_filters()
if f is None:
    st.stop()
where, params = f.where()
fb, fk, pk = focus_brand(), focus_key(), competitor_keys()

bt = brand_table(where, params)
if bt.empty:
    st.info("No listings match these filters.")
    st.stop()

names = dict(zip(bt["brand_key"], bt["brand"]))
clsmap = dict(zip(bt["brand_key"], bt["classification"]))
default = [k for k in [fk] + pk if k in names] or bt["brand_key"].head(3).tolist()
pick = st.multiselect("Brands", bt["brand_key"].tolist(), default=default,
                      format_func=lambda k: f"{names[k]} · {clsmap[k]}")
if not pick:
    st.info("Pick at least one brand.")
    st.stop()

prod = focus_products(where, params, tuple(pick))
if prod.empty:
    st.info("No products for these brands in this selection.")
    st.stop()
depth = sku_depth(where, params)
prod = prod.merge(depth[["brand_key", "skus_all"]].drop_duplicates("brand_key"), on="brand_key", how="left")

# ---- best-seller proxy: shelf rank (50%), rating volume (30%), pincode coverage (20%)
n_pins = q(f"""WITH s AS (SELECT l.* FROM listings l LEFT JOIN brands_live bl USING (brand_key)
                          WHERE {where} AND NOT l.is_variant)
               SELECT count(DISTINCT pincode) AS n FROM s""", params).iloc[0, 0] or 1
maxrank = max(prod["median_rank"].max(), 1)
rank_s = 1 - (prod["median_rank"].fillna(maxrank) / maxrank)
rc = np.log1p(prod["rating_count"].fillna(0))
rc_s = rc / rc.max() if rc.max() > 0 else rc
cov_s = prod["pincodes"] / n_pins
prod["seller_score"] = ((0.5 * rank_s + 0.3 * rc_s + 0.2 * cov_s) * 100).round(0)
prod["tier"] = pd.cut(prod["seller_score"], [-1, 40, 70, 101], labels=["Long tail", "Mid", "Major seller"]).astype(str)
prod = prod.sort_values("seller_score", ascending=False)

c = st.columns(5)
c[0].metric("Products", prod["product_id"].nunique())
c[1].metric("SKUs incl. pack sizes", int(prod.drop_duplicates("brand_key")["skus_all"].sum()))
c[2].metric("Major sellers (proxy)", int((prod["tier"] == "Major seller").sum()))
c[3].metric("Avg availability", f"{prod['availability_pct'].mean():.0f}%",
            help="Across every pack size, not just the card's default size.")
c[4].metric("Median price", f"₹{prod['price'].median():,.0f}")
n_hidden = int((prod["pack_sizes_oos"].fillna(0) > 0).sum())
if n_hidden:
    st.warning(f"**{n_hidden} product(s) have an out-of-stock pack size behind an in-stock listing card.** "
               "A shopper only discovers this after opening the size picker — see the `sizes OOS` column.")
st.caption(PROXY_NOTE)

tab_rank, tab_price, tab_table = st.tabs(["Best-seller proxy", "Price vs the shelf", "All products"])

with tab_rank:
    top = prod.head(25).copy()
    top["label"] = top["product_name"].str.slice(0, 46) + " · " + top["unit"].fillna("")
    d = top.sort_values("seller_score")
    fig = go.Figure(go.Bar(
        x=d["seller_score"], y=d["label"], orientation="h",
        marker=dict(color=[FOCUS_HUE if b == fb else PEER_HUE for b in d["brand"]], line=dict(width=0)),
        customdata=d[["brand", "median_rank", "rating_count", "pincodes", "availability_pct"]].values,
        hovertemplate="%{customdata[0]}<br>%{y}<br>score %{x:.0f}<br>median rank %{customdata[1]:.0f}"
                      "<br>%{customdata[2]:,.0f} ratings · %{customdata[3]:.0f} pincodes"
                      "<br>%{customdata[4]:.0f}% in stock<extra></extra>"))
    fig.update_xaxes(title="best-seller proxy score (0–100)", range=[0, 100])
    fig.update_yaxes(title=None)
    st.plotly_chart(style_fig(fig, height=max(320, 23 * len(d) + 80), legend=False, xgrid=True), width="stretch")
    st.caption(f"Blue = {fb}. Score = 50% shelf rank + 30% rating volume + 20% pincode coverage.")

with tab_price:
    mode = st.radio("Compare within", ["Blinkit product type", "Sub-category"], horizontal=True)
    col = "ptype" if mode.startswith("Blinkit") else "subcategory"
    opts = prod.groupby(col)["product_id"].nunique().sort_values(ascending=False)
    opts = opts[opts.index.notna()]
    if opts.empty:
        st.info("No product-type tags available here.")
    else:
        choice = st.selectbox("Group", opts.index.tolist(), format_func=lambda t: f"{t} ({opts[t]} of our products)")
        shelf = q(f"""
            WITH s AS (SELECT l.*, coalesce(bl.brand_name,l.brand_raw) AS brand,
                              coalesce(bl.classification,'Unclassified') AS classification
                       FROM listings l LEFT JOIN brands_live bl USING (brand_key)
                       WHERE {where} AND NOT l.is_variant)
            SELECT DISTINCT product_id, product_name, brand, brand_key, classification, unit, price, mrp, discount_pct
            FROM s WHERE {col} = ?""", params + (choice,))
        metric = st.radio("Price metric", ["Selling price", "Price per 100 g/ml"], horizontal=True)
        shelf = add_unit_price(shelf)
        ycol = "price" if metric == "Selling price" else "price_per_100"
        shelf = shelf.dropna(subset=[ycol])
        if shelf.empty:
            st.info("No priced products here.")
        else:
            med = shelf[ycol].median()
            ours = shelf[shelf["brand_key"].isin(pick)]
            k1, k2, k3 = st.columns(3)
            k1.metric("Products on this shelf", len(shelf))
            k2.metric("Shelf median", f"₹{med:,.0f}")
            k3.metric("Our median", f"₹{ours[ycol].median():,.0f}" if not ours.empty else "not listed",
                      delta=f"{(ours[ycol].median() / med - 1) * 100:+.0f}% vs shelf" if not ours.empty else None)
            l, r = st.columns([2, 3])
            with l:
                fig = px.box(shelf, x="classification", y=ycol, color="classification", points="all",
                             color_discrete_map=CLASS_COLORS, category_orders={"classification": CLASSES},
                             hover_data=["brand", "product_name", "unit"])
                fig.update_traces(marker_size=6, line_width=1.5, jitter=0.35, pointpos=0)
                fig.update_xaxes(title=None)
                fig.update_yaxes(title="₹" if ycol == "price" else "₹ per 100 g/ml")
                st.plotly_chart(style_fig(fig, height=400, legend=False), width="stretch")
            with r:
                bb = (shelf.groupby(["brand", "brand_key", "classification"])[ycol]
                      .agg(median="median", lo="min", hi="max", n="count")
                      .reset_index().sort_values("median"))
                keep = pd.concat([bb[bb["brand_key"].isin(pick)], bb.head(18)]).drop_duplicates("brand_key").sort_values("median")
                fig = go.Figure(go.Bar(
                    x=keep["median"], y=keep["brand"], orientation="h",
                    marker=dict(color=[FOCUS_HUE if k in pick else PEER_HUE for k in keep["brand_key"]], line=dict(width=0)),
                    customdata=keep[["lo", "hi", "n", "classification"]].values,
                    hovertemplate="%{y} (%{customdata[3]})<br>median ₹%{x:,.0f}"
                                  "<br>range ₹%{customdata[0]:,.0f}–₹%{customdata[1]:,.0f} · %{customdata[2]} products<extra></extra>"))
                fig.add_vline(x=med, line_dash="dot", line_color="#898781",
                              annotation_text="shelf median", annotation_position="top",
                              annotation_font_color="#52514e")
                fig.update_xaxes(title="median ₹" if ycol == "price" else "median ₹ per 100 g/ml")
                fig.update_yaxes(title=None)
                st.plotly_chart(style_fig(fig, height=max(360, 24 * len(keep) + 80), legend=False, xgrid=True),
                                width="stretch")
                st.caption("Blue = selected brands.")

with tab_table:
    show = prod[["brand", "product_name", "unit", "subcategory", "ptype", "price", "mrp", "discount_pct",
                 "price_index", "pincodes", "availability_pct", "pack_sizes", "pack_sizes_oos",
                 "median_rank", "best_rank", "rating", "rating_count", "seller_score", "tier", "product_url"]]
    st.dataframe(show, hide_index=True, width="stretch", height=560,
                 column_config={
                     "price": inr_col("price"), "mrp": inr_col("MRP"),
                     "discount_pct": st.column_config.NumberColumn("disc %", format="%.1f%%"),
                     "price_index": st.column_config.NumberColumn("price idx", format="%.0f",
                                                                  help="vs sub-category shelf median (100 = at median)"),
                     "availability_pct": st.column_config.NumberColumn("in stock %", format="%.0f%%"),
                     "pack_sizes": st.column_config.NumberColumn("sizes", format="%.0f"),
                     "pack_sizes_oos": st.column_config.NumberColumn("sizes OOS", format="%.0f",
                                                                     help="Pack sizes out of stock behind the listing card."),
                     "median_rank": st.column_config.NumberColumn("median rank", format="%.0f"),
                     "best_rank": st.column_config.NumberColumn("best rank", format="%.0f"),
                     "rating_count": st.column_config.NumberColumn("ratings", format="%.0f"),
                     "seller_score": st.column_config.ProgressColumn("seller score", min_value=0, max_value=100, format="%d"),
                     "product_url": link_col()})
    st.download_button("Download this table (CSV)", show.to_csv(index=False).encode(),
                       file_name="blinkit_products.csv", mime="text/csv")
