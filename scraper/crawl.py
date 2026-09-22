"""Crawl orchestration: pincodes x category groupings -> rows + a per-unit run log."""
from __future__ import annotations

import logging
import random
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .client import BlinkitBrowser, BlinkitError, BlinkitSession, LocationNotServiceable
from .parse import count_primary_cards, parse_listing_page, total_items

log = logging.getLogger(__name__)


@dataclass
class UnitLog:
    """One (pincode, grouping) crawl unit."""
    pincode: str
    city: str
    top_category: str
    subcategory: str
    group_id: int
    status: str = "pending"          # ok | partial | failed | skipped
    pages: int = 0
    primary_cards: int = 0
    rows: int = 0
    expected_items: int | None = None
    error: str | None = None
    seconds: float = 0.0


@dataclass
class PincodeLog:
    pincode: str
    city: str
    area: str
    status: str = "pending"          # ok | failed
    lat: float | None = None
    lon: float | None = None
    locality: str | None = None
    merchant_id: int | None = None
    error: str | None = None
    units: list[UnitLog] = field(default_factory=list)
    requests: int = 0
    retries: int = 0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


def enabled_groupings(categories_cfg: dict, only: set[str] | None = None) -> list[dict[str, Any]]:
    out = []
    for coll in categories_cfg.get("collections", []):
        for g in coll.get("groupings", []):
            if not g.get("enabled", True):
                continue
            if only and str(g["group_id"]) not in only and g["name"].lower() not in only:
                continue
            out.append({
                "top_category": coll["top_category"],
                "collection_id": coll["collection_id"],
                "collection_uuid": coll["collection_uuid"],
                "group_id": int(g["group_id"]),
                "subcategory": g["name"],
                "l1_cat_ids": g.get("l1_cat_ids") or [],
            })
    return out


# Cards vs Blinkit's reported item count is not 1:1 — its total counts shades and sizes that collapse
# into one card, so a complete Lipstick crawl lands near 83%. Calibrated on the 2026-09-09 crawl (384
# units, worst genuine unit 68.5%) against the truncated 2026-09-23 run (units at 8-60%): 0.65 flags
# every truncated unit and none of the good ones.
MIN_CAPTURE = 0.65


def crawl_grouping(session: BlinkitSession, grouping: dict[str, Any], *, max_pages: int,
                   min_capture: float = MIN_CAPTURE) -> tuple[list[dict], UnitLog]:
    loc = session.location
    assert loc is not None
    unit = UnitLog(pincode=loc.pincode, city="", top_category=grouping["top_category"],
                   subcategory=grouping["subcategory"], group_id=grouping["group_id"])
    t0 = time.time()
    rows: list[dict] = []
    seen_ids: set[int] = set()
    primary = 0
    try:
        for page_index, payload in session.iter_collection_pages(
            collection_uuid=grouping["collection_uuid"], group_id=grouping["group_id"], max_pages=max_pages,
        ):
            unit.pages += 1
            # Keep the *latest* total, not the first. Blinkit's opening estimate is inflated and it
            # revises it downward mid-list (Lipstick & Gloss: 345 on page 1, 308 from page 13, and
            # exactly 308 cards served). Benchmarking against page 1 made complete crawls look
            # 10-17% short. The final page reports a stale figure, so ignore it once we're past p1.
            t = total_items(payload)
            nxt = ((payload.get("response") or {}).get("pagination") or {}).get("next_url")
            if t is not None and (unit.expected_items is None or nxt):
                unit.expected_items = t
            n_primary = count_primary_cards(payload)
            page_rows = parse_listing_page(payload, page_index=page_index, position_offset=primary)
            primary += n_primary
            for r in page_rows:
                if r["product_id"] in seen_ids:
                    continue
                seen_ids.add(r["product_id"])
                rows.append(r)
            if n_primary == 0:
                # Empty page => end of collection even if next_url is still present.
                break
        unit.primary_cards = primary
        unit.rows = len(rows)
        # Blinkit sometimes stops paginating mid-collection: it drops next_url while its own reported
        # total is far higher (seen 2026-09-23: 15 cards served of 182 reported, in two pages). That is
        # a truncated crawl, not a short shelf. This used to require pages >= max_pages to count as
        # partial, so silently truncated units were recorded as complete and read as mass delistings.
        if unit.expected_items and primary < unit.expected_items * min_capture:
            unit.status = "partial"
            unit.error = unit.error or (f"truncated: captured {primary} of {unit.expected_items} items "
                                        f"in {unit.pages} pages")
        else:
            unit.status = "ok"
    except BlinkitError as e:
        unit.error = str(e)
        unit.status = "partial" if rows else "failed"
        unit.rows = len(rows)
        unit.primary_cards = primary
    unit.seconds = round(time.time() - t0, 1)
    return rows, unit


def crawl_pincode(browser: BlinkitBrowser, pin_cfg: dict[str, Any], groupings: list[dict[str, Any]], *,
                  run_id: str, max_pages: int, delay_between_groups: tuple[float, float] = (3.0, 6.0),
                  page_delay: tuple[float, float] | None = None, min_capture: float = MIN_CAPTURE,
                  truncated_retry_delay: tuple[float, float] = (20.0, 40.0)) -> tuple[list[dict], PincodeLog]:
    plog = PincodeLog(pincode=str(pin_cfg["pincode"]), city=pin_cfg.get("city", ""), area=pin_cfg.get("area", ""))
    all_rows: list[dict] = []
    session = browser.session(delay_range=page_delay) if page_delay else browser.session()
    try:
        try:
            loc = session.set_location(plog.pincode, pin_cfg.get("search_text"))
        except (LocationNotServiceable, BlinkitError) as e:
            plog.status = "failed"
            plog.error = str(e)
            log.error("pincode %s: %s", plog.pincode, e)
            return all_rows, plog
        plog.lat, plog.lon, plog.locality, plog.merchant_id = loc.lat, loc.lon, loc.locality, loc.merchant_id

        for g in groupings:
            rows, unit = crawl_grouping(session, g, max_pages=max_pages, min_capture=min_capture)
            # A truncation is usually Blinkit throttling this session, not an empty shelf, and it
            # clears after a pause. One slower retry recovers most of them; the better result wins.
            if unit.status == "partial" and unit.error and unit.error.startswith("truncated"):
                log.warning("  %s / %s: %s — pausing, then retrying once", plog.pincode, unit.subcategory, unit.error)
                time.sleep(random.uniform(*truncated_retry_delay))
                rows2, unit2 = crawl_grouping(session, g, max_pages=max_pages, min_capture=min_capture)
                if unit2.primary_cards > unit.primary_cards:
                    rows, unit = rows2, unit2
                    unit.error = (unit.error + " (best of 2 attempts)") if unit.error else "recovered on retry"
            unit.city = plog.city
            crawled_at = datetime.now(timezone.utc)
            for r in rows:
                r.update({
                    "run_id": run_id,
                    "crawled_at": crawled_at,
                    "pincode": plog.pincode,
                    "city": plog.city,
                    "area": plog.area,
                    "lat": loc.lat,
                    "lon": loc.lon,
                    "locality": loc.locality,
                    "location_merchant_id": loc.merchant_id,
                    "top_category": g["top_category"],
                    "subcategory": g["subcategory"],
                    "collection_id": g["collection_id"],
                    "group_id": g["group_id"],
                    "l1_cat_id": (g["l1_cat_ids"] or [None])[0],
                    "subcategory_total": unit.expected_items,
                })
            all_rows.extend(rows)
            plog.units.append(unit)
            log.info("  %s / %s: %s pages=%d cards=%d rows=%d expected=%s (%.1fs)%s",
                     plog.pincode, unit.subcategory, unit.status, unit.pages, unit.primary_cards, unit.rows,
                     unit.expected_items, unit.seconds, f" err={unit.error}" if unit.error else "")
            time.sleep(random.uniform(*delay_between_groups))
        # A pincode is only "ok" when every sub-category came back. Marking it ok because *some*
        # units succeeded hides the gap: a sub-category that failed to load is indistinguishable
        # from one where nothing is listed, so the dashboard would report a real category as empty.
        n_ok = sum(u.status == "ok" for u in plog.units)
        n_failed = sum(u.status == "failed" for u in plog.units)
        if plog.units and n_ok == len(plog.units):
            plog.status = "ok"
        elif plog.units and n_failed < len(plog.units):
            # Some sub-categories came back short or empty, but this store still yielded data.
            # "failed" is reserved for a store we got nothing usable from — previously a store whose
            # units were all partial was recorded as a total failure and its rows written off.
            plog.status = "partial"
            bad = [u.subcategory for u in plog.units if u.status != "ok"]
            plog.error = ((plog.error or "") + f" incomplete: {len(bad)} of {len(plog.units)} sub-categories "
                                               f"short or failed ({', '.join(bad[:6])})").strip()
        else:
            plog.status = "failed"
    finally:
        plog.requests = session.stats.requests
        plog.retries = session.stats.retries
        session.close()
    return all_rows, plog
