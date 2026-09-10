"""Turn a /v1/layout/listing_widgets response into flat listing rows.

Each product card (widget_type == product_card_snippet_type_2) becomes one row. Cards with a
variant dropdown carry a `variant_list`; each variant that is a distinct product_id becomes an
extra row flagged is_variant=True so pack-size/price differences are captured.
"""
from __future__ import annotations

import json
import re
from typing import Any

PRODUCT_WIDGET = "product_card_snippet_type_2"


def normalize_brand(name: str | None) -> str:
    """Key used to match brands across spellings: lowercase, alnum only."""
    if not name:
        return ""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "p"


def _text(node: Any) -> str | None:
    if isinstance(node, dict):
        return node.get("text")
    if isinstance(node, str):
        return node
    return None


def _money(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", text)
    return float(m.group(0).replace(",", "")) if m else None


def _int(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _rating(card: dict) -> tuple[float | None, int | None]:
    r = card.get("rating") or {}
    bar = r.get("bar") or {}
    value = bar.get("value")
    count_txt = _text(bar.get("title"))
    count = None
    if count_txt:
        m = re.search(r"[\d,]+", count_txt)
        if m:
            count = int(m.group(0).replace(",", ""))
    try:
        value = float(value) if value is not None else None
    except (TypeError, ValueError):
        value = None
    return value, count


def _badges(card: dict) -> list[str]:
    out: list[str] = []
    pb = card.get("product_badges")
    if isinstance(pb, list):
        for b in pb:
            t = _text(b) or _text((b or {}).get("title")) if isinstance(b, dict) else str(b)
            if t:
                out.append(t)
    elif isinstance(pb, dict):
        t = _text(pb.get("title")) or _text(pb)
        if t:
            out.append(t)
    # product_badge_type is a layout hint ("non_overlapping"), not a merchandising badge; ignored.
    return out


def _card_row(card: dict, tracking: dict, *, position: int, page_index: int,
              is_variant: bool = False, parent_product_id: int | None = None) -> dict[str, Any] | None:
    cart = (((card.get("atc_action") or {}).get("add_to_cart") or {}).get("cart_item")) or {}
    product_id = _int(card.get("product_id")) or _int(cart.get("product_id")) or _int((card.get("identity") or {}).get("id"))
    if product_id is None:
        return None
    name = _text(card.get("name")) or cart.get("product_name") or _text(card.get("display_name")) or tracking.get("name")
    brand = _text(card.get("brand_name")) or _text(cart.get("brand")) or _text(tracking.get("brand"))
    price = cart.get("price")
    mrp = cart.get("mrp")
    if price is None:
        price = _money(_text(card.get("normal_price"))) or _money(str(tracking.get("price") or ""))
    if mrp is None:
        mrp = _money(_text(card.get("mrp"))) or _money(str(tracking.get("mrp") or ""))
    try:
        price = float(price) if price is not None else None
        mrp = float(mrp) if mrp is not None else None
    except (TypeError, ValueError):
        price, mrp = None, None
    inventory = _int(card.get("inventory"))
    if inventory is None:
        inventory = _int(cart.get("inventory"))
    if inventory is None:
        inventory = _int(tracking.get("inventory"))
    sold_out = bool(card.get("is_sold_out")) or (inventory == 0)
    rating_value, rating_count = _rating(card)
    discount_pct = None
    if price is not None and mrp:
        discount_pct = round(max(0.0, (mrp - price) / mrp * 100.0), 2)
    return {
        "product_id": product_id,
        "variant_group_id": _int(card.get("group_id")) or _int(cart.get("group_id")),
        "parent_product_id": parent_product_id,
        "is_variant": is_variant,
        "product_name": name,
        "brand_raw": brand,
        "brand_key": normalize_brand(brand),
        "unit": _text(card.get("variant")) or cart.get("unit"),
        "ptype": tracking.get("ptype") or None,
        "price": price,
        "mrp": mrp,
        "discount_pct": discount_pct,
        "inventory": inventory,
        "in_stock": not sold_out,
        "product_state": card.get("product_state") or tracking.get("state"),
        "rating_value": rating_value,
        "rating_count": rating_count,
        "badges": json.dumps(_badges(card)) if _badges(card) else None,
        "merchant_id": _int(card.get("merchant_id")) or _int(cart.get("merchant_id")),
        "page_index": page_index,
        "position": position,
        "product_position": _int(tracking.get("product_position")),
        "image_url": ((card.get("image") or {}).get("url")) or cart.get("image_url"),
        "product_url": f"https://blinkit.com/prn/{slugify(name or '')}/prid/{product_id}",
    }


def parse_listing_page(payload: dict, *, page_index: int, position_offset: int) -> list[dict[str, Any]]:
    """Rows for one page. `position_offset` is how many primary cards preceded this page."""
    rows: list[dict[str, Any]] = []
    snippets = (payload.get("response") or {}).get("snippets") or []
    pos = position_offset
    for snip in snippets:
        if snip.get("widget_type") != PRODUCT_WIDGET:
            continue
        card = snip.get("data") or {}
        tracking = (snip.get("tracking") or {}).get("common_attributes") or {}
        pos += 1
        base = _card_row(card, tracking, position=pos, page_index=page_index)
        if base is None:
            continue
        rows.append(base)
        seen = {base["product_id"]}
        for v in card.get("variant_list") or []:
            vcard = (v or {}).get("data") or {}
            vtrack = ((v or {}).get("tracking") or {}).get("common_attributes") or {}
            vrow = _card_row(vcard, vtrack, position=pos, page_index=page_index,
                             is_variant=True, parent_product_id=base["product_id"])
            if vrow and vrow["product_id"] not in seen:
                # Variants often omit brand/name at the card level; inherit from the parent.
                vrow["brand_raw"] = vrow["brand_raw"] or base["brand_raw"]
                vrow["brand_key"] = vrow["brand_key"] or base["brand_key"]
                vrow["product_name"] = vrow["product_name"] or base["product_name"]
                vrow["ptype"] = vrow["ptype"] or base["ptype"]
                seen.add(vrow["product_id"])
                rows.append(vrow)
    return rows


def count_primary_cards(payload: dict) -> int:
    return sum(1 for s in ((payload.get("response") or {}).get("snippets") or []) if s.get("widget_type") == PRODUCT_WIDGET)


def total_items(payload: dict) -> int | None:
    """Blinkit reports the collection size in next_url (total_pagination_items) and postback meta."""
    pagination = (payload.get("response") or {}).get("pagination") or {}
    m = re.search(r"total_pagination_items=(\d+)", pagination.get("next_url") or "")
    if m:
        return int(m.group(1))
    try:
        return int(payload["postback_params"]["postback_meta"]["pageMeta"]["scrollMeta"][0]["entitiesCount"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None
