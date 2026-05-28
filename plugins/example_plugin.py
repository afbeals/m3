"""
Example Plugin — ExampleSite

This file demonstrates how to write a plugin for m3. Copy it, rename it to
your site's name (e.g., warnerbros.py), and fill in the real API logic.

Drop the finished file into the plugins/ directory (mounted at /plugins in
the container) and restart the container to pick it up.

----------------------------------------------------------------------
Filename examples this plugin handles (site_id = "examplesite"):

  Enhanced:
    Jane Doe with Drama % examplesite - 19-06-15 - 98765 - An Interesting Plot
    Jane Doe with Drama % examplesite - 98765 - Jane Doe
    Jane Doe with Drama % ES - 19-06-15 - An Interesting Plot   (via alias)

  Limited:
    Jane Doe with Drama % examplesite - Jane Doe - An Interesting Plot

  Exact:
    Jane Doe with Drama % examplesite - 12345
    Jane Doe with Drama % examplesite - 2019-01-01 - 12345
    Jane Doe with Drama % examplesite - eager-hands
----------------------------------------------------------------------
"""

import logging
import os
from urllib.parse import quote

import httpx

from app.plugins.base import MetadataPlugin, MetadataResult, ParsedFilename

logger = logging.getLogger(__name__)

# Base URL for the example API — override in your real plugin.
BASE_URL = "https://api.example-site.com/v1"

# NOTE: Do NOT read API keys at module load time (e.g. `API_KEY = os.environ.get(...)`).
# Reading at load time means the key is captured once when the plugin is imported,
# so key rotation or late-setting of env vars won't take effect without a restart.
# Read from os.environ inside the method that needs it (see _api_get below).


class ExampleSitePlugin(MetadataPlugin):
    # Must match the site token in filenames: `% examplesite - ...`
    site_id = "examplesite"

    # Optional shorthand aliases users can also use in filenames: `% ES - ...`
    aliases = ("ES",)

    def __init__(self) -> None:
        # Share one httpx.Client across all fetch() calls so the underlying TCP
        # connection is reused between scenes in the same run. This reduces TLS
        # handshake overhead and is the recommended pattern for plugins that make
        # multiple requests per run.
        # Use a context manager (with ExampleSitePlugin() as p: ...) or call
        # p.close() when the plugin is no longer needed if you want deterministic
        # connection teardown. In normal m3 usage the process exits after each run,
        # so the OS reclaims the connection automatically.
        self._client = httpx.Client(timeout=15, follow_redirects=True)

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    def __enter__(self) -> "ExampleSitePlugin":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        """
        Called by the router with the parsed filename tokens.
        Return a MetadataResult, or None if the lookup fails.
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
            return self._fetch_by_id(parsed)
        elif subtype == "enhanced":
            return self._fetch_enhanced(parsed)
        elif subtype == "limited":
            return self._fetch_limited(parsed)
        else:
            logger.warning("[examplesite] Unsupported match subtype: %s", subtype)
            return None

    # ------------------------------------------------------------------
    # Exact match — use scene_id or direct_url to call the API directly
    # ------------------------------------------------------------------

    def _fetch_by_id(self, parsed: ParsedFilename) -> MetadataResult | None:
        scene_id = parsed.scene_id or parsed.direct_url
        if not scene_id:
            logger.warning("[examplesite] Exact match but no scene_id or direct_url")
            return None

        # Replace this with your real API call.
        # Example: GET https://api.example-site.com/v1/scenes/12345?api_key=...
        url_path = f"/scenes/{quote(str(scene_id), safe='')}"
        data = self._api_get(url_path)
        if not isinstance(data, dict):
            return None

        return self._to_result(data)

    # ------------------------------------------------------------------
    # Enhanced search — site supports title + actor + date + sceneID
    # ------------------------------------------------------------------

    def _fetch_enhanced(self, parsed: ParsedFilename) -> MetadataResult | None:
        params: dict = {}
        if parsed.title:
            params["q"] = parsed.title
        if parsed.actors:
            params["actor"] = parsed.actors[0]   # use primary actor
        if parsed.date:
            params["date"] = parsed.date
        if parsed.scene_id:
            params["scene_id"] = parsed.scene_id

        results = self._api_get("/scenes/search", params=params)
        if not results or not isinstance(results, list):
            return None
        result_dicts = [r for r in results if isinstance(r, dict)]
        if not result_dicts:
            return None

        # Take the first (best) result
        return self._to_result(result_dicts[0])

    # ------------------------------------------------------------------
    # Limited search — site supports title and/or actor only
    # ------------------------------------------------------------------

    def _fetch_limited(self, parsed: ParsedFilename) -> MetadataResult | None:
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

        return self._to_result(result_dicts[0])

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _api_get(self, path: str, params: dict | None = None) -> dict | list | None:
        """Make a GET request to the example API. Replace with your real logic."""
        # Read the key at call time so key rotation takes effect without restart.
        api_key = os.environ.get("EXAMPLESITE_API_KEY", "")
        if not api_key:
            logger.error("[examplesite] EXAMPLESITE_API_KEY is not set")
            return None

        url = f"{BASE_URL}{path}"
        all_params = {"api_key": api_key, **(params or {})}

        try:
            r = self._client.get(url, params=all_params)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            logger.warning("[examplesite] HTTP %s for %s", exc.response.status_code, url)
        except Exception:
            logger.exception("[examplesite] Request failed: %s", url)

        return None

    def _to_result(self, data: dict) -> MetadataResult | None:
        """
        Map the raw API response dict to a MetadataResult.
        Adjust field names to match your actual API response shape.
        """
        if not isinstance(data, dict):
            logger.warning("[examplesite] _to_result: expected dict, got %s", type(data).__name__)
            return None
        return MetadataResult(
            # Use `or` (not just `.get(..., default)`) so a present-but-null title
            # field also falls back to the default, instead of passing None to MetadataResult.
            title=data.get("title") or "Unknown Title",
            summary=data.get("description"),
            rating=data.get("rating"),
            year=data.get("year"),
            content_rating=data.get("content_rating"),
            genres=data.get("genres", []),
            tags=data.get("tags", []),
            labels=data.get("labels", []),
            actors=data.get("performers", []),
            poster_url=data.get("poster_url"),
            fanart_url=data.get("fanart_url"),
            source_url=data.get("url"),
            # Convert to str only when the value is actually present; passing None
            # is safe — MetadataResult.__post_init__ accepts None for source_id.
            source_id=str(data["id"]) if data.get("id") is not None else None,
        )
