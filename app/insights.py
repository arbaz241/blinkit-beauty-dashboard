"""The insight engine behind the story page.

Every number and every sentence on the main page is computed here from the crawl, never typed in by
hand, so the narrative stays true when next week's data lands. Each function answers one question
and returns plain dicts / DataFrames; the page only lays them out.

Three definitions carry most of the weight, and each is forced by how Blinkit's data is shaped:

* **Card (view C) vs SKU (view L).** A listing page shows one *card* per product; its pack sizes are
  separate product_ids behind a size picker. 62% of rows are pack sizes, so counting product_ids
  flatters brands selling one cream in five sizes. Shelf metrics — share, rank, visibility — count
  cards only, because that is what a shopper actually sees.
* **Availability is the exception: it counts every SKU.** A card always renders an in-stock size as
  its default, so card-level stock is a constant ~100% and says nothing. The stock-outs hide in the
  pack sizes (15.9% of them were dark in the first crawl, vs 0% of cards).
* **Rank is a demand proxy, not sales.** Blinkit publishes no volumes. Its "bestsellers" sort does
  track demand — position correlates -0.39 to -0.63 with review count in every sub-category — so a
  product's shelf position is a fair stand-in, and is always labelled as one.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

BASE = """
WITH L AS (
    SELECT l.*, coalesce(bl.brand_name, l.brand_raw) AS brand,
           coalesce(bl.classification, 'Unclassified') AS cls
    FROM listings l LEFT JOIN brands_live bl USING (brand_key)
    WHERE l.run_id = $run
),
C AS (SELECT * FROM L WHERE NOT is_variant)
"""
# Brands that market products as Korean without being classified Korean. Detected from the product
# names themselves ("Korean Sheet Mask", "Korean Glass Skin"), so this describes how a product is
# sold — it makes no claim about where the brand is from.
K_STYLE = "(lower(product_name) LIKE '%korean%' OR lower(product_name) LIKE '%k-beauty%' OR lower(brand) LIKE '%seoul%')"


class Ctx:
    def __init__(self, conn, run_id: str, focus_key: str, peer_keys: list[str], focus_name: str = ""):
        self.conn, self.run = conn, run_id
        self.fk, self.pk = focus_key, [k for k in peer_keys if k != focus_key]
        self.fname = focus_name or focus_key.title()

    def q(self, sql: str, **extra) -> pd.DataFrame:
        # BASE already opens a WITH clause; a query that brings its own CTEs must continue that
        # chain with a comma rather than start a second WITH (which DuckDB rejects).
        body = sql.lstrip()
        if body[:4].upper() == "WITH":
            body = "," + body[4:]
        return self.conn.execute(BASE + body, {"run": self.run, **extra}).df()

    def one(self, sql: str, **extra):
        return self.q(sql, **extra).iloc[0]


def _qty(unit) -> float | None:
    m = re.search(r"(?:(\d+)\s*[xX]\s*)?(\d+(?:\.\d+)?)\s*(ml|g|gm|kg|l)\b", str(unit or ""), re.I)
    if not m:
        return None
    q = float(m.group(2)) * (float(m.group(1)) if m.group(1) else 1.0)
    return q * 1000 if m.group(3).lower() in ("kg", "l") else q


# ============================================================================ 0. trust
def trust(ctx: Ctx) -> dict:
    """Can these numbers be believed? Every check here must pass for the story to stand."""
    s = ctx.one("""
        SELECT count(*) AS n_rows, count(*) FILTER (WHERE NOT is_variant) AS cards,
               count(DISTINCT pincode) AS pincodes, count(DISTINCT city) AS cities,
               count(DISTINCT subcategory) AS subcats, count(DISTINCT product_id) AS skus,
               count(DISTINCT brand_key) AS brands, count(DISTINCT location_merchant_id) AS stores,
               count(*) FILTER (WHERE price IS NULL OR price <= 0) AS bad_price,
               count(*) FILTER (WHERE price > mrp) AS price_over_mrp,
               count(*) FILTER (WHERE brand_raw IS NULL OR brand_raw = '') AS no_brand,
               count(*) FILTER (WHERE in_stock AND inventory = 0) AS stock_contradictions,
               count(*) FILTER (WHERE is_variant AND parent_product_id IS NULL) AS orphan_variants,
               avg((rating_value IS NULL)::int) * 100 AS unrated_pct
        FROM L""")
    grid = ctx.q("SELECT pincode, count(DISTINCT subcategory) AS s FROM L GROUP BY 1")
    dups = ctx.one("""SELECT count(*) AS n FROM (SELECT pincode, subcategory, product_id FROM L
                      GROUP BY 1, 2, 3 HAVING count(*) > 1)""")["n"]
    uncls = ctx.one("""SELECT sum((cls = 'Unclassified')::int) * 100.0 / count(*) AS pct,
                              count(DISTINCT brand_key) FILTER (WHERE cls = 'Unclassified') AS n FROM C""")
    combos = ctx.one("""SELECT count(*) AS n FROM (SELECT product_id FROM C GROUP BY 1
                        HAVING count(DISTINCT subcategory) > 1)""")["n"]
    full_grid = bool((grid["s"] == grid["s"].max()).all()) if len(grid) else False
    checks = [
        ("Every pincode × sub-category crawled", full_grid, f"{int(s.pincodes)} × {int(s.subcats)}"),
        ("Every pincode is a distinct dark store", int(s.stores) == int(s.pincodes), f"{int(s.stores)} stores"),
        ("No duplicate listings", int(dups) == 0, f"{int(dups)} duplicates"),
        ("Prices valid (> 0, never above MRP)", int(s.bad_price) + int(s.price_over_mrp) == 0,
         f"{int(s.bad_price) + int(s.price_over_mrp)} bad"),
        ("Every listing has a brand", int(s.no_brand) == 0, f"{int(s.no_brand)} missing"),
        ("Stock flag agrees with inventory", int(s.stock_contradictions) == 0, f"{int(s.stock_contradictions)} conflicts"),
        ("Every pack size has its parent product", int(s.orphan_variants) == 0, f"{int(s.orphan_variants)} orphans"),
    ]
    caveats = [
        f"**{uncls.pct:.0f}% of listings are from {int(uncls.n)} brands not yet classified.** They are overwhelmingly "
        "Indian D2C and nail brands, so the Korean share is robust, but the Local/Global split is a floor.",
        f"**{int(combos)} combo packs are listed in several sub-categories at once.** They genuinely occupy each "
        "shelf, so they count toward share of shelf everywhere they appear — see *Competitor tactics*.",
        f"**{s.unrated_pct:.0f}% of listings show no rating.** Blinkit hides ratings under ~20 reviews, "
        "so these are low-demand products, not missing data.",
    ]
    return {"s": s, "checks": checks, "caveats": caveats, "all_ok": all(c[1] for c in checks)}


# ============================================================================ 1. market
def market(ctx: Ctx) -> dict:
    tot = ctx.one("SELECT count(*) AS cards, count(DISTINCT pincode) AS pins FROM C")
    per_store = tot.cards / max(tot.pins, 1)
    cats = ctx.q("""
        SELECT top_category, subcategory, count(*) AS cards, count(DISTINCT brand_key) AS brands,
               count(*) * 100.0 / sum(count(*)) OVER () AS share, median(price) AS med_price
        FROM C GROUP BY 1, 2 ORDER BY cards DESC""")
    conc = ctx.q("""
        WITH s AS (SELECT subcategory, brand, count(*) AS n FROM C GROUP BY 1, 2),
             t AS (SELECT *, n * 100.0 / sum(n) OVER (PARTITION BY subcategory) AS sh,
                          row_number() OVER (PARTITION BY subcategory ORDER BY n DESC) AS rk FROM s)
        SELECT subcategory, count(*) AS brands, sum(sh * sh) AS hhi,
               sum(sh) FILTER (WHERE rk <= 3) AS top3, any_value(brand) FILTER (WHERE rk = 1) AS leader,
               max(sh) AS leader_share
        FROM t GROUP BY 1 ORDER BY hhi""")
    cls = ctx.q("""
        SELECT cls, count(DISTINCT brand_key) AS brands, count(*) AS cards,
               count(*) * 100.0 / sum(count(*)) OVER () AS share,
               count(*) FILTER (WHERE position <= 10) * 100.0
                 / sum(count(*) FILTER (WHERE position <= 10)) OVER () AS top10_share,
               median(price) AS med_price
        FROM C GROUP BY 1 ORDER BY cards DESC""")
    leaders = ctx.q("""
        WITH s AS (SELECT subcategory, brand, count(*) AS n,
                          row_number() OVER (PARTITION BY subcategory ORDER BY count(*) DESC) AS rk
                   FROM C GROUP BY 1, 2)
        SELECT brand, count(*) AS categories_led FROM s WHERE rk = 1 GROUP BY 1 ORDER BY 2 DESC""")
    return {"cards": int(tot.cards), "pins": int(tot.pins), "per_store": per_store, "cats": cats,
            "conc": conc, "cls": cls, "leaders": leaders,
            "most_open": conc.iloc[0], "most_closed": conc.iloc[-1]}


# ============================================================================ 2. K-beauty segment
def kbeauty(ctx: Ctx) -> dict:
    seg = ctx.q("""
        SELECT subcategory, count(*) AS cards,
               count(*) FILTER (WHERE cls = 'Korean') * 100.0 / count(*) AS k_share,
               count(*) FILTER (WHERE brand_key = $fk) * 100.0 / count(*) AS focus_share,
               median(price) FILTER (WHERE cls = 'Korean') AS k_price, median(price) AS shelf_price
        FROM C GROUP BY 1 ORDER BY k_share DESC""", fk=ctx.fk)
    bands = ctx.q("""
        SELECT cls,
          avg((price < 200)::int) * 100 AS "Under ₹200", avg((price >= 200 AND price < 400)::int) * 100 AS "₹200–400",
          avg((price >= 400 AND price < 800)::int) * 100 AS "₹400–800",
          avg((price >= 800 AND price < 1500)::int) * 100 AS "₹800–1.5k", avg((price >= 1500)::int) * 100 AS "₹1.5k+",
          median(price) AS med
        FROM C GROUP BY 1""")
    promo = ctx.q("""
        SELECT cls, avg(discount_pct) AS avg_disc, avg((discount_pct > 0)::int) * 100 AS on_promo
        FROM C GROUP BY 1 ORDER BY avg_disc DESC""")
    avail = ctx.q("SELECT cls, avg(in_stock::int) * 100 AS avail FROM L GROUP BY 1 ORDER BY avail")
    kstyle = ctx.q(f"""
        SELECT brand, count(*) AS cards, count(DISTINCT product_id) AS products, median(price) AS med_price,
               any_value(product_name) AS example
        FROM C WHERE cls <> 'Korean' AND {K_STYLE} GROUP BY 1 ORDER BY cards DESC""")
    k = ctx.one("""SELECT count(DISTINCT brand_key) FILTER (WHERE cls = 'Korean') AS brands,
                          count(*) FILTER (WHERE cls = 'Korean') * 100.0 / count(*) AS share,
                          median(price) FILTER (WHERE cls = 'Korean') AS k_price,
                          median(price) FILTER (WHERE cls = 'Local') AS local_price FROM C""")
    absent = seg[seg["k_share"] == 0]["subcategory"].tolist()
    return {"seg": seg, "bands": bands, "promo": promo, "avail": avail, "kstyle": kstyle, "k": k,
            "absent": absent, "kstyle_cards": int(kstyle["cards"].sum()) if len(kstyle) else 0}


# ============================================================================ 3. focus vs peers
def peers(ctx: Ctx) -> dict:
    t = ctx.q("""
        WITH a AS (SELECT brand_key, avg(in_stock::int) * 100 AS avail,
                          count(DISTINCT product_id) AS skus,
                          count(DISTINCT coalesce(parent_product_id, product_id)) AS families
                   FROM L WHERE cls = 'Korean' GROUP BY 1)
        SELECT C.brand, C.brand_key, count(DISTINCT C.product_id) AS products, any_value(a.skus) AS skus,
               any_value(a.families) AS families, count(DISTINCT subcategory) AS categories,
               count(DISTINCT pincode) AS pincodes, count(*) AS cards, any_value(a.avail) AS availability,
               median(position) AS median_rank, count(*) FILTER (WHERE position <= 10) AS top10_slots,
               median(price) AS median_price, avg(discount_pct) AS avg_discount,
               max(rating_count) AS top_reviews, median(rating_count) AS median_reviews,
               avg(rating_value) AS avg_rating
        FROM C JOIN a USING (brand_key) WHERE C.cls = 'Korean'
        GROUP BY 1, 2 ORDER BY cards DESC""")
    tot = ctx.one("SELECT count(*) AS cards, count(DISTINCT pincode) AS pins FROM C")
    t["share"] = t["cards"] / tot.cards * 100
    t["top10_rate"] = t["top10_slots"] / t["cards"] * 100
    catmap = ctx.q("""SELECT brand, subcategory, count(DISTINCT product_id) AS n FROM C
                      WHERE cls = 'Korean' GROUP BY 1, 2""")
    f = t[t["brand_key"] == ctx.fk]
    return {"t": t, "catmap": catmap, "focus": f.iloc[0] if len(f) else None, "pins": int(tot.pins)}


# ============================================================================ 4. visibility
def visibility(ctx: Ctx) -> dict:
    corr = ctx.q("""SELECT subcategory, corr(position, ln(rating_count + 1)) AS r
                    FROM C WHERE rating_count IS NOT NULL GROUP BY 1 ORDER BY r""")
    reviews = ctx.q("""
        SELECT brand, brand_key, count(DISTINCT product_id) AS products,
               median(rating_count) AS median_reviews, max(rating_count) AS top_reviews,
               median(position) AS median_rank,
               count(*) FILTER (WHERE position <= 10) * 100.0 / count(*) AS top10_rate
        FROM C WHERE cls = 'Korean' AND rating_count IS NOT NULL
        GROUP BY 1, 2 HAVING count(DISTINCT product_id) >= 2 ORDER BY median_reviews DESC""")
    focus_products = ctx.q("""
        SELECT product_name, subcategory, median(position) AS median_rank, min(position) AS best_rank,
               count(DISTINCT pincode) AS pincodes, max(rating_count) AS reviews,
               avg(rating_value) AS rating, median(price) AS price
        FROM C WHERE brand_key = $fk GROUP BY 1, 2 ORDER BY median_rank""", fk=ctx.fk)
    return {"corr": corr, "reviews": reviews, "fp": focus_products,
            "r_min": float(corr["r"].max()) if len(corr) else np.nan,
            "r_max": float(corr["r"].min()) if len(corr) else np.nan}


# ============================================================================ 5. availability
def availability(ctx: Ctx) -> dict:
    oos = ctx.q("""
        SELECT product_name, unit, count(DISTINCT pincode) AS listed_in,
               count(DISTINCT pincode) FILTER (WHERE NOT in_stock) AS dark_in,
               avg(in_stock::int) * 100 AS avail, median(price) AS price
        FROM L WHERE brand_key = $fk GROUP BY 1, 2 ORDER BY dark_in DESC, avail""", fk=ctx.fk)
    city = ctx.q("""
        SELECT city, avg(in_stock::int) * 100 AS market,
               avg(in_stock::int) FILTER (WHERE cls = 'Korean') * 100 AS kbeauty,
               avg(in_stock::int) FILTER (WHERE brand_key = $fk) * 100 AS focus
        FROM L GROUP BY 1 ORDER BY market""", fk=ctx.fk)
    hidden = ctx.one("""
        WITH f AS (SELECT coalesce(parent_product_id, product_id) AS fam, pincode,
                          bool_or(NOT is_variant AND in_stock) AS card_ok, bool_or(NOT in_stock) AS any_oos
                   FROM L GROUP BY 1, 2)
        SELECT avg((card_ok AND any_oos)::int) * 100 AS pct FROM f""")["pct"]
    return {"oos": oos, "city": city, "hidden_pct": float(hidden)}


# ============================================================================ 6. pricing
def pricing(ctx: Ctx) -> dict:
    d = ctx.q("SELECT DISTINCT product_id, brand, brand_key, cls, subcategory, unit, price, discount_pct FROM C")
    d["qty"] = d["unit"].map(_qty)
    d = d[d["qty"] > 0].copy()
    d["p100"] = d["price"] / d["qty"] * 100
    shelf = d.groupby("subcategory")["p100"].median().rename("shelf")
    keys = [ctx.fk] + ctx.pk
    names = d[d["brand_key"].isin(keys)].groupby("brand_key")["brand"].first()
    unit_idx = (d[d["brand_key"].isin(keys)].groupby(["subcategory", "brand_key"])["p100"].median()
                .unstack().div(shelf, axis=0) * 100)
    unit_idx = unit_idx.rename(columns=names)
    promo = ctx.q("""
        SELECT brand, brand_key, avg(discount_pct) AS avg_disc, avg((discount_pct > 0)::int) * 100 AS on_promo,
               median(price) AS med_price
        FROM C WHERE cls = 'Korean' GROUP BY 1, 2 ORDER BY avg_disc DESC""")
    shelf_promo = ctx.one("SELECT avg((discount_pct > 0)::int) * 100 AS on_promo, avg(discount_pct) AS d FROM C")
    fu = unit_idx[names.get(ctx.fk)] if ctx.fk in names.index else pd.Series(dtype=float)
    return {"unit_idx": unit_idx, "promo": promo, "shelf_promo": shelf_promo,
            "focus_unit_median": float(fu.median()) if fu.notna().any() else np.nan,
            "focus_unit": fu.dropna().sort_values(ascending=False)}


# ============================================================================ 7. geography
def geography(ctx: Ctx) -> dict:
    stores = ctx.q("""
        SELECT city, pincode, area, count(*) AS cards,
               count(DISTINCT product_id) FILTER (WHERE brand_key = $fk) AS focus_products,
               count(*) FILTER (WHERE cls = 'Korean') * 100.0 / count(*) AS k_share
        FROM C GROUP BY 1, 2, 3 ORDER BY focus_products, cards""", fk=ctx.fk)
    city = (stores.groupby("city", as_index=False)
            .agg(cards=("cards", "mean"), focus_products=("focus_products", "mean"), k_share=("k_share", "mean"))
            .sort_values("focus_products"))
    return {"stores": stores, "city": city}


# ============================================================================ 8. tactics
def tactics(ctx: Ctx) -> dict:
    cross = ctx.q("""
        WITH multi AS (SELECT product_id FROM C GROUP BY 1 HAVING count(DISTINCT subcategory) > 1)
        SELECT brand, brand_key, cls, count(DISTINCT product_id) FILTER (WHERE product_id IN (SELECT * FROM multi)) AS combos,
               count(*) FILTER (WHERE product_id IN (SELECT * FROM multi)) AS combo_cards, count(*) AS cards,
               count(*) FILTER (WHERE product_id IN (SELECT * FROM multi)) * 100.0 / count(*) AS pct_from_combos
        FROM C GROUP BY 1, 2, 3 HAVING combos > 0 ORDER BY combo_cards DESC LIMIT 12""")
    packs = ctx.q("""
        SELECT brand, brand_key, count(DISTINCT product_id) AS skus,
               count(DISTINCT coalesce(parent_product_id, product_id)) AS families
        FROM L WHERE cls = 'Korean' GROUP BY 1, 2 HAVING count(DISTINCT product_id) >= 3""")
    packs["sizes_per_product"] = packs["skus"] / packs["families"]
    focus_combos = int(cross[cross["brand_key"] == ctx.fk]["combos"].sum())
    return {"cross": cross, "packs": packs.sort_values("sizes_per_product", ascending=False),
            "focus_combos": focus_combos}


# ============================================================================ 9. opportunities
def opportunities(ctx: Ctx, kb: dict, av: dict, pr: dict, vi: dict, tc: dict, ge: dict) -> list[dict]:
    """Ranked, computed recommendations. Each cites the evidence it rests on."""
    out: list[dict] = []
    seg = kb["seg"]
    ws = seg[(seg["k_share"] >= 5) & (seg["focus_share"] == 0)]
    for _, r in ws.iterrows():
        out.append({"area": "Range", "impact": r["k_share"] * r["cards"] / 1000,
                    "move": f"Enter **{r['subcategory']}**",
                    "why": f"K-beauty already holds {r['k_share']:.1f}% of this shelf and {ctx.fname} has nothing on it."})
    dark = av["oos"][av["oos"]["dark_in"] >= 3]
    for _, r in dark.head(3).iterrows():
        out.append({"area": "Availability", "impact": r["dark_in"] * 2.0,
                    "move": f"Restock **{r['product_name']}**",
                    "why": f"Out of stock in {int(r['dark_in'])} of {int(r['listed_in'])} stores listing it "
                           f"({r['avail']:.0f}% available)."})
    rv = vi["reviews"]
    f = rv[rv["brand_key"] == ctx.fk]
    if len(f) and len(rv) > 1:
        lead = rv.iloc[0]
        if lead["brand_key"] != ctx.fk and f.iloc[0]["median_reviews"] > 0:
            gap = lead["median_reviews"] / f.iloc[0]["median_reviews"]
            out.append({"area": "Visibility", "impact": min(gap, 20) * 2,
                        "move": "Run a ratings & reviews drive",
                        "why": f"Shelf rank tracks review volume in every sub-category. {lead['brand']} products carry "
                               f"{gap:.0f}× the median reviews of ours — the single biggest reason we rank lower."})
    sp = pr["shelf_promo"]
    fp = pr["promo"][pr["promo"]["brand_key"] == ctx.fk]
    if len(fp) and fp.iloc[0]["on_promo"] < sp["on_promo"] - 25:
        out.append({"area": "Pricing", "impact": 8,
                    "move": "Join Blinkit's promotion calendar",
                    "why": f"Only {fp.iloc[0]['on_promo']:.0f}% of our listings carry a discount, against "
                           f"{sp['on_promo']:.0f}% of the whole shelf — we are full-price in a sea of offers."})
    if tc["focus_combos"] == 0 and len(tc["cross"]):
        top = tc["cross"].iloc[0]
        out.append({"area": "Tactics", "impact": 6,
                    "move": "Launch combo packs that cross-list",
                    "why": f"A combo appears on every shelf it touches. {top['brand']} gets {top['pct_from_combos']:.0f}% "
                           f"of its listings this way; we have none."})
    st = ge["stores"]
    if len(st) and st["focus_products"].max() > 0:
        lo, hi = st.iloc[0], st["focus_products"].max()
        if hi >= 2 * max(lo["focus_products"], 1):
            out.append({"area": "Distribution", "impact": 5,
                        "move": f"Fix the range gap in **{lo['city']}**",
                        "why": f"{lo['area']} ({lo['pincode']}) stocks {int(lo['focus_products'])} of our products; "
                               f"our best stores carry {int(hi)}."})
    return sorted(out, key=lambda x: x["impact"], reverse=True)
