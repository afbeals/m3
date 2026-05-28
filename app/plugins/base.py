# -----------------------------------------------------------------------------
# app/plugins/base.py
#
# Core data types shared across the whole application.
#
# ParsedFilename — the structured output of the universal filename parser.
#   Every field the parser can extract from a filename lives here. Plugins
#   receive a ParsedFilename and use whichever fields their API needs.
#
# MetadataResult — the normalised metadata a plugin returns after calling
#   its API. Both the NFO writer and the Plex writer consume this type.
#
# MetadataPlugin — the abstract base class every plugin must subclass.
#   A plugin declares its site_id (and optional aliases), and implements
#   fetch() to call its API and return a MetadataResult.
# -----------------------------------------------------------------------------

from __future__ import annotations

import inspect
import logging
import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal

_logger = logging.getLogger(__name__)


# The four search subtypes the parser can detect (see FILENAME_PATTERNS.md)
MatchSubtype = Literal["enhanced", "limited", "exact", "add"]

# Top-level filename form: "add" = Manual Add form, "general" = everything else
FilenameForm = Literal["add", "general"]


@dataclass
class ParsedFilename:
    # Which top-level form was detected ("add" or "general")
    form: FilenameForm

    # --- Fields shared by both forms ---

    # Comma-separated actor names extracted from the left side of the filename
    actors: list[str] = field(default_factory=list)
    # Comma-separated genres extracted from "with <genres>" in the filename
    genres: list[str] = field(default_factory=list)

    # --- General form fields (populated when form == "general") ---

    # The site token extracted from the % payload (lowercased), e.g. "warnerbros"
    site: str | None = None
    # Which search strategy the parser recommends for this payload
    match_subtype: MatchSubtype = "exact"
    # Release date, normalised to YYYY-MM-DD
    date: str | None = None
    # Numeric scene ID from the URL of the scene page
    scene_id: str | None = None
    # Numeric studio ID (used on sites that host many independent studios)
    studio_id: str | None = None
    # Alphanumeric actress ID from the URL of the actress page
    actress_id: str | None = None
    # Direct URL slug — the path suffix of a scene URL (e.g. "eager-hands")
    direct_url: str | None = None
    # Free-text title extracted from the payload
    title: str | None = None
    # Additional actor tokens found inside the match payload
    extra_actors: list[str] = field(default_factory=list)

    # --- Manual Add form fields (populated when form == "add") ---

    # Studio name from "At <Studio>" in the filename
    studio: str | None = None

    # The raw string after the % character — plugins can re-parse this themselves
    # if they need more control than the structured fields provide
    raw_match_payload: str | None = None


@dataclass
class MetadataResult:
    # Resolved full title for this scene/movie (required, must be non-empty)
    title: str

    # Long-form description / plot summary
    summary: str | None = None
    # Numeric rating (e.g. 7.5 out of 10)
    rating: float | None = None

    # Lists of classification metadata — all written to both NFO and Plex
    genres: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)   # Plex labels (flexible bucket)
    tags: list[str] = field(default_factory=list)
    actors: list[str] = field(default_factory=list)

    # Remote URLs for artwork — downloaded and saved as sidecar image files
    poster_url: str | None = None
    fanart_url: str | None = None

    # Release year
    year: int | None = None
    # Content rating string (e.g. "NR", "R", "TV-MA")
    content_rating: str | None = None

    # Canonical page URL for this item on the source site (written to NFO <source>)
    source_url: str | None = None
    # Site-internal ID — stored in NFO <uniqueid> for traceability
    source_id: str | None = None

    def __post_init__(self) -> None:
        # Validate at the plugin boundary so bad data never reaches the writers.
        # A missing title would produce a corrupt NFO and garbage in Plex.
        if not self.title or not self.title.strip():
            raise ValueError("MetadataResult.title must be a non-empty string")
        # Normalise: strip whitespace from title
        self.title = self.title.strip()
        # Ensure list fields are actually lists (guard against plugins returning None)
        for field_name in ("actors", "genres", "tags", "labels"):
            if getattr(self, field_name, None) is None:
                _logger.warning(
                    "MetadataResult.%s was None from plugin — coercing to []. "
                    "Plugin should return an empty list, not None.",
                    field_name,
                )
                # Only set attributes that actually exist on this dataclass
                if hasattr(self, field_name):
                    setattr(self, field_name, [])
        # Validate rating is in a sensible range if provided.
        # math.isfinite() explicitly rejects float('nan') and float('inf').
        # Note: nan is also caught by the chained comparison alone (0.0 <= nan
        # evaluates to False), but isfinite() makes the intent unambiguous.
        if self.rating is not None:
            if not math.isfinite(self.rating) or not (0.0 <= self.rating <= 10.0):
                raise ValueError(
                    f"rating must be a finite number 0–10, got {self.rating!r}"
                )
        # Guard against plugins passing str(None) = "None" as source_id, summary,
        # content_rating, or source_url (common mistake when building results from
        # API responses that may return Python None coerced to the string "None").
        for str_field in ("source_id", "summary", "content_rating", "source_url"):
            val = getattr(self, str_field, None)
            if isinstance(val, str) and val.strip().lower() in ("none", ""):
                setattr(self, str_field, None)


class MetadataPlugin(ABC):
    # The canonical identifier that must match the site token in filenames.
    # e.g. site_id = "warnerbros" matches "% warnerbros - 12345"
    site_id: str = ""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Only check concrete subclasses (not abstract ones)
        if not inspect.isabstract(cls) and not cls.site_id:
            raise TypeError(
                f"Plugin class {cls.__name__!r} must set a non-empty class-level "
                f"'site_id' attribute. Did you accidentally set it in __init__ instead?"
            )

    # Optional shorthand aliases users can also use in filenames.
    # e.g. aliases = ("WB",) means "% WB - 12345" also routes here.
    #
    # Tuple (not list) so the class-level default is immutable — a mutable list
    # default is shared across all subclasses that don't override it, making it
    # easy to accidentally corrupt all plugins with a single .append() call.
    aliases: tuple[str, ...] = ()  # subclasses should override: aliases = ("MY", "ALIAS")

    @abstractmethod
    def fetch(self, parsed: ParsedFilename) -> MetadataResult | None:
        """
        Fetch metadata for the given parsed filename from the plugin's API.

        Use parsed.match_subtype to decide which search strategy to use:
          "exact"    — use parsed.scene_id or parsed.direct_url for a direct lookup
          "enhanced" — search with title, actors, date, and/or scene_id
          "limited"  — search with title and/or actors only

        Return None if the lookup fails or returns no usable result.
        """
        ...

    def close(self) -> None:
        """Called by the loader when the plugin is being replaced. Release any open resources."""
        pass

    def all_ids(self) -> list[str]:
        """Return all identifiers this plugin responds to (site_id + aliases), lowercased.

        Returns an empty list if site_id is not set — callers should treat an
        empty result as "this plugin is not registerable".
        """
        if not self.site_id:
            return []
        ids = [self.site_id.lower()]
        ids.extend(a.lower() for a in self.aliases if a)
        return ids
