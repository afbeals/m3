# -----------------------------------------------------------------------------
# app/scrape.py
#
# Shared HTML fetch helper for plugins that scrape web pages instead of
# calling a JSON API.
#
# Why a shared helper?
#   Every HTML plugin needs the same retry / timeout / redirect / user-agent
#   behaviour. Centralising it here means a single fix (e.g. backoff tuning)
#   improves all HTML plugins at once.  Site-specific selector logic stays in
#   each plugin — this module never touches the DOM.
#
# Usage in a plugin:
#   from app.scrape import fetch_html, ScrapeError, SelectorMissingError
#
#   html = fetch_html(f"https://mysite.com/scenes/{scene_id}")
#   soup = BeautifulSoup(html, "html.parser")
#   el = soup.select_one("h1.scene-title")
#   if not el:
#       raise SelectorMissingError("title not found at h1.scene-title")
#   title = el.get_text(strip=True)
# -----------------------------------------------------------------------------

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

# Maximum download size for an HTML page (10 MB). Pages larger than this are
# almost certainly not a scene detail page — reject them to prevent OOM.
_MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# Retry settings: exponential backoff 2s, 4s between attempts.
_DEFAULT_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2.0

# A realistic browser-like User-Agent. Some CDNs and anti-scraping layers block
# the default "python-httpx/…" UA string; this bypasses most of them.
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class ScrapeError(Exception):
    """
    Raised when an HTML fetch fails permanently (HTTP error, wrong content-type,
    empty body, size exceeded).  Plugins should let this propagate; main.py
    records it as status="scrape_error" in the run report.
    """


class SelectorMissingError(ScrapeError):
    """
    Raised by a plugin when an expected CSS selector or tag is absent in the
    parsed HTML.  This typically means the site changed its markup.  Include
    the selector in the message so the run report tells the user exactly what
    broke, e.g.:
        raise SelectorMissingError("title not found at h1.scene-title")
    """


def fetch_html(
    url: str,
    *,
    timeout: float = 30.0,
    max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
) -> str:
    """
    Fetch a URL and return the response body as a UTF-8 string.

    - Follows redirects automatically.
    - Sets a browser-like User-Agent to avoid bot-blocking CDNs.
    - Retries up to `max_attempts` times with exponential backoff.
    - Raises ScrapeError on:
        - HTTP 4xx / 5xx after all retries
        - Response body missing a text/html Content-Type
        - Empty response body
        - Response body larger than _MAX_RESPONSE_BYTES (10 MB)
    """
    last_exc: Exception | None = None

    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers={"User-Agent": _USER_AGENT},
    ) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                r = client.get(url)
                r.raise_for_status()

                content_type = r.headers.get("content-type", "")
                if not content_type or "text/html" not in content_type:
                    raise ScrapeError(
                        f"Expected text/html but got {content_type!r} from {url}"
                    )

                if len(r.content) > _MAX_RESPONSE_BYTES:
                    raise ScrapeError(
                        f"Response too large ({len(r.content)} bytes) from {url}"
                    )

                text = r.text
                if not text.strip():
                    raise ScrapeError(f"Empty response body from {url}")

                return text

            except ScrapeError:
                raise  # don't retry on our own validation errors

            except Exception as exc:
                last_exc = exc
                if attempt < max_attempts:
                    wait = _BACKOFF_BASE ** attempt
                    logger.warning(
                        "fetch_html attempt %d/%d failed for %s (%s); retrying in %.0fs",
                        attempt, max_attempts, url, exc, wait,
                    )
                    time.sleep(wait)

    raise ScrapeError(
        f"Failed to fetch {url} after {max_attempts} attempts: {last_exc}"
    )
