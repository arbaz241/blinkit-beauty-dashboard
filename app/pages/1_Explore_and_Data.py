"""Explore & data — everything behind the story page, for when you need to check a number.

Four tabs: every product on the shelf (filterable, downloadable), week-over-week changes, the brand
classification queue, and the crawl's own health and integrity report.
"""
import base64
import json
import urllib.request

import pandas as pd
import streamlit as st

import insights as I
from common import (BRANDS_CSV, CLASSES, competitor_keys, focus_brand, focus_key, get_conn, invalidate,
                    link_col, q, run_ids, runs)
from db.brands import upsert_csv

st.set_page_config(page_title="Explore & data", page_icon="🧴", layout="wide")
st.markdown("<style>.block-container{padding-top:2rem;max-width:1280px}</style>", unsafe_allow_html=True)
st.title("Explore & data")

ids = run_ids()
if not ids:
    st.warning("No crawl data yet — run `python scrape.py`.")
    st.stop()
with st.sidebar:
    run_id = st.selectbox("Crawl", ids, format_func=lambda r: f"{r}{'  (latest)' if r == ids[0] else ''}")
FB, FK = focus_brand(), focus_key()

tab_p, tab_c, tab_b, tab_h = st.tabs(["Products", "Changes", "Brand classification", "Data quality & crawl health"])

# ============================================================================ products
with tab_p:
    st.caption("Every product on the shelf, aggregated across the stores that list it. Filter, sort, download.")
    base = q("""
        WITH L AS (SELECT l.*, coalesce(bl.brand_name, l.brand_raw) AS brand,
                          coalesce(bl.classification, 'Unclassified') AS cls
                   FROM listings l LEFT JOIN brands_live bl USING (brand_key) WHERE l.run_id = ?),
             -- Stock must be judged per store. Counting "sizes ever out of stock" across all 24 stores
             -- flags any product dark in even one of them, which reads as a hidden pack-size problem
             -- for 91% of K-beauty when the real figure is ~27%.
             fp AS (SELECT coalesce(parent_product_id, product_id) AS fid, pincode,
                           bool_or(NOT is_variant AND in_stock) AS card_ok, bool_or(NOT in_stock) AS any_dark,
                           count(DISTINCT product_id) AS sizes
                    FROM L GROUP BY 1, 2),
             fam AS (SELECT fid, max(sizes) AS sizes,
                            count(*) FILTER (WHERE any_dark) AS stores_dark,
                            count(*) FILTER (WHERE card_ok AND any_dark) AS hidden_dark
                     FROM fp GROUP BY 1),
             av AS (SELECT coalesce(parent_product_id, product_id) AS fid, avg(in_stock::int) * 100 AS avail
                    FROM L GROUP BY 1)
        SELECT c.brand, c.brand_key, c.cls, c.top_category, c.subcategory, c.product_id,
               any_value(c.product_name) AS product, any_value(c.unit) AS unit, any_value(c.ptype) AS type,
               median(c.price) AS price, median(c.mrp) AS mrp, avg(c.discount_pct) AS discount,
               count(DISTINCT c.pincode) AS stores, any_value(a.avail) AS in_stock, any_value(f.sizes) AS sizes,
               any_value(f.stores_dark) AS stores_dark, any_value(f.hidden_dark) AS hidden_dark,
               median(c.position) AS median_rank, min(c.position) AS best_rank,
               max(c.rating_count) AS reviews, max(c.rating_value) AS rating,
               string_agg(DISTINCT c.city, ', ') AS cities, any_value(c.product_url) AS url
        FROM L c LEFT JOIN fam f ON f.fid = c.product_id LEFT JOIN av a ON a.fid = c.product_id
        WHERE NOT c.is_variant
        GROUP BY 1, 2, 3, 4, 5, 6""", (run_id,))
    c1, c2, c3, c4 = st.columns([2, 2, 2, 3])
    who = c1.selectbox("Brands", ["K-beauty", f"{FB} only", f"{FB} + peers", "All brands"])
    cats = c2.multiselect("Sub-category", sorted(base["subcategory"].unique()))
    cls_ = c3.multiselect("Classification", CLASSES)
    text = c4.text_input("Search product or brand", placeholder="e.g. sunscreen, volcanic, laneige")
    d = base
    if who == "K-beauty":
        d = d[d["cls"] == "Korean"]
    elif who == f"{FB} only":
        d = d[d["brand_key"] == FK]
    elif who == f"{FB} + peers":
        d = d[d["brand_key"].isin([FK] + competitor_keys())]
    if cats:
        d = d[d["subcategory"].isin(cats)]
    if cls_:
        d = d[d["cls"].isin(cls_)]
    if text:
        t = text.lower()
        d = d[d["product"].str.lower().str.contains(t, regex=False) | d["brand"].str.lower().str.contains(t, regex=False)]
    d = d.sort_values(["median_rank"])
    # A combo listed in three sub-categories is three rows here (one per shelf) but one product, so
    # every product count is on distinct product_id.
    u = d.drop_duplicates("product_id")
    m = st.columns(6)
    m[0].metric("Products", f"{len(u):,}", help="Distinct products. A combo on several shelves is one product "
                                                 "but one row per shelf in the table.")
    m[1].metric("Brands", d["brand"].nunique())
    m[2].metric("Median price", f"₹{u['price'].median():,.0f}" if len(u) else "–")
    m[3].metric("Avg in stock", f"{u['in_stock'].mean():.0f}%" if len(u) else "–")
    m[4].metric("Dark in some store", int((u["stores_dark"].fillna(0) > 0).sum()),
                help="Out of stock (any pack size) in at least one of the stores that list it.")
    m[5].metric("Hidden stock-outs", int((u["hidden_dark"].fillna(0) > 0).sum()),
                help="A pack size is dark while the listing card still shows in stock — invisible until a "
                     "shopper opens the size picker.")
    show = d[["brand", "cls", "product", "unit", "subcategory", "type", "price", "mrp", "discount", "stores",
              "in_stock", "stores_dark", "sizes", "hidden_dark", "median_rank", "best_rank", "reviews", "rating", "url"]]
    st.dataframe(show, hide_index=True, width="stretch", height=600, column_config={
        "cls": "class", "price": st.column_config.NumberColumn("price", format="₹%.0f"),
        "mrp": st.column_config.NumberColumn("MRP", format="₹%.0f"),
        "discount": st.column_config.NumberColumn("disc", format="%.0f%%"),
        "in_stock": st.column_config.ProgressColumn("in stock", min_value=0, max_value=100, format="%.0f%%"),
        "stores_dark": st.column_config.NumberColumn("stores dark", help="Stores where any pack size is out of stock."),
        "hidden_dark": st.column_config.NumberColumn("hidden", help="Stores where a pack size is dark behind an "
                                                                    "in-stock listing card."),
        "median_rank": st.column_config.NumberColumn("median rank", format="%.0f",
                                                     help="Position in Blinkit's bestseller-sorted listing (1 = top)."),
        "best_rank": st.column_config.NumberColumn("best rank", format="%.0f"),
        "reviews": st.column_config.NumberColumn("reviews", format="%.0f"),
        "url": link_col()})
    st.download_button("Download as CSV", show.to_csv(index=False).encode(), "blinkit_products.csv", "text/csv")

# ============================================================================ changes
with tab_c:
    if len(ids) < 2 or ids.index(run_id) == len(ids) - 1:
        st.info("Only one crawl exists, so there is nothing to compare yet. After next week's run this tab lists "
                "new listings, delistings, stock flips and price changes.")
    else:
        older = st.selectbox("Compare against", [r for r in ids if r != run_id], index=0)
        both = q("""
            SELECT l.run_id, l.pincode, l.city, l.product_id, l.product_name, l.unit, l.subcategory, l.price, l.in_stock,
                   coalesce(bl.brand_name, l.brand_raw) AS brand, l.brand_key,
                   coalesce(bl.classification, 'Unclassified') AS cls, l.product_url
            FROM listings l LEFT JOIN brands_live bl USING (brand_key)
            WHERE NOT l.is_variant AND l.run_id IN (?, ?)""", (run_id, older))
        a, b = both[both["run_id"] == run_id], both[both["run_id"] == older]
        key = ["pincode", "product_id"]
        new_l = a.merge(b[key], on=key, how="left", indicator=True).query("_merge=='left_only'").drop(columns="_merge")
        gone = b.merge(a[key], on=key, how="left", indicator=True).query("_merge=='left_only'").drop(columns="_merge")
        mv = a.merge(b[key + ["price", "in_stock"]], on=key, suffixes=("", "_prev"))
        rep = mv[mv["price"] != mv["price_prev"]].assign(change=lambda x: (x["price"] / x["price_prev"] - 1) * 100)
        flips = mv[mv["in_stock"] != mv["in_stock_prev"]]
        scope = st.radio("Show", [f"{FB} + K-beauty", "Everything"], horizontal=True)
        if scope != "Everything":
            f_ = lambda x: x[x["cls"] == "Korean"]  # noqa: E731
            new_l, gone, rep, flips = f_(new_l), f_(gone), f_(rep), f_(flips)
        k = st.columns(4)
        k[0].metric("New listings", len(new_l)); k[1].metric("Delisted", len(gone))
        k[2].metric("Repriced", len(rep)); k[3].metric("Stock flips", len(flips))
        st.caption("A pincode that failed to crawl in either run shows up as churn — check the Data quality tab first.")
        cols = ["pincode", "brand", "product_name", "unit", "subcategory", "price", "product_url"]
        for label, df in [("New", new_l), ("Delisted", gone)]:
            with st.expander(f"{label} ({len(df)})", expanded=label == "New"):
                st.dataframe(df[cols], hide_index=True, width="stretch", column_config={"product_url": link_col()})
        with st.expander(f"Price changes ({len(rep)})"):
            st.dataframe(rep.sort_values("change")[["pincode", "brand", "product_name", "price_prev", "price", "change"]],
                         hide_index=True, width="stretch",
                         column_config={"change": st.column_config.NumberColumn(format="%+.1f%%")})

# ============================================================================ brands
with tab_b:
    st.caption("Brands are auto-classified from `config/brands_seed.yaml`. Confirm or correct them here — confirmed "
               "brands are never re-suggested. Sorted by shelf presence, so classify from the top.")
    brands = q("""
        SELECT bl.brand_key, bl.brand_name, bl.classification, bl.country_of_origin, bl.notes, bl.confirmed,
               bl.suggested_by, coalesce(s.cards, 0) AS listings, s.sample
        FROM brands_live bl LEFT JOIN (
            SELECT brand_key, count(*) AS cards, string_agg(DISTINCT product_name, ' · ') AS sample
            FROM listings WHERE run_id = ? AND NOT is_variant GROUP BY 1) s USING (brand_key)
        ORDER BY bl.confirmed, listings DESC""", (run_id,))
    brands["sample"] = brands["sample"].fillna("").str.slice(0, 130)
    for c in ("country_of_origin", "notes"):
        brands[c] = brands[c].fillna("")
    which = st.radio("Show", ["Unclassified", "Unconfirmed", "All"], horizontal=True)
    view = (brands[brands["classification"] == "Unclassified"] if which == "Unclassified"
            else brands[~brands["confirmed"]] if which == "Unconfirmed" else brands)
    tot = brands["listings"].sum() or 1
    m = st.columns(3)
    m[0].metric("Brands", len(brands))
    m[1].metric("Unclassified", int((brands["classification"] == "Unclassified").sum()),
                f"{brands[brands['classification'] == 'Unclassified']['listings'].sum() / tot * 100:.0f}% of listings",
                delta_color="off")
    m[2].metric("Confirmed", int(brands["confirmed"].sum()))
    edited = st.data_editor(
        view[["brand_key", "brand_name", "classification", "country_of_origin", "notes", "confirmed", "suggested_by",
              "listings", "sample"]],
        hide_index=True, width="stretch", height=520, key="brand_editor",
        disabled=["brand_key", "brand_name", "suggested_by", "listings", "sample"],
        column_config={"brand_key": None, "brand_name": "brand",
                       "classification": st.column_config.SelectboxColumn(options=CLASSES, required=True),
                       "country_of_origin": "country", "suggested_by": "suggested by",
                       "sample": st.column_config.TextColumn("sample products", width="large")})
    mg = edited.merge(view, on="brand_key", suffixes=("", "_old"))
    changed = mg[(mg["classification"] != mg["classification_old"]) | (mg["confirmed"] != mg["confirmed_old"])
                 | (mg["country_of_origin"] != mg["country_of_origin_old"]) | (mg["notes"] != mg["notes_old"])]
    if st.button(f"Save {len(changed)} change(s)", type="primary", disabled=changed.empty):
        n = upsert_csv([{"brand_key": r["brand_key"], "classification": r["classification"],
                         "country_of_origin": r["country_of_origin"], "notes": r["notes"],
                         "confirmed": bool(r["confirmed"])} for _, r in changed.iterrows()])
        invalidate()
        st.success(f"Saved {n} brand(s). The story page reflects this immediately.")
        st.rerun()
    if BRANDS_CSV.exists():
        st.download_button("Download brands.csv", BRANDS_CSV.read_bytes(), "brands.csv", "text/csv")

    def _secret(name):
        try:
            return st.secrets.get(name)
        except Exception:  # noqa: BLE001 - no secrets.toml configured
            return None

    token, repo = _secret("GITHUB_TOKEN"), _secret("GITHUB_REPO")
    if token and repo and st.button("Commit brands.csv to GitHub"):
        api = f"https://api.github.com/repos/{repo}/contents/data/brands.csv"
        hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "blinkit-dashboard"}
        try:
            with urllib.request.urlopen(urllib.request.Request(api, headers=hdr), timeout=20) as r:
                sha = json.load(r).get("sha")
        except Exception:  # noqa: BLE001 - file may not exist yet
            sha = None
        body = {"message": "brands: review from dashboard", "content": base64.b64encode(BRANDS_CSV.read_bytes()).decode()}
        if sha:
            body["sha"] = sha
        try:
            req = urllib.request.Request(api, data=json.dumps(body).encode(),
                                         headers={**hdr, "Content-Type": "application/json"}, method="PUT")
            with urllib.request.urlopen(req, timeout=30) as r:
                st.success(f"Committed: {json.load(r)['commit']['html_url']}")
        except Exception as e:  # noqa: BLE001
            st.error(f"GitHub commit failed: {e}")
    elif not (token and repo):
        st.caption("On Streamlit Cloud, add `GITHUB_TOKEN` and `GITHUB_REPO` secrets to commit edits back to the repo; "
                   "otherwise download the CSV and commit it yourself.")

# ============================================================================ health
with tab_h:
    tr = I.trust(I.Ctx(get_conn(), run_id, FK, competitor_keys(), FB))
    s = tr["s"]
    st.subheader("Integrity checks")
    st.caption("Run on every page load against the selected crawl. If any fail, treat the story page with suspicion.")
    st.dataframe(pd.DataFrame(tr["checks"], columns=["check", "passed", "result"]), hide_index=True, width="stretch",
                 column_config={"passed": st.column_config.CheckboxColumn("pass")})
    st.subheader("Known caveats")
    for cv in tr["caveats"]:
        st.markdown(f"- {cv}")
    st.markdown("- **Completeness is benchmarked against Blinkit's final count, not its first.** Blinkit's opening "
                "estimate of a category's size is inflated and revised mid-list; the crawl records the revised figure.")
    st.subheader("Crawl run")
    rl = q("SELECT * FROM crawl_runs WHERE run_id = ?", (run_id,))
    if len(rl):
        r = rl.iloc[0]
        m = st.columns(5)
        m[0].metric("Pincodes ok", f"{r.pincodes_ok} / {r.pincodes_total}")
        m[1].metric("Sub-category units", f"{r.units_ok} ok")
        m[2].metric("Listings", f"{int(s.cards):,}")
        m[3].metric("SKU rows", f"{int(s.n_rows):,}")
        m[4].metric("Requests (retries)", f"{r.requests} ({r.retries})")
        if r.notes:
            st.caption(f"Run notes: {r.notes}")
        log = json.loads(r.run_log_json or "{}")
        pins = pd.DataFrame([{
            "pincode": p["pincode"], "city": p["city"], "area": p["area"], "status": p["status"],
            "Blinkit resolved to": p.get("locality"), "store id": p.get("merchant_id"),
            "units ok": sum(u["status"] == "ok" for u in p["units"]),
            "rows": sum(u["rows"] for u in p["units"])} for p in log.get("pincodes", [])])
        if len(pins):
            st.dataframe(pins.sort_values(["city", "pincode"]), hide_index=True, width="stretch")
            st.caption("'Blinkit resolved to' is the locality Blinkit geocoded each pincode to. A district name "
                       "(e.g. Rangareddy) instead of the area is normal; a different city would mean a mis-targeted crawl.")
    st.subheader("All crawls")
    st.dataframe(runs(), hide_index=True, width="stretch")
