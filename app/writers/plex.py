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
        logger.exception("Failed to connect to Plex at %s", plex_url)
        return None


def find_plex_item(server: PlexServer, file_path: str):
    """
    Find the Plex library item that corresponds to a given media file path.

    Tries a fast filepath filter first; falls back to a full library scan
    if the filter doesn't return results (some Plex versions don't support it).
    Returns None if the file isn't found in any library section.
    """
    try:
        # Fast path: filter by the exact file path stored in Plex's database
        results = server.library.search(filters={"media.filepath": file_path})
        if results:
            return results[0]

        # Slow fallback: iterate all sections → items → media parts to find a match.
        # This is O(library size) and can be very slow on large libraries.
        # It only runs when the fast filepath filter isn't supported by this Plex version.
        logger.warning(
            "Fast Plex filepath filter returned no results for %s — falling back to full "
            "library scan. This may be slow on large libraries.", file_path
        )
        for section in server.library.sections():
            for item in section.search():
                for media in item.media:
                    for part in media.parts:
                        if part.file == file_path:
                            return item
    except Exception:
        logger.exception("Error searching Plex for file: %s", file_path)

    return None


def push_to_plex(server: PlexServer, file_path: str, result: MetadataResult) -> bool:
    """
    Update the Plex item for file_path with all fields from MetadataResult.
    Each field is written with a lock (.locked = 1) to prevent Plex from
    overwriting it during the next library refresh.

    Genres, labels, tags, and actors are cleared before writing so that
    re-runs with --force don't accumulate stale values from previous metadata.

    Returns True on success, False if the item wasn't found or an error occurred.
    """
    item = find_plex_item(server, file_path)
    if item is None:
        logger.warning("Plex item not found for: %s", file_path)
        return False

    try:
        # Build a dict of field edits — each field needs both a .value and a .locked key
        edits: dict = {}

        if result.title:
            edits["title.value"] = result.title
            edits["title.locked"] = 1          # prevents Plex agent from overwriting
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

        # Clear existing list fields before writing so re-runs don't accumulate
        # stale values alongside the new ones.
        item.removeGenres()
        item.removeLabels()
        item.removeTags()

        # Add fresh values with locks so the Plex agent can't clear them on refresh
        for genre in result.genres:
            item.addGenre(genre, locked=True)
        for label in result.labels:
            item.addLabel(label, locked=True)
        for tag in result.tags:
            item.addTag(tag, locked=True)
        for actor in result.actors:
            item.addActor(actor, locked=True)

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
