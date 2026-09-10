"""The analysis layer.

Every number the dashboard shows is defined once, here, so the pages stay thin and two pages can
never disagree about what "share of shelf" means.

Two vocabulary decisions that matter, both forced by how Blinkit's data actually looks:

* **Card vs SKU vs product.** A listing page shows one *card* per product; tapping it reveals pack-size
  *variants* (each its own product_id). 62% of scraped rows are variants, so counting product_ids
  flatters brands that sell one cream in five sizes. Shelf-facing metrics (share of shelf, rank,
  visibility) therefore count **primary cards only** (`NOT is_variant`) — that is literally what a
  shopper sees. Depth metrics count variants and say so.
* **Rank is a demand proxy, not sales.** Blinkit sorts categories by "bestsellers"; in the data,
  position correlates -0.43 with rating_count, so the ordering does track demand. It is still a proxy
  and is always labelled as one.

**Availability is the exception, and it matters.** A listing card always renders an *in-stock* variant
as its default, so out-of-stock never appears on a primary card — measured there, availability is a
constant 100% and tells you nothing. In the first full crawl, primary cards were 0% OOS while pack-size
variants were 15.9% OOS, and 17% of product families had an in-stock card hiding an out-of-stock pack
size. Availability is therefore always computed over **all SKU rows, variants included**.
"""
from __future__ import annotations

import pandas as pd

# Brand classification is joined from the live CSV (registered as `brands_live`), never from the
# persisted table, so edits in the review queue take effect without a DB write. See common.get_conn.
SHELF_CTE = """
WITH shelf AS (
    SELECT l.*,
           coalesce(bl.brand_name, l.brand_raw)        AS brand,
           coalesce(bl.classification, 'Unclassified') AS classification,
           coalesce(bl.confirmed, FALSE)               AS brand_confirmed
    FROM listings l LEFT JOIN brands_live bl USING (brand_key)
    WHERE {where} AND NOT l.is_variant
),
submed AS (SELECT subcategory, median(price) AS sub_median FROM shelf GROUP BY 1),
j AS (
    SELECT s.*, m.sub_median,
           s.price / nullif(m.sub_median, 0)                        AS price_ratio,
           count(*) OVER (PARTITION BY s.pincode, s.subcategory)     AS shelf_size,
           percent_rank() OVER (PARTITION BY s.pincode, s.subcategory ORDER BY s.position) AS rank_pct
    FROM shelf s JOIN submed m USING (subcategory)
),
-- Same scope but WITHOUT the variant filter: the only place stock-outs are visible.
allsku AS (
    SELECT l.*, coalesce(l.parent_product_id, l.product_id) AS family_id
    FROM listings l LEFT JOIN brands_live bl USING (brand_key)
    WHERE {where}
),
avail AS (
    SELECT brand_key,
           avg(in_stock::int) * 100                                              AS availability_pct,
           count(*)                                                              AS sku_listings,
           count(DISTINCT product_id)                                            AS skus_all,
           count(DISTINCT CASE WHEN NOT in_stock THEN product_id END)            AS skus_oos,
           count(DISTINCT CASE WHEN NOT in_stock THEN family_id END)             AS families_with_oos,
           count(DISTINCT family_id)                                             AS families
    FROM allsku GROUP BY 1
)
"""


def cte(where: str, params: list) -> tuple[str, list]:
    """Render SHELF_CTE and bind its parameters.

    The template interpolates `{where}` twice — once for the shelf (cards only) and once for allsku
    (variants included, where stock lives) — so the filter's parameters must be supplied twice, in
    that order, ahead of any the caller appends.
    """
    return SHELF_CTE.format(where=where), list(params) + list(params)


def _scope(conn, where: str, params: list) -> dict:
    """Totals for the current filter scope — the denominators everything else divides by."""
    sql, p = cte(where, params)
    row = conn.execute(sql + """
        SELECT count(*) AS cards, count(DISTINCT pincode) AS pincodes,
               count(DISTINCT brand_key) AS brands, count(DISTINCT subcategory) AS subcategories,
               count(DISTINCT product_id) AS products
        FROM j""", p).df().iloc[0]
    return {k: int(row[k]) for k in row.index}


def scope_totals(conn, where: str, params: list) -> dict:
    return _scope(conn, where, params)


def brand_table(conn, where: str, params: list) -> pd.DataFrame:
    """One row per brand with every headline metric. The backbone of the whole dashboard."""
    sql, p = cte(where, params)
    df = conn.execute(sql + """
        SELECT j.brand, j.brand_key, j.classification, j.brand_confirmed,
               count(DISTINCT j.product_id)                      AS products,
               count(*)                                          AS cards,
               count(DISTINCT j.pincode)                         AS pincodes,
               count(DISTINCT j.subcategory)                     AS subcategories,
               any_value(a.availability_pct)                     AS availability_pct,
               any_value(a.skus_all)                             AS skus_all,
               any_value(a.skus_oos)                             AS skus_oos,
               any_value(a.families_with_oos)                    AS families_with_oos,
               median(j.position)                                AS median_rank,
               min(j.position)                                   AS best_rank,
               median(j.rank_pct)                                AS median_rank_pct,
               median(j.price)                                   AS median_price,
               median(j.price_ratio) * 100                       AS price_index,
               avg(j.discount_pct)                               AS avg_discount_pct,
               max(j.rating_count)                               AS top_rating_count,
               avg(j.rating_value)                               AS avg_rating
        FROM j LEFT JOIN avail a USING (brand_key) GROUP BY 1, 2, 3, 4""", p).df()
    if df.empty:
        return df
    sc = _scope(conn, where, params)
    df["share_of_shelf_pct"] = df["cards"] / max(sc["cards"], 1) * 100
    df["distribution_pct"] = df["pincodes"] / max(sc["pincodes"], 1) * 100
    # Visibility: how high up the shelf, 100 = top of every list.
    df["visibility"] = (1 - df["median_rank_pct"]) * 100
    return df.sort_values("share_of_shelf_pct", ascending=False).reset_index(drop=True)


def sku_depth(conn, where: str, params: list) -> pd.DataFrame:
    """Variant counts — the one place variants are counted on purpose."""
    return conn.execute("""
        SELECT coalesce(bl.brand_name, l.brand_raw) AS brand, l.brand_key,
               count(DISTINCT l.product_id)                                    AS skus_all,
               count(DISTINCT CASE WHEN NOT l.is_variant THEN l.product_id END) AS products
        FROM listings l LEFT JOIN brands_live bl USING (brand_key)
        WHERE {} GROUP BY 1, 2""".format(where), params).df()


def by_geo(conn, where: str, params: list, brand_keys: list[str] | None = None) -> pd.DataFrame:
    """Per-pincode presence/availability, optionally restricted to a brand set."""
    extra = ""
    sql, p = cte(where, params)
    if brand_keys:
        extra = " AND brand_key IN ({})".format(",".join("?" * len(brand_keys)))
        # `extra` appears twice below (geo_avail, then the outer WHERE), so bind its keys twice.
        p = p + list(brand_keys) + list(brand_keys)
    return conn.execute(sql + f"""
        , geo_avail AS (
            SELECT pincode, avg(in_stock::int) * 100 AS availability_pct
            FROM allsku WHERE TRUE {extra} GROUP BY 1)
        SELECT j.city, j.pincode, j.area, count(*) AS cards, count(DISTINCT j.brand_key) AS brands,
               count(DISTINCT j.product_id) AS products, any_value(g.availability_pct) AS availability_pct
        FROM j LEFT JOIN geo_avail g USING (pincode)
        WHERE TRUE {extra} GROUP BY 1, 2, 3 ORDER BY j.city, j.pincode""", p).df()


def shelf_share_by(conn, where: str, params: list, dim: str) -> pd.DataFrame:
    """Share of shelf split by classification across any dimension (city, subcategory, ...)."""
    sql, p = cte(where, params)
    return conn.execute(sql + f"""
        SELECT {dim} AS dim, classification, count(*) AS cards,
               count(*) * 100.0 / sum(count(*)) OVER (PARTITION BY {dim}) AS share_pct
        FROM j GROUP BY 1, 2""", p).df()


def focus_products(conn, where: str, params: list, brand_keys: list[str]) -> pd.DataFrame:
    """Product-level rows for a brand set, aggregated across pincodes."""
    if not brand_keys:
        return pd.DataFrame()
    ph = ",".join("?" * len(brand_keys))
    sql, p = cte(where, params)
    return conn.execute(sql + f"""
        , fam_avail AS (
            SELECT family_id, avg(in_stock::int) * 100 AS availability_pct,
                   count(DISTINCT product_id) AS pack_sizes,
                   count(DISTINCT CASE WHEN NOT in_stock THEN product_id END) AS pack_sizes_oos
            FROM allsku GROUP BY 1)
        SELECT j.brand, j.brand_key, j.product_id, any_value(j.product_name) AS product_name,
               any_value(j.unit) AS unit, any_value(j.subcategory) AS subcategory, any_value(j.ptype) AS ptype,
               count(DISTINCT j.pincode) AS pincodes,
               any_value(fa.availability_pct) AS availability_pct,
               any_value(fa.pack_sizes) AS pack_sizes, any_value(fa.pack_sizes_oos) AS pack_sizes_oos,
               median(j.price) AS price, median(j.mrp) AS mrp, avg(j.discount_pct) AS discount_pct,
               median(j.position) AS median_rank, min(j.position) AS best_rank,
               median(j.price_ratio) * 100 AS price_index, max(j.rating_value) AS rating,
               max(j.rating_count) AS rating_count, any_value(j.product_url) AS product_url
        FROM j LEFT JOIN fam_avail fa ON fa.family_id = j.product_id
        WHERE j.brand_key IN ({ph}) GROUP BY 1, 2, 3""", p + list(brand_keys)).df()


# --------------------------------------------------------------------------- the attention engine
def attention(conn, where: str, params: list, *, focus_key: str, peer_keys: list[str],
              prev_where: str | None = None, prev_params: list | None = None) -> pd.DataFrame:
    """Ranked, actionable findings for the focus brand. This is the dashboard's "so what".

    Each item carries a `severity` (roughly: how many pincode-listings it touches) so the list sorts
    by consequence rather than by category.
    """
    items: list[dict] = []
    sc = _scope(conn, where, params)
    n_pins = max(sc["pincodes"], 1)
    keys = [focus_key] + peer_keys
    ph = ",".join("?" * len(keys))

    base_sql, base_p = cte(where, params)
    grid = conn.execute(base_sql + f"""
        SELECT brand_key, brand, pincode, city, area, product_id, product_name, unit, subcategory,
               in_stock, position, price, price_ratio, rating_count
        FROM j WHERE brand_key IN ({ph})""", base_p + keys).df()
    focus = grid[grid["brand_key"] == focus_key]
    peers = grid[grid["brand_key"] != focus_key]

    # 1. Out of stock. Read from allsku, never from the shelf cards: a card always renders an
    #    in-stock variant, so card-level stock is a constant and would flag nothing.
    stock = conn.execute(base_sql + f"""
        SELECT brand_key, pincode, city, area, family_id, product_id, product_name, unit,
               in_stock, is_variant
        FROM allsku WHERE brand_key IN ({ph})""", base_p + keys).df()
    fstock = stock[stock["brand_key"] == focus_key]
    if not fstock.empty:
        for fam, g in fstock.groupby("family_id"):
            oos = g[~g["in_stock"]]
            if oos.empty:
                continue
            named = g[~g["is_variant"]]["product_name"]
            name = named.iloc[0] if len(named) else g["product_name"].iloc[0]
            pins_oos = sorted(oos["pincode"].unique())
            # Fully out of stock where every pack size is dark, vs partially (some sizes gone).
            full = [p for p in pins_oos if not g[(g["pincode"] == p) & (g["in_stock"])].shape[0]]
            sizes = sorted(set(oos["unit"].dropna()))
            if full:
                items.append({
                    "severity": len(full) * 6, "kind": "Out of stock", "what": name,
                    "detail": f"every pack size dark at {len(full)} pincode(s) — {', '.join(full[:5])}",
                    "action": "Zero availability on shelf space already won. Chase replenishment first.",
                })
            partial = [p for p in pins_oos if p not in full]
            if partial:
                items.append({
                    "severity": len(partial) * 2, "kind": "Pack size OOS", "what": name,
                    "detail": f"{', '.join(sizes[:3])} out of stock at {len(partial)} pincode(s); "
                              "the listing still looks available because Blinkit defaults the card to an in-stock size",
                    "action": "Partial stock-out — invisible to a shopper until they open the size picker.",
                })

    # 2. Whitespace — rivals are on a shelf we are not.
    focus_pins = set(focus["pincode"])
    peer_pins = peers.groupby("pincode")["brand"].nunique()
    gaps = [(pin, n) for pin, n in peer_pins.items() if pin not in focus_pins]
    if not focus_pins and gaps:
        # Absent everywhere: that is one strategic finding, not one row per dark store. Emitting
        # per-pincode here would bury every other kind of issue under near-identical rows.
        cities = sorted(peers[peers["pincode"].isin([g[0] for g in gaps])]["city"].dropna().unique())
        rivals = sorted(peers["brand"].unique())
        items.append({
            "severity": 100, "kind": "Not listed", "what": f"Absent from all {len(gaps)} pincodes crawled",
            "detail": f"{len(rivals)} K-beauty rival(s) are on the shelf across {len(cities)} cities "
                      f"({', '.join(cities[:6])}) — {', '.join(rivals[:6])}",
            "action": "No Blinkit distribution at all. This is a listing conversation, not a merchandising one.",
        })
    else:
        for pin, n_peers in gaps:
            row = peers[peers["pincode"] == pin].iloc[0]
            names = ", ".join(sorted(peers[peers["pincode"] == pin]["brand"].unique()))
            items.append({
                "severity": int(n_peers) * 4, "kind": "Not listed", "what": f"{row['city']} · {pin} ({row['area']})",
                "detail": f"{n_peers} K-beauty rival(s) listed here, we are absent — {names}",
                "action": "Distribution gap: pitch listing at this dark store.",
            })

    # 3. Price outliers — priced away from the shelf we compete on.
    if not focus.empty:
        pr = focus.groupby(["product_id", "product_name", "subcategory"], as_index=False)["price_ratio"].median()
        for _, r in pr.iterrows():
            if pd.isna(r["price_ratio"]):
                continue
            gap = (r["price_ratio"] - 1) * 100
            if abs(gap) >= 25:
                items.append({
                    "severity": int(min(abs(gap), 100) / 4), "kind": "Price gap", "what": r["product_name"],
                    "detail": f"{gap:+.0f}% vs the {r['subcategory']} shelf median",
                    "action": "Review price ladder / pack-size mix." if gap > 0 else "Check for margin leakage or a stale promo.",
                })

    # 4. Buried on the shelf — listed but invisible.
    if not focus.empty:
        deep = focus.groupby(["product_id", "product_name"], as_index=False)["position"].median()
        for _, r in deep.iterrows():
            if r["position"] >= 40:
                items.append({
                    "severity": 6, "kind": "Low visibility", "what": r["product_name"],
                    "detail": f"median shelf position {r['position']:.0f} — below the fold on most listings",
                    "action": "Visibility play: ads, ratings drive, or improve conversion to lift the bestseller sort.",
                })

    # 5. Week-over-week movement (only when a previous run exists).
    if prev_where is not None:
        prev_sql, prev_p = cte(prev_where, prev_params or [])
        prev = conn.execute(prev_sql + f"""
            SELECT brand_key, brand, pincode, product_id, product_name, in_stock, position, price
            FROM j WHERE brand_key IN ({ph})""", prev_p + keys).df()
        if not prev.empty:
            pf, pp = prev[prev["brand_key"] == focus_key], prev[prev["brand_key"] != focus_key]
            # focus SKUs that vanished from a pincode
            now = set(map(tuple, focus[["pincode", "product_id"]].values))
            for (pin, pid), g in pf.groupby(["pincode", "product_id"]):
                if (pin, pid) not in now:
                    items.append({
                        "severity": 8, "kind": "Delisted", "what": g.iloc[0]["product_name"],
                        "detail": f"was listed at {pin} last crawl, gone now",
                        "action": "Confirm whether this is a stock-out roll-off or a true delisting.",
                    })
            # rank slips
            a = focus.groupby("product_id", as_index=False).agg(pos=("position", "median"), name=("product_name", "first"))
            b = pf.groupby("product_id", as_index=False).agg(pos_prev=("position", "median"))
            m = a.merge(b, on="product_id")
            for _, r in m.iterrows():
                slip = r["pos"] - r["pos_prev"]
                if slip >= 5:
                    items.append({
                        "severity": int(min(slip, 40) / 2), "kind": "Rank slip", "what": r["name"],
                        "detail": f"median shelf position {r['pos_prev']:.0f} → {r['pos']:.0f}",
                        "action": "Losing ground on the bestseller sort — check price, stock and ratings.",
                    })
            # new rival products
            new_rivals = set(peers["product_id"]) - set(pp["product_id"])
            if new_rivals:
                nr = peers[peers["product_id"].isin(new_rivals)].drop_duplicates("product_id")
                for _, r in nr.iterrows():
                    items.append({
                        "severity": 5, "kind": "Rival launch", "what": f"{r['brand']} — {r['product_name']}",
                        "detail": f"new on Blinkit this crawl, {r['subcategory']}",
                        "action": "Competitive watch.",
                    })

    if not items:
        return pd.DataFrame(columns=["severity", "kind", "what", "detail", "action"])
    return (pd.DataFrame(items).sort_values("severity", ascending=False).reset_index(drop=True))
