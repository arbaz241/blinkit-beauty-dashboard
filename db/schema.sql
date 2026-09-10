-- Append-only snapshot of every product card seen per (run, pincode, subcategory).
CREATE TABLE IF NOT EXISTS listings (
    run_id                 VARCHAR NOT NULL,
    crawled_at             TIMESTAMPTZ NOT NULL,
    pincode                VARCHAR NOT NULL,
    city                   VARCHAR,
    area                   VARCHAR,
    lat                    DOUBLE,
    lon                    DOUBLE,
    locality               VARCHAR,
    location_merchant_id   BIGINT,
    top_category           VARCHAR NOT NULL,      -- Skincare | Makeup
    subcategory            VARCHAR NOT NULL,      -- Blinkit grouping name, e.g. "Sunscreen"
    collection_id          BIGINT,
    group_id               BIGINT,
    l1_cat_id              BIGINT,
    subcategory_total      INTEGER,               -- Blinkit's reported collection size
    page_index             INTEGER,
    position               INTEGER,               -- 1-based rank in bestseller-sorted crawl (best-seller proxy)
    product_position       INTEGER,
    product_id             BIGINT NOT NULL,
    variant_group_id       BIGINT,
    parent_product_id      BIGINT,
    is_variant             BOOLEAN,
    product_name           VARCHAR,
    brand_raw              VARCHAR,
    brand_key              VARCHAR,
    unit                   VARCHAR,
    ptype                  VARCHAR,               -- Blinkit product type, e.g. "Sunscreen", "Lipstick Kit"
    price                  DOUBLE,
    mrp                    DOUBLE,
    discount_pct           DOUBLE,
    inventory              INTEGER,
    in_stock               BOOLEAN,
    product_state          VARCHAR,
    rating_value           DOUBLE,
    rating_count           INTEGER,
    badges                 VARCHAR,               -- JSON list of badge strings
    merchant_id            BIGINT,
    image_url              VARCHAR,
    product_url            VARCHAR
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id            VARCHAR PRIMARY KEY,
    started_at        TIMESTAMPTZ,
    finished_at       TIMESTAMPTZ,
    pincodes_total    INTEGER,
    pincodes_ok       INTEGER,
    pincodes_failed   INTEGER,
    units_total       INTEGER,
    units_ok          INTEGER,
    units_partial     INTEGER,
    units_failed      INTEGER,
    rows              INTEGER,
    requests          INTEGER,
    retries           INTEGER,
    notes             VARCHAR,
    run_log_json      VARCHAR
);

-- Brand reference table. Mirrors data/brands.csv (the human-edited source of truth).
CREATE TABLE IF NOT EXISTS brands (
    brand_key           VARCHAR PRIMARY KEY,
    brand_name          VARCHAR NOT NULL,
    classification      VARCHAR NOT NULL,   -- Local | Global | Korean | Unclassified
    country_of_origin   VARCHAR,
    notes               VARCHAR,
    confirmed           BOOLEAN NOT NULL DEFAULT FALSE,
    suggested_by        VARCHAR,            -- seed:korean | seed:global | seed:local | none | user
    first_seen_run      VARCHAR,
    updated_at          TIMESTAMPTZ
);

CREATE OR REPLACE VIEW v_runs AS
SELECT run_id, min(crawled_at) AS crawled_at, count(DISTINCT pincode) AS pincodes,
       count(*) AS rows, count(DISTINCT product_id) AS products, count(DISTINCT brand_key) AS brands
FROM listings GROUP BY run_id ORDER BY crawled_at DESC;

CREATE OR REPLACE VIEW v_latest_run AS
SELECT run_id FROM v_runs ORDER BY crawled_at DESC LIMIT 1;

CREATE OR REPLACE VIEW v_prev_run AS
SELECT run_id FROM v_runs ORDER BY crawled_at DESC LIMIT 1 OFFSET 1;

-- Listings joined with brand classification.
CREATE OR REPLACE VIEW v_listings AS
SELECT l.*,
       coalesce(b.brand_name, l.brand_raw)            AS brand,
       coalesce(b.classification, 'Unclassified')     AS classification,
       coalesce(b.confirmed, FALSE)                   AS brand_confirmed
FROM listings l LEFT JOIN brands b USING (brand_key);
