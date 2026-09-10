"""Housekeeping: classify newly-seen brands, and check whether the crawl that produced these
numbers actually completed.

Both live here because they answer the same question — "can I trust what the other pages say?"
"""
import base64
import json
import urllib.request

import pandas as pd
import streamlit as st

from common import CLASSES, invalidate, q, runs, sidebar_filters
from db import BRANDS_CSV
from db.brands import upsert_csv

st.set_page_config(page_title="Data & brands", page_icon="🧴", layout="wide")
st.title("Data & brands")

f = sidebar_filters(show_class=False, show_stock=False)
run_id = f.run_id if f else None

tab_brands, tab_health = st.tabs(["Brand review queue", "Crawl health"])

# ------------------------------------------------------------------ brands
with tab_brands:
    st.caption("New brands are auto-classified from the seed lists in `config/brands_seed.yaml`. Confirm or correct "
               "them here — confirmed brands are locked and never re-suggested. Edits save to `data/brands.csv`, "
               "which every page reads live.")
    which = st.radio("Show", ["Unconfirmed only", "All brands"], horizontal=True)
    brands = q("""
        SELECT bl.brand_key, bl.brand_name, bl.classification, bl.country_of_origin, bl.notes,
               bl.confirmed, bl.suggested_by,
               coalesce(s.cards, 0) AS listings, coalesce(s.pincodes, 0) AS pincodes, s.sample
        FROM brands_live bl LEFT JOIN (
            SELECT brand_key, count(*) AS cards, count(DISTINCT pincode) AS pincodes,
                   string_agg(DISTINCT product_name, ' · ') AS sample
            FROM listings WHERE run_id = (SELECT run_id FROM v_latest_run) AND NOT is_variant
            GROUP BY 1) s USING (brand_key)
        ORDER BY bl.confirmed, listings DESC, bl.brand_name""")
    brands["sample"] = brands["sample"].fillna("").str.slice(0, 140)
    for c in ("country_of_origin", "notes"):
        brands[c] = brands[c].fillna("")
    view = brands[~brands["confirmed"]] if which.startswith("Unconfirmed") else brands

    n_unconf = int((~brands["confirmed"]).sum())
    a, b, c = st.columns(3)
    a.metric("Brands known", len(brands))
    b.metric("Unconfirmed", n_unconf)
    c.metric("Unclassified", int((brands["classification"] == "Unclassified").sum()))
    if not view.empty and view["listings"].sum() > 0:
        st.caption("Sorted so the brands with the most shelf presence are at the top — classify those first.")

    edited = st.data_editor(
        view[["brand_key", "brand_name", "classification", "country_of_origin", "notes", "confirmed",
              "suggested_by", "listings", "pincodes", "sample"]],
        hide_index=True, width="stretch", height=520, key="brand_editor",
        disabled=["brand_key", "brand_name", "suggested_by", "listings", "pincodes", "sample"],
        column_config={
            "brand_key": None,
            "brand_name": st.column_config.TextColumn("brand", width="medium"),
            "classification": st.column_config.SelectboxColumn("classification", options=CLASSES, required=True),
            "country_of_origin": st.column_config.TextColumn("country", width="small"),
            "confirmed": st.column_config.CheckboxColumn("confirmed"),
            "suggested_by": st.column_config.TextColumn("suggested by", width="small"),
            "sample": st.column_config.TextColumn("sample products", width="large")})

    m = edited.merge(view, on="brand_key", suffixes=("", "_old"))
    changed = m[(m["classification"] != m["classification_old"]) | (m["confirmed"] != m["confirmed_old"])
                | (m["country_of_origin"].fillna("") != m["country_of_origin_old"].fillna(""))
                | (m["notes"].fillna("") != m["notes_old"].fillna(""))]
    col1, col2 = st.columns([1, 3])
    if col1.button(f"Save {len(changed)} change(s)", type="primary", disabled=changed.empty):
        n = upsert_csv([{ "brand_key": r["brand_key"], "classification": r["classification"],
                          "country_of_origin": r["country_of_origin"] or "", "notes": r["notes"] or "",
                          "confirmed": bool(r["confirmed"])} for _, r in changed.iterrows()])
        invalidate()
        st.success(f"Saved {n} brand(s) to data/brands.csv.")
        st.rerun()
    col2.caption("Tip: tick **confirmed** on rows whose suggestion is already right — that clears the queue fastest.")

    st.divider()
    st.subheader("Persist beyond this session")
    st.caption("Streamlit Community Cloud has an ephemeral filesystem: commit `data/brands.csv` back to the repo so "
               "edits survive the next deploy. Running locally, the file is already saved.")
    if BRANDS_CSV.exists():
        st.download_button("Download brands.csv", BRANDS_CSV.read_bytes(), "brands.csv", "text/csv")

    def _secret(name):
        try:
            return st.secrets.get(name)
        except Exception:  # noqa: BLE001 - no secrets.toml configured
            return None

    token, repo = _secret("GITHUB_TOKEN"), _secret("GITHUB_REPO")
    if token and repo:
        if st.button("Commit brands.csv to GitHub"):
            api = f"https://api.github.com/repos/{repo}/contents/data/brands.csv"
            hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                   "User-Agent": "blinkit-dashboard"}
            try:
                with urllib.request.urlopen(urllib.request.Request(api, headers=hdr), timeout=20) as r:
                    sha = json.load(r).get("sha")
            except Exception:  # noqa: BLE001 - file may not exist yet
                sha = None
            body = {"message": "brands: review from dashboard",
                    "content": base64.b64encode(BRANDS_CSV.read_bytes()).decode()}
            if sha:
                body["sha"] = sha
            try:
                req = urllib.request.Request(api, data=json.dumps(body).encode(),
                                             headers={**hdr, "Content-Type": "application/json"}, method="PUT")
                with urllib.request.urlopen(req, timeout=30) as r:
                    st.success(f"Committed: {json.load(r)['commit']['html_url']}")
            except Exception as e:  # noqa: BLE001
                st.error(f"GitHub commit failed: {e}")
    else:
        st.caption("Add `GITHUB_TOKEN` and `GITHUB_REPO` to Streamlit secrets for one-click commits from here.")

# ------------------------------------------------------------------ crawl health
with tab_health:
    st.caption("Blinkit is scraped, not queried through an API. Everything on the other pages is only as complete "
               "as this table — a missing pincode looks identical to a brand that left it.")
    rl = q("SELECT * FROM crawl_runs WHERE run_id = ?", (run_id,)) if run_id else pd.DataFrame()
    if rl.empty:
        st.info("No run log recorded for this crawl.")
    else:
        r = rl.iloc[0]
        m = st.columns(5)
        m[0].metric("Pincodes ok / failed", f"{r.pincodes_ok} / {r.pincodes_failed}")
        m[1].metric("Units ok", int(r.units_ok))
        m[2].metric("Units partial / failed", f"{r.units_partial} / {r.units_failed}")
        m[3].metric("Requests (retries)", f"{r.requests} ({r.retries})")
        dur = ((pd.to_datetime(r.finished_at) - pd.to_datetime(r.started_at)).total_seconds() / 60
               if pd.notna(r.finished_at) else None)
        m[4].metric("Duration", f"{dur:.0f} min" if dur else "–")

        log = json.loads(r.run_log_json or "{}")
        pins = pd.DataFrame([{
            "pincode": p["pincode"], "city": p["city"], "area": p["area"], "status": p["status"],
            "resolved": p.get("locality"), "merchant": p.get("merchant_id"),
            "ok": sum(u["status"] == "ok" for u in p["units"]),
            "partial": sum(u["status"] == "partial" for u in p["units"]),
            "failed": sum(u["status"] == "failed" for u in p["units"]),
            "rows": sum(u["rows"] for u in p["units"]), "error": p.get("error")} for p in log.get("pincodes", [])])
        if not pins.empty:
            bad = pins[pins["status"] != "ok"]
            if not bad.empty:
                st.error(f"{len(bad)} pincode(s) failed: {', '.join(bad['pincode'])}")
            st.dataframe(pins, hide_index=True, width="stretch", height=min(520, 36 * len(pins) + 40))
        issues = [{"pincode": p["pincode"], "subcategory": u["subcategory"], "status": u["status"],
                   "pages": u["pages"], "cards": u["primary_cards"], "expected": u.get("expected_items"),
                   "error": u.get("error")}
                  for p in log.get("pincodes", []) for u in p["units"] if u["status"] != "ok"]
        if issues:
            with st.expander(f"{len(issues)} sub-category units not fully crawled"):
                st.dataframe(pd.DataFrame(issues), hide_index=True, width="stretch")

    st.divider()
    st.subheader("All crawls")
    st.dataframe(runs(), hide_index=True, width="stretch")
