# -----------------------------------------------------------------------------
# app/writers/nfo.py
#
# Writes metadata to disk as Kodi-compatible NFO sidecar files and downloads
# poster/fanart images alongside the media file.
#
# NFO format:
#   Follows the Kodi NFO spec (https://kodi.wiki/view/NFO_files/Movies) so the
#   sidecars work with Kodi, Jellyfin, and Plex (via XBMCnfoMoviesImporter).
#   Each video file gets a matching .nfo file with the same stem:
#     My Movie.mp4  →  My Movie.nfo
#
# Images:
#   Poster and fanart are downloaded from the URLs in MetadataResult and saved
#   as <stem>-poster.jpg and <stem>-fanart.jpg next to the video file.
#   These filenames are also referenced inside the NFO <art> block.
#
# Why write sidecars at all?
#   Plex DB is the fast path, but it's not portable — if you rebuild Plex or
#   migrate to Jellyfin, all custom metadata is lost. NFO sidecars travel with
#   the media files and serve as the canonical source of truth.
# -----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import time

import httpx
from lxml import etree

from app.plugins.base import MetadataResult
from app.scanner import MediaFile

logger = logging.getLogger(__name__)

# Retry settings for image downloads.
# Exponential backoff: wait 2s, 4s, 8s between attempts before giving up.
_DOWNLOAD_MAX_ATTEMPTS = 3
_DOWNLOAD_BACKOFF_BASE = 2.0
# Refuse images larger than 50 MB to prevent OOM on malicious or misconfigured URLs
_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024


def write_nfo(media: MediaFile, result: MetadataResult) -> None:
    """Build and write a Kodi-spec NFO XML file next to the media file."""

    # Root element for a movie NFO
    root = etree.Element("movie")

    # Helper: only add an element if the value is not None
    def add(tag: str, text: str | None) -> None:
        if text is not None:
            el = etree.SubElement(root, tag)
            el.text = str(text)

    # Core metadata fields
    add("title", result.title)
    add("year", str(result.year) if result.year else None)
    add("rating", str(result.rating) if result.rating is not None else None)
    add("mpaa", result.content_rating)   # content rating, e.g. "NR", "R"
    add("plot", result.summary)

    # Each genre, tag, and label gets its own element (Kodi/Jellyfin expect this)
    for genre in result.genres:
        add("genre", genre)
    for tag in result.tags:
        add("tag", tag)
    # Labels are stored as "label:<value>" tags so they round-trip back to Plex labels
    for label in result.labels:
        add("tag", f"label:{label}")

    # Each actor gets a nested <actor><name> structure
    for actor in result.actors:
        actor_el = etree.SubElement(root, "actor")
        name_el = etree.SubElement(actor_el, "name")
        name_el.text = actor

    # <art> block references the sidecar image filenames.
    # Kodi and Jellyfin resolve bare filenames relative to the NFO file's directory,
    # so "My Movie-poster.jpg" correctly resolves to the file sitting next to the NFO.
    # Plex's XBMCnfoMoviesImporter also accepts bare filenames in the same directory.
    # If you find Plex does not pick up the images automatically, the Plex push path
    # (plex.py: uploadPoster / uploadArt) handles artwork independently via the API.
    if result.poster_url or result.fanart_url:
        art_el = etree.SubElement(root, "art")
        if result.poster_url:
            p = etree.SubElement(art_el, "poster")
            p.text = f"{media.stem}-poster.jpg"
        if result.fanart_url:
            f = etree.SubElement(art_el, "fanart")
            f.text = f"{media.stem}-fanart.jpg"

    # Source URL and unique ID for traceability back to the originating site
    add("source", result.source_url)
    if result.source_id:
        uid = etree.SubElement(root, "uniqueid")
        uid.set("type", "pm")   # "pm" identifies this tool as the source
        uid.text = result.source_id

    # Serialise to XML with pretty-printing
    tree = etree.ElementTree(root)
    etree.indent(tree, space="  ")

    # Write atomically: write to a .tmp file first, then rename into place.
    # os.replace() is atomic on all platforms — if the process crashes mid-write
    # the original .nfo is never left in a corrupt state.
    tmp_path = media.nfo_path + ".tmp"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
            tree.write(fh, encoding="utf-8", xml_declaration=False)
        os.replace(tmp_path, media.nfo_path)
    except Exception:
        # Clean up the temp file if anything went wrong before the rename
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    logger.info("Wrote NFO: %s", media.nfo_path)


def _download_image(url: str, dest_path: str) -> bool:
    """Download an image from url and save it to dest_path. Returns True on success.

    - Retries up to _DOWNLOAD_MAX_ATTEMPTS times with exponential backoff.
    - Uses a single httpx.Client for all attempts (keeps connection pooling).
    - Writes atomically via a .tmp file + os.replace() so interrupted downloads
      never leave a partial file at dest_path.
    """
    last_exc: Exception | None = None
    # One client for all attempts — avoids creating a new TCP connection per retry
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        for attempt in range(1, _DOWNLOAD_MAX_ATTEMPTS + 1):
            try:
                r = client.get(url)
                r.raise_for_status()   # raise on 4xx/5xx responses

                # Reject non-image content types to avoid writing HTML error pages
                # or other garbage to disk when a CDN returns an unexpected response.
                # A missing Content-Type header is also treated as invalid — a legitimate
                # image CDN should always send one.
                content_type = r.headers.get("content-type", "")
                if not content_type or not content_type.startswith("image/"):
                    raise ValueError(
                        f"Unexpected content-type {content_type!r} for image URL {url}"
                    )

                # Reject payloads that exceed the size cap to prevent OOM
                if len(r.content) > _DOWNLOAD_MAX_BYTES:
                    raise ValueError(
                        f"Image response too large ({len(r.content)} bytes) from {url}"
                    )

                # Write atomically: write to a .tmp file then rename into place.
                # os.replace() is atomic on all platforms, so dest_path is never
                # left in a partial/empty state if the process is interrupted.
                tmp_path = dest_path + ".tmp"
                try:
                    with open(tmp_path, "wb") as fh:
                        fh.write(r.content)
                    os.replace(tmp_path, dest_path)
                except Exception:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    raise

                logger.info("Downloaded image: %s", dest_path)
                return True
            except Exception as exc:
                last_exc = exc
                if attempt < _DOWNLOAD_MAX_ATTEMPTS:
                    wait = _DOWNLOAD_BACKOFF_BASE ** attempt
                    logger.warning(
                        "Image download attempt %d/%d failed (%s); retrying in %.0fs",
                        attempt, _DOWNLOAD_MAX_ATTEMPTS, exc, wait,
                    )
                    time.sleep(wait)

    logger.error("Failed to download image from %s after %d attempts: %s",
                 url, _DOWNLOAD_MAX_ATTEMPTS, last_exc)
    return False


def write_images(media: MediaFile, result: MetadataResult) -> bool:
    """Download poster and fanart images into the same directory as the media file.

    Returns True if all requested images downloaded successfully, False if any failed.
    Failures are logged as errors but do not raise — image download issues should not
    abort an otherwise successful metadata write.
    """
    base = os.path.dirname(media.path)
    all_ok = True

    if result.poster_url:
        dest = os.path.join(base, f"{media.stem}-poster.jpg")
        if not _download_image(result.poster_url, dest):
            logger.error("Poster download failed for %s — NFO written but image missing", media.path)
            all_ok = False

    if result.fanart_url:
        dest = os.path.join(base, f"{media.stem}-fanart.jpg")
        if not _download_image(result.fanart_url, dest):
            logger.error("Fanart download failed for %s — NFO written but image missing", media.path)
            all_ok = False

    return all_ok
