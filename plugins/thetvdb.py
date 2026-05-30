"""
TheTVDB HTML Scraper Plugin

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

_BASE = "https://www.thetvdb.com"
_SEARCH_URL = f"{_BASE}/search"

# Slugs embed the numeric ID as the trailing component: "the-mummy-362775" → "362775"
_SLUG_ID_RE = re.compile(r"-(\d+)$")

# CSS selectors — update here if the site changes its markup.
_SEL = {
    "title":        "h1#series_title",
    "poster_img":   ".col-xs-12.col-sm-4 img.img-responsive",
    "poster_links": "#artwork-posters a.lightbox",
    "fanart_links": "#artwork-backgrounds a.lightbox",
    "cast_cards":   "#people-actor .thumbnail h3",
    "director_row": "#people-director table td a",
    "general_items": "#general li.list-group-item",
    "search_links": "a[href*='/movies/']",
}


class TheTVDBPlugin(MetadataPlugin):
    """Scrapes TheTVDB movie pages — no API key required."""

    site_id = "thetvdb"
    aliases = ("tvdb",)

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=20,
            follow_redirects=True,
            limits=httpx.Limits(keepalive_expiry=30),
            headers={
                # TVDB serves full HTML to browser UAs; bots may get reduced content.
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TheTVDBPlugin:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # fetch() dispatch
    # ------------------------------------------------------------------

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
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

    # ------------------------------------------------------------------
    # Exact — direct page load by TVDB ID or slug
    # ------------------------------------------------------------------

    def _fetch_by_id(self, parsed: ParsedFilename) -> MetadataResult | None:
        slug = self._resolve_slug(parsed)
        if not slug:
            logger.warning("[thetvdb] Cannot resolve a slug/ID from %r", parsed)
            return None
        url = f"{_BASE}/movies/{slug}"
        return self._scrape_movie_page(url)

    # ------------------------------------------------------------------
    # Enhanced — try direct ID first, fall back to search
    # ------------------------------------------------------------------

    def _fetch_enhanced(self, parsed: ParsedFilename) -> MetadataResult | None:
        slug = self._resolve_slug(parsed)
        if slug:
            result = self._scrape_movie_page(f"{_BASE}/movies/{slug}")
            if result:
                return result
            logger.debug("[thetvdb] Direct page load failed; falling back to search")
        return self._search(parsed.title or "", parsed.date)

    # ------------------------------------------------------------------
    # Limited — title search
    # ------------------------------------------------------------------

    def _fetch_limited(self, parsed: ParsedFilename) -> MetadataResult | None:
        if not parsed.title:
            logger.warning("[thetvdb] Limited match but no title available")
            return None
        return self._search(parsed.title, parsed.date)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_slug(self, parsed: ParsedFilename) -> str | None:
        """Return a URL slug for /movies/<slug>.

        TVDB requires the full slug (e.g. 'the-mummy-362775'), not a bare
        numeric ID. Use the URL slug from the movie's page as the scene token:
          The Mummy % thetvdb - the-mummy-362775
        """
        if parsed.direct_url:
            return parsed.direct_url
        # Bare scene_id (numeric) is not usable without the title slug on TVDB.
        if parsed.scene_id and not parsed.direct_url:
            logger.warning(
                "[thetvdb] Bare numeric scene_id %r is not usable — use the full slug from the "
                "movie URL, e.g. 'the-mummy-362775'. "
                "Filename should be: Movie Name %% thetvdb - the-mummy-362775",
                parsed.scene_id,
            )
        return None

    def _search(self, title: str, date: str | None) -> MetadataResult | None:
        """Search TVDB site search for a movie by title."""
        try:
            r = self._client.get(
                _SEARCH_URL,
                params={"query": title, "type": "movies"},
            )
            r.raise_for_status()
        except httpx.TimeoutException as exc:
            logger.warning("[thetvdb] Search timeout: %s", exc)
            raise
        except httpx.ConnectError as exc:
            logger.warning("[thetvdb] Search connection error: %s", exc)
            raise
        except httpx.HTTPStatusError as exc:
            logger.warning("[thetvdb] Search HTTP %s", exc.response.status_code)
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        # Search results are <a href="/movies/slug-id"> links
        title_lower = title.lower()
        year = int(date[:4]) if date and len(date) >= 4 else None

        best_url: str | None = None
        for link in soup.select(_SEL["search_links"]):
            href = link.get("href", "")
            if not href.startswith("/movies/"):
                continue
            link_title = link.get_text(strip=True)
            if link_title.lower() == title_lower:
                # Exact title match — use it immediately
                best_url = urljoin(_BASE, href)
                break
            if best_url is None and title_lower in link_title.lower():
                best_url = urljoin(_BASE, href)

        if not best_url:
            logger.info("[thetvdb] Search found no results for %r", title)
            return None

        logger.debug("[thetvdb] Search resolved to: %s", best_url)
        return self._scrape_movie_page(best_url)

    def _scrape_movie_page(self, url: str) -> MetadataResult | None:
        """Load a /movies/<slug> page and extract metadata."""
        logger.debug("[thetvdb] Loading: %s", url)
        try:
            r = self._client.get(url)
            if r.status_code == 404:
                logger.debug("[thetvdb] 404 for %s", url)
                return None
            r.raise_for_status()
        except httpx.TimeoutException as exc:
            logger.warning("[thetvdb] Timeout loading %s: %s", url, exc)
            raise
        except httpx.ConnectError as exc:
            logger.warning("[thetvdb] Connection error for %s: %s", url, exc)
            raise
        except httpx.HTTPStatusError as exc:
            logger.warning("[thetvdb] HTTP %s for %s", exc.response.status_code, url)
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        return self._parse_movie_page(soup, url)

    def _parse_movie_page(self, soup: BeautifulSoup, source_url: str) -> MetadataResult | None:
        """Extract MetadataResult from a parsed TVDB movie page."""

        # --- Title (h1#series_title) ---
        title_el = soup.select_one(_SEL["title"])
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            raise ScrapeError(f"Could not find title on TVDB page: {source_url}")

        # --- Overview (English translation from #translations block) ---
        summary: str | None = None
        for trans in soup.select("[data-language]"):
            if trans.get("data-language") != "eng":
                continue
            p = trans.find("p")
            if p:
                text = p.get_text(strip=True)
                if text:
                    summary = text
                    break

        # --- Parse #general info fields (keyed by <strong> label) ---
        general_data: dict[str, list[str]] = {}
        for li in soup.select(_SEL["general_items"]):
            strong = li.find("strong")
            if not strong:
                continue
            label = strong.get_text(strip=True)
            # Collect all span values (excluding nested <small> country labels)
            values: list[str] = []
            for span in li.find_all("span"):
                # Remove nested <small> (country names in release dates)
                for small in span.find_all("small"):
                    small.decompose()
                val = span.get_text(strip=True)
                if val:
                    values.append(val)
            general_data[label] = values

        # --- TVDB Movie ID (for source_id) ---
        tvdb_id = (general_data.get("TheTVDB.com Movie ID") or [None])[0]
        # Extract from URL as fallback: /movies/the-mummy-362775 → 362775
        if not tvdb_id:
            m = _SLUG_ID_RE.search(source_url)
            if m:
                tvdb_id = m.group(1)

        # --- Release date (prefer US, fall back to first available) ---
        # The "Released" li has multiple spans, each with a date stripped of the
        # country <small>. The original HTML order is US first for US movies.
        release_date: str | None = None
        released_li = next(
            (li for li in soup.select(_SEL["general_items"])
             if li.find("strong") and li.find("strong").get_text(strip=True) == "Released"),
            None,
        )
        if released_li:
            for span in released_li.find_all("span"):
                # Each span has a <small> country label and a bare text node with the date.
                # e.g. <span><small>United States of America</small>\n April 17, 2026\n</span>
                small = span.find("small")
                country = small.get_text(strip=True) if small else ""
                # Date is the text node(s) that are direct children of the span
                date_str = "".join(
                    t for t in span.strings
                    if t.parent is span
                ).strip()
                if date_str:
                    try:
                        parsed_date = datetime.strptime(date_str, "%B %d, %Y")
                        if "United States" in country:
                            release_date = parsed_date.strftime("%Y-%m-%d")
                            break
                        if release_date is None:
                            release_date = parsed_date.strftime("%Y-%m-%d")
                    except ValueError:
                        pass

        year: int | None = int(release_date[:4]) if release_date else None

        # --- Content rating (e.g. "R") ---
        content_ratings = general_data.get("Content Rating") or []
        content_rating = content_ratings[0] if content_ratings else None

        # --- Runtime (e.g. "133 minutes") ---
        runtime_vals = general_data.get("Runtime") or []
        runtime_str = runtime_vals[0] if runtime_vals else ""
        m_rt = re.search(r"\d+", runtime_str)
        runtime_mins: int | None = int(m_rt.group()) if m_rt else None

        # --- Genres ---
        genre_el = next(
            (li for li in soup.select(_SEL["general_items"])
             if li.find("strong") and li.find("strong").get_text(strip=True) == "Genres"),
            None,
        )
        genres: list[str] = []
        if genre_el:
            genres = [a.get_text(strip=True) for a in genre_el.find_all("a") if a.get_text(strip=True)]

        # --- Studio ---
        studio_vals = general_data.get("Studio") or []
        studio = studio_vals[0] if studio_vals else None

        # --- Cast: #people-actor .thumbnail h3 ---
        # Structure: <h3>Name<br/><small>as Role</small></h3>
        actors: list[str] = []
        for card in soup.select(_SEL["cast_cards"]):
            # Remove <small> (role) and <span class="text-danger"> ("needs image")
            for el in card.find_all(["small", "span"]):
                el.decompose()
            name = card.get_text(strip=True)
            if name:
                actors.append(name)
        actors = list(dict.fromkeys(actors))  # deduplicate, preserve order

        # --- Directors: #people-director table first-column links ---
        directors: list[str] = []
        for a in soup.select(_SEL["director_row"]):
            href = a.get("href", "")
            # Skip delete/action links; keep /people/ links
            if "/people/" in href:
                name = a.get_text(strip=True)
                if name:
                    directors.append(name)
        directors = list(dict.fromkeys(directors))

        # --- Poster: prefer first image in #artwork-posters, fall back to sidebar ---
        poster_url: str | None = None
        poster_link = soup.select_one(_SEL["poster_links"])
        if poster_link:
            poster_url = poster_link.get("href") or poster_link.get("data-src")
        if not poster_url:
            img = soup.select_one(_SEL["poster_img"])
            if img:
                poster_url = img.get("src") or img.get("data-src")

        # --- Fanart: first image in #artwork-backgrounds ---
        fanart_url: str | None = None
        fanart_link = soup.select_one(_SEL["fanart_links"])
        if fanart_link:
            fanart_url = fanart_link.get("href") or fanart_link.get("data-src")

        return MetadataResult(
            title=title,
            summary=summary,
            year=year,
            release_date=release_date,
            content_rating=content_rating,
            genres=genres,
            actors=actors,
            directors=directors,
            studio=studio,
            poster_url=poster_url,
            fanart_url=fanart_url,
            source_id=tvdb_id,
            source_url=source_url,
            tags=[f"runtime:{runtime_mins}min"] if runtime_mins else [],
        )
