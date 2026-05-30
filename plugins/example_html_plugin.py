"""
Example HTML Plugin — ExampleHTMLSite  (HTML scraping template)
===============================================================
PRIMARY TEMPLATE: Copy this file when the target site has NO JSON API.
For sites that DO have a JSON API, copy example_plugin.py instead.

How to use this template
------------------------
1.  cp plugins/example_html_plugin.py plugins/mysite.py
2.  Change ``site_id = "examplehtml"`` → your token.
3.  Set ``BASE_URL`` to your site's root URL.
4.  Update the CSS selectors in ``_SEL`` to match the site's real markup.
    (Use browser DevTools → Inspect to find the right selectors.)
5.  Update ``_parse_detail_page()`` to extract the fields your site has.
6.  Validate: ``python -m app.main --validate-plugins``
7.  Test:     ``python -m app.main --test-plugin plugins/mysite.py
                  --filename "Jane Doe % mysite - 12345.mp4"``

----------------------------------------------------------------------
KEY DIFFERENCES vs. example_plugin.py (JSON API):
  - Import ``fetch_html``, ``ScrapeError``, ``SelectorMissingError`` from app.scrape.
  - Use BeautifulSoup to parse HTML (bs4 is pre-installed).
  - Raise ``SelectorMissingError`` with the exact selector when a REQUIRED
    element is missing — this surfaces in the run report so you know which
    selector broke if the site changes its markup.
  - For OPTIONAL elements (no selector miss = no value), silently use None.
  - No ``setup()`` needed for public sites — HTML plugins typically fetch
    publicly accessible pages without credentials.

----------------------------------------------------------------------
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

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# ADAPT: change to your site's root URL (used for urljoin on relative hrefs).
BASE_URL = "https://example-html-site.com"

# ---------------------------------------------------------------------------
# CSS selector registry — update ONLY THIS DICT when the site changes markup
# ---------------------------------------------------------------------------

# Centralising selectors here (rather than scattering them through the parse
# method) means a site redesign requires exactly ONE edit per selector, and the
# name makes it obvious what the selector is supposed to target.
#
# HOW TO FIND SELECTORS:
#   1. Open the site in Chrome/Firefox.
#   2. Right-click the element you want → "Inspect".
#   3. In DevTools, right-click the element in the DOM → "Copy" → "Copy selector".
#   4. Simplify the copied selector to just the meaningful class/ID parts.
#      (DevTools often copies over-qualified selectors like "div > div > h1",
#       which break if the site adds one wrapper div. Use "h1.scene-title" instead.)
#
# PITFALL: Avoid ``nth-child`` or positional selectors — they break on the first
# site redesign. Prefer stable classes and IDs.
_SEL = {
    "title":         "h1.scene-title",          # ADAPT: selector for the scene title element
    "summary":       "div.scene-description",   # ADAPT: selector for plot/description block
    "year":          "span.release-year",        # ADAPT: selector for a text node containing the year
    "rating":        "span.rating-value",        # ADAPT: selector for a numeric rating string
    "genres":        "a.genre-tag",              # ADAPT: selector for one-or-more genre link elements
    "actors":        "a.performer-name",         # ADAPT: selector for one-or-more performer link elements
    "poster":        "img.poster-image",         # ADAPT: selector for the poster <img>
    "fanart":        "img.fanart-image",         # ADAPT: selector for the fanart/background <img>
    "scene_id_meta": 'meta[name="scene-id"]',   # ADAPT: selector for a meta tag holding the scene ID
    "result_link":   "a.result-link",            # ADAPT: selector for search-result links
    # New fields — adjust selectors to match your site's actual markup
    "release_date":  "span.release-date",        # ADAPT: selector for the full release date string
    "directors":     "a.director-name",          # ADAPT: selector for one-or-more director link elements
    "studio":        "span.studio-name",         # ADAPT: selector for the studio name text
}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class ExampleHTMLPlugin(MetadataPlugin):
    # -----------------------------------------------------------------------
    # Class-level identity — MUST adapt for your plugin
    # -----------------------------------------------------------------------

    # Token that must appear in filenames: ``% examplehtml - ...``
    site_id = "examplehtml"

    # Use tuple (not list) — class-level defaults must be immutable.
    # A mutable list default is shared across ALL subclasses.
    aliases = ("EH",)

    # -----------------------------------------------------------------------
    # Lifecycle: connection management
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        # WHY A BROWSER USER-AGENT IS REQUIRED:
        #   Most sites block the default python-httpx/x.x.x User-Agent with a
        #   403 or a CAPTCHA redirect. Using a realistic browser UA bypasses
        #   the most common scraping filters. If the site still blocks you, try
        #   rotating UAs or adding Referer/Accept-Language headers.
        #
        # PITFALL: If you see 403s or empty HTML responses, the User-Agent is
        #   almost always the cause. Compare the response you get to what the
        #   browser gets using ``curl -A "Mozilla/..." URL``.
        self._client = httpx.Client(
            follow_redirects=True,
            timeout=30,                                # longer than API plugins — HTML is heavier
            limits=httpx.Limits(keepalive_expiry=30),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                # Accept-Language: ensures the site serves English content
                # rather than redirecting to a localised version based on your
                # server's IP — especially important for detail page text fields.
                # Uncomment and add here if needed:
                # "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def close(self) -> None:
        """Release the underlying connection pool.

        Called by the plugin loader during hot-reload. Without this, file
        descriptors accumulate on each reload.
        """
        self._client.close()

    # __enter__/__exit__ allow using the plugin as a context manager in tests:
    #   with ExampleHTMLPlugin() as p:
    #       result = p.fetch(parsed)
    def __enter__(self) -> "ExampleHTMLPlugin":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # NOTE: No setup() needed for public sites. HTML scraping plugins typically
    # fetch publicly accessible pages without credentials. If your site requires
    # login, add a setup() method here to validate session cookies or API tokens.

    # -----------------------------------------------------------------------
    # Primary dispatch — fetch()
    # -----------------------------------------------------------------------

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Entry point called by the m3 router for every file this plugin owns.

        WHAT IT RECEIVES:
          ``parsed`` is a ParsedFilename with fields set by the filename parser.
          For HTML plugins, the most relevant fields are:
            parsed.match_subtype  — "exact" | "enhanced" | "limited"
            parsed.scene_id       — numeric ID or slug, or None
            parsed.direct_url     — URL slug (e.g. "eager-hands"), or None
            parsed.title          — text title from the filename, or None

        HTML PLUGINS OFTEN UNIFY "enhanced" AND "limited":
          Because HTML scraping searches are all "go to the search page and
          pick the first result", enhanced and limited usually share the same
          code path (both call _fetch_by_search). Contrast with JSON API plugins
          where enhanced can pass extra search params (date, actor ID) that the
          API understands — HTML search pages typically only accept a text query.

        RETURN / RAISE CONTRACT — same as JSON API plugins:
          - MetadataResult  → success ("updated")
          - None            → not found ("unmatched") — do NOT raise
          - ScrapeError     → network failure ("scrape_error")
          - SelectorMissingError → broken selector ("scrape_error")
          - Anything else   → plugin crash ("error")
        """
        logger.info(
            "[examplehtml] fetch called | subtype=%s scene_id=%s title=%r",
            parsed.match_subtype,
            parsed.scene_id,
            parsed.title,
        )

        subtype = parsed.match_subtype
        if subtype == "exact":
            # Direct fetch by ID — fastest, most accurate.
            return self._fetch_by_id(parsed)
        elif subtype in ("limited", "enhanced"):
            # Both limited and enhanced go through the same search path for HTML
            # plugins because HTML search pages accept only a text query — there
            # are no API params to pass date or actor filters through.
            return self._fetch_by_search(parsed)
        else:
            logger.warning("[examplehtml] Unsupported match subtype: %s", subtype)
            return None

    # -----------------------------------------------------------------------
    # Strategy 1: Exact — fetch the detail page directly by scene ID
    # -----------------------------------------------------------------------

    def _fetch_by_id(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Load the scene detail page directly using the ID or slug.

        WHY fetch_html INSTEAD OF self._client.get:
          ``fetch_html`` is the m3 scraping utility. It handles retries, raises
          ``ScrapeError`` on HTTP errors, and validates that the response looks
          like real HTML (not a bot-detection page). Using ``self._client.get``
          directly requires you to reproduce all of that error handling yourself.

          WHEN TO USE self._client.get directly:
            - When you need full control over the request (custom headers per
              request, session cookies, multi-step login flows).
            - See thetvdb.py for an example of using self._client.get directly
              with its own error handling.
        """
        scene_id = parsed.scene_id or parsed.direct_url
        if not scene_id:
            logger.warning("[examplehtml] Exact match but no scene_id or direct_url")
            return None

        # ADAPT: change the URL pattern to match your site's scene detail page structure.
        # Common patterns: /scenes/{id}, /videos/{slug}, /content/{id}
        url = f"{BASE_URL}/scenes/{quote(str(scene_id), safe='-_')}"
        # fetch_html raises ScrapeError on HTTP errors; let it propagate so
        # main.py records it as status="scrape_error" in the run report.
        html = fetch_html(url)
        return self._parse_detail_page(html, url)

    # -----------------------------------------------------------------------
    # Strategy 2: Search — find the scene, then scrape its detail page
    # -----------------------------------------------------------------------

    def _fetch_by_search(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Fetch via site search — used for both "limited" and "enhanced" subtypes.

        FLOW:
          1. Build a search query from the best available signal (title, then actors).
          2. Fetch the search results page.
          3. Extract the first matching result link.
          4. Fetch and parse the scene detail page for that link.

        WHY TWO HTTP REQUESTS:
          HTML search pages list results as links, not full metadata — you must
          follow the link to get the actual metadata. This is the unavoidable cost
          of HTML scraping vs. a JSON API (which can return full metadata in a
          single call).

        PITFALL: Return None (not raise) when the search returns zero results.
          "The site doesn't have this scene" is "unmatched", not "scrape_error".
          Raise ScrapeError only if the HTTP request itself failed, or
          SelectorMissingError if a required structural element is missing.
        """
        if parsed.title:
            query = parsed.title
        elif parsed.actors:
            # No title available — fall back to actor search as a last resort.
            # Warn because actor searches on most sites return performer pages,
            # not scene pages, so the first result is likely wrong.
            logger.warning(
                "[examplehtml] No title available for %r; searching by actor name %r — "
                "result may be wrong if the site returns performer pages for actor queries",
                parsed.site, parsed.actors[0],
            )
            query = parsed.actors[0]
        else:
            # Nothing to search with — no title and no actors.
            logger.warning(
                "[examplehtml] No title or actors available for %r — cannot search",
                parsed.site,
            )
            return None

        # ADAPT: change the search URL pattern to match your site.
        # Common patterns: /search?q=..., /find?query=..., /videos?search=...
        search_url = f"{BASE_URL}/search?q={quote_plus(query)}"
        html = fetch_html(search_url)
        soup = BeautifulSoup(html, "html.parser")

        # Example selector: search results are <a class="result-link"> elements.
        # ADAPT: change _SEL["result_link"] to match your site's search result markup.
        first_result = soup.select_one(_SEL["result_link"])
        if not first_result:
            # No results — the query just didn't match anything on this site.
            # Return None (not ScrapeError) so main.py records this as "unmatched".
            # PITFALL: Do NOT raise here — raising would mark this file as an error,
            # meaning it will never be retried with different metadata.
            logger.info("[examplehtml] No search results for query %r", query)
            return None

        href = first_result.get("href")
        if not href:
            # The result link exists in the DOM but has no href — unexpected markup.
            # Log a warning and return None rather than crashing.
            logger.warning("[examplehtml] Search result link has no href")
            return None

        # urljoin handles both absolute and relative hrefs correctly:
        #   urljoin("https://site.com", "/scenes/123")  → "https://site.com/scenes/123"
        #   urljoin("https://site.com", "https://site.com/scenes/123")  → unchanged
        detail_url = urljoin(BASE_URL, href)
        detail_html = fetch_html(detail_url)
        return self._parse_detail_page(detail_html, detail_url)

    # -----------------------------------------------------------------------
    # Detail page parser — THE MOST IMPORTANT METHOD TO ADAPT
    # -----------------------------------------------------------------------

    def _parse_detail_page(self, html: str, source_url: str) -> MetadataResult:
        """Extract MetadataResult from a scene detail page HTML string.

        THIS IS THE METHOD YOU WILL CHANGE MOST.
        Every site has different HTML structure. Your job is to find the right
        CSS selectors and text-extraction patterns for each field.

        REQUIRED vs OPTIONAL FIELDS:
          - REQUIRED (raise SelectorMissingError if missing): title.
            A missing title means the page is wrong — likely a bot wall, a 404
            that didn't return a 404 status, or a fundamental site change.
          - OPTIONAL (use None if missing): everything else.
            Fanart, actors, genres, rating, etc. may not be present on every
            page. Don't raise for optional fields — just set them to None or [].

        DATA FORMAT EXPECTATIONS:
          release_date → MUST be "YYYY-MM-DD" before passing to MetadataResult.
            MetadataResult.__post_init__ validates this; a wrong format clears
            the field with a warning rather than crashing.
          rating → float 0.0–10.0. Convert from whatever format the site uses.
          year → int. Can be omitted if release_date is set (auto-derived).
          actors, genres, directors → list[str], NOT list[Tag]. Always call
            .get_text(strip=True) and collect into a plain Python list of strings.

        WHY return MetadataResult (not MetadataResult | None) here:
          By the time we're parsing an HTML page, we've already committed to a
          URL. If the title is missing it means something is fundamentally broken
          (wrong page, site changed markup), so raising SelectorMissingError is
          more correct than returning None — it surfaces the broken selector in
          the run report.
        """
        soup = BeautifulSoup(html, "html.parser")

        # ==================================================================
        # Title (REQUIRED)
        # ==================================================================
        title_el = soup.select_one(_SEL["title"])
        if not title_el:
            # Raise SelectorMissingError (not a generic Exception) so the run
            # report names the broken selector. The message will appear in the
            # "message" column of the report so you know EXACTLY which selector
            # to update when the site changes its markup.
            raise SelectorMissingError("title not found at h1.scene-title")
        title = title_el.get_text(strip=True)
        if not title:
            # The element exists but is empty — still a fatal condition.
            raise SelectorMissingError(
                f"Title selector '{_SEL['title']}' matched but contained no text"
            )

        # ==================================================================
        # Summary / plot (optional)
        # ==================================================================
        summary_el = soup.select_one(_SEL["summary"])
        # Use None (not "") when the element is absent — MetadataResult
        # treats "" the same as a real value, but None means "not provided".
        summary = summary_el.get_text(strip=True) if summary_el else None

        # ==================================================================
        # Year (optional — auto-derived from release_date if omitted)
        # ==================================================================
        year_el = soup.select_one(_SEL["year"])
        year: int | None = None
        year_text = year_el.get_text(strip=True) if year_el else ""
        # Use re.match to extract just the 4-digit year, ignoring surrounding text.
        # Common year formats: "2024", "Released: 2024", "2024-03-15 (US)"
        year_match = re.match(r"\d{4}", year_text)
        year = int(year_match.group()) if year_match else None

        # ==================================================================
        # Rating (optional)
        # ==================================================================
        rating_el = soup.select_one(_SEL["rating"])
        rating: float | None = None
        if rating_el:
            raw_rating = rating_el.get_text(strip=True)
            # re.search (not re.match) because the number may not be at the start:
            #   "★ 8.5 / 10" → extracts "8.5"
            #   "7" → extracts "7"
            m = re.search(r"\d+(?:\.\d+)?", raw_rating)
            if m:
                try:
                    rating = float(m.group())
                except ValueError:
                    logger.warning("[examplehtml] Could not parse rating from %r", raw_rating)
            else:
                logger.warning("[examplehtml] No numeric value found in rating %r", raw_rating)
            # PITFALL: MetadataResult.__post_init__ validates that rating is 0–10.
            # If your site uses a 0–100 scale, divide by 10 here before returning.
            # If your site uses 0–5 stars, multiply by 2 to normalise to 0–10.

        # ==================================================================
        # Genres (optional, multi-value)
        # ==================================================================
        # dict.fromkeys(...) deduplicates while preserving insertion order.
        # The walrus operator `:=` lets us filter empty strings in one expression.
        # \xa0 is a non-breaking space that get_text() does NOT strip by default;
        # replace it to avoid "Action\xa0" ≠ "Action" comparisons downstream.
        genres = list(dict.fromkeys(
            name
            for el in soup.select(_SEL["genres"])
            if (name := el.get_text(strip=True).replace("\xa0", " ").strip())
        ))

        # ==================================================================
        # Actors (optional, multi-value)
        # ==================================================================
        # Same deduplication pattern as genres.
        # PITFALL: some sites have actor NAME in a nested span that get_text()
        # would include alongside role text ("Jane Doe as Nurse"). You may need
        # to select_one(".performer-name > span") or strip role text separately.
        actors = list(dict.fromkeys(
            name
            for el in soup.select(_SEL["actors"])
            if (name := el.get_text(strip=True).replace("\xa0", " ").strip())
        ))

        # ==================================================================
        # Release date (optional)
        # ==================================================================
        # ADAPT: your site may store the date in many formats:
        #   "March 15, 2024" → use datetime.strptime(raw, "%B %d, %Y").strftime("%Y-%m-%d")
        #   "15/03/2024"     → parse with strptime(raw, "%d/%m/%Y")
        #   "2024-03-15"     → already ISO, just validate with re.match
        #   ISO 8601 datetime "2024-03-15T12:00:00Z" → slice [:10]
        release_date_el = soup.select_one(_SEL["release_date"])
        release_date: str | None = None
        if release_date_el:
            raw_date = release_date_el.get_text(strip=True)
            # This example handles only pre-formatted ISO dates ("YYYY-MM-DD").
            # ADAPT: add datetime.strptime() parsing if your site uses other formats.
            m_date = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw_date)
            if m_date:
                release_date = m_date.group()   # "YYYY-MM-DD" — MetadataResult accepts this directly
            # If the date doesn't match, release_date stays None — MetadataResult won't crash.

        # ==================================================================
        # Directors (optional, multi-value)
        # ==================================================================
        directors = list(dict.fromkeys(
            el.get_text(strip=True)
            for el in soup.select(_SEL["directors"])
            if el.get_text(strip=True)
        ))

        # ==================================================================
        # Studio (optional, single value)
        # ==================================================================
        studio_el = soup.select_one(_SEL["studio"])
        studio = studio_el.get_text(strip=True) if studio_el else None

        # ==================================================================
        # Labels (Plex-only labels — optional)
        # ==================================================================
        # Uncomment and populate if your site has a concept like "labels" or
        # "networks" that maps to Plex labels (not genres):
        # labels: list[str] = []

        # ==================================================================
        # Images (optional)
        # ==================================================================

        # PITFALL: Many sites use lazy-loading. The real image URL is in
        # "data-src" or "data-lazy", not "src" (which holds a placeholder gif).
        # Always check data-src first, then fall back to src.
        poster_el = soup.select_one(_SEL["poster"])
        raw_poster = (
            poster_el.get("data-src")
            or poster_el.get("data-lazy")
            or poster_el.get("src")
        ) if poster_el else None
        # urljoin makes relative URLs absolute: "/images/poster.jpg" → "https://site.com/images/poster.jpg"
        poster_url = urljoin(BASE_URL, raw_poster) if raw_poster else None

        fanart_el = soup.select_one(_SEL["fanart"])
        raw_fanart = (
            fanart_el.get("data-src")
            or fanart_el.get("data-lazy")
            or fanart_el.get("src")
        ) if fanart_el else None
        fanart_url = urljoin(BASE_URL, raw_fanart) if raw_fanart else None

        # ==================================================================
        # Scene ID (optional — for traceability in NFO <uniqueid>)
        # ==================================================================
        # ADAPT: your site may embed the ID in a different element.
        # Common patterns:
        #   <meta name="scene-id" content="12345">  → meta[name="scene-id"]["content"]
        #   <div data-scene-id="12345">             → div[data-scene-id]
        #   the URL itself (parse from source_url)  → re.search(r"/scenes/(\d+)", source_url)
        id_el = soup.select_one(_SEL["scene_id_meta"])
        source_id = id_el.get("content") if id_el else None

        # ==================================================================
        # Assemble result
        # ==================================================================
        return MetadataResult(
            title=title,
            summary=summary,
            year=year,
            release_date=release_date,   # "YYYY-MM-DD" or None
            rating=rating,               # float 0.0–10.0, or None
            genres=genres,               # list[str], may be empty
            actors=actors,               # list[str], may be empty
            directors=directors,         # list[str], may be empty
            studio=studio,
            poster_url=poster_url,
            fanart_url=fanart_url,
            source_url=source_url,       # canonical URL of this scene's detail page
            # Convert source_id to str only when actually present.
            # PITFALL: str(None) = "None" (the literal string) — __post_init__
            # catches it and clears it, but it's cleaner to guard explicitly.
            source_id=str(source_id) if source_id is not None else None,
        )
