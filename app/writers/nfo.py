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

logger = logging.getLogger(__name__)


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

    # <art> block references the sidecar image filenames (relative to the media file)
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

    with open(media.nfo_path, "wb") as fh:
        # Write the XML declaration manually so encoding is explicit
        fh.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(fh, encoding="utf-8", xml_declaration=False)

    logger.info("Wrote NFO: %s", media.nfo_path)


def _download_image(url: str, dest_path: str) -> bool:
    """Download an image from url and save it to dest_path. Returns True on success."""
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            r = client.get(url)
            r.raise_for_status()   # raise on 4xx/5xx responses
        with open(dest_path, "wb") as fh:
            fh.write(r.content)
        logger.info("Downloaded image: %s", dest_path)
        return True
    except Exception:
        logger.exception("Failed to download image from %s", url)
        return False


def write_images(media: MediaFile, result: MetadataResult) -> None:
    """Download poster and fanart images into the same directory as the media file."""
    base = os.path.dirname(media.path)

    if result.poster_url:
        dest = os.path.join(base, f"{media.stem}-poster.jpg")
        _download_image(result.poster_url, dest)

    if result.fanart_url:
        dest = os.path.join(base, f"{media.stem}-fanart.jpg")
        _download_image(result.fanart_url, dest)
