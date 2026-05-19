# -----------------------------------------------------------------------------
# app/writers/plex.py
#
# Pushes metadata to Plex Media Server via the PlexAPI library.
#
# Why use Plex API at all if we already write NFO sidecars?
#   NFO sidecars are the portable backup, but Plex doesn't pick them up
#   automatically unless you run a scan. Pushing directly via the API updates
#   Plex's database instantly, with no rescan needed.
#
# Field locking:
#   Every field we write is "locked" so that Plex's built-in agent doesn't
#   overwrite it the next time it refreshes the library. Without locks, Plex
#   would silently discard our custom metadata on its next scheduled refresh.
#   Locked fields show a padlock icon in the Plex item editor UI.
#
# Fallback behaviour:
#   If Plex is unreachable (connect_plex returns None), the app continues and
#   only writes NFO sidecars. The Plex push is always non-fatal.
# -----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import time

from plexapi.server import PlexServer

from app.plugins.base import MetadataResult

logger = logging.getLogger(__name__)


def connect_plex(plex_url: str, plex_token: str) -> PlexServer | None:
    """
    Establish a connection to the Plex Media Server.
    Returns None if the connection fails (app continues in sidecar-only mode).
    """
    try:
        server = PlexServer(plex_url, plex_token)
        logger.info("Connected to Plex: %s", server.friendlyName)
        return server
    except Exception:
        # WARNING not ERROR: a failed connection is recoverable — the run continues
        # in sidecar-only mode and the Plex push is non-fatal by design.
        logger.warning("Failed to connect to Plex at %s", plex_url, exc_info=True)
        return None


def find_plex_item(server: PlexServer, file_path: str, _fallback_cache: dict | None = None):
    """
    Find the Plex library item that corresponds to a given media file path.

    Tries a fast filepath filter first; falls back to a full library scan
    if the filter doesn't return results (some Plex versions don't support it).

    _fallback_cache: optional dict (path → item | None) shared across calls for
    the same run. The slow scan is O(library size) and runs per-file without this
    cache, so on older Plex versions a 1000-file run would do 1000 full scans.
    Pass an empty dict at the start of each run and reuse it for all push_to_plex
    calls to reduce the slow path from O(n * library) to O(library + n).
    """
    try:
        # Fast path: filter by the exact file path stored in Plex's database
        results = server.library.search(filters={"media.filepath": file_path})
        if results:
            return results[0]

        # Slow fallback: iterate all sections → items → media parts to find a match.
        # O(library size) — only runs when the fast filepath filter isn't supported.
        # A 60-second wall-clock guard prevents this from hanging a run indefinitely
        # on very large libraries.
        if _fallback_cache is not None and file_path in _fallback_cache:
            return _fallback_cache[file_path]

        logger.warning(
            "Fast Plex filepath filter returned no results for %s — falling back to full "
            "library scan. This may be slow on large libraries.", file_path
        )
        deadline = time.monotonic() + 60
        found = None
        for section in server.library.sections():
            for item in section.search():
                if time.monotonic() > deadline:
                    logger.warning(
                        "Plex full-library scan timed out after 60s searching for %s", file_path
                    )
                    # Do NOT cache None on timeout — a transient timeout should not
                    # permanently prevent future retries within the same run.
                    return None
                for media in item.media:
                    for part in media.parts:
                        if _fallback_cache is not None:
                            _fallback_cache[part.file] = item
                        if part.file == file_path:
                            found = item

        if _fallback_cache is not None and file_path not in _fallback_cache:
            _fallback_cache[file_path] = found
        return found

    except Exception:
        logger.exception("Error searching Plex for file: %s", file_path)

    return None


def push_to_plex(
    server: PlexServer,
    file_path: str,
    result: MetadataResult,
    _fallback_cache: dict | None = None,
) -> bool:
    """
    Update the Plex item for file_path with all fields from MetadataResult.
    Each field is written with a lock (.locked = 1) to prevent Plex from
    overwriting it during the next library refresh.

    Genres, labels, tags, and actors are written first (add new), then old
    values are cleared so a mid-call failure leaves the item with both old and
    new values rather than no values at all.

    _fallback_cache: optional dict for the slow-scan path; see find_plex_item.

    Returns True on success, False if the item wasn't found or an error occurred.
    """
    item = find_plex_item(server, file_path, _fallback_cache=_fallback_cache)
    if item is None:
        logger.warning("Plex item not found for: %s", file_path)
        return False

    try:
        # Build a dict of field edits — each field needs both a .value and a .locked key.
        # The .locked = 1 flag tells Plex's built-in metadata agent to leave that field
        # alone on its next scheduled refresh.  Without the lock, Plex silently overwrites
        # our custom metadata the next time it runs its own agent (e.g. The Movie Database).
        # Locked fields show a padlock icon in the Plex item editor so users can see
        # which fields are under external control.
        edits: dict = {}

        if result.title:
            edits["title.value"] = result.title
            edits["title.locked"] = 1
        if result.summary:
            edits["summary.value"] = result.summary
            edits["summary.locked"] = 1
        if result.rating is not None:
            edits["rating.value"] = result.rating
            edits["rating.locked"] = 1
        if result.content_rating:
            edits["contentRating.value"] = result.content_rating
            edits["contentRating.locked"] = 1
        if result.year:
            edits["year.value"] = result.year
            edits["year.locked"] = 1

        # Apply all scalar field edits in one API call
        if edits:
            item.edit(**edits)

        # Add the new list-field values first, then remove the old ones.
        # This ordering means a mid-call failure leaves the item with both old
        # and new values rather than no values at all (which would be worse).
        # Each addX / removeX is a separate HTTP round-trip to the Plex API.
        # Only remove stale values when we have new ones to replace them with —
        # calling removeGenres() with an empty result.genres would clear genres
        # that Plex already had without writing any replacement values.
        for genre in result.genres:
            item.addGenre(genre, locked=True)
        for label in result.labels:
            item.addLabel(label, locked=True)
        for tag in result.tags:
            item.addTag(tag, locked=True)
        for actor in result.actors:
            item.addActor(actor, locked=True)

        if result.genres:
            item.removeGenres()
        if result.labels:
            item.removeLabels()
        if result.tags:
            item.removeTags()
        if result.actors:
            item.removeActors()

        # Upload poster and background art directly to Plex from the remote URLs
        if result.poster_url:
            item.uploadPoster(url=result.poster_url)
        if result.fanart_url:
            item.uploadArt(url=result.fanart_url)

        logger.info("Updated Plex item: %s", item.title)
        return True

    except Exception:
        logger.exception("Failed to update Plex item for: %s", file_path)
        return False


def push_nfo_to_plex(
    server: PlexServer,
    file_path: str,
    nfo_path: str,
    _fallback_cache: dict | None = None,
) -> bool:
    """Re-push metadata to Plex from an existing NFO sidecar file.

    Used during the rename workflow: after assets are renamed on disk, the NFO
    already has the correct metadata but Plex's database entry for the new file
    path has no custom fields (Plex sees a new file path after the rename).
    This reads the NFO and re-pushes all fields it can extract.

    Scalar fields pushed: title, plot/summary, rating, content rating, year.
    List fields pushed: genres, actors (tag elements in the NFO).
    Images: uploaded from the local <stem>-poster.jpg / <stem>-fanart.jpg files
    next to the NFO (avoids re-downloading).

    Returns True on success, False if the item wasn't found or an error occurred.
    """
    try:
        from lxml import etree
        tree = etree.parse(nfo_path)
        root = tree.getroot()
    except Exception:
        logger.exception("Could not parse NFO for Plex push: %s", nfo_path)
        return False

    def _text(tag: str) -> str:
        el = root.find(tag)
        return (el.text or "").strip() if el is not None else ""

    item = find_plex_item(server, file_path, _fallback_cache=_fallback_cache)
    if item is None:
        logger.warning("Plex item not found for renamed file: %s", file_path)
        return False

    try:
        edits: dict = {}
        if title := _text("title"):
            edits["title.value"] = title
            edits["title.locked"] = 1
        if plot := _text("plot"):
            edits["summary.value"] = plot
            edits["summary.locked"] = 1
        if rating_str := _text("rating"):
            try:
                edits["rating.value"] = float(rating_str)
                edits["rating.locked"] = 1
            except ValueError:
                pass
        if mpaa := _text("mpaa"):
            edits["contentRating.value"] = mpaa
            edits["contentRating.locked"] = 1
        if year_str := _text("year"):
            try:
                edits["year.value"] = int(year_str)
                edits["year.locked"] = 1
            except ValueError:
                pass
        if edits:
            item.edit(**edits)

        genres = [el.text.strip() for el in root.findall("genre") if el.text]
        actors = [el.text.strip() for el in root.findall("actor/name") if el.text]
        tags = [el.text.strip() for el in root.findall("tag") if el.text]
        labels = [el.text.strip() for el in root.findall("label") if el.text]

        # Add new values first, then remove old — same add-first ordering as
        # push_to_plex. Only call remove when we actually added something; calling
        # removeGenres() with nothing added would wipe whatever Plex already had.
        for genre in genres:
            item.addGenre(genre, locked=True)
        for actor in actors:
            item.addActor(actor, locked=True)
        for tag in tags:
            item.addTag(tag, locked=True)
        for label in labels:
            item.addLabel(label, locked=True)

        if genres:
            item.removeGenres()
        if actors:
            item.removeActors()
        if tags:
            item.removeTags()
        if labels:
            item.removeLabels()

        # Upload images from the local renamed files — avoids a redundant network
        # round-trip to the source site since the content hasn't changed.
        dirpath = os.path.dirname(nfo_path)
        stem = os.path.splitext(os.path.basename(nfo_path))[0]
        poster_path = os.path.join(dirpath, f"{stem}-poster.jpg")
        fanart_path = os.path.join(dirpath, f"{stem}-fanart.jpg")
        if os.path.exists(poster_path):
            item.uploadPoster(filepath=poster_path)
        if os.path.exists(fanart_path):
            item.uploadArt(filepath=fanart_path)

        logger.info("Re-pushed Plex metadata for renamed item: %s", item.title)
        return True

    except Exception:
        logger.exception("Failed to re-push Plex metadata for renamed file: %s", file_path)
        return False
