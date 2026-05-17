"""
Example HTML Plugin — ExampleHTMLSite

This file demonstrates how to write a plugin for pm when the target site
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

from bs4 import BeautifulSoup

from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename
from app.scrape import ScrapeError, SelectorMissingError, fetch_html

logger = logging.getLogger(__name__)

BASE_URL = "https://example-html-site.com"


class ExampleHTMLPlugin(MetadataPlugin):
    site_id = "examplehtml"
    aliases = ["EH"]

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

        url = f"{BASE_URL}/scenes/{scene_id}"
        # fetch_html raises ScrapeError on HTTP errors; let it propagate so
        # main.py records it as status="scrape_error" in the run report.
        html = fetch_html(url)
        return self._parse_detail_page(html, url)

    # ------------------------------------------------------------------
    # Search — fetch search results, pick the first match, then scrape detail
    # ------------------------------------------------------------------

    def _fetch_by_search(self, parsed: ParsedFilename) -> MetadataResult | None:
        query = parsed.title or (parsed.actors[0] if parsed.actors else None)
        if not query:
            logger.warning("[examplehtml] No title or actor to search with")
            return None

        search_url = f"{BASE_URL}/search?q={query}"
        html = fetch_html(search_url)
        soup = BeautifulSoup(html, "html.parser")

        # Example selector: search results are <a class="result-link"> elements
        first_result = soup.select_one("a.result-link")
        if not first_result:
            # No results — the query just didn't match anything. Return None
            # (not a ScrapeError) so main.py records this as "unmatched".
            logger.info("[examplehtml] No search results for query %r", query)
            return None

        detail_url = BASE_URL + first_result["href"]
        detail_html = fetch_html(detail_url)
        return self._parse_detail_page(detail_html, detail_url)

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail_page(self, html: str, source_url: str) -> MetadataResult:
        soup = BeautifulSoup(html, "html.parser")

        # ---- Title ----
        title_el = soup.select_one("h1.scene-title")
        if not title_el:
            # Raise SelectorMissingError so the run report names the broken
            # selector. This tells you exactly what to update if the site
            # changes its markup.
            raise SelectorMissingError("title not found at h1.scene-title")
        title = title_el.get_text(strip=True)

        # ---- Summary / plot ----
        summary_el = soup.select_one("div.scene-description")
        summary = summary_el.get_text(strip=True) if summary_el else None

        # ---- Year ----
        year_el = soup.select_one("span.release-year")
        year: int | None = None
        if year_el:
            try:
                year = int(year_el.get_text(strip=True)[:4])
            except ValueError:
                pass

        # ---- Rating ----
        rating_el = soup.select_one("span.rating-value")
        rating: float | None = None
        if rating_el:
            try:
                rating = float(rating_el.get_text(strip=True))
            except ValueError:
                pass

        # ---- Genres ----
        genres = [
            el.get_text(strip=True)
            for el in soup.select("a.genre-tag")
        ]

        # ---- Actors ----
        actors = [
            el.get_text(strip=True)
            for el in soup.select("a.performer-name")
        ]

        # ---- Images ----
        poster_el = soup.select_one("img.poster-image")
        poster_url = poster_el["src"] if poster_el else None

        fanart_el = soup.select_one("img.fanart-image")
        fanart_url = fanart_el["src"] if fanart_el else None

        # ---- Scene ID for traceability ----
        # Example: <meta name="scene-id" content="12345">
        id_el = soup.select_one('meta[name="scene-id"]')
        source_id = id_el["content"] if id_el else None

        return MetadataResult(
            title=title or "Unknown Title",
            summary=summary,
            year=year,
            rating=rating,
            genres=genres,
            actors=actors,
            poster_url=poster_url,
            fanart_url=fanart_url,
            source_url=source_url,
            source_id=str(source_id) if source_id is not None else None,
        )
