"""
TheTVDB HTML Scraper Plugin
===========================
Scrapes movie metadata directly from www.thetvdb.com without requiring an
API key. Uses the public movie pages (e.g. /movies/the-mummy-362775).

No credentials required — the site is publicly accessible.

----------------------------------------------------------------------
Filename examples this plugin handles (site_id = "thetvdb"):

  Exact (TVDB numeric ID in the filename):
    The Mummy % thetvdb - 362775
    The Mummy % thetvdb - 2026-04-17 - 362775

  Exact (URL slug from the movie's page URL):
    The Mummy % thetvdb - the-mummy-362775

  Enhanced (ID found, title available for validation):
    The Mummy % thetvdb - 362775 - The Mummy

  Limited (title-only search — uses TVDB site search):
    The Mummy % thetvdb - The Mummy

URL slug tip: copy the path segment from the movie URL, e.g.
  https://www.thetvdb.com/movies/the-mummy-362775
                                  ^^^^^^^^^^^^^^^^ ← paste this as the slug

IMPORTANT: Bare numeric IDs (scene_id without a slug) are NOT usable on
TVDB because the URL path requires the full slug (title + ID). Always
use the full slug from the movie URL. See _resolve_slug() for details.
----------------------------------------------------------------------

HTML structure verified against:
  https://www.thetvdb.com/movies/the-mummy-362775
  (scraped 2026-05-29)

Key selectors:
  Title:       h1#series_title
  Overview:    #translations [data-language="eng"] p  (first non-empty)
  Poster:      img.img-responsive.img-rounded (sidebar)
  Poster hrefs: #artwork-posters a.lightbox[href]
  Fanart hrefs: #artwork-backgrounds a.lightbox[href]
  #general fields: li.list-group-item — keyed by strong text
  Cast:        #people-actor .thumbnail h3
  Director:    #people-director table td a (first column)
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup

from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename
from app.scrape import ScrapeError, fetch_html

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_BASE = "https://www.thetvdb.com"
_SEARCH_URL = f"{_BASE}/search"

# TVDB movie slugs embed the numeric ID as their final hyphenated component:
# "the-mummy-362775" → captures group 1 = "362775"
# Used as a fallback to extract source_id when the page's general info block
# doesn't include an explicit "TheTVDB.com Movie ID" field.
_SLUG_ID_RE = re.compile(r"-(\d+)$")

# ---------------------------------------------------------------------------
# CSS selector registry — update ONLY THIS DICT when the site changes markup
# ---------------------------------------------------------------------------

_SEL = {
    "title":         "h1#series_title",             # <h1 id="series_title">
    "poster_img":    ".col-xs-12.col-sm-4 img.img-responsive",  # sidebar poster <img>
    "poster_links":  "#artwork-posters a.lightbox",  # full-res poster links (preferred)
    "fanart_links":  "#artwork-backgrounds a.lightbox",  # full-res background/fanart links
    "cast_cards":    "#people-actor .thumbnail h3",  # each cast member card heading
    "director_row":  "#people-director table td a",  # all links inside director table cells
    "general_items": "#general li.list-group-item",  # labeled metadata rows (release date, etc.)
    "search_links":  "a[href*='/movies/']",           # any link containing /movies/ in search results
}


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class TheTVDBPlugin(MetadataPlugin):
    """Scrapes TheTVDB movie pages — no API key required."""

    site_id = "thetvdb"
    # "tvdb" is the most common shorthand users know; both route to this plugin.
    aliases = ("tvdb",)

    # -----------------------------------------------------------------------
    # Lifecycle: connection management
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        # TVDB serves full HTML to browser UAs; bots may get reduced content
        # or a CAPTCHA. Accept-Language ensures we get English overviews.
        self._client = httpx.Client(
            timeout=20,
            follow_redirects=True,
            limits=httpx.Limits(keepalive_expiry=30),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                # Accept-Language: without this, TVDB may return the overview
                # in another language based on your server's geo-location.
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def close(self) -> None:
        """Release the underlying connection pool on hot-reload."""
        self._client.close()

    def __enter__(self) -> TheTVDBPlugin:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # No setup() needed — TVDB movie pages are publicly accessible.
    # If TVDB ever requires a login to view full metadata, add setup() here
    # to validate a session cookie or API token.

    # -----------------------------------------------------------------------
    # Primary dispatch — fetch()
    # -----------------------------------------------------------------------

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Route the fetch to the appropriate strategy based on match_subtype."""
        logger.info(
            "[thetvdb] fetch | subtype=%s scene_id=%s title=%r",
            parsed.match_subtype, parsed.scene_id, parsed.title,
        )

        subtype = parsed.match_subtype
        if subtype == "exact":
            return self._fetch_by_id(parsed)
        elif subtype == "enhanced":
            return self._fetch_enhanced(parsed)
        elif subtype == "limited":
            return self._fetch_limited(parsed)
        else:
            logger.warning("[thetvdb] Unsupported match subtype: %s", subtype)
            return None

    # -----------------------------------------------------------------------
    # Strategy 1: Exact — direct page load by TVDB slug
    # -----------------------------------------------------------------------

    def _fetch_by_id(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Load the movie page directly using the URL slug.

        WHY _resolve_slug INSTEAD OF using parsed.scene_id directly:
          TVDB movie URLs require the full slug (e.g. "the-mummy-362775"), not a
          bare numeric ID. /movies/362775 does NOT work — the site will 404.
          _resolve_slug() checks whether the user provided a proper slug (via
          direct_url) or just a number (via scene_id) and warns accordingly.
        """
        slug = self._resolve_slug(parsed)
        if not slug:
            logger.warning("[thetvdb] Cannot resolve a slug/ID from %r", parsed)
            return None
        url = f"{_BASE}/movies/{slug}"
        return self._scrape_movie_page(url)

    # -----------------------------------------------------------------------
    # Strategy 2: Enhanced — try slug first, fall back to search
    # -----------------------------------------------------------------------

    def _fetch_enhanced(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Try direct slug load first, then fall back to title search.

        WHY TRY SLUG FIRST:
          Enhanced filenames may include a scene_id (which _resolve_slug might
          convert to a usable slug). Trying the direct page first is faster and
          more accurate than search. If the slug fails (404, site migration), we
          still have the title to search with.

        RELATIONSHIP TO _fetch_limited:
          Both enhanced and limited ultimately call _search() as a fallback, but
          enhanced gets there only after a failed direct slug attempt. Limited
          goes straight to _search() because it has no ID to try first.
        """
        slug = self._resolve_slug(parsed)
        if slug:
            result = self._scrape_movie_page(f"{_BASE}/movies/{slug}")
            if result:
                return result
            logger.debug("[thetvdb] Direct page load failed; falling back to search")
        # Fall back to search using the title from the filename.
        # parsed.date is passed to _search() for potential year-filtering logic.
        return self._search(parsed.title or "", parsed.date)

    # -----------------------------------------------------------------------
    # Strategy 3: Limited — title search only
    # -----------------------------------------------------------------------

    def _fetch_limited(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Search-only path — lowest confidence; used when only a title is available."""
        if not parsed.title:
            logger.warning("[thetvdb] Limited match but no title available")
            return None
        return self._search(parsed.title, parsed.date)

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _resolve_slug(self, parsed: ParsedFilename) -> str | None:
        """Return a URL slug suitable for /movies/<slug>.

        WHY TVDB IS DIFFERENT FROM OTHER SITES:
          Most sites accept a numeric ID in the URL (e.g. /movies/12345).
          TVDB requires a FULL SLUG that includes the movie title:
            ✓  /movies/the-mummy-362775
            ✗  /movies/362775
          The slug is always found in the URL on the movie's TVDB page.

        HOW TO GET THE SLUG:
          Visit the movie's page on thetvdb.com and copy the path after /movies/:
            https://www.thetvdb.com/movies/the-mummy-362775
                                            ^^^^^^^^^^^^^^^^ ← this is the slug
          Use it in your filename: The Mummy % thetvdb - the-mummy-362775

        parsed.direct_url vs parsed.scene_id:
          The parser stores a slug (non-numeric token) in direct_url and a
          numeric token in scene_id. For TVDB, we need direct_url (the slug).
          A bare scene_id (numeric only) cannot be used and generates a warning.
        """
        if parsed.direct_url:
            # direct_url holds the slug exactly as it appears in the URL.
            return parsed.direct_url
        # Bare scene_id (numeric) is not usable without the title component on TVDB.
        if parsed.scene_id and not parsed.direct_url:
            logger.warning(
                "[thetvdb] Bare numeric scene_id %r is not usable — use the full slug from the "
                "movie URL, e.g. 'the-mummy-362775'. "
                "Filename should be: Movie Name %% thetvdb - the-mummy-362775",
                parsed.scene_id,
            )
        return None

    def _search(self, title: str, date: str | None) -> MetadataResult | None:
        """Search the TVDB site search for a movie by title.

        WHY self._client INSTEAD OF fetch_html HERE:
          We use self._client.get directly (instead of fetch_html) because:
          1. We need to pass query params (?query=...&type=movies).
          2. We want fine-grained error handling — different HTTP errors have
             different consequences (warn+raise vs. warn+return None).

        SEARCH SCORING LOGIC:
          - Exact title match (case-insensitive) wins immediately.
          - Partial match (title_lower in link_title.lower()) is a fallback.
          - WHY NOT year-filter: TVDB search doesn't accept a year param, so
            ``date`` is currently unused here. It's accepted as a parameter for
            future use (e.g. ranking candidates by release year proximity).
        """
        try:
            r = self._client.get(
                _SEARCH_URL,
                params={"query": title, "type": "movies"},  # type=movies excludes TV series results
            )
            r.raise_for_status()
        except httpx.TimeoutException as exc:
            # Network timeout — propagate so main.py records status="error".
            logger.warning("[thetvdb] Search timeout: %s", exc)
            raise
        except httpx.ConnectError as exc:
            # DNS failure or refused connection — propagate.
            logger.warning("[thetvdb] Search connection error: %s", exc)
            raise
        except httpx.HTTPStatusError as exc:
            # HTTP 4xx/5xx — log and return None (not raise) because a search
            # HTTP error doesn't mean the plugin is broken, just that this search
            # failed. The file will appear as "unmatched" in the run report.
            logger.warning("[thetvdb] Search HTTP %s", exc.response.status_code)
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        # _SEL["search_links"] matches any <a> whose href contains "/movies/",
        # which includes both result links and site navigation. We filter to
        # hrefs that start with "/movies/" to exclude nav.
        title_lower = title.lower()
        year = int(date[:4]) if date and len(date) >= 4 else None  # reserved for future ranking use

        best_url: str | None = None
        for link in soup.select(_SEL["search_links"]):
            href = link.get("href", "")
            if not href.startswith("/movies/"):
                continue          # skip nav links and other /movies/* non-result links
            link_title = link.get_text(strip=True)
            if link_title.lower() == title_lower:
                # Exact case-insensitive title match — stop immediately.
                best_url = urljoin(_BASE, href)
                break
            if best_url is None and title_lower in link_title.lower():
                # Partial match — keep as candidate but keep scanning for better.
                best_url = urljoin(_BASE, href)

        if not best_url:
            logger.info("[thetvdb] Search found no results for %r", title)
            return None

        logger.debug("[thetvdb] Search resolved to: %s", best_url)
        return self._scrape_movie_page(best_url)

    def _scrape_movie_page(self, url: str) -> MetadataResult | None:
        """Load a /movies/<slug> page and extract metadata.

        WHY self._client.get INSTEAD OF fetch_html:
          We use self._client.get directly so we can treat 404 as a clean
          "return None" (not a ScrapeError). fetch_html would raise ScrapeError
          on 404, which would record the file as "scrape_error" in the run report
          — but a 404 on an exact URL lookup is a normal "not found" case (e.g.
          the user provided a wrong slug), not an infrastructure failure.

        RELATIONSHIP TO _parse_movie_page:
          This method handles the HTTP layer (GET, status check, error handling).
          _parse_movie_page handles the HTML parsing layer. Separating them makes
          unit testing easier: you can test _parse_movie_page by passing raw HTML
          without needing to mock HTTP calls.
        """
        logger.debug("[thetvdb] Loading: %s", url)
        try:
            r = self._client.get(url)
            if r.status_code == 404:
                # Clean not-found — e.g. slug is wrong or movie was deleted on TVDB.
                # Return None instead of raising so the file is "unmatched", not "error".
                logger.debug("[thetvdb] 404 for %s", url)
                return None
            r.raise_for_status()  # raises HTTPStatusError for other 4xx/5xx
        except httpx.TimeoutException as exc:
            logger.warning("[thetvdb] Timeout loading %s: %s", url, exc)
            raise    # propagates → status="error"
        except httpx.ConnectError as exc:
            logger.warning("[thetvdb] Connection error for %s: %s", url, exc)
            raise
        except httpx.HTTPStatusError as exc:
            # Non-404 HTTP errors (500, 503, etc.) — log and return None.
            # The file will appear as "unmatched" rather than crashing the run.
            logger.warning("[thetvdb] HTTP %s for %s", exc.response.status_code, url)
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        return self._parse_movie_page(soup, url)

    # -----------------------------------------------------------------------
    # Detail page parser — THE MOST IMPORTANT METHOD
    # -----------------------------------------------------------------------

    def _parse_movie_page(self, soup: BeautifulSoup, source_url: str) -> MetadataResult | None:
        """Extract MetadataResult from a parsed TVDB movie page.

        TVDB PAGE STRUCTURE (as of 2026-05-29):
          - Title:    <h1 id="series_title">
          - Overview: Inside a #translations block; each language variant has
                      data-language="eng". We search for the first non-empty <p>
                      in the English variant.
          - General info: a <ul id="general"> list where each <li class="list-group-item">
                      has a <strong> label (e.g. "Released", "Runtime", "Studio")
                      and one or more <span> values. Some spans contain a nested
                      <small> with a country name (e.g. for release dates).
          - Cast:     #people-actor section; each member is a .thumbnail card
                      with an <h3> containing the name and a <small> for the role.
          - Director: #people-director section; a table where the first <td> of
                      each row contains an <a href="/people/..."> link.
          - Poster:   Preferred source is #artwork-posters (full-resolution gallery
                      links). Falls back to the sidebar <img> if the gallery is absent.
          - Fanart:   #artwork-backgrounds gallery links.
        """

        # ==================================================================
        # Title (REQUIRED — raise if missing)
        # ==================================================================
        title_el = soup.select_one(_SEL["title"])
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            # Raise ScrapeError (not SelectorMissingError) because a missing title
            # on what should be a movie page means the page is fundamentally wrong —
            # possibly a login wall, a redirect, or a 200 that's actually an error page.
            raise ScrapeError(f"Could not find title on TVDB page: {source_url}")

        # ==================================================================
        # Overview (optional — English translation)
        # ==================================================================
        # TVDB wraps translations in a #translations block. Each language gets a
        # div with data-language="eng" (or "deu", "fra", etc.). We look for the
        # first non-empty <p> inside the English block.
        # WHY NOT select_one("#translations [data-language='eng'] p"):
        #   The CSS :is() / descendant combinator approach works, but iterating
        #   lets us skip empty <p> tags (which TVDB sometimes emits as placeholders).
        summary: str | None = None
        for trans in soup.select("[data-language]"):
            if trans.get("data-language") != "eng":
                continue
            p = trans.find("p")
            if p:
                text = p.get_text(strip=True)
                if text:
                    summary = text
                    break  # stop at the first non-empty English paragraph

        # ==================================================================
        # General info block — parse all <li> rows into a dict
        # ==================================================================
        # Each li looks like:
        #   <li class="list-group-item">
        #     <strong>Runtime</strong>
        #     <span>133 minutes</span>
        #   </li>
        # After building general_data, we pull specific keys by label string.
        # This approach is more robust than positional indexing: adding or
        # removing a row doesn't break the parser for other fields.
        general_data: dict[str, list[str]] = {}
        for li in soup.select(_SEL["general_items"]):
            strong = li.find("strong")
            if not strong:
                continue
            label = strong.get_text(strip=True)
            # Collect all <span> text values for this row, stripping nested <small>
            # elements (which hold country labels for release-date entries).
            values: list[str] = []
            for span in li.find_all("span"):
                # Remove nested <small> in-place — this mutates the BeautifulSoup
                # tree, so we're operating on a copy implicitly (each loop iter
                # works on the span's subtree, not the original li). This is safe
                # because we only read values AFTER decomposing <small>.
                for small in span.find_all("small"):
                    small.decompose()   # removes the <small> from the parse tree
                val = span.get_text(strip=True)
                if val:
                    values.append(val)
            general_data[label] = values

        # ==================================================================
        # TVDB Movie ID (for source_id / NFO <uniqueid>)
        # ==================================================================
        # TVDB sometimes includes the numeric ID in the general info block under
        # "TheTVDB.com Movie ID". If it's absent, extract from the URL slug
        # ("the-mummy-362775" → "362775") using _SLUG_ID_RE.
        tvdb_id = (general_data.get("TheTVDB.com Movie ID") or [None])[0]
        if not tvdb_id:
            m = _SLUG_ID_RE.search(source_url)  # e.g. "/movies/the-mummy-362775"
            if m:
                tvdb_id = m.group(1)

        # ==================================================================
        # Release date (prefer US, fall back to first available)
        # ==================================================================
        # The "Released" li contains multiple <span>s, each with:
        #   <span>
        #     <small>United States of America</small>
        #     April 17, 2026
        #   </span>
        # After decomposing the <small> during general_data parsing above, the
        # span texts are just the date strings — but the general_data pass
        # strips country labels, making it hard to prefer US here. We re-parse
        # the "Released" li directly to preserve country-to-date associations.
        release_date: str | None = None
        released_li = next(
            (li for li in soup.select(_SEL["general_items"])
             if li.find("strong") and li.find("strong").get_text(strip=True) == "Released"),
            None,
        )
        if released_li:
            for span in released_li.find_all("span"):
                # Identify the country from the <small> BEFORE decomposing it.
                small = span.find("small")
                country = small.get_text(strip=True) if small else ""
                # The date text is a direct child text node of <span>, not inside <small>.
                # We collect only strings whose parent IS the span (i.e. direct children),
                # which excludes the <small>'s text.
                date_str = "".join(
                    t for t in span.strings
                    if t.parent is span  # direct child text node only
                ).strip()
                if date_str:
                    try:
                        # TVDB formats dates as "April 17, 2026" (Month DD, YYYY)
                        parsed_date = datetime.strptime(date_str, "%B %d, %Y")
                        if "United States" in country:
                            # US date found — prefer it and stop scanning other spans.
                            release_date = parsed_date.strftime("%Y-%m-%d")
                            break
                        if release_date is None:
                            # No US date yet — use the first date found as a fallback.
                            release_date = parsed_date.strftime("%Y-%m-%d")
                    except ValueError:
                        pass  # skip unparseable date strings (e.g. "TBA", "Unknown")

        # Auto-derive year from release_date; MetadataResult.__post_init__ also does
        # this, but setting it explicitly avoids a redundant re-parse.
        year: int | None = int(release_date[:4]) if release_date else None

        # ==================================================================
        # Content rating (e.g. "R", "PG-13")
        # ==================================================================
        content_ratings = general_data.get("Content Rating") or []
        content_rating = content_ratings[0] if content_ratings else None

        # ==================================================================
        # Runtime (e.g. "133 minutes" → 133)
        # ==================================================================
        runtime_vals = general_data.get("Runtime") or []
        runtime_str = runtime_vals[0] if runtime_vals else ""
        m_rt = re.search(r"\d+", runtime_str)
        runtime_mins: int | None = int(m_rt.group()) if m_rt else None

        # ==================================================================
        # Genres
        # ==================================================================
        # TVDB genres are inside the "Genres" li as <a> links, not plain spans.
        # We re-select the li directly (not via general_data) to get the <a> text.
        genre_el = next(
            (li for li in soup.select(_SEL["general_items"])
             if li.find("strong") and li.find("strong").get_text(strip=True) == "Genres"),
            None,
        )
        genres: list[str] = []
        if genre_el:
            genres = [a.get_text(strip=True) for a in genre_el.find_all("a") if a.get_text(strip=True)]

        # ==================================================================
        # Studio
        # ==================================================================
        studio_vals = general_data.get("Studio") or []
        studio = studio_vals[0] if studio_vals else None

        # ==================================================================
        # Cast — #people-actor .thumbnail h3
        # ==================================================================
        # Each cast card has structure:
        #   <h3>
        #     Name
        #     <br/>
        #     <small>as Role Name</small>
        #     <span class="text-danger">needs image</span>  ← optional
        #   </h3>
        # We remove <small> (role) and <span class="text-danger"> before reading
        # the text, so we get only the actor name.
        actors: list[str] = []
        for card in soup.select(_SEL["cast_cards"]):
            for el in card.find_all(["small", "span"]):
                el.decompose()     # remove role text and "needs image" labels
            name = card.get_text(strip=True)
            if name:
                actors.append(name)
        # Deduplicate preserving order (dict.fromkeys trick).
        # TVDB sometimes lists the same actor twice (different roles).
        actors = list(dict.fromkeys(actors))

        # ==================================================================
        # Directors — #people-director table first-column links
        # ==================================================================
        # The director table has multiple columns; the first column contains
        # <a href="/people/..."> links for the actual director names.
        # Other columns may have action links (/movies/.../people/... with "edit"
        # or "delete" in the href) — filter those out by checking for /people/.
        directors: list[str] = []
        for a in soup.select(_SEL["director_row"]):
            href = a.get("href", "")
            if "/people/" in href:   # skip action/edit links; keep /people/ profile links
                name = a.get_text(strip=True)
                if name:
                    directors.append(name)
        directors = list(dict.fromkeys(directors))  # deduplicate

        # ==================================================================
        # Poster — prefer full-res gallery, fall back to sidebar thumbnail
        # ==================================================================
        # TVDB has a dedicated artwork section (#artwork-posters) with
        # full-resolution images as <a href="..."> lightbox links. These are
        # higher quality than the sidebar thumbnail. Fall back to the sidebar
        # img[src] if the gallery section isn't present on this page.
        poster_url: str | None = None
        poster_link = soup.select_one(_SEL["poster_links"])
        if poster_link:
            # prefer href (full-res) over data-src (may be a thumbnail CDN URL)
            poster_url = poster_link.get("href") or poster_link.get("data-src")
        if not poster_url:
            # Sidebar fallback
            img = soup.select_one(_SEL["poster_img"])
            if img:
                poster_url = img.get("src") or img.get("data-src")

        # ==================================================================
        # Fanart — first image in #artwork-backgrounds gallery
        # ==================================================================
        fanart_url: str | None = None
        fanart_link = soup.select_one(_SEL["fanart_links"])
        if fanart_link:
            fanart_url = fanart_link.get("href") or fanart_link.get("data-src")

        # ==================================================================
        # Assemble result
        # ==================================================================
        return MetadataResult(
            title=title,
            summary=summary,
            year=year,
            release_date=release_date,     # "YYYY-MM-DD" or None
            content_rating=content_rating, # e.g. "R", "PG-13", or None
            genres=genres,
            actors=actors,
            directors=directors,
            studio=studio,
            poster_url=poster_url,
            fanart_url=fanart_url,
            source_id=tvdb_id,             # TVDB numeric ID string for NFO <uniqueid>
            source_url=source_url,         # canonical /movies/<slug> URL for NFO <source>
            # Runtime is not a first-class MetadataResult field, so we encode it
            # as a tag in the format "runtime:133min". This writes to NFO <tag>
            # and Plex tags — useful for filtering/displaying runtime in Plex.
            tags=[f"runtime:{runtime_mins}min"] if runtime_mins else [],
        )
