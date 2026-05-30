"""
Example HTML Plugin — ExampleHTMLSite

This file demonstrates how to write a plugin for m3 when the target site
does NOT have a JSON API — you have to scrape the HTML page directly.

Copy it, rename it to your site's name (e.g., mysite_scraper.py), and fill
in the real CSS selectors and URLs for your target site.

Drop the finished file into the plugins/ directory (mounted at /plugins in
the container) and restart the container to pick it up.

----------------------------------------------------------------------
Key differences from example_plugin.py (JSON API):
  - Import fetch_html, ScrapeError, SelectorMissingError from app.scrape
  - Use BeautifulSoup to parse the HTML (bs4 is installed as a dependency)
  - Raise SelectorMissingError with the exact selector when an expected
    element is missing — this appears in the run report so you know which
    selector broke if the site changes its markup

Filename examples this plugin handles (site_id = "examplehtml"):
  Jane Doe % examplehtml - 12345          (exact match by scene ID)
  Jane Doe % examplehtml - An Interesting Plot  (limited search by title)
----------------------------------------------------------------------
"""

import logging
import re
from urllib.parse import quote, quote_plus, urljoin

import httpx
from bs4 import BeautifulSoup

from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename
from app.scrape import ScrapeError, SelectorMissingError, fetch_html

logger = logging.getLogger(__name__)

BASE_URL = "https://example-html-site.com"

# All CSS selectors in one place — update here when the site changes its markup.
_SEL = {
    "title":         "h1.scene-title",
    "summary":       "div.scene-description",
    "year":          "span.release-year",
    "rating":        "span.rating-value",
    "genres":        "a.genre-tag",
    "actors":        "a.performer-name",
    "poster":        "img.poster-image",
    "fanart":        "img.fanart-image",
    "scene_id_meta": 'meta[name="scene-id"]',
    "result_link":   "a.result-link",
    # New fields — adjust selectors to match your site's actual markup
    "release_date":  "span.release-date",
    "directors":     "a.director-name",
    "studio":        "span.studio-name",
}


class ExampleHTMLPlugin(MetadataPlugin):
    site_id = "examplehtml"
    aliases = ("EH",)  # Use tuple (not list) — class-level defaults must be immutable

    def __init__(self) -> None:
        # Most sites block the default python-httpx/x.x.x User-Agent with 403
        # or CAPTCHA pages. Using a browser UA is required for HTML scraping.
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=30,
            limits=httpx.Limits(keepalive_expiry=30),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ExampleHTMLPlugin":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        logger.info(
            "[examplehtml] fetch called | subtype=%s scene_id=%s title=%r",
            parsed.match_subtype,
            parsed.scene_id,
            parsed.title,
        )

        subtype = parsed.match_subtype
        if subtype == "exact":
            return self._fetch_by_id(parsed)
        elif subtype in ("limited", "enhanced"):
            return self._fetch_by_search(parsed)
        else:
            logger.warning("[examplehtml] Unsupported match subtype: %s", subtype)
            return None

    # ------------------------------------------------------------------
    # Exact match — fetch the scene detail page directly by ID
    # ------------------------------------------------------------------

    def _fetch_by_id(self, parsed: ParsedFilename) -> MetadataResult | None:
        scene_id = parsed.scene_id or parsed.direct_url
        if not scene_id:
            logger.warning("[examplehtml] Exact match but no scene_id or direct_url")
            return None

        url = f"{BASE_URL}/scenes/{quote(str(scene_id), safe='-_')}"
        # fetch_html raises ScrapeError on HTTP errors; let it propagate so
        # main.py records it as status="scrape_error" in the run report.
        html = fetch_html(url)
        return self._parse_detail_page(html, url)

    # ------------------------------------------------------------------
    # Search — fetch search results, pick the first match, then scrape detail
    # ------------------------------------------------------------------

    def _fetch_by_search(self, parsed: ParsedFilename) -> MetadataResult | None:
        if parsed.title:
            query = parsed.title
        elif parsed.actors:
            logger.warning(
                "[examplehtml] No title available for %r; searching by actor name %r — "
                "result may be wrong if the site returns performer pages for actor queries",
                parsed.site, parsed.actors[0],
            )
            query = parsed.actors[0]
        else:
            logger.warning(
                "[examplehtml] No title or actors available for %r — cannot search",
                parsed.site,
            )
            return None

        search_url = f"{BASE_URL}/search?q={quote_plus(query)}"
        html = fetch_html(search_url)
        soup = BeautifulSoup(html, "html.parser")

        # Example selector: search results are <a class="result-link"> elements
        first_result = soup.select_one(_SEL["result_link"])
        if not first_result:
            # No results — the query just didn't match anything. Return None
            # (not a ScrapeError) so main.py records this as "unmatched".
            logger.info("[examplehtml] No search results for query %r", query)
            return None

        href = first_result.get("href")
        if not href:
            logger.warning("[examplehtml] Search result link has no href")
            return None
        detail_url = urljoin(BASE_URL, href)
        detail_html = fetch_html(detail_url)
        return self._parse_detail_page(detail_html, detail_url)

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail_page(self, html: str, source_url: str) -> MetadataResult:
        soup = BeautifulSoup(html, "html.parser")

        # ---- Title ----
        title_el = soup.select_one(_SEL["title"])
        if not title_el:
            # Raise SelectorMissingError so the run report names the broken
            # selector. This tells you exactly what to update if the site
            # changes its markup.
            raise SelectorMissingError("title not found at h1.scene-title")
        title = title_el.get_text(strip=True)
        if not title:
            raise SelectorMissingError(
                f"Title selector '{_SEL['title']}' matched but contained no text"
            )

        # ---- Summary / plot ----
        summary_el = soup.select_one(_SEL["summary"])
        summary = summary_el.get_text(strip=True) if summary_el else None

        # ---- Year ----
        year_el = soup.select_one(_SEL["year"])
        year: int | None = None
        year_text = year_el.get_text(strip=True) if year_el else ""
        year_match = re.match(r"\d{4}", year_text)
        year = int(year_match.group()) if year_match else None

        # ---- Rating ----
        rating_el = soup.select_one(_SEL["rating"])
        rating: float | None = None
        if rating_el:
            raw_rating = rating_el.get_text(strip=True)
            m = re.search(r"\d+(?:\.\d+)?", raw_rating)
            if m:
                try:
                    rating = float(m.group())
                except ValueError:
                    logger.warning("[examplehtml] Could not parse rating from %r", raw_rating)
            else:
                logger.warning("[examplehtml] No numeric value found in rating %r", raw_rating)

        # ---- Genres ----
        genres = list(dict.fromkeys(
            name
            for el in soup.select(_SEL["genres"])
            if (name := el.get_text(strip=True).replace("\xa0", " ").strip())
        ))

        # ---- Actors ----
        actors = list(dict.fromkeys(
            name
            for el in soup.select(_SEL["actors"])
            if (name := el.get_text(strip=True).replace("\xa0", " ").strip())
        ))

        # ---- Release date (from <span class="release-date"> or similar) ----
        # Adjust selector to match your site's HTML
        release_date_el = soup.select_one(_SEL["release_date"])
        release_date: str | None = None
        if release_date_el:
            raw_date = release_date_el.get_text(strip=True)
            # Parse "March 15, 2024" → "2024-03-15" or handle ISO format directly
            m_date = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw_date)
            if m_date:
                release_date = m_date.group()

        # ---- Directors (from <a class="director-name"> elements) ----
        directors = list(dict.fromkeys(
            el.get_text(strip=True)
            for el in soup.select(_SEL["directors"])
            if el.get_text(strip=True)
        ))

        # ---- Studio (from <span class="studio-name">) ----
        studio_el = soup.select_one(_SEL["studio"])
        studio = studio_el.get_text(strip=True) if studio_el else None

        # ---- Labels (Plex/Kodi labels — site-specific) ----
        # labels: list[str] = []  # Uncomment and populate if your site provides labels

        # ---- Images ----
        poster_el = soup.select_one(_SEL["poster"])
        # Try data-src first (used by lazy-loading sites), then src
        raw_poster = (
            poster_el.get("data-src")
            or poster_el.get("data-lazy")
            or poster_el.get("src")
        ) if poster_el else None
        poster_url = urljoin(BASE_URL, raw_poster) if raw_poster else None

        fanart_el = soup.select_one(_SEL["fanart"])
        # Try data-src first (used by lazy-loading sites), then src
        raw_fanart = (
            fanart_el.get("data-src")
            or fanart_el.get("data-lazy")
            or fanart_el.get("src")
        ) if fanart_el else None
        fanart_url = urljoin(BASE_URL, raw_fanart) if raw_fanart else None

        # ---- Scene ID for traceability ----
        # Example: <meta name="scene-id" content="12345">
        id_el = soup.select_one(_SEL["scene_id_meta"])
        source_id = id_el.get("content") if id_el else None

        return MetadataResult(
            title=title,
            summary=summary,
            year=year,
            release_date=release_date,
            rating=rating,
            genres=genres,
            actors=actors,
            directors=directors,
            studio=studio,
            poster_url=poster_url,
            fanart_url=fanart_url,
            source_url=source_url,
            source_id=str(source_id) if source_id is not None else None,
        )
