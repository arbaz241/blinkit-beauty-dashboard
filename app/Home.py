"""Main page — the focus brand's competitive position, and what to do about it.

Deliberately *not* a data-inventory page. The old version led with "SKUs listed / brands / pincodes",
which describes the crawl rather than the business. This page answers the weekly question:
"where does Innisfree stand on Blinkit, and what needs attention?" — and falls back to an
opportunity read when the focus brand has little or no presence.
"""
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from common import (CLASS_COLORS, FOCUS_HUE, PEER_HUE, PROXY_NOTE, attention, brand_table, by_geo,
                    competitor_keys, competitor_names, focus_brand, focus_key, peer_bar, scope_totals,
                    shelf_share_by, sidebar_filters, style_fig)

st.set_page_config(page_title="Blinkit Beauty Intel", page_icon="🧴", layout="wide")

f = sidebar_filters()
if f is None:
    st.stop()
where, params = f.where()
fb, fk, pk = focus_brand(), focus_key(), competitor_keys()

bt = brand_table(where, params)
sc = scope_totals(where, params)
if bt.empty:
    st.title("Blinkit Beauty")
    st.info("No listings match these filters.")
    st.stop()

focus = bt[bt["brand_key"] == fk]
peers = bt[bt["brand_key"].isin(pk)]
kset = bt[bt["classification"] == "Korean"]
present = not focus.empty
prev_bt = brand_table(*f.where_for_run(f.prev_run_id)) if f.prev_run_id else pd.DataFrame()


def delta_for(col: str, fmt: str = "{:+.1f} pts"):
    """Week-over-week change for the focus brand, or None on the first crawl."""
    if prev_bt.empty or not present:
        return None
    p = prev_bt[prev_bt["brand_key"] == fk]
    if p.empty or pd.isna(p.iloc[0][col]) or pd.isna(focus.iloc[0][col]):
        return None
    return fmt.format(focus.iloc[0][col] - p.iloc[0][col])


# ---------------------------------------------------------------- header
st.title(f"{fb} on Blinkit" if present else f"K-beauty on Blinkit — {fb} entry view")
st.caption(f"Skincare + Makeup · crawl `{f.run_id}` · {sc['pincodes']} pincodes · "
           f"{sc['cards']:,} shelf listings · {sc['brands']} brands")

if present:
    r = focus.iloc[0]
    peer_share = peers["share_of_shelf_pct"].median() if not peers.empty else float("nan")
    c = st.columns(5)
    c[0].metric("Distribution", f"{r.pincodes} / {sc['pincodes']}", delta_for("distribution_pct"),
                help="Pincodes where at least one SKU is listed. The first battle in q-commerce.")
    c[1].metric("Availability", f"{r.availability_pct:.0f}%", delta_for("availability_pct"),
                help="Share of this brand's SKU listings in stock, counting every pack size. Measured across "
                     "variants on purpose: Blinkit's listing card always shows an in-stock size, so card-level "
                     "stock reads 100% even when pack sizes are dark.")
    c[2].metric("Share of shelf", f"{r.share_of_shelf_pct:.2f}%", delta_for("share_of_shelf_pct", "{:+.2f} pts"),
                help="Brand's listing cards as a share of every card crawled in scope. "
                     f"K-beauty peer median: {peer_share:.2f}%" if pd.notna(peer_share) else None)
    c[3].metric("Visibility", f"{r.visibility:.0f} / 100", delta_for("visibility"),
                help="How high up the bestseller-sorted shelf, 100 = top of every listing. A demand proxy, not sales.")
    c[4].metric("Price index", f"{r.price_index:.0f}", delta_for("price_index", "{:+.0f}"),
                help="Median price vs the sub-category shelf median (100 = at the median).")
else:
    c = st.columns(4)
    c[0].metric("Korean brands live", int(kset["brand_key"].nunique()),
                help="Distinct Korean-classified brands with at least one listing in scope.")
    c[1].metric("K-beauty share of shelf", f"{kset['share_of_shelf_pct'].sum():.2f}%")
    c[2].metric("Pincodes with K-beauty", int(kset["pincodes"].max()) if not kset.empty else 0)
    c[3].metric(f"{fb} listings", 0, help=f"{fb} was not found in this scope.")
    st.warning(f"**{fb} is not listed anywhere in this selection.** The page below reads as an entry map: "
               "where K-beauty already sells, which sub-categories it wins, and where the whitespace is.")

unclassified_share = bt[bt["classification"] == "Unclassified"]["share_of_shelf_pct"].sum()
if unclassified_share >= 5:
    st.info(f"**{unclassified_share:.0f}% of the shelf is still unclassified** "
            f"({int((bt['classification'] == 'Unclassified').sum())} brands), so the Korean/Global/Local split above is a "
            "floor, not a final number — some of those brands are likely K-beauty. Clear the queue on "
            "**Data & brands** to firm it up.")

# ---------------------------------------------------------------- attention
st.subheader("What needs attention")
prev_w, prev_p = f.where_for_run(f.prev_run_id) if f.prev_run_id else (None, None)
att = attention(where, params, fk, tuple(pk), prev_w, prev_p)
if att.empty:
    st.success("Nothing flagged: no stock-outs, distribution gaps or price outliers in this selection.")
else:
    kinds = att["kind"].value_counts()
    st.caption(" · ".join(f"**{n}** {k.lower()}" for k, n in kinds.items())
               + ("" if f.prev_run_id else "  —  week-over-week checks activate once a second crawl exists."))
    pick_kinds = st.multiselect("Filter by issue", kinds.index.tolist(), default=[],
                                help="Leave empty to see everything, highest severity first.")
    att = att[att["kind"].isin(pick_kinds)] if pick_kinds else att
    st.dataframe(att[["kind", "what", "detail", "action"]].head(40), hide_index=True, width="stretch",
                 height=min(430, 36 * min(len(att), 11) + 42),
                 column_config={"kind": st.column_config.TextColumn("issue", width="small"),
                                "what": st.column_config.TextColumn("what", width="medium"),
                                "detail": st.column_config.TextColumn("detail", width="large"),
                                "action": st.column_config.TextColumn("suggested action", width="medium")})
    if len(att) > 40:
        st.caption(f"Showing the 40 highest-severity of {len(att)} findings.")

# ---------------------------------------------------------------- peer set
st.divider()
st.subheader("K-beauty peer set")
peer_view = bt[bt["brand_key"].isin([fk] + pk) | (bt["classification"] == "Korean")].copy()
if peer_view.empty:
    st.info("No Korean-classified brands in this selection — check the Brand review queue.")
else:
    left, right = st.columns([3, 4])
    with left:
        st.plotly_chart(peer_bar(peer_view, value="share_of_shelf_pct", label="share of shelf",
                                 focus=fb, suffix="%"), width="stretch")
        st.caption(f"Blue = {fb}. Grey = other Korean brands on Blinkit.")
    with right:
        show = peer_view[["brand", "products", "pincodes", "distribution_pct", "availability_pct",
                          "share_of_shelf_pct", "visibility", "median_price", "price_index"]]
        st.dataframe(show, hide_index=True, width="stretch", height=36 * len(show) + 42,
                     column_config={
                         "brand": "brand", "products": "products",
                         "pincodes": st.column_config.NumberColumn("pincodes"),
                         "distribution_pct": st.column_config.ProgressColumn("distribution", min_value=0, max_value=100, format="%.0f%%"),
                         "availability_pct": st.column_config.ProgressColumn("in stock", min_value=0, max_value=100, format="%.0f%%"),
                         "share_of_shelf_pct": st.column_config.NumberColumn("shelf %", format="%.2f%%"),
                         "visibility": st.column_config.NumberColumn("visibility", format="%.0f"),
                         "median_price": st.column_config.NumberColumn("median ₹", format="₹%.0f"),
                         "price_index": st.column_config.NumberColumn("price idx", format="%.0f"),
                     })
st.caption(PROXY_NOTE)

# ---------------------------------------------------------------- geography
st.divider()
st.subheader("Where the K-beauty shelf is")
kb_keys = tuple(kset["brand_key"].tolist())
geo_all = by_geo(where, params)
if kb_keys and not geo_all.empty:
    geo_k = by_geo(where, params, kb_keys).rename(columns={"cards": "kbeauty_cards", "brands": "kbeauty_brands"})
    g = geo_all.merge(geo_k[["pincode", "kbeauty_cards", "kbeauty_brands"]], on="pincode", how="left").fillna(
        {"kbeauty_cards": 0, "kbeauty_brands": 0})
    g["kbeauty_share_pct"] = g["kbeauty_cards"] / g["cards"] * 100
    if present:
        fpins = set(by_geo(where, params, (fk,))["pincode"])
        g["focus"] = g["pincode"].isin(fpins)
    else:
        g["focus"] = False
    g["label"] = g["city"].str.slice(0, 10) + " " + g["pincode"]
    d = g.sort_values("kbeauty_share_pct", ascending=True)
    fig = go.Figure(go.Bar(
        x=d["kbeauty_share_pct"], y=d["label"], orientation="h",
        marker=dict(color=[FOCUS_HUE if x else PEER_HUE for x in d["focus"]], line=dict(width=0)),
        customdata=d[["kbeauty_brands", "cards", "focus"]].values,
        hovertemplate="%{y}<br>K-beauty share: %{x:.2f}%<br>K-beauty brands: %{customdata[0]:.0f}"
                      "<br>total shelf: %{customdata[1]:.0f} cards<extra></extra>"))
    fig.update_xaxes(title="K-beauty share of shelf %")
    fig.update_yaxes(title=None)
    st.plotly_chart(style_fig(fig, height=max(260, 22 * len(d) + 80), legend=False, xgrid=True), width="stretch")
    st.caption(f"Blue = pincodes where {fb} is listed; grey = K-beauty present but {fb} is not."
               if present else f"No pincode carries {fb} yet — bars show where K-beauty rivals already sell.")
else:
    st.info("No Korean-classified brands to map yet.")

# ---------------------------------------------------------------- category structure
st.subheader("Which sub-categories K-beauty wins")
ss = shelf_share_by(where, params, "subcategory")
if not ss.empty:
    kb = ss[ss["classification"] == "Korean"].sort_values("share_pct", ascending=True)
    if kb.empty:
        st.info("No Korean listings in these sub-categories.")
    else:
        fig = go.Figure(go.Bar(x=kb["share_pct"], y=kb["dim"], orientation="h",
                               marker=dict(color=CLASS_COLORS["Korean"], line=dict(width=0)),
                               text=[f"{v:.1f}%" for v in kb["share_pct"]], textposition="outside", cliponaxis=False,
                               customdata=kb[["cards"]].values,
                               hovertemplate="%{y}<br>K-beauty share: %{x:.2f}%<br>%{customdata[0]:.0f} cards<extra></extra>"))
        fig.update_xaxes(title="K-beauty share of sub-category shelf %",
                         range=[0, float(kb["share_pct"].max()) * 1.25])
        fig.update_yaxes(title=None)
        st.plotly_chart(style_fig(fig, height=max(240, 26 * len(kb) + 70), legend=False, xgrid=True), width="stretch")
        st.caption("Sub-categories where K-beauty already has traction are the easiest places to add SKUs; "
                   "zero-share sub-categories are either a real gap or a category K-beauty does not play in.")
