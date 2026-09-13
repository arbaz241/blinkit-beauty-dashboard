"""The insight engine behind the brand review page.

Every number and every sentence on the page is computed here from the crawl, never typed in by hand,
so the review stays true when next week's data lands. It is written for the person who runs the focus
brand on Blinkit: which SKU is losing, to whom, why, and what to change.

The definitions the page leans on, each forced by how Blinkit's data is shaped:

* **Product = listing card.** A card is what a shopper sees; other pack sizes sit behind its size
  picker as variant rows whose parent is the card. Rank, price and reviews are read off the card.
* **Sellable = some size of the product can be bought in that store.** "Coming soon" and "out of
  stock" both mean a shopper cannot buy it there today.
* **Rank is a demand proxy, not sales.** Blinkit publishes no volumes. Its bestseller sort and the
  review count are the two demand signals it exposes, and both are labelled as proxies.
* **Peer group = Blinkit's own product type** ("Sheet Mask", "Tinted Lip Balm") — the like-for-like
  fight. A type with fewer than MIN_PEERS widely listed products falls back to the whole
  sub-category shelf, so a SKU is never "#1 of 3".
* **Benchmark = the 12 best-ranked products of the peer group**: what winning looks like on price,
  pack, discount and reviews.
* **Demand share = share of reviews, counted once per product family.** Shades and sizes of one
  product often repeat the family's review count; counting each would reward brands with many shades.
"""
from __future__ import annotations

import math
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

TOP = 12            # benchmark set: the best-ranked products of a peer group
MIN_PEERS = 8       # a product type needs this many widely listed products to be its own peer group
WIDE = 0.25         # "widely listed" = in at least this share of stores; one-store oddities don't set the bar
PRICE_FLAG = 1.5    # priced this many times the benchmark (ticket AND per g/ml) = overpriced
STOCK_FLAG = 70     # sellable in fewer than this % of listing stores = stock problem
LISTING_FLAG = 0.5  # listed in fewer than this share of stores = distribution problem
REVIEW_FLAG = 5     # benchmark median reviews this many times ours = review gap
RATING_FLAG = 4.0


class Ctx:
    def __init__(self, conn, run_id: str, focus_key: str, peer_keys: list[str], focus_name: str = ""):
        self.conn, self.run = conn, run_id
        self.fk, self.pk = focus_key, [k for k in peer_keys if k != focus_key]
        self.fname = focus_name or focus_key.title()
        self._products: pd.DataFrame | None = None
        self.pins = 0

    def q(self, sql: str, **extra) -> pd.DataFrame:
        # BASE already opens a WITH clause; a query that brings its own CTEs must continue that
        # chain with a comma rather than start a second WITH (which DuckDB rejects).
        body = sql.lstrip()
        if body[:4].upper() == "WITH":
            body = "," + body[4:]
        return self.conn.execute(BASE + body, {"run": self.run, **extra}).df()

    def one(self, sql: str, **extra):
        return self.q(sql, **extra).iloc[0]


_UNIT = re.compile(r"(?:(\d+)\s*[xX]\s*)?(\d+(?:\.\d+)?)\s*(ml|gm|g|kg|ltr|l|pcs|pc|pieces|piece)\b", re.I)


def _qty(unit) -> tuple[float, str | None]:
    """Pack size as (quantity, kind). g and ml are compared as one kind; pieces are their own."""
    m = _UNIT.search(str(unit or ""))
    if not m:
        return np.nan, None
    q = float(m.group(2)) * (float(m.group(1)) if m.group(1) else 1.0)
    u = m.group(3).lower()
    if u in ("kg", "ltr", "l"):
        return q * 1000, "g/ml"
    return (q, "g/ml") if u in ("ml", "gm", "g") else (q, "pc")


def short_name(product: str, brand: str) -> str:
    s = re.sub(rf"^\s*{re.escape(str(brand))}\s*", "", str(product), flags=re.I).strip()
    return s or str(product)


# ============================================================================ 0. trust
def trust(ctx: Ctx) -> dict:
    """Can these numbers be believed? Every check here must pass for the review to stand."""
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
        "**Blinkit publishes no sales.** Shelf rank (its bestseller sort) and review counts are the demand "
        "signals used throughout; both are proxies.",
        "**Stock depth is not observable.** Blinkit caps the inventory it exposes at a few units, so only "
        "sellable vs not is used — never units on hand.",
        f"**{uncls.pct:.0f}% of listings are from {int(uncls.n)} brands not yet classified** (mostly Indian D2C "
        "and nail brands). K-beauty totals are robust; review the queue on *Explore & data*.",
        f"**{s.unrated_pct:.0f}% of listings show no rating.** Blinkit hides ratings on low-review products, so "
        "these count as zero reviews, not missing data.",
    ]
    return {"s": s, "checks": checks, "caveats": caveats, "all_ok": all(c[1] for c in checks)}


# ============================================================================ 1. products
def products(ctx: Ctx) -> pd.DataFrame:
    """One row per product card across all stores, with its peer group and rank inside it."""
    if ctx._products is not None:
        return ctx._products
    # Stores are counted over every row, not just cards: a pack size that is its own card in 3 stores
    # may sit behind another size's card in 17 more, and it is on sale in all 20.
    # Every aggregate is deterministic (min, not any_value; ties broken by name). DuckDB aggregates in
    # parallel, so any_value/mode can pick a different row on each load and shift the numbers.
    p = ctx.q(f"""
        WITH sku AS (SELECT product_id, count(DISTINCT pincode) AS listed,
                            count(DISTINCT pincode) FILTER (WHERE product_state = 'available') AS sellable
                     FROM L GROUP BY 1),
             home AS (SELECT product_id, subcategory FROM (
                          SELECT product_id, subcategory,
                                 row_number() OVER (PARTITION BY product_id ORDER BY count(*) DESC, subcategory) AS rn
                          FROM C GROUP BY 1, 2) WHERE rn = 1)
        SELECT c.product_id, min(c.brand) AS brand, min(c.brand_key) AS brand_key,
               min(c.cls) AS cls, min(c.product_name) AS product, min(c.unit) AS unit,
               min(c.ptype) AS ptype, min(home.subcategory) AS subcategory,
               min(sku.listed) AS stores, min(sku.sellable) AS sellable,
               median(c.position) AS med_rank, avg((c.position <= {TOP})::int) * 100 AS top_slot_pct,
               median(c.price) AS price, avg(c.discount_pct) AS disc, max(c.rating_count) AS reviews,
               max(c.rating_value) AS rating, min(c.variant_group_id) AS family,
               min(c.product_url) AS url
        FROM C c LEFT JOIN sku ON sku.product_id = c.product_id
                 LEFT JOIN home ON home.product_id = c.product_id
        GROUP BY c.product_id
        ORDER BY c.product_id""")
    for col in ("stores", "sellable", "med_rank", "top_slot_pct", "price", "disc", "reviews", "rating"):
        p[col] = pd.to_numeric(p[col], errors="coerce").astype(float)
    ctx.pins = int(ctx.one("SELECT count(DISTINCT pincode) AS n FROM C")["n"])
    p["sellable"] = p["sellable"].fillna(0)
    p["sellable_pct"] = (p["sellable"] / p["stores"] * 100).clip(upper=100)
    p["disc"] = p["disc"].fillna(0)
    qk = p["unit"].map(_qty)
    p["qty"], p["kind"] = [x[0] for x in qk], [x[1] for x in qk]
    p["p_unit"] = p["price"] / p["qty"]
    p["wide"] = p["stores"] >= max(2, round(ctx.pins * WIDE))

    n_type = p[p["wide"]].groupby("ptype").size()
    p["group_is_type"] = p["ptype"].map(n_type).fillna(0) >= MIN_PEERS
    p["group"] = np.where(p["group_is_type"], p["ptype"], p["subcategory"])
    p["rank_in_group"], p["group_n"] = np.nan, np.nan
    for is_type, key in ((True, "ptype"), (False, "subcategory")):
        for k, g in p.groupby(key):
            ref = np.sort(g.loc[g["wide"], "med_rank"].to_numpy(float))
            idx = g.index[g["group_is_type"] == is_type]
            if not len(idx):
                continue
            r = np.searchsorted(ref, p.loc[idx, "med_rank"].to_numpy(float), side="left") + 1
            p.loc[idx, "rank_in_group"] = r
            p.loc[idx, "group_n"] = len(ref) + (~p.loc[idx, "wide"]).astype(int)
    p["win_cut"] = np.minimum(TOP, np.ceil(p["group_n"] / 4))
    p["winning"] = p["rank_in_group"] <= p["win_cut"]
    ctx._products = p
    return p


def peer_group(p: pd.DataFrame, row) -> pd.DataFrame:
    """Every widely listed product in the row's peer group (including the row itself if wide)."""
    m = (p["ptype"] == row["group"]) if row["group_is_type"] else (p["subcategory"] == row["group"])
    return p[m & p["wide"]]


# ============================================================================ 2. SKU scorecard
def sku_table(ctx: Ctx, p: pd.DataFrame) -> pd.DataFrame:
    """Every focus-brand SKU judged against the best-ranked products of its own type."""
    rows = []
    for _, r in p[p["brand_key"] == ctx.fk].iterrows():
        wide = peer_group(p, r)
        bench = wide.nsmallest(TOP, "med_rank")
        others = wide[wide["product_id"] != r["product_id"]].sort_values("med_rank")
        lead = others.iloc[0] if len(others) else None
        kr = others[(others["cls"] == "Korean") & (others["brand_key"] != ctx.fk)]
        krow = kr.iloc[0] if len(kr) else None
        same_kind = bench[bench["kind"] == r["kind"]]
        b_punit = same_kind["p_unit"].median() if r["kind"] and len(same_kind) >= 3 else np.nan
        # Per-gram price only compares like with like: a 5 g powder against 75 g tins is a different
        # format, not a 14× markup, so drop the unit comparison when pack sizes differ by more than 3×.
        b_qty = same_kind["qty"].median() if len(same_kind) else np.nan
        if pd.notna(b_qty) and pd.notna(r["qty"]) and not (1 / 3 <= r["qty"] / b_qty <= 3):
            b_punit = np.nan
        b_price = bench["price"].median()
        ticket = r["price"] / b_price if b_price else np.nan
        unit_idx = r["p_unit"] / b_punit if pd.notna(b_punit) and pd.notna(r["p_unit"]) else np.nan
        b_rev = float(bench["reviews"].fillna(0).median())
        rev = 0.0 if pd.isna(r["reviews"]) else float(r["reviews"])

        flags = []
        if r["stores"] < ctx.pins * LISTING_FLAG:
            flags.append("Widen listing")
        if r["sellable_pct"] < STOCK_FLAG:
            flags.append("Fix stock")
        if ticket >= PRICE_FLAG and (np.isnan(unit_idx) or unit_idx >= PRICE_FLAG):
            flags.append("Reprice / repack")
        if pd.notna(r["rating"]) and r["rating"] < RATING_FLAG and rev >= 10:
            flags.append("Fix rating")
        if b_rev >= REVIEW_FLAG * max(rev, 1):
            flags.append("Build reviews")
        if r["winning"]:
            action = "Fix stock" if "Fix stock" in flags else "Defend & scale"
        else:
            action = flags[0] if flags else "Hold"

        def who(x):
            return None if x is None else {
                "brand": x["brand"], "name": short_name(x["product"], x["brand"]), "unit": x["unit"],
                "price": x["price"], "disc": x["disc"], "reviews": x["reviews"], "rating": x["rating"],
                "rank": x["med_rank"], "stores": x["stores"], "p_unit": x["p_unit"], "kind": x["kind"]}

        rows.append({
            "product_id": r["product_id"], "sku": short_name(r["product"], r["brand"]), "product": r["product"],
            "unit": r["unit"], "ptype": r["ptype"], "subcategory": r["subcategory"], "group": r["group"],
            "group_is_type": bool(r["group_is_type"]), "group_n": int(r["group_n"]),
            "rank_in_group": int(r["rank_in_group"]), "winning": bool(r["winning"]), "stores": int(r["stores"]),
            "sellable": int(r["sellable"]), "sellable_pct": r["sellable_pct"], "med_rank": r["med_rank"],
            "top_slot_pct": r["top_slot_pct"], "price": r["price"], "disc": r["disc"], "reviews": rev,
            "rating": r["rating"], "p_unit": r["p_unit"], "kind": r["kind"],
            "bench_price": b_price, "bench_lo": bench["price"].quantile(.25), "bench_hi": bench["price"].quantile(.75),
            "bench_disc": bench["disc"].median(), "bench_offer": (bench["disc"] > 0).mean() * 100,
            "bench_reviews": b_rev, "bench_rating": bench["rating"].median(), "bench_punit": b_punit,
            "ticket_idx": ticket, "unit_idx": unit_idx, "flags": flags, "action": action,
            "lead": who(lead), "k_rival": who(krow), "url": r["url"]})
    t = pd.DataFrame(rows)
    if len(t):
        order = {"Widen listing": 0, "Fix stock": 1, "Reprice / repack": 2, "Fix rating": 3, "Build reviews": 4,
                 "Defend & scale": 5, "Hold": 6}
        t = t.sort_values(["winning", "rank_in_group"], ascending=[False, True])
        t["_o"] = t["action"].map(order)
        t = t.sort_values(["_o", "group", "rank_in_group"]).drop(columns="_o").reset_index(drop=True)
    return t


# ============================================================================ 3. field scorecard
def scorecard(ctx: Ctx, p: pd.DataFrame, n_brands: int = 6) -> dict:
    """The focus brand against the K-beauty brands that matter on Blinkit."""
    fam = (p[p["reviews"].notna()].groupby("family", as_index=False)
           .agg(brand_key=("brand_key", "first"), brand=("brand", "first"), cls=("cls", "first"),
                reviews=("reviews", "max")))
    k = (fam[fam["cls"] == "Korean"].groupby(["brand_key", "brand"], as_index=False)["reviews"].sum()
         .sort_values("reviews", ascending=False).reset_index(drop=True))
    k["demand_share"] = k["reviews"] / max(k["reviews"].sum(), 1) * 100
    k["demand_rank"] = np.arange(1, len(k) + 1)
    present = set(p["brand_key"])
    keys = [ctx.fk] + [b for b in ctx.pk if b in present]
    for b in k["brand_key"]:
        if len(keys) >= max(n_brands, len(keys)):
            break
        if b not in keys:
            keys.append(b)
    reach = ctx.q("SELECT brand_key, count(DISTINCT pincode) AS reach FROM C GROUP BY 1").set_index("brand_key")["reach"]
    rows = []
    for b in keys:
        g = p[p["brand_key"] == b]
        if not len(g):
            continue
        kd = k[k["brand_key"] == b]
        rated = g[g["reviews"].notna()]
        rows.append({
            "brand_key": b, "brand": g["brand"].iloc[0],
            "demand_share": float(kd["demand_share"].iloc[0]) if len(kd) else 0.0,
            "demand_rank": int(kd["demand_rank"].iloc[0]) if len(kd) else None,
            "reviews": float(kd["reviews"].iloc[0]) if len(kd) else 0.0,
            "products": len(g), "winning": int(g["winning"].sum()),
            "win_rate": g["winning"].mean() * 100,
            "top_slot_pct": np.average(g["top_slot_pct"], weights=g["stores"]),
            "reach": int(reach.get(b, 0)), "sellable_pct": g["sellable"].sum() / g["stores"].sum() * 100,
            "median_price": g["price"].median(), "on_offer": (g["disc"] > 0).mean() * 100,
            "avg_disc": g["disc"].mean(),
            "median_reviews": rated["reviews"].median() if len(rated) else 0.0,
            "rating": rated["rating"].median() if len(rated) else np.nan})
    t = pd.DataFrame(rows).sort_values("demand_share", ascending=False).reset_index(drop=True)
    f = t[t["brand_key"] == ctx.fk]
    return {"t": t, "k": k, "focus": f.iloc[0] if len(f) else None, "n_k": len(k)}


# ============================================================================ 4. what moves rank
DRIVERS = {"lrev": "Review count", "stores": "Stores listing it", "rating": "Star rating",
           "disc": "Discount depth", "lprice": "Price"}


def rank_drivers(ctx: Ctx, p: pd.DataFrame) -> dict:
    """Within each sub-category, how far each lever moves a product up its shelf.

    A standardised linear model of shelf-rank percentile on the five levers a brand controls or
    earns. Coefficients read as percentile points of shelf gained for a one-standard-deviation
    step in that lever with the other four held fixed. It is descriptive, not causal: Blinkit's sort
    may use signals we cannot see (sales velocity, margin), which the R² makes explicit.
    """
    out = []
    d = p.copy()
    d["lrev"] = np.log1p(d["reviews"].fillna(0))
    d["lprice"] = np.log(d["price"].clip(lower=1))
    d["rating"] = d["rating"].fillna(d["rating"].median())
    for sc, g in d.groupby("subcategory"):
        if len(g) < 40:
            continue
        y = g["med_rank"].rank(pct=True).to_numpy(float)
        X = g[list(DRIVERS)].astype(float)
        X = ((X - X.mean()) / X.std(ddof=0).replace(0, 1)).fillna(0)
        A = np.column_stack([np.ones(len(X)), X.to_numpy(float)])
        beta, *_ = np.linalg.lstsq(A, y, rcond=None)
        r2 = 1 - ((y - A @ beta) ** 2).sum() / max(((y - y.mean()) ** 2).sum(), 1e-9)
        out.append({"subcategory": sc, "n": len(g), "r2": r2,
                    **{DRIVERS[k]: -b * 100 for k, b in zip(DRIVERS, beta[1:])}})
    t = pd.DataFrame(out)
    med = t[list(DRIVERS.values())].median().sort_values(ascending=False) if len(t) else pd.Series(dtype=float)
    agree = {c: int((t[c] > 0).sum()) for c in DRIVERS.values()} if len(t) else {}
    return {"t": t, "median": med, "r2": float(t["r2"].median()) if len(t) else np.nan,
            "agree": agree, "n": len(t)}


# ============================================================================ 5. range expansion
def whitespace(ctx: Ctx, p: pd.DataFrame) -> dict:
    """Product types ranked by demand, with K-beauty's proven share and the price that wins there."""
    fam = (p[p["reviews"].notna()].groupby(["ptype", "family"], as_index=False)
           .agg(brand_key=("brand_key", "first"), brand=("brand", "first"), cls=("cls", "first"),
                reviews=("reviews", "max"), product=("product", "first"), price=("price", "median"),
                unit=("unit", "first")))
    agg = fam.groupby("ptype").agg(demand=("reviews", "sum"), families=("family", "nunique"))
    agg["k_demand"] = fam[fam["cls"] == "Korean"].groupby("ptype")["reviews"].sum()
    agg["focus_demand"] = fam[fam["brand_key"] == ctx.fk].groupby("ptype")["reviews"].sum()
    agg = agg.fillna({"k_demand": 0, "focus_demand": 0})
    agg["k_share"] = agg["k_demand"] / agg["demand"] * 100
    agg["focus_share_of_k"] = np.where(agg["k_demand"] > 0, agg["focus_demand"] / agg["k_demand"].clip(lower=1) * 100, 0)
    kb = fam[fam["cls"] == "Korean"].sort_values("reviews", ascending=False).drop_duplicates("ptype").set_index("ptype")
    agg["best_k"] = (kb["brand"] + " — " + [short_name(a, b) for a, b in zip(kb["product"], kb["brand"])]
                     ).reindex(agg.index)
    agg["best_k_price"] = kb["price"].reindex(agg.index)
    agg["best_k_reviews"] = kb["reviews"].reindex(agg.index)
    top = p[p["wide"]].sort_values("med_rank").groupby("ptype").head(TOP).groupby("ptype")["price"]
    agg["win_price"], agg["win_lo"], agg["win_hi"] = top.median(), top.quantile(.25), top.quantile(.75)
    agg["focus_skus"] = p[p["brand_key"] == ctx.fk].groupby("ptype").size()
    agg["focus_skus"] = agg["focus_skus"].fillna(0).astype(int)
    agg = agg.reset_index().sort_values("demand", ascending=False).reset_index(drop=True)
    proven = agg[(agg["k_share"] >= 5) & (agg["families"] >= MIN_PEERS)].sort_values("k_demand", ascending=False)
    uncracked = agg[(agg["k_share"] < 1) & (agg["families"] >= MIN_PEERS)].head(8)
    return {"t": agg, "proven": proven, "uncracked": uncracked,
            "k_total": float(agg["k_demand"].sum()), "all_total": float(agg["demand"].sum())}


# ============================================================================ 6. store fix list
STATES = {"ok": "✓ sellable", "oos": "✕ out of stock", "soon": "… coming soon", "none": "— not listed"}


def store_gaps(ctx: Ctx) -> dict:
    """Which focus SKUs a shopper can actually buy in each crawled store."""
    # Per SKU, over every row: a pack size behind another size's card is still on that store's shelf.
    cells = ctx.q("""
        SELECT pincode, product_id AS fid,
               bool_or(product_state = 'available') AS ok, bool_or(product_state = 'coming_soon') AS soon
        FROM L WHERE brand_key = $fk GROUP BY 1, 2""", fk=ctx.fk)
    names = ctx.q("""SELECT product_id AS fid, min(product_name) AS product, min(brand) AS brand,
                            min(unit) AS unit FROM L WHERE brand_key = $fk GROUP BY 1 ORDER BY 1""", fk=ctx.fk)
    stores = ctx.q("SELECT pincode, min(city) AS city, min(area) AS area FROM L GROUP BY 1 ORDER BY 1")
    if not len(names):
        return {"matrix": pd.DataFrame(), "stores": pd.DataFrame(), "city": pd.DataFrame(), "n_skus": 0}
    names["label"] = [f"{short_name(a, b)[:30]} · {u}" for a, b, u in zip(names["product"], names["brand"], names["unit"])]
    cells["state"] = np.where(cells["ok"], STATES["ok"], np.where(cells["soon"], STATES["soon"], STATES["oos"]))
    grid = stores.merge(names[["fid", "label"]], how="cross").merge(cells[["pincode", "fid", "state"]],
                                                                  on=["pincode", "fid"], how="left")
    grid["state"] = grid["state"].fillna(STATES["none"])
    grid["store"] = grid["city"] + " · " + grid["area"].fillna("") + " (" + grid["pincode"] + ")"
    mat = grid.pivot_table(index="store", columns="label", values="state", aggfunc="first")
    per = (grid.assign(sellable=grid["state"] == STATES["ok"], oos=grid["state"] == STATES["oos"],
                       soon=grid["state"] == STATES["soon"], unlisted=grid["state"] == STATES["none"])
           .groupby(["store", "city", "area", "pincode"], as_index=False)[["sellable", "oos", "soon", "unlisted"]].sum())
    per = per.sort_values(["sellable", "store"]).reset_index(drop=True)
    mat = mat.loc[per["store"]]
    col_order = grid.groupby("label")["state"].apply(lambda s: (s == STATES["ok"]).sum()).sort_values(ascending=False)
    mat = mat[col_order.index]
    city = (per.groupby("city", as_index=False).agg(sellable=("sellable", "mean"), worst=("sellable", "min"),
                                                     best=("sellable", "max"))
            .sort_values("sellable"))
    return {"matrix": mat, "stores": per, "city": city, "n_skus": len(names)}


# ============================================================================ 7. week over week
def momentum(cur: pd.DataFrame, prev: pd.DataFrame, fk: str) -> dict | None:
    """Review growth between two crawls — the closest thing to sell-through Blinkit exposes."""
    if prev is None or not len(prev):
        return None

    def fam(d):
        return (d[d["reviews"].notna()].groupby("family", as_index=False)
                .agg(brand_key=("brand_key", "first"), brand=("brand", "first"), cls=("cls", "first"),
                     product=("product", "first"), reviews=("reviews", "max")))
    m = fam(cur).merge(fam(prev)[["family", "reviews"]], on="family", suffixes=("", "_prev"))
    m["gain"] = (m["reviews"] - m["reviews_prev"]).clip(lower=0)
    k = m[m["cls"] == "Korean"]
    brands = (k.groupby(["brand_key", "brand"], as_index=False)[["gain", "reviews_prev"]].sum()
              .sort_values("gain", ascending=False))
    brands["gain_share"] = brands["gain"] / max(brands["gain"].sum(), 1) * 100
    brands["growth"] = brands["gain"] / brands["reviews_prev"].clip(lower=1) * 100
    movers = k.sort_values("gain", ascending=False).head(10)
    focus = m[m["brand_key"] == fk].sort_values("gain", ascending=False)
    return {"brands": brands, "movers": movers, "focus": focus}


# ============================================================================ 8. actions
def actions(ctx: Ctx, skus: pd.DataFrame, sc: dict, ws: dict, sg: dict, dr: dict) -> list[dict]:
    """Ranked moves. Each names the SKUs, cites the evidence, and counts the store listings it touches."""
    out: list[dict] = []
    if not len(skus):
        return out
    fb = ctx.fname

    # Price / pack — one move per product type, since a price architecture is set per type.
    rp = skus[skus["action"] == "Reprice / repack"]
    for grp, g in rp.groupby("group"):
        r0 = g.iloc[0]
        lead, kr = r0["lead"], r0["k_rival"]
        n = len(g)
        what = f"{n} {grp.lower()} SKUs" if n > 1 else f"{r0['sku']} ({r0['unit']})"
        per = ""
        if pd.notna(r0["unit_idx"]) and r0["kind"] == "g/ml":
            per = f", {r0['unit_idx']:.1f}× per g/ml"
        rival = ""
        for x, tag in ((kr, "best-ranked K-beauty rival"), (lead, "type leader")):
            if x is not None:
                off = f" ({x['disc']:.0f}% off)" if x["disc"] >= 1 else ""
                reviews = 0 if pd.isna(x["reviews"]) else x["reviews"]
                rival = (f" The {tag} is {x['brand']} {x['name'][:40]} at ₹{x['price']:,.0f} for {x['unit']}{off}, "
                         f"{reviews:,.0f} reviews, median rank {x['rank']:.0f}.")
                break
        out.append({
            "area": "Price & pack", "affects": int(g["stores"].sum()),
            "move": f"Reprice or repack {what}",
            "why": (f"We sell at ₹{g['price'].median():,.0f} ({r0['ticket_idx']:.1f}× the winners{per}); the {TOP} "
                    f"best-ranked {grp.lower()} products sit at ₹{r0['bench_lo']:,.0f}–₹{r0['bench_hi']:,.0f}, "
                    f"{r0['bench_offer']:.0f}% of them on offer. Our best in the type ranks "
                    f"{int(g['rank_in_group'].min())} of {int(r0['group_n'])}." + rival)})

    # Distribution and stock — a shopper cannot buy what the store does not have.
    for _, r in skus[skus["flags"].map(lambda f: "Widen listing" in f)].iterrows():
        out.append({"area": "Distribution", "affects": int(ctx.pins - r["stores"]),
                    "move": f"Get {r['sku']} ({r['unit']}) listed in more stores",
                    "why": f"Listed in only {r['stores']} of {ctx.pins} stores, so it cannot build rank or reviews "
                           f"anywhere else. It ranks {r['rank_in_group']} of {r['group_n']} in {r['group'].lower()}."})
    for _, r in skus[skus["flags"].map(lambda f: "Fix stock" in f)].sort_values("sellable_pct").iterrows():
        lost = r["stores"] - r["sellable"]
        out.append({"area": "Availability", "affects": int(lost),
                    "move": f"Restock {r['sku']} ({r['unit']})",
                    "why": f"Sellable in {r['sellable']} of the {r['stores']} stores that list it "
                           f"({r['sellable_pct']:.0f}%). Store coverage is the second-strongest rank lever "
                           f"(+{dr['median'].get('Stores listing it', 0):.0f} pts)."})

    # The winners — scale what already works.
    for _, r in skus[skus["winning"]].iterrows():
        gap = ctx.pins - r["sellable"]
        out.append({"area": "Winner", "affects": int(gap),
                    "move": f"Scale {r['sku']} ({r['unit']}) — #{r['rank_in_group']} of {r['group_n']} in {r['group'].lower()}",
                    "why": f"At ₹{r['price']:,.0f} with {r['reviews']:,.0f} reviews ({r['rating']:.1f}★) it beats "
                           f"the {r['group'].lower()} field. Close the {gap} stores where shoppers cannot buy it, "
                           f"and use its price and pack as the template for the rest of the range."
                    if pd.notna(r["rating"]) else
                    f"It ranks #{r['rank_in_group']} of {r['group_n']}. Close the {gap} stores where it is not sellable."})

    # Stores that can barely sell the range.
    st_ = sg.get("stores", pd.DataFrame())
    if len(st_) and sg["n_skus"]:
        best = int(st_["sellable"].max())
        thin = st_[st_["sellable"] <= best / 2]
        if len(thin):
            names = ", ".join(f"{a} ({int(s)})" for a, s in zip(thin["area"].head(4), thin["sellable"].head(4)))
            out.append({"area": "Store range", "affects": int((best - thin["sellable"]).sum()),
                        "move": f"Fix the range in {len(thin)} thin store{'s' if len(thin) > 1 else ''}",
                        "why": f"Shoppers there can buy half or less of what our best store sells ({best} of "
                               f"{sg['n_skus']} SKUs): {names}."})

    # Reviews on SKUs that are well rated and fairly priced — the gap is visibility, not the product.
    rv = skus[(skus["action"].isin(["Build reviews", "Hold"])) & skus["flags"].map(lambda f: "Build reviews" in f)
              & (skus["rating"].fillna(0) >= 4.2)]
    for _, r in rv.iterrows():
        out.append({"area": "Reviews", "affects": int(r["stores"]),
                    "move": f"Seed reviews on {r['sku']} ({r['unit']})",
                    "why": f"Rated {r['rating']:.1f}★ but has {r['reviews']:,.0f} reviews; the {TOP} best-ranked "
                           f"{r['group'].lower()} products have a median of {r['bench_reviews']:,.0f}. Reviews are the "
                           f"strongest rank lever (+{dr['median'].get('Review count', 0):.0f} pts)."})
    for _, r in skus[skus["flags"].map(lambda f: "Fix rating" in f)].iterrows():
        out.append({"area": "Rating", "affects": int(r["stores"]),
                    "move": f"Find out why {r['sku']} is rated {r['rating']:.1f}★",
                    "why": f"The {TOP} best-ranked {r['group'].lower()} products average {r['bench_rating']:.1f}★. "
                           f"A weak rating drags rank and conversion before any spend can help."})

    # Range expansion where K-beauty already sells and we are absent.
    pv = ws["proven"]
    pv = pv[pv["focus_skus"] == 0]
    for _, r in pv.head(2).iterrows():
        out.append({"area": "Range", "affects": int(ctx.pins),
                    "move": f"Test a {r['ptype'].lower()} at ₹{r['win_lo']:,.0f}–₹{r['win_hi']:,.0f}",
                    "why": f"K-beauty earns {r['k_share']:.0f}% of {r['ptype'].lower()} reviews on Blinkit and {fb} has "
                           f"no product there. Proof point: {r['best_k']} — {r['best_k_reviews']:,.0f} reviews at "
                           f"₹{r['best_k_price']:,.0f}."})

    return sorted(out, key=lambda x: x["affects"], reverse=True)
