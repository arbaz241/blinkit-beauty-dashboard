"""Browser session against blinkit.com.

One BlinkitSession == one browser context == one delivery location. Blinkit resolves the store
(merchant) from the lat/lon it geocodes when you pick a delivery location, so we set the location
through the real UI and then read what it resolved from localStorage/cookies.

API calls are made with fetch() *inside the page* (page.evaluate) rather than from Python, so the
Cloudflare cookies and the app's auth_key / device_id / lat / lon headers ride along untouched.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Iterator

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

log = logging.getLogger(__name__)

BASE_URL = "https://blinkit.com"
LISTING_PATH = "/v1/layout/listing_widgets"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# Headers the consumer web app attaches to its /v1 calls. We copy these off a real request the
# page makes after the location is set, so app_version etc. track whatever Blinkit ships today.
API_HEADER_KEYS = {
    "app_client", "platform", "auth_key", "device_id", "lat", "lon", "session_uuid",
    "web_app_version", "app_version", "rn_bundle_version", "access_token",
    "x-age-consent-granted",
}


class BlinkitError(RuntimeError):
    pass


class LocationNotServiceable(BlinkitError):
    pass


@dataclass
class ResolvedLocation:
    pincode: str
    search_text: str
    lat: float | None = None
    lon: float | None = None
    locality: str | None = None
    landmark: str | None = None
    merchant_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.lat is not None and self.merchant_id is not None


@dataclass
class FetchStats:
    requests: int = 0
    retries: int = 0
    failures: int = 0
    statuses: dict[int, int] = field(default_factory=dict)


class BlinkitBrowser:
    """Owns the Playwright process + one Chromium instance. Use as a context manager."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._pw: Playwright | None = None
        self.browser: Browser | None = None

    def __enter__(self) -> "BlinkitBrowser":
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(headless=self.headless)
        return self

    def __exit__(self, *exc):
        try:
            if self.browser:
                self.browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def session(self, *, delay_range: tuple[float, float] = (0.8, 2.0)) -> "BlinkitSession":
        assert self.browser is not None
        context = self.browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1280, "height": 900},
            locale="en-IN",
            timezone_id="Asia/Kolkata",
        )
        return BlinkitSession(context, delay_range=delay_range)


class BlinkitSession:
    def __init__(self, context: BrowserContext, *, delay_range: tuple[float, float]):
        self.context = context
        self.page: Page = context.new_page()
        self.delay_range = delay_range
        self.stats = FetchStats()
        self._api_headers: dict[str, str] = {}
        self.location: ResolvedLocation | None = None
        self.page.on("request", self._capture_api_headers)

    # ------------------------------------------------------------------ lifecycle
    def close(self):
        try:
            self.context.close()
        except Exception:  # pragma: no cover - best effort
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _sleep(self, lo: float | None = None, hi: float | None = None):
        a, b = self.delay_range if lo is None else (lo, hi if hi is not None else lo)
        time.sleep(random.uniform(a, b))

    # ------------------------------------------------------------------ header sniffing
    def _capture_api_headers(self, request):
        url = request.url
        if "blinkit.com/v1/" not in url and "blinkit.com/v2/" not in url:
            return
        headers = request.headers
        if "auth_key" not in headers:
            return
        self._api_headers = {k: v for k, v in headers.items() if k in API_HEADER_KEYS}

    def _headers_for_fetch(self) -> dict[str, str]:
        h = dict(self._api_headers)
        if not h:
            # Fallback: rebuild from localStorage if we never saw an app request.
            ls = self.page.evaluate(
                "() => ({authKey: localStorage.getItem('authKey'), deviceId: localStorage.getItem('deviceId'),"
                " location: localStorage.getItem('location')})"
            )
            coords = {}
            try:
                coords = json.loads(ls.get("location") or "{}").get("coords") or {}
            except Exception:
                pass
            h = {
                "app_client": "consumer_web",
                "platform": "desktop_web",
                "auth_key": ls.get("authKey") or "",
                "device_id": ls.get("deviceId") or "",
                "lat": str(coords.get("lat", "")),
                "lon": str(coords.get("lon", "")),
                "access_token": "null",
            }
        if self.location and self.location.lat is not None:
            h["lat"] = str(self.location.lat)
            h["lon"] = str(self.location.lon)
        h["content-type"] = "application/json"
        h["accept"] = "application/json, text/plain, */*"
        return h

    # ------------------------------------------------------------------ location
    def set_location(self, pincode: str, search_text: str | None = None, *, attempts: int = 2) -> ResolvedLocation:
        search_text = search_text or pincode
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                loc = self._set_location_once(pincode, search_text)
                if not loc.ok:
                    raise LocationNotServiceable(
                        f"{pincode}: Blinkit resolved no store (lat={loc.lat}, merchant={loc.merchant_id}, locality={loc.locality!r})"
                    )
                self.location = loc
                log.info("location set %s -> %s (%.5f, %.5f) merchant=%s", pincode, loc.locality, loc.lat, loc.lon, loc.merchant_id)
                return loc
            except Exception as e:  # noqa: BLE001 - we want to retry anything here
                last_err = e
                log.warning("set_location attempt %d/%d for %s failed: %s", attempt, attempts, pincode, e)
                self._sleep(2, 4)
        raise BlinkitError(f"could not set location for {pincode}: {last_err}")

    def _set_location_once(self, pincode: str, search_text: str) -> ResolvedLocation:
        page = self.page
        page.goto(BASE_URL + "/", wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(2_000)

        box = page.locator('input[placeholder="search delivery location"]')
        if box.count() == 0:
            # Location already set in this context (or modal closed): open the picker from the header.
            header = page.get_by_text(re.compile(r"select location|delivery in", re.I)).first
            header.click(timeout=5_000)
            page.wait_for_timeout(1_000)
            box = page.locator('input[placeholder="search delivery location"]')
        box.wait_for(state="visible", timeout=10_000)
        box.click()
        box.fill("")
        box.type(search_text, delay=random.randint(60, 120))
        page.wait_for_timeout(2_500)

        # Prefer a suggestion that mentions the pincode; else take the first suggestion row.
        suggestion = page.get_by_text(pincode, exact=False).first
        if suggestion.count() == 0:
            suggestion = page.locator("div[class*='LocationSearchList'] >> nth=0")
        suggestion.click(timeout=8_000)
        page.wait_for_timeout(3_500)

        state = page.evaluate(
            """() => {
                const g = k => { try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { return null; } };
                return { location: g('location'), merchant: g('merchant') };
            }"""
        )
        coords = ((state or {}).get("location") or {}).get("coords") or {}
        merchant = ((state or {}).get("merchant") or {}).get("id")
        cookies = {c["name"]: c["value"] for c in self.context.cookies()}
        lat = coords.get("lat") or _float_or_none(cookies.get("gr_1_lat"))
        lon = coords.get("lon") or _float_or_none(cookies.get("gr_1_lon"))
        return ResolvedLocation(
            pincode=pincode,
            search_text=search_text,
            lat=_float_or_none(lat),
            lon=_float_or_none(lon),
            locality=coords.get("locality") or cookies.get("gr_1_locality"),
            landmark=coords.get("landmark"),
            merchant_id=int(merchant) if merchant not in (None, "") else None,
        )

    # ------------------------------------------------------------------ fetch
    def fetch_json(self, path: str, body: dict[str, Any] | None = None, *, method: str = "POST",
                   retries: int = 3) -> dict[str, Any]:
        url = path if path.startswith("http") else BASE_URL + path
        js = """async ({url, method, headers, body}) => {
            const r = await fetch(url, {method, headers, body: body === null ? undefined : body, credentials: 'include'});
            const text = await r.text();
            return {status: r.status, text};
        }"""
        payload = {
            "url": url,
            "method": method,
            "headers": self._headers_for_fetch(),
            "body": json.dumps(body) if body is not None else None,
        }
        delay = 2.0
        last = None
        for attempt in range(1, retries + 1):
            self.stats.requests += 1
            try:
                res = self.page.evaluate(js, payload)
            except PWTimeout as e:
                res = {"status": -1, "text": f"evaluate timeout: {e}"}
            except Exception as e:  # noqa: BLE001
                res = {"status": -1, "text": f"evaluate error: {e}"}
            status = int(res.get("status", -1))
            self.stats.statuses[status] = self.stats.statuses.get(status, 0) + 1
            if status == 200:
                try:
                    data = json.loads(res["text"])
                except json.JSONDecodeError as e:
                    last = f"bad json: {e}"
                else:
                    if data.get("is_success", True):
                        return data
                    last = f"is_success=false: {str(data)[:200]}"
            else:
                last = f"http {status}: {res.get('text', '')[:200]}"
            self.stats.retries += 1
            log.warning("fetch %s attempt %d/%d failed (%s)", url[:120], attempt, retries, last)
            time.sleep(delay + random.uniform(0, 1.0))
            delay *= 2
        self.stats.failures += 1
        raise BlinkitError(f"fetch failed after {retries} attempts: {url[:160]} :: {last}")

    # ------------------------------------------------------------------ listing pagination
    def iter_collection_pages(self, *, collection_uuid: str, group_id: int,
                              max_pages: int = 80) -> Iterator[tuple[int, dict[str, Any]]]:
        """Yield (page_index, response_json) for one grouping, following pagination.next_url.

        Page 1 mirrors what the /dc/<slug> page sends: just the group id + collection uuid.
        Later pages POST to pagination.next_url and echo back `postback_params`, which is what
        the app does — the server uses postback_meta.processedGroupIds to avoid repeats.
        """
        body: dict[str, Any] = {"collection_group_id": str(group_id), "collection_uuid": collection_uuid}
        url: str | None = LISTING_PATH
        page_index = 0
        while url and page_index < max_pages:
            data = self.fetch_json(url, body)
            yield page_index, data
            pagination = (data.get("response") or {}).get("pagination") or {}
            next_url = pagination.get("next_url")
            if not next_url:
                return
            qs = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(next_url).query, keep_blank_values=True))
            postback = data.get("postback_params") or {}
            body = {**qs, **postback, "is_subsequent_page": True}
            url = next_url
            page_index += 1
            self._sleep()


def _float_or_none(v) -> float | None:
    try:
        return float(v) if v not in (None, "", "undefined") else None
    except (TypeError, ValueError):
        return None
