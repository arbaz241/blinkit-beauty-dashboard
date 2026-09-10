# Blinkit Beauty Market-Analysis Dashboard — Build Instructions for Claude Code

## 0. Context for whoever builds this
This is a competitive-intelligence dashboard for Blinkit (India quick-commerce), scoped to
Skincare + Makeup. It exists to track how a Korean beauty brand (and its competitors) perform
across pincodes on Blinkit — availability, SKU-level pricing, best-sellers, brand classification,
and new-listing detection. It is a sibling project to an existing multi-platform q-commerce
dashboard (Blinkit + Zepto + Swiggy Instamart, built with DuckDB + Streamlit) — reuse patterns
from that build where sensible, but this one is Blinkit-only and beauty-only, with deeper
per-brand/per-SKU analysis.

**Read this whole spec before writing code.** Flag any step that looks technically infeasible
rather than silently skipping it.

---

## 1. Important caveat to surface to the user before building
Blinkit has no public API. Any data collection here means scraping the consumer-facing web/app
endpoints. This is fragile (layout and endpoint changes break it), rate-limit/IP-block prone, and
sits in a legal/ToS gray area. Build for resilience (retries, backoff, logging of failures) but
do not assume 100% uptime or completeness. If Claude Code cannot find a workable data-collection
path, say so explicitly rather than producing a shell dashboard with no real data behind it.

---

## 2. Scope

**Platform:** Blinkit only.

**Category scope:** Skincare + Makeup listings only (not haircare, bath/body, fragrance, etc. —
whatever falls under Blinkit's own Skincare and Makeup category/subcategory pages).

**Geography:** Start with 5–10 pincodes covering key Indian metros. Suggested starter set
(confirm with user, adjust as needed):
- Mumbai (e.g. 400001 or a residential-density pincode)
- Delhi/NCR (e.g. 110001 or Gurgaon)
- Bengaluru
- Chennai
- Hyderabad
- Kolkata
- Pune (optional 7th–8th if going to 8-10)

Pincode list should live in a config file, not hardcoded, so it's easy to expand later.

**Refresh cadence:** Weekly. Build the scraper to be run on a schedule (see Section 6) but keep
manual/on-demand trigger available too.

---

## 3. Data to collect (per pincode, per crawl)

For every product listing under Skincare + Makeup at each pincode:
- Brand name (raw, as shown)
- Product name / SKU title
- Price (current/selling price)
- MRP / discount if shown
- Pack size / variant if shown
- In-stock vs out-of-stock status
- Category/subcategory tag (Skincare vs Makeup, and sub-tag if available e.g. "Serums", "Lipstick")
- Position/rank in listing or search result (if extractable — useful for "what's most prominent")
- Product ID / listing URL (for dedup and change-tracking across crawls)
- Timestamp of the crawl

Store every crawl as a snapshot (don't overwrite previous data) so that week-over-week
comparisons — new listings, delistings, price changes — are derivable.

---

## 4. Brand classification

Maintain a `brands` reference table with columns: `brand_name`, `classification`
(Local / Global / Korean), `country_of_origin` (optional), `notes`, `confirmed` (bool).

Workflow:
1. On first sighting of a new brand name in the scraped data, auto-suggest a classification
   using simple heuristics (known K-beauty brand list, known global-conglomerate brand list —
   e.g. L'Oréal/Estée Lauder/P&G/Unilever house brands — else default "Unclassified").
2. Surface unclassified/unconfirmed brands in the dashboard in a review queue so the user can
   confirm or correct them.
3. Once confirmed, the brand is locked in and won't be re-suggested unless the user resets it.

This keeps classification accurate without requiring the user to manually seed the full list
up front.

---

## 5. Analysis / dashboard views to build

- **Brand landscape**: count of brands live per pincode, split by classification
  (Local/Global/Korean), with trend over time.
- **SKU tracker per brand**: for the user's own K-beauty brand and any competitors flagged,
  list all active SKUs, price, and estimated "major seller" status.
  - Note: Blinkit doesn't expose actual sales volume. "Best-selling" will have to be inferred
    from proxies — listing position/rank, review count if shown, "bestseller" tags if the
    platform surfaces them. Be explicit in the UI that this is a proxy, not confirmed sales data.
- **Price positioning**: compare price levels for equivalent SKU types (e.g. "vitamin C serum")
  across brands/classifications.
- **New arrivals / delisting tracker**: brands or SKUs that appear at a pincode for the first
  time, or disappear from a pincode where they were previously present — week-over-week diff.
- **Pincode coverage map**: which brands are present in which pincodes — highlights gaps/
  expansion opportunities.
- **Focus brand dashboard**: a dedicated view filtered to the user's own brand vs a
  user-selected competitor set.

---

## 6. Suggested technical approach

- **Stack**: Python, DuckDB (local analytical storage, append-only snapshots), Streamlit
  (frontend) — consistent with the existing q-commerce dashboard stack.
- **Scraper**: separate module from the dashboard app; should be runnable standalone
  (`python scrape.py`) and log successes/failures per pincode/category. Build in retry logic
  and randomized delays to reduce block risk given the weekly (low-frequency) cadence.
- **Scheduling**: since hosting is Streamlit Community Cloud, note that Streamlit Cloud does
  not run background cron jobs. Options to raise with the user:
  - Run the weekly scrape locally/manually and push the updated DuckDB file to the repo the
    Streamlit app reads from.
  - Use a free scheduler (e.g. GitHub Actions on a weekly cron) to run the scraper and commit
    the refreshed data file, which Streamlit Cloud then picks up on next load.
  GitHub Actions is likely the cleaner fit — flag this choice to the user rather than assuming.
- **Data storage**: DuckDB file (or Parquet snapshots) checked into the repo or pulled from
  object storage at app startup — Streamlit Community Cloud has no persistent writable disk
  across sessions, so the data source needs to live somewhere durable outside the app process.
- **Repo structure**: mirror the existing q-commerce dashboard's structure where reasonable
  (e.g. `/scraper`, `/data`, `/app`, `.env` for any config, requirements.txt).

---

## 7. Open items to confirm with the user during build

- Exact starter pincode list (metros suggested above — confirm final list).
- The user's own K-beauty brand name + initial competitor set to pin in the "focus" view.
- Whether GitHub Actions (vs manual local runs) is acceptable for the weekly refresh, given
  Streamlit Community Cloud's lack of native scheduling.
- Any known Blinkit anti-scraping behavior already encountered while building the sibling
  q-commerce dashboard (reuse those learnings/workarounds if documented anywhere).

---

## 8. Explicitly out of scope for v1
- Zepto, Swiggy Instamart, or any platform other than Blinkit (already covered elsewhere).
- Categories outside Skincare + Makeup.
- Real sales-volume data (not obtainable from the platform).
- Real-time/sub-daily refresh (weekly cadence only for v1).
