# -----------------------------------------------------------------------------
# app/router.py
#
# Dispatches a media filename to the correct plugin using O(1) dict lookup.
#
# Flow:
#   1. The universal parser (app/parser.py) decodes the filename stem into a
#      structured ParsedFilename, extracting actors, genres, site ID, etc.
#   2. If parsing fails → return (None, None); file goes to the unmatched report.
#   3. If the filename uses the "Add" form (starts with keyword "Add") → return
#      (parsed, None); file goes to the manual-add pending list.
#   4. Otherwise, look up the site token in the plugin registry dict.
#      The dict is keyed by site_id and all aliases (lowercase), so both
#      "warnerbros" and "WB" resolve to the same plugin instance in O(1).
#   5. If no plugin is registered for the site → return (parsed, None); file
#      goes to the unmatched report with a "no plugin for site X" message.
# -----------------------------------------------------------------------------

from __future__ import annotations

import logging

from app.parser import parse
from app.plugins.base import MetadataPlugin, ParsedFilename

logger = logging.getLogger(__name__)


class Router:
    def __init__(self, registry: dict[str, MetadataPlugin]) -> None:
        # registry maps lowercase site_id / alias → plugin instance
        # Built by app/plugins/loader.py from the drop-in plugin directory
        self._registry = registry

    def dispatch(self, filename_stem: str) -> tuple[ParsedFilename | None, MetadataPlugin | None]:
        """
        Parse the filename and return (ParsedFilename, plugin).

        Return values:
          (None,   None)   — filename could not be parsed at all
          (parsed, None)   — parsed OK but no plugin handles it (Add form or unknown site)
          (parsed, plugin) — ready to call plugin.fetch(parsed)
        """
        # Step 1: decode the filename into structured tokens
        parsed = parse(filename_stem)

        if parsed is None:
            # Filename doesn't match either known form — log and surface in report
            logger.debug("Could not parse filename: %r", filename_stem)
            return None, None

        if parsed.form == "add":
            # Manual Add form: no API call needed, just surface for human review
            logger.debug("Manual Add form detected for: %r", filename_stem)
            return parsed, None

        if not parsed.site or not parsed.site.strip():
            # General form but no % token found (or whitespace-only) — shouldn't happen
            # after parsing, but guard anyway. Log the actual value to aid debugging.
            logger.debug("No site token found in: %r (site=%r)", filename_stem, parsed.site)
            return parsed, None

        # Step 2: O(1) plugin lookup by site id (already lowercased by the parser)
        plugin = self._registry.get(parsed.site.lower())
        if plugin is None:
            logger.debug("No plugin registered for site %r in: %r", parsed.site, filename_stem)

        return parsed, plugin
