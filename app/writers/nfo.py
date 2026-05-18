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

import httpx
from lxml import etree

from app.plugins.base import MetadataResult
from app.scanner import MediaFile
from app.utils import retry_with_backoff

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
        # Clean up the temp file if anything went wrong before the rename.
        # Use try/except rather than os.path.exists() + os.remove() to avoid
        # a TOCTOU race; also prevents the cleanup error from masking the
        # original exception.
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
        raise  # re-raise so the caller's status bucket reflects the failure

    logger.info("Wrote NFO: %s", media.nfo_path)


def _download_image(url: str, dest_path: str) -> bool:
    """Download an image from url and save it to dest_path. Returns True on success.

    - Retries up to _DOWNLOAD_MAX_ATTEMPTS times with exponential backoff.
    - Uses a single httpx.Client for all attempts (keeps connection pooling).
    - Writes atomically via a .tmp file + os.replace() so interrupted downloads
      never leave a partial file at dest_path.
    """
    # One client for all attempts — reuses the connection across retries
    with httpx.Client(follow_redirects=True, timeout=30) as client:
        def _attempt() -> bool:
            # Stream the response so we can abort mid-download if the body
            # exceeds the size cap — avoids buffering hundreds of MB for a
            # misconfigured or malicious URL before discovering it's oversized.
            with client.stream("GET", url) as r:
                r.raise_for_status()

                # Reject non-image content types to avoid writing HTML error pages to disk
                content_type = r.headers.get("content-type", "")
                if not content_type or not content_type.startswith("image/"):
                    raise ValueError(
                        f"Unexpected content-type {content_type!r} for image URL {url}"
                    )

                # Content-Length pre-flight: reject before reading a single byte.
                # Parse int() in its own try/except so a malformed header value
                # (raises ValueError/OverflowError) doesn't silently swallow
                # the intentional raise below.
                cl = r.headers.get("content-length")
                if cl is not None:
                    try:
                        cl_int = int(cl)
                    except (ValueError, OverflowError):
                        cl_int = None  # malformed header; fall through to streaming check
                    if cl_int is not None and cl_int > _DOWNLOAD_MAX_BYTES:
                        raise ValueError(
                            f"Image Content-Length ({cl_int} bytes) exceeds limit from {url}"
                        )

                # Write atomically: accumulate chunks into .tmp, abort if size cap
                # exceeded mid-stream, then os.replace() into final path.
                tmp_path = dest_path + ".tmp"
                try:
                    received = 0
                    with open(tmp_path, "wb") as fh:
                        for chunk in r.iter_bytes(chunk_size=65536):
                            received += len(chunk)
                            if received > _DOWNLOAD_MAX_BYTES:
                                raise ValueError(
                                    f"Image stream exceeded {_DOWNLOAD_MAX_BYTES} bytes from {url}"
                                )
                            fh.write(chunk)
                    os.replace(tmp_path, dest_path)
                except Exception:
                    try:
                        os.remove(tmp_path)
                    except FileNotFoundError:
                        pass
                    raise

            logger.info("Downloaded image: %s", dest_path)
            return True

        try:
            return retry_with_backoff(
                _attempt,
                max_attempts=_DOWNLOAD_MAX_ATTEMPTS,
                backoff_base=_DOWNLOAD_BACKOFF_BASE,
                description=f"image download {url}",
            )
        except Exception as exc:
            logger.error("Failed to download image from %s after %d attempts: %s",
                         url, _DOWNLOAD_MAX_ATTEMPTS, exc)
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
