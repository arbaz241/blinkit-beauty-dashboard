"""Week-over-week diff: what entered the shelf, what fell off it, and what repriced."""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import (CLASS_COLORS, CLASSES, FOCUS_HUE, PEER_HUE, competitor_keys, focus_brand, focus_key,
                    inr_col, link_col, q, run_ids, sidebar_filters, style_fig)

st.set_page_config(page_title="Weekly changes", page_icon="🧴", layout="wide")
st.title("Weekly changes")

f = sidebar_filters(show_stock=False)
if f is None:
    st.stop()
ids = run_ids()
if len(ids) < 2:
    st.info("Diffs need two crawls. Only one exists so far — this page populates after the next weekly run.")
    st.stop()

newer = f.run_id
older = st.selectbox("Compare against", [r for r in ids if r != newer],
                     index=0 if f.prev_run_id is None else max(0, [r for r in ids if r != newer].index(f.prev_run_id)))
fb, fk, pk = focus_brand(), focus_key(), competitor_keys()

w, p = f.where_all_runs()
both = q(f"""
    WITH s AS (SELECT l.*, coalesce(bl.brand_name,l.brand_raw) AS brand,
                      coalesce(bl.classification,'Unclassified') AS classification
               FROM listings l LEFT JOIN brands_live bl USING (brand_key)
               WHERE {w} AND NOT l.is_variant AND l.run_id IN (?, ?))
    SELECT run_id, pincode, city, area, product_id, product_name, brand, brand_key, classification,
           top_category, subcategory, unit, price, mrp, in_stock, position, product_url FROM s""",
         p + (newer, older))
a, b = both[both["run_id"] == newer], both[both["run_id"] == older]
if a.empty or b.empty:
    st.info("One of the two crawls has no rows under these filters.")
    st.stop()

key = ["pincode", "product_id"]
new_l = a.merge(b[key].drop_duplicates(), on=key, how="left", indicator=True).query("_merge=='left_only'").drop(columns="_merge")
gone_l = b.merge(a[key].drop_duplicates(), on=key, how="left", indicator=True).query("_merge=='left_only'").drop(columns="_merge")
new_brand = set(a["brand_key"]) - set(b["brand_key"])
gone_brand = set(b["brand_key"]) - set(a["brand_key"])
price = a.merge(b[key + ["price"]], on=key, suffixes=("", "_prev"))
price = price[price["price"].notna() & price["price_prev"].notna() & (price["price"] != price["price_prev"])].copy()
price["change_pct"] = ((price["price"] / price["price_prev"] - 1) * 100).round(1)
oos_now = a.merge(b[key + ["in_stock"]], on=key, suffixes=("", "_prev"))
went_oos = oos_now[(~oos_now["in_stock"]) & (oos_now["in_stock_prev"])]
came_back = oos_now[(oos_now["in_stock"]) & (~oos_now["in_stock_prev"])]

c = st.columns(5)
c[0].metric("New listings", f"{len(new_l):,}", help="SKU × pincode pairs present now, absent last crawl.")
c[1].metric("Delisted", f"{len(gone_l):,}")
c[2].metric("Brands entered", len(new_brand))
c[3].metric("Went out of stock", f"{len(went_oos):,}")
c[4].metric("Repriced", f"{len(price):,}")
st.caption(f"`{older}` → `{newer}`. A pincode or sub-category that failed to crawl in either run shows up as "
           "spurious churn — cross-check *Crawl health* on the Data & brands page before acting on a spike.")

kset = [fk] + pk
ours = lambda d: d[d["brand_key"].isin(kset)]  # noqa: E731
st.subheader(f"Affecting {fb} and its K-beauty peers")
k = st.columns(4)
k[0].metric("New listings", len(ours(new_l)))
k[1].metric("Delisted", len(ours(gone_l)))
k[2].metric("Went OOS", len(ours(went_oos)))
k[3].metric("Repriced", len(ours(price)))

if not new_l.empty or not gone_l.empty:
    st.subheader("Net listing change by brand")
    net = (pd.concat([new_l.assign(d=1), gone_l.assign(d=-1)])
           .groupby(["brand", "brand_key", "classification"], as_index=False)["d"].sum())
    net = net.reindex(net["d"].abs().sort_values(ascending=False).index).head(20).sort_values("d")
    fig = go.Figure(go.Bar(
        x=net["d"], y=net["brand"], orientation="h",
        marker=dict(color=[FOCUS_HUE if kk in kset else PEER_HUE for kk in net["brand_key"]], line=dict(width=0)),
        customdata=net[["classification"]].values,
        hovertemplate="%{y} (%{customdata[0]})<br>net %{x:+.0f} listings<extra></extra>"))
    fig.add_vline(x=0, line_color="#c3c2b7")
    fig.update_xaxes(title="net change in SKU × pincode listings")
    fig.update_yaxes(title=None)
    st.plotly_chart(style_fig(fig, height=max(280, 24 * len(net) + 80), legend=False, xgrid=True), width="stretch")
    st.caption(f"Blue = {fb} and its K-beauty peers.")

cols = ["pincode", "area", "brand", "classification", "product_name", "unit", "subcategory", "price", "product_url"]
t1, t2, t3, t4 = st.tabs([f"New ({len(new_l)})", f"Delisted ({len(gone_l)})",
                          f"Stock flips ({len(went_oos) + len(came_back)})", f"Price moves ({len(price)})"])
with t1:
    if new_brand:
        st.success("Brands new to Blinkit in scope: " + ", ".join(sorted(a[a["brand_key"].isin(new_brand)]["brand"].unique())))
    st.dataframe(new_l.sort_values(["classification", "brand"])[cols], hide_index=True, width="stretch",
                 height=480, column_config={"price": inr_col("price"), "product_url": link_col()})
with t2:
    if gone_brand:
        st.warning("Brands gone from Blinkit in scope: " + ", ".join(sorted(b[b["brand_key"].isin(gone_brand)]["brand"].unique())))
    st.dataframe(gone_l.sort_values(["classification", "brand"])[cols], hide_index=True, width="stretch",
                 height=480, column_config={"price": inr_col("price"), "product_url": link_col()})
with t3:
    flips = pd.concat([went_oos.assign(flip="went OOS"), came_back.assign(flip="back in stock")])
    st.dataframe(flips.sort_values(["flip", "brand"])[["flip"] + cols], hide_index=True, width="stretch",
                 height=480, column_config={"price": inr_col("price"), "product_url": link_col()})
with t4:
    st.dataframe(price.sort_values("change_pct")[["pincode", "brand", "classification", "product_name", "unit",
                                                  "price_prev", "price", "change_pct", "product_url"]],
                 hide_index=True, width="stretch", height=480,
                 column_config={"price_prev": inr_col("was"), "price": inr_col("now"),
                                "change_pct": st.column_config.NumberColumn("change", format="%+.1f%%"),
                                "product_url": link_col()})
