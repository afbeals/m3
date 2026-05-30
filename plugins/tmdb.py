"""
TMDb (The Movie Database) Plugin

Fetches movie metadata from the TMDb v3 API.
https://developer.themoviedb.org/reference/intro/getting-started

API key:
  1. Create a free account at https://www.themoviedb.org/signup
  2. Go to Settings → API → Request an API Key (choose "Developer")
  3. Copy the "API Read Access Token" (Bearer token) — use that as TMDB_BEARER_TOKEN
     OR copy the "API Key (v3 auth)" — use that as TMDB_API_KEY

  TMDB_BEARER_TOKEN is recommended (passed as Authorization: Bearer header).
  TMDB_API_KEY is the legacy query-param method; both work.

----------------------------------------------------------------------
Filename examples this plugin handles (site_id = "tmdb"):

  Exact (TMDb numeric movie ID):
    The Mummy % tmdb - 335988
    The Mummy % tmdb - 2017-06-09 - 335988

  Enhanced (ID + title for result validation):
    The Mummy % tmdb - 335988 - The Mummy

  Limited (title-only search):
    The Mummy % tmdb - The Mummy
----------------------------------------------------------------------

API endpoints used:
  GET /3/movie/{movie_id}
      → title, overview, release_date, genres, vote_average,
        runtime, original_language, production_companies, status
  GET /3/movie/{movie_id}/credits
      → cast[].name, crew[].name where crew[].job == "Director"
  GET /3/movie/{movie_id}/images
      → posters[].file_path, backdrops[].file_path
  GET /3/search/movie?query=...&year=...
      → results[].id, results[].title, results[].release_date

Image base URL: https://image.tmdb.org/t/p/original/{file_path}
"""

from __future__ import annotations

import logging
import os
from urllib.parse import quote

import httpx

from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename
from app.scrape import ScrapeError

logger = logging.getLogger(__name__)

_BASE = "https://api.themoviedb.org/3"
_IMG_BASE = "https://image.tmdb.org/t/p/original"


class TMDbPlugin(MetadataPlugin):
    site_id = "tmdb"
    aliases = ("themoviedb",)

    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=15,
            follow_redirects=True,
            limits=httpx.Limits(keepalive_expiry=30),
        )

    def setup(self) -> None:
        api_key_present = bool(
            os.environ.get("TMDB_BEARER_TOKEN") or os.environ.get("TMDB_API_KEY")
        )
        if not api_key_present:
            raise RuntimeError(
                "Neither TMDB_BEARER_TOKEN nor TMDB_API_KEY is set. "
                "Register at https://www.themoviedb.org/signup and add one to your .env file."
            )
        # Smoke-test credentials — but don't fail on transient network errors
        try:
            result = self._api_get("/configuration")
            if result is None:
                logger.warning("[tmdb] Credential check returned empty — will verify on first fetch()")
            else:
                logger.info("[tmdb] Credentials verified")
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning(
                "[tmdb] Could not reach TMDb at startup (%s) — plugin registered anyway, "
                "will retry on first fetch()", exc,
            )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> TMDbPlugin:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # fetch() dispatch
    # ------------------------------------------------------------------

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        logger.info(
            "[tmdb] fetch | subtype=%s scene_id=%s title=%r",
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
            logger.warning("[tmdb] Unsupported match subtype: %s", subtype)
            return None

    # ------------------------------------------------------------------
    # Exact — direct movie lookup by TMDb ID
    # ------------------------------------------------------------------

    def _fetch_by_id(self, parsed: ParsedFilename) -> MetadataResult | None:
        if not parsed.scene_id or not parsed.scene_id.isdigit():
            logger.warning("[tmdb] Exact match but scene_id is not a numeric TMDb ID: %r", parsed.scene_id)
            return None
        return self._fetch_movie(parsed.scene_id)

    # ------------------------------------------------------------------
    # Enhanced — try ID first, fall back to title search
    # ------------------------------------------------------------------

    def _fetch_enhanced(self, parsed: ParsedFilename) -> MetadataResult | None:
        if parsed.scene_id and parsed.scene_id.isdigit():
            result = self._fetch_movie(parsed.scene_id)
            if result:
                return result
            logger.debug("[tmdb] Direct ID lookup failed; falling back to search")
        return self._search(parsed.title or "", parsed.date)

    # ------------------------------------------------------------------
    # Limited — title search only
    # ------------------------------------------------------------------

    def _fetch_limited(self, parsed: ParsedFilename) -> MetadataResult | None:
        if not parsed.title:
            logger.warning("[tmdb] Limited match but no title available")
            return None
        return self._search(parsed.title, parsed.date)

    # ------------------------------------------------------------------
    # Core fetch: load detail + credits + images for one movie ID
    # ------------------------------------------------------------------

    def _fetch_movie(self, movie_id: str) -> MetadataResult | None:
        # Fetch detail, credits, and images in parallel-ish using append_to_response
        # (single API call that returns all three in one response).
        data = self._api_get(
            f"/movie/{movie_id}",
            params={"append_to_response": "credits,images"},
        )
        if not isinstance(data, dict):
            return None
        return self._to_result(data)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _search(self, title: str, date: str | None) -> MetadataResult | None:
        params: dict = {"query": title, "language": "en-US", "page": 1}
        if date and len(date) >= 4:
            try:
                params["year"] = int(date[:4])
            except ValueError:
                pass

        data = self._api_get("/search/movie", params=params)
        if not isinstance(data, dict):
            return None

        results = data.get("results") or []
        if not results:
            logger.info("[tmdb] Search found no results for %r", title)
            return None

        # Prefer an exact title match; fall back to top result.
        title_lower = title.lower()
        match = next(
            (r for r in results if r.get("title", "").lower() == title_lower),
            results[0],
        )

        if len(results) > 1:
            logger.debug(
                "[tmdb] Search returned %d results for %r; selected %r",
                len(results), title, match.get("title"),
            )

        movie_id = str(match.get("id", ""))
        if not movie_id:
            return None

        return self._fetch_movie(movie_id)

    # ------------------------------------------------------------------
    # API helper
    # ------------------------------------------------------------------

    def _api_get(self, path: str, params: dict | None = None) -> dict | None:
        """GET a TMDb v3 endpoint, authenticating via Bearer token or api_key param."""
        # Prefer the modern Bearer token; fall back to legacy api_key query param.
        bearer = os.environ.get("TMDB_BEARER_TOKEN", "")
        api_key = os.environ.get("TMDB_API_KEY", "")

        if not bearer and not api_key:
            raise RuntimeError("No TMDb credentials — set TMDB_BEARER_TOKEN or TMDB_API_KEY")

        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        all_params = dict(params or {})
        if not bearer and api_key:
            all_params["api_key"] = api_key

        url = f"{_BASE}{path}"
        try:
            r = self._client.get(url, params=all_params, headers=headers)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                logger.debug("[tmdb] Not found (404): %s", url)
                return None
            if status == 429:
                logger.warning("[tmdb] Rate-limited (429): %s", url)
                raise ScrapeError(f"TMDb rate-limited: {url}") from exc
            if status == 401:
                raise RuntimeError(
                    "TMDb returned 401 Unauthorized — check TMDB_BEARER_TOKEN or TMDB_API_KEY"
                ) from exc
            logger.warning("[tmdb] HTTP %s for %s", status, url)
            return None
        except httpx.TimeoutException as exc:
            logger.warning("[tmdb] Timeout: %s", exc)
            raise
        except httpx.ConnectError as exc:
            logger.warning("[tmdb] Connection error: %s", exc)
            raise
        except Exception:
            logger.exception("[tmdb] Unexpected error fetching %s", url)
            return None

    # ------------------------------------------------------------------
    # Map API response → MetadataResult
    # ------------------------------------------------------------------

    def _to_result(self, data: dict) -> MetadataResult | None:
        """
        Map a /movie/{id}?append_to_response=credits,images response.

        Relevant fields in the TMDb movie detail response:
          id, title, overview, release_date (YYYY-MM-DD), vote_average (0-10),
          runtime (minutes), status, original_language,
          genres[].name,
          production_companies[].name,
          credits.cast[].name + .order (sorted by billing order)
          credits.crew[].name + .job ("Director", "Producer", etc.)
          images.posters[].file_path + .vote_average (use highest-voted)
          images.backdrops[].file_path + .vote_average
        """
        title = data.get("title") or ""
        if not title:
            logger.warning("[tmdb] Movie id=%s has no title", data.get("id"))
            return None

        # Release date
        release_date: str | None = data.get("release_date") or None
        year: int | None = None
        if release_date and len(release_date) >= 4:
            try:
                year = int(release_date[:4])
            except ValueError:
                pass

        # Rating: TMDb vote_average is already 0–10
        vote = data.get("vote_average")
        try:
            rating = float(vote) if vote is not None else None
            if rating is not None and not (0.0 <= rating <= 10.0):
                rating = None
        except (TypeError, ValueError):
            rating = None

        # Genres
        genres = [g["name"] for g in (data.get("genres") or []) if g.get("name")]

        # Production company (first listed)
        companies = data.get("production_companies") or []
        studio: str | None = companies[0]["name"] if companies and companies[0].get("name") else None

        # Credits (present when append_to_response=credits was used)
        credits = data.get("credits") or {}

        # Cast: sorted by billing order (TMDb includes .order field)
        cast = sorted(
            [c for c in (credits.get("cast") or []) if c.get("name")],
            key=lambda c: c.get("order", 999),
        )
        actors = [c["name"] for c in cast]

        # Directors from crew
        directors = list(dict.fromkeys(
            c["name"]
            for c in (credits.get("crew") or [])
            if c.get("job") == "Director" and c.get("name")
        ))

        # Artwork (present when append_to_response=images was used)
        images = data.get("images") or {}

        def _best_image(items: list[dict]) -> str | None:
            """Return file_path of the highest-voted image, or first available."""
            valid = [i for i in items if i.get("file_path")]
            if not valid:
                return None
            best = max(valid, key=lambda i: i.get("vote_average") or 0)
            return f"{_IMG_BASE}{best['file_path']}"

        poster_url = _best_image(images.get("posters") or [])
        fanart_url = _best_image(images.get("backdrops") or [])

        # Tags: include runtime for Kodi display
        runtime_mins: int | None = data.get("runtime")
        tags = [f"runtime:{runtime_mins}min"] if runtime_mins else []

        movie_id = str(data["id"]) if data.get("id") is not None else None
        source_url = f"https://www.themoviedb.org/movie/{movie_id}" if movie_id else None

        return MetadataResult(
            title=title,
            summary=data.get("overview"),
            year=year,
            release_date=release_date,
            rating=rating,
            # content_rating is not included — TMDb requires a separate /movie/{id}/release_dates
            # call to get MPAA ratings. To add it, include "release_dates" in the
            # append_to_response parameter and extract:
            #   data.get("release_dates", {}).get("results", [])
            #   → filter for country="US" → get release_dates[].certification
            # See: https://developer.themoviedb.org/reference/movie-release-dates
            content_rating=None,
            genres=genres,
            actors=actors,
            directors=directors,
            studio=studio,
            poster_url=poster_url,
            fanart_url=fanart_url,
            source_id=movie_id,
            source_url=source_url,
            tags=tags,
        )
