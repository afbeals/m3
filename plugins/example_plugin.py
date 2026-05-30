"""
Example Plugin — ExampleSite  (JSON API template)
==================================================
PRIMARY TEMPLATE: Copy this file when the target site has a JSON REST API.
For HTML scraping (no API), copy example_html_plugin.py instead.

How to use this template
------------------------
1.  cp plugins/example_plugin.py plugins/mysite.py
2.  Change ``site_id = "examplesite"`` → your token (see Step 1 below).
3.  Set ``BASE_URL`` to your API's base URL.
4.  Update the env-var name in ``setup()`` and ``_api_get()``.
5.  Update ``_to_result()`` to map YOUR API's response field names.
6.  Validate: ``python -m app.main --validate-plugins``
7.  Test:     ``python -m app.main --test-plugin plugins/mysite.py
                  --filename "Jane Doe % mysite - 12345.mp4"``

----------------------------------------------------------------------
FILENAME FORMATS this plugin handles (site_id = "examplesite"):

  Enhanced (date + optional ID + title in the % payload):
    Jane Doe with Drama % examplesite - 19-06-15 - 98765 - An Interesting Plot
    Jane Doe with Drama % examplesite - 98765 - Jane Doe
    Jane Doe with Drama % ES - 19-06-15 - An Interesting Plot   (via alias)

  Limited (only title and/or actors — no date or ID):
    Jane Doe with Drama % examplesite - Jane Doe - An Interesting Plot

  Exact (scene ID or slug — highest confidence, fastest path):
    Jane Doe with Drama % examplesite - 12345
    Jane Doe with Drama % examplesite - 2019-01-01 - 12345
    Jane Doe with Drama % examplesite - eager-hands

The filename parser sets ``parsed.match_subtype`` to one of these values.
Use it in ``fetch()`` to route to the appropriate helper method.
----------------------------------------------------------------------
"""

import logging
import os
from urllib.parse import quote

import httpx

from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename, PluginValidationError
from app.scrape import ScrapeError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Base URL for the example API — ADAPT THIS to your real API's root URL.
# Keep it at module level (not in __init__) so it's easy to find and change.
BASE_URL = "https://api.example-site.com/v1"

# NOTE: Do NOT read API keys at module load time (e.g. `API_KEY = os.environ.get(...)`).
# Reading at load time means the key is captured once when the plugin is imported,
# so key rotation or late-setting of env vars won't take effect without a restart.
# Read from os.environ inside the method that needs it (see _api_get below).


# ---------------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------------

class ExampleSitePlugin(MetadataPlugin):
    # -----------------------------------------------------------------------
    # Class-level identity — MUST adapt these for your plugin
    # -----------------------------------------------------------------------

    # Must match the site token in filenames: `% examplesite - ...`
    # Use lowercase, no spaces (e.g. "warnerbros", "adulttime", "studioname").
    # The router lowercases both sides when matching, so case doesn't matter in
    # practice — but convention is lowercase here.
    site_id = "examplesite"

    # Optional shorthand aliases users can also use in filenames: `% ES - ...`
    # Use a TUPLE (not a list) — class-level mutable defaults are shared across
    # ALL subclasses, so an accidentally appended item would corrupt every plugin.
    aliases = ("ES",)

    # -----------------------------------------------------------------------
    # Lifecycle: connection management
    # -----------------------------------------------------------------------

    def __init__(self) -> None:
        # Share one httpx.Client across all fetch() calls so the underlying TCP
        # connection is reused between scenes in the same run. This reduces TLS
        # handshake overhead and is the recommended pattern for plugins that make
        # multiple requests per run.
        #
        # PITFALL: Don't create a new httpx.Client inside fetch() on every call —
        # that opens a new TCP+TLS connection for each scene and is ~10x slower.
        #
        # Use a context manager (with ExampleSitePlugin() as p: ...) or call
        # p.close() when the plugin is no longer needed if you want deterministic
        # connection teardown. In normal m3 usage the process exits after each run,
        # so the OS reclaims the connection automatically.
        self._client = httpx.Client(
            timeout=15,                               # seconds; raise if your API is slow
            follow_redirects=True,                    # follow 301/302 automatically
            limits=httpx.Limits(keepalive_expiry=30), # keep idle connections for 30 s
        )

    def close(self) -> None:
        """Release the underlying connection pool.

        Called automatically by the plugin loader during hot-reload (SIGUSR2 or
        --watch mode) when the old plugin instance is being replaced. Without
        this, file descriptors accumulate on each reload.
        """
        self._client.close()

    # __enter__/__exit__ allow using the plugin as a context manager in tests:
    #   with ExampleSitePlugin() as p:
    #       result = p.fetch(parsed)
    def __enter__(self) -> "ExampleSitePlugin":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    # -----------------------------------------------------------------------
    # Lifecycle: startup validation
    # -----------------------------------------------------------------------

    def setup(self) -> None:
        """Called ONCE after instantiation — RAISE here to block plugin registration.

        WHY THIS EXISTS:
          setup() is your chance to validate credentials, required env vars, or
          connectivity BEFORE any fetch() calls. If you raise here, the plugin is
          skipped entirely (with a logged error) rather than failing silently on
          every fetch() call at runtime.

        WHEN TO USE IT:
          - Validate required env vars (API key, secret, base URL override).
          - Optionally smoke-test credentials against a cheap API endpoint.
          - Log a clear startup message so operators can confirm the plugin loaded.

        WHEN NOT TO USE IT:
          - Don't raise on transient network errors (ConnectError, TimeoutException) —
            those may be temporary and you'd block the plugin unnecessarily. See the
            TMDb plugin's setup() for an example of the right pattern: catch network
            errors and log a warning without re-raising.

        PITFALL: If you raise a generic Exception here (instead of RuntimeError),
          Python will still prevent registration — but the logged message may be
          confusing. Prefer RuntimeError with a human-readable message that says
          exactly what to add to the .env file.
        """
        api_key = os.environ.get("EXAMPLESITE_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "EXAMPLESITE_API_KEY is not set. "
                "Add it to your .env file: EXAMPLESITE_API_KEY=your_key_here"
            )
        logger.debug("[examplesite] API key found — plugin ready")

    # -----------------------------------------------------------------------
    # Primary dispatch — fetch()
    # -----------------------------------------------------------------------

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Entry point called by the m3 router for every file this plugin owns.

        WHAT IT RECEIVES:
          ``parsed`` is a ParsedFilename dataclass produced by the filename
          parser. The fields most useful here:

            parsed.match_subtype  — "exact" | "enhanced" | "limited"
            parsed.scene_id       — numeric ID string, or None
            parsed.direct_url     — URL slug (e.g. "eager-hands"), or None
            parsed.title          — text title from the filename, or None
            parsed.actors         — list of actor names from the left side
            parsed.date           — YYYY-MM-DD release date, or None

        WHAT IT SHOULD RETURN:
          - A MetadataResult on success (the file will be written as "updated").
          - None if no matching record was found ("unmatched" in the run report).
            Do NOT raise for "not found" — raise only for actual errors.

        RAISE CONTRACT:
          - ScrapeError       → recorded as status="scrape_error" (network issue)
          - PluginValidationError → recorded as status="plugin_error" (bad data)
          - Anything else     → recorded as status="error" (unexpected crash)

        NOTE: fetch() is NEVER called with parsed.form == "add". The router
          handles Add-form filenames before dispatch; your plugin will never see them.
        """
        logger.info(
            "[examplesite] fetch called | subtype=%s scene_id=%s title=%r actors=%s",
            parsed.match_subtype,
            parsed.scene_id,
            parsed.title,
            parsed.actors,
        )

        subtype = parsed.match_subtype
        if subtype == "exact":
            # Exact: we have a scene ID or URL slug — fastest, most accurate path.
            return self._fetch_by_id(parsed)
        elif subtype == "enhanced":
            # Enhanced: we have date + possibly ID + title; try ID first, then search.
            return self._fetch_enhanced(parsed)
        elif subtype == "limited":
            # Limited: only title and/or actors; search-only, lowest confidence.
            return self._fetch_limited(parsed)
        else:
            # Unknown subtype — log and return None rather than crashing.
            # New subtypes could be added to the parser in future; handle gracefully.
            logger.warning("[examplesite] Unsupported match subtype: %s", subtype)
            return None

    # -----------------------------------------------------------------------
    # Strategy 1: Exact match — direct lookup by scene ID or URL slug
    # -----------------------------------------------------------------------

    def _fetch_by_id(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Direct API lookup — fastest and most accurate path.

        WHY IT'S FIRST:
          An exact ID or slug is deterministic: there's exactly one matching
          record. No ranking or confidence scoring needed.

        parsed.direct_url vs parsed.scene_id:
          - direct_url: a URL path slug like "eager-hands" (alphanumeric, not numeric).
            Appears in filenames like ``% examplesite - eager-hands``.
          - scene_id: a numeric string like "12345".
            Appears in filenames like ``% examplesite - 12345``.
          Check direct_url FIRST because some sites use slugs that look numeric
          (e.g. "12345-my-scene"). The parser uses heuristics that may classify
          these as scene_id — check both to be safe.
        """
        if parsed.direct_url:
            # URL slug — use the slug-based endpoint (adapt path to your site's API)
            url_path = f"/scenes/by-slug/{quote(parsed.direct_url, safe='-_')}"
            logger.debug("[examplesite] Slug lookup: %s", url_path)
        elif parsed.scene_id:
            # Numeric ID — use the direct ID endpoint
            url_path = f"/scenes/{quote(parsed.scene_id, safe='')}"
            logger.debug("[examplesite] ID lookup: %s", url_path)
        else:
            # Should not happen if the parser set subtype="exact", but guard anyway.
            # Return None (not raise) — this is "no data available", not a crash.
            logger.warning("[examplesite] Exact match but no scene_id or direct_url in parsed filename")
            return None
        data = self._api_get(url_path)
        if not isinstance(data, dict):
            # API returned None (404) or an unexpected type — scene not found.
            return None
        return self._to_result(data)

    # -----------------------------------------------------------------------
    # Strategy 2: Enhanced match — ID-first, then full search
    # -----------------------------------------------------------------------

    def _fetch_enhanced(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Search with maximum available signals: ID + title + actor + date.

        WHY IT EXISTS:
          Enhanced filenames have richer metadata than limited ones (they include
          date and optionally an ID), so we can make a more targeted search.
          The strategy is: try the direct ID path first (exact, unambiguous), and
          only fall back to text search if the ID path returns nothing.

        RELATIONSHIP TO _fetch_by_id:
          _fetch_enhanced calls _fetch_by_id first because a scene_id in an
          enhanced filename is just as deterministic as one in an exact filename.
          The difference is that enhanced filenames ALSO have a title, which lets
          us search as a fallback if the ID returns nothing (e.g. the ID is stale
          after a site migration).
        """
        # If we have a scene ID, use the direct endpoint — faster and unambiguous.
        if parsed.scene_id:
            result = self._fetch_by_id(parsed)
            if result is not None:
                return result
            # ID lookup failed (likely a 404 or site migration) — try text search.
            logger.debug("[examplesite] Direct ID lookup failed; falling back to search")

        # Build search params from whatever signals are available.
        # ADAPT: change the param names to match your API's query parameters.
        params: dict = {}
        if parsed.title:
            params["q"] = parsed.title          # ADAPT: may be "title", "search", "query"
        if parsed.actors:
            params["actor"] = parsed.actors[0]  # use primary actor (first in list)
        if parsed.date:
            params["date"] = parsed.date        # ADAPT: some APIs want "year" not "date"
        if parsed.scene_id:
            params["scene_id"] = parsed.scene_id

        results = self._api_get("/scenes/search", params=params)
        if not results or not isinstance(results, list):
            # No results at all, or the API returned an unexpected shape.
            return None
        result_dicts = [r for r in results if isinstance(r, dict)]
        if not result_dicts:
            return None

        # Prefer the result whose ID matches parsed.scene_id exactly.
        # If no ID to match (scene_id was None), take the top result.
        # PITFALL: Don't blindly trust result[0] if parsed.scene_id is set —
        # search APIs often return the "closest" match first, not the exact ID.
        if parsed.scene_id:
            match = next(
                (r for r in result_dicts if str(r.get("id", "")) == str(parsed.scene_id)),
                result_dicts[0],  # fallback to first result if no ID match
            )
        else:
            match = result_dicts[0]
        return self._to_result(match)

    # -----------------------------------------------------------------------
    # Strategy 3: Limited match — title/actor text search only
    # -----------------------------------------------------------------------

    def _fetch_limited(self, parsed: ParsedFilename) -> MetadataResult | None:
        """Lowest-confidence path: search by title and/or actor only.

        WHY IT'S LEAST CONFIDENT:
          Limited filenames have no date and no ID — just a title and possibly
          actors. Text search can return many plausible-but-wrong results.
          Always prefer exact or enhanced over limited; this is the last resort.

        PITFALL: Limited searches are the most error-prone because there's no
          date or ID to validate the result against. Consider adding a
          _score_match() helper (see PLUGIN_WRITING.md) to reject low-confidence
          results with ``return None`` rather than returning a wrong scene.
        """
        params: dict = {}
        if parsed.title:
            params["q"] = parsed.title
        if parsed.actors:
            params["actor"] = parsed.actors[0]

        results = self._api_get("/scenes/search", params=params)
        if not results or not isinstance(results, list):
            return None
        result_dicts = [r for r in results if isinstance(r, dict)]
        if not result_dicts:
            return None

        # Prefer result whose title exactly matches (case-insensitive).
        # If no exact match, fall back to the first result (highest API relevance).
        if parsed.title:
            title_lower = parsed.title.lower()
            match = next(
                (r for r in result_dicts if r.get("title", "").lower() == title_lower),
                result_dicts[0],  # fallback to first if no exact title match
            )
        else:
            match = result_dicts[0]

        if len(result_dicts) > 1:
            logger.debug(
                "[examplesite] Limited search returned %d results; selected %r",
                len(result_dicts), match.get("title", "?"),
            )
        return self._to_result(match)

    # -----------------------------------------------------------------------
    # HTTP helper
    # -----------------------------------------------------------------------

    def _api_get(self, path: str, params: dict | None = None) -> dict | list | None:
        """GET a single API endpoint. Returns the parsed JSON, or None on failure.

        This is the ONLY place that touches the network — all three strategy
        methods above call this helper. Centralising HTTP here means:
          - Error handling is in one place.
          - Tests can mock just ``_api_get`` to test strategy logic in isolation.
          - You only change authentication in one place.

        ADAPT: Replace with your real endpoint path, auth scheme (API key,
          Bearer token, session cookie, etc.), and any required headers.

        RAISE CONTRACT (what main.py expects):
          - ScrapeError          → status="scrape_error" (expected network issue)
          - httpx.TimeoutException / ConnectError → let propagate → status="error"
          - Any other exception  → let propagate → status="error"
          Returning None is safe and means "not found" — it will not crash the run.
        """
        # Read the key at call time so key rotation takes effect without restart.
        # Credentials are validated once in setup(); this is a belt-and-suspenders
        # check in case _api_get is called in a context where setup() was skipped.
        api_key = os.environ.get("EXAMPLESITE_API_KEY", "")

        url = f"{BASE_URL}{path}"
        # Merge the API key into params so it's included in every request.
        # ADAPT: your API may use a header instead: headers={"X-API-Key": api_key}
        all_params = {"api_key": api_key, **(params or {})}

        try:
            r = self._client.get(url, params=all_params)
            r.raise_for_status()   # raises HTTPStatusError for 4xx/5xx
            return r.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status == 429:
                # Rate-limited: raise ScrapeError so main.py records status="scrape_error"
                # (and the file is retried on the next run) rather than status="error"
                # (which would mark it as a plugin crash).
                logger.warning("[examplesite] Rate-limited (429) for %s — site is throttling requests", exc.request.url)
                raise ScrapeError(f"Rate-limited (429) by {exc.request.url}") from exc
            if status == 404:
                # Scene not found — return None (not an error; the caller handles it).
                # PITFALL: Don't raise ScrapeError for 404 — that would record the
                # file as "scrape_error" even though the real issue is just a wrong ID.
                logger.debug("[examplesite] Scene not found (404) for %s", exc.request.url)
                return None
            # All other HTTP errors (500, 503, etc.) — log and return None.
            # Alternatively, raise ScrapeError here to mark the file for retry.
            logger.warning("[examplesite] HTTP %s for %s", status, url)
        except httpx.TimeoutException as exc:
            # Network timeout — propagate so main.py records status="error"
            # and logs the traceback. Don't swallow timeouts silently.
            logger.warning("[examplesite] Request timed out for %s: %s", url, exc)
            raise  # main.py will catch this and record status="scrape_error"
        except httpx.ConnectError as exc:
            # DNS failure or refused connection — propagate for the same reason.
            logger.warning("[examplesite] Connection error for %s: %s", url, exc)
            raise
        except Exception:
            # Truly unexpected error (JSON decode failure, etc.) — log the full
            # traceback and re-raise. Swallowing unexpected errors makes bugs
            # invisible in the run report.
            logger.exception("[examplesite] Unexpected error fetching %s", url)
            raise  # propagates to main.py as status="error", not "unmatched"

        return None

    # -----------------------------------------------------------------------
    # Result mapping — THE MOST IMPORTANT METHOD TO ADAPT
    # -----------------------------------------------------------------------

    def _to_result(self, data: dict) -> MetadataResult | None:
        """Map a raw API response dict to a MetadataResult.

        THIS IS THE METHOD YOU WILL CHANGE MOST.
        Every site returns different field names for the same concepts. Your job
        here is to translate "your site's shape" → "m3's shape".

        HOW TO FIND YOUR FIELD NAMES:
          1. Run ``python -m app.main --test-plugin plugins/mysite.py
                --filename "Jane Doe % mysite - 12345.mp4"``
             and add a ``logger.debug("[mysite] raw data: %s", data)`` line here
             to dump the full API response, then read its keys.
          2. Cross-reference with your API's documentation.

        METADATARESULT FIELD REFERENCE:
          title         → NFO <title>, Plex title. Required — must be non-empty.
          summary       → NFO <plot>, Plex summary/description.
          rating        → NFO <rating>, Plex audience rating. Float, 0.0–10.0.
          year          → NFO <year>. Int. Auto-derived from release_date if omitted.
          release_date  → NFO <premiered>. String, MUST be "YYYY-MM-DD" format.
                          __post_init__ validates this and clears it on bad format.
                          If you set both year and release_date, year wins for the
                          NFO <year> field; release_date is used for <premiered>.
          content_rating → NFO <mpaa> / <contentrating>. String (e.g. "NR", "R").
          genres        → NFO <genre> list, Plex genres. Empty list = leave Plex unchanged.
          tags          → NFO <tag> list, Plex tags. Empty list = leave Plex unchanged.
          labels        → Plex labels only (flexible bucket, not in NFO). Empty = unchanged.
          actors        → NFO <actor> list, Plex cast. Empty list = leave Plex unchanged.
          directors     → NFO <director> list, Plex directors. Empty list = leave unchanged.
          studio        → NFO <studio>, Plex studio.
          poster_url    → URL downloaded and saved as poster sidecar image.
          fanart_url    → URL downloaded and saved as fanart sidecar image.
          source_url    → NFO <source> (canonical page URL for this item).
          source_id     → NFO <uniqueid> (site's internal ID, for traceability).

        EMPTY LIST vs OMITTING:
          An empty list ``actors=[]`` tells the writers to LEAVE the existing Plex
          values unchanged. If you explicitly want to clear them, you'd have to set
          a non-empty list — but there's no "clear" sentinel. Default to [] when
          the API doesn't return a field, not None (None raises PluginValidationError).
        """
        if not isinstance(data, dict):
            # Guard against calling _to_result on a list or None (shouldn't happen
            # if callers check types, but defensive coding saves hours of debugging).
            logger.warning("[examplesite] _to_result: expected dict, got %s", type(data).__name__)
            return None

        # OPTIONAL: raise PluginValidationError here for custom business-logic checks.
        # This is the right place for: out-of-range ratings, required fields missing,
        # or any site-specific invariant you want to enforce.
        # Example:
        # if data.get("rating", 0) > 10:
        #     raise PluginValidationError(f"Rating {data['rating']} exceeds 10.0 — check the API response")

        return MetadataResult(
            # ADAPT: replace each "data.get(...)" key with your API's actual field name.

            # Title: use `or "Unknown Title"` (not just `.get("title", "Unknown Title")`)
            # so that a present-but-null API field (which becomes Python None) ALSO
            # falls back to the default string instead of passing None to MetadataResult
            # and triggering a PluginValidationError in __post_init__.
            title=data.get("title") or "Unknown Title",

            summary=data.get("description"),     # ADAPT: may be "overview", "plot", "synopsis"

            # PITFALL: Don't pass an integer rating to MetadataResult — it must be
            # a float. data.get("rating") returns whatever the API gives; if your
            # API returns an integer (e.g. 8) rather than a float (8.0), Python will
            # accept it without complaint, but being explicit with float() is safer.
            rating=data.get("rating"),           # float 0.0–10.0; ADAPT field name

            year=data.get("year"),               # int; omit if setting release_date (auto-derived)

            # IMPORTANT: release_date must be "YYYY-MM-DD". If your API returns
            # "March 15, 2024" or a Unix timestamp, convert it here before passing.
            # MetadataResult.__post_init__ validates the format and clears it with a
            # warning (rather than crashing) if the format is wrong.
            release_date=data.get("release_date"),  # ISO date string e.g. "2024-03-15"

            content_rating=data.get("content_rating"),  # ADAPT: "mpaa", "rating_code", etc.

            genres=data.get("genres", []),       # list[str]; ADAPT field name
            tags=data.get("tags", []),           # list[str]; ADAPT field name
            labels=data.get("labels", []),       # list[str]; Plex-only bucket

            # ADAPT: your API may call performers "cast", "models", "talent", etc.
            actors=data.get("performers", []),   # list[str] of actor names

            # ADAPT: may be "crew", or directors may be nested in a "crew" list.
            directors=data.get("directors", []), # list[str] of director name strings

            poster_url=data.get("poster_url"),
            fanart_url=data.get("fanart_url"),
            source_url=data.get("url"),          # ADAPT: may be "scene_url", "link", etc.

            # Convert to str only when the value is actually present; passing None
            # is safe — MetadataResult.__post_init__ accepts None for source_id.
            # PITFALL: Never do ``source_id=str(data.get("id"))`` — if "id" is absent,
            # data.get("id") returns None, and str(None) = "None" (the literal string),
            # which __post_init__ will catch and clear with a warning. Use the pattern
            # below: check for None before converting.
            source_id=str(data["id"]) if data.get("id") is not None else None,
        )
