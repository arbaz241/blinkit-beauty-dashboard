"""Blinkit beauty scraper.

Playwright drives a real (headless) Chromium so Cloudflare's bot check passes, then the crawl
replays Blinkit's own JSON endpoints (/v1/layout/listing_widgets) from inside the page, so every
request carries the same cookies, auth_key and lat/lon headers the consumer web app sends.
"""
