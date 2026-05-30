"""
TMDb (The Movie Database) Plugin
=================================
Fetches movie metadata from the TMDb v3 API.
https://developer.themoviedb.org/reference/intro/getting-started

API key setup:
  1. Create a free account at https://www.themoviedb.org/signup
  2. Go to Settings → API → Request an API Key (choose "Developer")
  3. Copy the "API Read Access Token" (Bearer token) → TMDB_BEARER_TOKEN
     OR copy the "API Key (v3 auth)" → TMDB_API_KEY
  4. Add to your .env file:
       TMDB_BEARER_TOKEN=eyJ...  ← preferred (Bearer header auth)
       # OR
       TMDB_API_KEY=abc123       ← legacy (query param auth)

  TMDB_BEARER_TOKEN is recommended (passed as Authorization: Bearer header).
  TMDB_API_KEY is the legacy query-param method; both work.
  If BOTH are set, the Bearer token takes precedence.

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
  GET /3/movie/{movie_id}?append_to_response=credits,images
      → title, overview, release_date, genres, vote_average,
        runtime, original_language, production_companies, status,
        credits.cast[].name+order, credits.crew[].name+job,
        images.posters[].file_path+vote_average,
        images.backdrops[].file_path+vote_average

  GET /3/search/movie?query=...&year=...
      → results[].id, results[].title, results[].release_date

NOTE: content_rating (MPAA rating) is NOT fetched. It requires a separate
  append_to_response=release_dates call and country-specific filtering.
  See the comment in _to_result() for how to add it if needed.

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

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

_BASE = "https://api.themoviedb.org/3"
# TMDb image URLs: base + size slug + file_path from the API.
# "original" gives the highest resolution; other sizes: "w500", "w780", "w1280"
_IMG_BASE = "https://image.tmdb.org/t/p/original"


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class TMDbPlugin(MetadataPlugin):
    site_id = "tmdb"
    # "themoviedb" lets users use the full site name as a token in filenames.
    aliases = ("themoviedb",)

    # -----------------------------------------------------------------------
    # Lifecycle: connection management
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        # Shared httpx.Client — reuses TCP connections across fetch() calls.
        # No browser User-Agent needed: TMDb is a JSON API that doesn't check UA.
        self._client = httpx.Client(
            timeout=15,
            follow_redirects=True,
            limits=httpx.Limits(keepalive_expiry=30),
        )

    # -----------------------------------------------------------------------
    # Lifecycle: startup credential validation
    # -----------------------------------------------------------------------

    def setup(self) -> None:
        """Validate credentials at startup; raise to prevent plugin registration.

        WHY SMOKE-TEST /configuration:
          We check credentials immediately rather than discovering a bad token
          on the first real fetch() call. This surfaces auth problems at startup
          (visible in the logs) rather than making every file appear as "error"
          in the run report.

        WHY NOT RAISE ON NETWORK ERRORS:
          If TMDb is temporarily unreachable at startup (e.g. the container
          starts before network is available), we want the plugin to still
          register. Network errors in setup() are logged as warnings, not re-raised.
          The plugin will fail on the first fetch() if TMDb is truly unreachable.

        PITFALL: setup() raising RuntimeError here prevents the plugin from
          being registered AT ALL — every file that matches "% tmdb - ..." will
          appear as "unmatched" in the run report because no plugin handles it.
          That's the correct behavior when credentials are missing (it's a
          configuration error, not a per-file scraping failure).
        """
        api_key_present = bool(
            os.environ.get("TMDB_BEARER_TOKEN") or os.environ.get("TMDB_API_KEY")
        )
        if not api_key_present:
            raise RuntimeError(
                "Neither TMDB_BEARER_TOKEN nor TMDB_API_KEY is set. "
                "Register at https://www.themoviedb.org/signup and add one to your .env file."
            )
        # Smoke-test credentials against a cheap "is the key valid" endpoint.
        # /configuration returns API config (image base URLs, etc.) — small, fast.
        try:
            result = self._api_get("/configuration")
            if result is None:
                logger.warning("[tmdb] Credential check returned empty — will verify on first fetch()")
            else:
                logger.info("[tmdb] Credentials verified")
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            # Transient network error at startup — register anyway and let
            # the first fetch() surface the problem.
            logger.warning(
                "[tmdb] Could not reach TMDb at startup (%s) — plugin registered anyway, "
                "will retry on first fetch()", exc,
            )
        # Note: _api_get raises RuntimeError on 401 (invalid credentials).
        # That propagates through setup() and prevents plugin registration,
        # which is exactly what we want for a wrong key.

    def close(self) -> None:
        """Release the underlying connection pool on hot-reload."""
        self._client.close()

    def __enter__(self) -> TMDbPlugin:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -----------------------------------------------------------------------
    # Primary dispatch — fetch()
    # -----------------------------------------------------------------------

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Route the fetch to the appropriate strategy based on match_subtype."""
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

    # -----------------------------------------------------------------------
    # Strategy 1: Exact — direct movie lookup by TMDb ID
    # -----------------------------------------------------------------------

    def _fetch_by_id(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Direct lookup using the TMDb numeric movie ID — fastest, most accurate.

        WHY scene_id.isdigit() CHECK:
          TMDb IDs are always positive integers (e.g. 335988). If the filename
          contains a non-numeric token in the ID position (e.g. a slug), isdigit()
          is False and we return None rather than calling /movie/my-slug which
          would get a 400 or unexpected 200. This is a guard against malformed
          filenames, not an API limitation.

        PITFALL: Don't use int(scene_id) to validate — that raises ValueError on
          non-numeric strings. isdigit() is the correct guard here.
        """
        if not parsed.scene_id or not parsed.scene_id.isdigit():
            logger.warning("[tmdb] Exact match but scene_id is not a numeric TMDb ID: %r", parsed.scene_id)
            return None
        return self._fetch_movie(parsed.scene_id)

    # -----------------------------------------------------------------------
    # Strategy 2: Enhanced — ID first, then title search
    # -----------------------------------------------------------------------

    def _fetch_enhanced(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Try direct ID lookup first; fall back to title+year search.

        WHY ID FIRST:
          Enhanced filenames may include a scene_id. A direct ID lookup is
          faster (one API call vs. two: search + detail) and more accurate
          (no risk of picking the wrong result from a list).

        RELATIONSHIP TO _fetch_limited:
          _fetch_enhanced falls through to _search() on ID failure, making it
          behave like _fetch_limited as a fallback — but only after attempting
          the direct path first.
        """
        if parsed.scene_id and parsed.scene_id.isdigit():
            result = self._fetch_movie(parsed.scene_id)
            if result:
                return result
            logger.debug("[tmdb] Direct ID lookup failed; falling back to search")
        return self._search(parsed.title or "", parsed.date)

    # -----------------------------------------------------------------------
    # Strategy 3: Limited — title + optional year search
    # -----------------------------------------------------------------------

    def _fetch_limited(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Search-only path — used when only a title is available."""
        if not parsed.title:
            logger.warning("[tmdb] Limited match but no title available")
            return None
        return self._search(parsed.title, parsed.date)

    # -----------------------------------------------------------------------
    # Core fetch: detail + credits + images in one API call
    # -----------------------------------------------------------------------

    def _fetch_movie(self, movie_id: str) -> MetadataResult | None:
        """Fetch full movie metadata for a single TMDb numeric ID.

        WHY append_to_response:
          TMDb's append_to_response parameter bundles multiple sub-requests
          into a single HTTP response. Instead of three separate calls:
            GET /movie/335988
            GET /movie/335988/credits
            GET /movie/335988/images
          We make ONE call:
            GET /movie/335988?append_to_response=credits,images
          This halves API quota usage and reduces latency.

        The returned dict includes top-level movie fields plus:
          data["credits"]["cast"]   — list of cast members
          data["credits"]["crew"]   — list of crew members
          data["images"]["posters"] — list of poster image objects
          data["images"]["backdrops"] — list of background image objects
        """
        data = self._api_get(
            f"/movie/{movie_id}",
            params={"append_to_response": "credits,images"},
        )
        if not isinstance(data, dict):
            return None
        return self._to_result(data)

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    def _search(self, title: str, date: str | None) -> MetadataResult | None:
        """Search TMDb for a movie by title, optionally filtered by year.

        WHY PASS year TO THE SEARCH:
          The TMDb search API accepts a ``year`` param that narrows results to
          movies released in that year. Without it, a common title like "The
          Mummy" returns many results. Even an approximate year (from the
          filename date) dramatically improves result quality.

        RESULT SELECTION LOGIC:
          1. Exact title match (case-insensitive) → preferred.
          2. Top result (highest TMDb relevance score) → fallback.
          This avoids picking a sequel or remake when the exact title is present.

        WHY FETCH MOVIE AFTER SEARCH:
          The search endpoint returns limited fields (id, title, release_date).
          We need the full movie detail (genres, cast, images) which requires a
          second call to /movie/{id}?append_to_response=credits,images.
          TMDb does not support append_to_response on the search endpoint.
        """
        params: dict = {"query": title, "language": "en-US", "page": 1}
        if date and len(date) >= 4:
            try:
                params["year"] = int(date[:4])  # extract year from "YYYY-MM-DD"
            except ValueError:
                pass  # malformed date — omit year filter rather than crashing

        data = self._api_get("/search/movie", params=params)
        if not isinstance(data, dict):
            return None

        results = data.get("results") or []
        if not results:
            logger.info("[tmdb] Search found no results for %r", title)
            return None

        # Prefer exact title match; fall back to TMDb's top-ranked result.
        title_lower = title.lower()
        match = next(
            (r for r in results if r.get("title", "").lower() == title_lower),
            results[0],  # fallback to first result (highest TMDb relevance)
        )

        if len(results) > 1:
            logger.debug(
                "[tmdb] Search returned %d results for %r; selected %r",
                len(results), title, match.get("title"),
            )

        movie_id = str(match.get("id", ""))
        if not movie_id:
            return None

        # Fetch full detail for the chosen result (search response lacks credits/images).
        return self._fetch_movie(movie_id)

    # -----------------------------------------------------------------------
    # API helper
    # -----------------------------------------------------------------------

    def _api_get(self, path: str, params: dict | None = None) -> dict | None:
        """GET a TMDb v3 endpoint, authenticating via Bearer token or api_key param.

        AUTH PRIORITY:
          1. Bearer token (TMDB_BEARER_TOKEN) — modern, recommended.
             Sent as: Authorization: Bearer eyJ...
          2. API key (TMDB_API_KEY) — legacy, still works.
             Sent as query param: ?api_key=abc123

        WHY READ CREDENTIALS AT CALL TIME (NOT MODULE LEVEL):
          Reading from os.environ inside this method means key rotation takes
          effect without a container restart. setup() validates that SOME
          credential is present; _api_get trusts it will be there.

        ERROR HANDLING:
          - 404 → return None ("not found" is expected for some IDs)
          - 429 → raise ScrapeError (rate-limited; recorded as scrape_error)
          - 401 → raise RuntimeError (bad credentials; this is a config error)
          - Other 4xx/5xx → log + return None
          - Network errors → re-raise (recorded as error in run report)
        """
        # Read credentials at call time for key-rotation support.
        bearer = os.environ.get("TMDB_BEARER_TOKEN", "")
        api_key = os.environ.get("TMDB_API_KEY", "")

        if not bearer and not api_key:
            # This should never happen if setup() ran, but guard defensively.
            raise RuntimeError("No TMDb credentials — set TMDB_BEARER_TOKEN or TMDB_API_KEY")

        # Bearer token: sent as a request header (not in the URL).
        # When bearer is present, do NOT also add api_key to params — the API
        # will accept either auth method but mixing them can cause unexpected 401s.
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        all_params = dict(params or {})
        if not bearer and api_key:
            # Legacy api_key method: append to query string.
            all_params["api_key"] = api_key

        url = f"{_BASE}{path}"
        try:
            r = self._client.get(url, params=all_params, headers=headers)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 404:
                # Movie not found — normal for wrong or stale IDs.
                # Return None (not raise) so the caller treats it as "unmatched".
                logger.debug("[tmdb] Not found (404): %s", url)
                return None
            if status == 429:
                # Rate-limited. Raise ScrapeError (not a generic exception) so
                # main.py records this as status="scrape_error", distinguishing it
                # from a plugin crash. The file will be retried on the next run.
                logger.warning("[tmdb] Rate-limited (429): %s", url)
                raise ScrapeError(f"TMDb rate-limited: {url}") from exc
            if status == 401:
                # Bad credentials — this is a configuration error, not a per-file
                # issue. Raise RuntimeError so main.py records it prominently.
                # PITFALL: Don't catch RuntimeError here or in the callers —
                # let it propagate to the top-level handler so the operator sees it.
                raise RuntimeError(
                    "TMDb returned 401 Unauthorized — check TMDB_BEARER_TOKEN or TMDB_API_KEY"
                ) from exc
            # Other server errors (500, 503) — log and return None.
            logger.warning("[tmdb] HTTP %s for %s", status, url)
            return None
        except httpx.TimeoutException as exc:
            # Propagate — recorded as status="error" so the operator knows to
            # investigate (could be a transient issue or a rate-limit disguised as timeout).
            logger.warning("[tmdb] Timeout: %s", exc)
            raise
        except httpx.ConnectError as exc:
            logger.warning("[tmdb] Connection error: %s", exc)
            raise
        except Exception:
            # Truly unexpected (JSON decode error, etc.) — log and return None
            # rather than crashing the entire run. The movie will appear as
            # "unmatched" rather than "error" in the run report.
            logger.exception("[tmdb] Unexpected error fetching %s", url)
            return None

    # -----------------------------------------------------------------------
    # Map API response → MetadataResult — THE MOST IMPORTANT METHOD
    # -----------------------------------------------------------------------

    def _to_result(self, data: dict) -> MetadataResult | None:
        """Map a /movie/{id}?append_to_response=credits,images response.

        INPUT SHAPE (relevant fields from the TMDb API response):
          data = {
            "id":                  335988,
            "title":               "Venom",
            "overview":            "When ...",
            "release_date":        "2018-10-03",   # always YYYY-MM-DD from TMDb
            "vote_average":        6.8,            # float, 0–10
            "runtime":             112,            # minutes, int or None
            "genres":              [{"id": 28, "name": "Action"}, ...],
            "production_companies": [{"name": "Columbia Pictures", ...}, ...],
            "credits": {
              "cast": [{"name": "Tom Hardy", "order": 0}, ...],  # sorted by billing order
              "crew": [{"name": "Ruben Fleischer", "job": "Director"}, ...],
            },
            "images": {
              "posters":   [{"file_path": "/abc.jpg", "vote_average": 5.4}, ...],
              "backdrops": [{"file_path": "/def.jpg", "vote_average": 7.1}, ...],
            },
          }

        NOTES ON SPECIFIC FIELDS:
          vote_average: already 0–10, no conversion needed.
          release_date: TMDb always returns YYYY-MM-DD — safe to pass directly.
          cast.order: TMDb includes billing order; lower = higher billing.
            We sort by it to ensure "Tom Hardy" comes before "Michelle Williams".
          crew: multiple people may have job="Director" (co-directed films);
            dict.fromkeys() deduplicates while preserving order.
          images: sorted by vote_average; we pick the highest-voted poster and
            fanart. A nested helper _best_image() encapsulates this logic.

        CONTENT RATING NOTE:
          MPAA/content rating (e.g. "R", "PG-13") is NOT included in this response.
          To add it, include "release_dates" in append_to_response:
            GET /movie/{id}?append_to_response=credits,images,release_dates
          Then filter:
            release_dates = data.get("release_dates", {}).get("results", [])
            us = next((r for r in release_dates if r["iso_3166_1"] == "US"), None)
            rating = us["release_dates"][0]["certification"] if us else None
          See: https://developer.themoviedb.org/reference/movie-release-dates
        """
        title = data.get("title") or ""
        if not title:
            # A TMDb movie record without a title is broken data — return None
            # rather than constructing a MetadataResult with an empty title
            # (which would raise PluginValidationError in __post_init__ anyway).
            logger.warning("[tmdb] Movie id=%s has no title", data.get("id"))
            return None

        # ------------------------------------------------------------------
        # Release date and year
        # ------------------------------------------------------------------
        # TMDb always returns YYYY-MM-DD, so no format conversion needed.
        # We pass both release_date AND year explicitly — MetadataResult could
        # auto-derive year from release_date, but being explicit is clearer.
        release_date: str | None = data.get("release_date") or None
        year: int | None = None
        if release_date and len(release_date) >= 4:
            try:
                year = int(release_date[:4])
            except ValueError:
                pass

        # ------------------------------------------------------------------
        # Rating — TMDb vote_average is already 0–10
        # ------------------------------------------------------------------
        vote = data.get("vote_average")
        try:
            rating = float(vote) if vote is not None else None
            if rating is not None and not (0.0 <= rating <= 10.0):
                # Reject out-of-range ratings — MetadataResult.__post_init__
                # would also catch this, but clearing here avoids a PluginValidationError.
                rating = None
        except (TypeError, ValueError):
            rating = None

        # ------------------------------------------------------------------
        # Genres — list of {"id": ..., "name": "Action"} dicts
        # ------------------------------------------------------------------
        genres = [g["name"] for g in (data.get("genres") or []) if g.get("name")]

        # ------------------------------------------------------------------
        # Studio — first production company name
        # ------------------------------------------------------------------
        # TMDb returns a list of production companies; we use the first one.
        # For co-productions you could join them, but one name is usually enough.
        companies = data.get("production_companies") or []
        studio: str | None = companies[0]["name"] if companies and companies[0].get("name") else None

        # ------------------------------------------------------------------
        # Credits (present only when append_to_response=credits was used)
        # ------------------------------------------------------------------
        credits = data.get("credits") or {}

        # Cast: sorted by TMDb billing order (lower order = top billing).
        # WHY SORT: TMDb's API returns cast in billing order, but the sort
        # makes it explicit and robust against future API changes.
        cast = sorted(
            [c for c in (credits.get("cast") or []) if c.get("name")],
            key=lambda c: c.get("order", 999),  # 999 as sentinel for missing order
        )
        actors = [c["name"] for c in cast]

        # Directors: filter crew where job == "Director".
        # dict.fromkeys() deduplicates (some co-directors appear twice in TMDb data)
        # while preserving the order they appear in the crew list.
        directors = list(dict.fromkeys(
            c["name"]
            for c in (credits.get("crew") or [])
            if c.get("job") == "Director" and c.get("name")
        ))

        # ------------------------------------------------------------------
        # Artwork (present only when append_to_response=images was used)
        # ------------------------------------------------------------------
        images = data.get("images") or {}

        def _best_image(items: list[dict]) -> str | None:
            """Return the full URL for the highest-voted image in the list.

            WHY vote_average FOR SELECTION:
              TMDb's community votes on artwork quality. The highest-voted image
              is typically the most recognisable / best-cropped version. If all
              images have vote_average=0 (unvoted), max() picks the first one,
              which is usually the most recently uploaded.
            """
            valid = [i for i in items if i.get("file_path")]
            if not valid:
                return None
            best = max(valid, key=lambda i: i.get("vote_average") or 0)
            return f"{_IMG_BASE}{best['file_path']}"  # e.g. "https://image.tmdb.org/t/p/original/abc.jpg"

        poster_url = _best_image(images.get("posters") or [])
        fanart_url = _best_image(images.get("backdrops") or [])

        # ------------------------------------------------------------------
        # Runtime — encoded as a tag since MetadataResult has no runtime field
        # ------------------------------------------------------------------
        # MetadataResult has no dedicated "runtime" field (it's not in the NFO
        # spec that m3 targets). We encode it as a tag so it's preserved and
        # visible in the run report. Format: "runtime:112min"
        runtime_mins: int | None = data.get("runtime")
        tags = [f"runtime:{runtime_mins}min"] if runtime_mins else []

        # ------------------------------------------------------------------
        # Source ID and canonical URL
        # ------------------------------------------------------------------
        movie_id = str(data["id"]) if data.get("id") is not None else None
        # Build the canonical TMDb page URL for NFO <source> — provides
        # a human-readable link back to the original data source.
        source_url = f"https://www.themoviedb.org/movie/{movie_id}" if movie_id else None

        # ------------------------------------------------------------------
        # Assemble result
        # ------------------------------------------------------------------
        return MetadataResult(
            title=title,
            summary=data.get("overview"),       # long-form plot description
            year=year,                           # int, derived from release_date
            release_date=release_date,           # "YYYY-MM-DD" → NFO <premiered>
            rating=rating,                       # float 0–10 → NFO <rating>
            # content_rating is omitted — requires an extra API call.
            # See the docstring above for how to add it.
            content_rating=None,
            genres=genres,                       # list[str] → NFO <genre>, Plex genres
            actors=actors,                       # list[str], billing-ordered → NFO <actor>
            directors=directors,                 # list[str] → NFO <director>
            studio=studio,                       # first production company → NFO <studio>
            poster_url=poster_url,               # highest-voted poster image URL
            fanart_url=fanart_url,               # highest-voted backdrop image URL
            source_id=movie_id,                  # TMDb numeric ID → NFO <uniqueid>
            source_url=source_url,               # canonical TMDb page URL → NFO <source>
            tags=tags,                           # ["runtime:112min"] or []
        )
