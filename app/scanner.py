# -----------------------------------------------------------------------------
# app/scanner.py
#
# Walks configured library directories and returns a list of media files
# that need to be processed.
#
# A file is considered "already processed" if a .nfo sidecar exists alongside
# it (e.g. "My Movie.mp4" → "My Movie.nfo"). These files are skipped unless
# --force is passed, which forces a full re-process of everything.
#
# Only files whose extension is in VIDEO_EXTENSIONS are collected; everything
# else (images, subtitles, text files, etc.) is ignored.
# -----------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# File extensions treated as video media files.
# Add more here if your library uses other formats.
VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v",
    ".flv", ".webm", ".mpg", ".mpeg", ".ts", ".m2ts",
}


@dataclass
class MediaFile:
    path: str        # absolute path to the video file, e.g. /media/Movies/My Movie.mp4
    stem: str        # filename without extension, e.g. "My Movie"
    nfo_path: str    # where the .nfo sidecar should live, e.g. /media/Movies/My Movie.nfo


def scan_library(library_paths: list[str], force: bool = False) -> list[MediaFile]:
    """
    Walk each path in library_paths recursively and collect video files to process.

    Skips files that already have a .nfo sidecar, unless force=True.
    Returns a list of MediaFile objects ready for routing and metadata fetching.
    """
    results: list[MediaFile] = []

    for lib_path in library_paths:
        # Warn and skip paths that don't exist (e.g. misconfigured volume mount)
        if not os.path.isdir(lib_path):
            logger.warning("Library path not found, skipping: %s", lib_path)
            continue

        # os.walk recursively yields (directory, subdirs, files) for the whole tree
        for dirpath, _, filenames in os.walk(lib_path):
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()

                # Skip non-video files
                if ext not in VIDEO_EXTENSIONS:
                    continue

                full_path = os.path.join(dirpath, fname)
                stem = os.path.splitext(fname)[0]  # filename without extension
                nfo_path = os.path.join(dirpath, f"{stem}.nfo")

                # If the sidecar already exists and we're not forcing, skip this file
                if not force and os.path.exists(nfo_path):
                    logger.debug("Skipping (sidecar exists): %s", full_path)
                    continue

                results.append(MediaFile(path=full_path, stem=stem, nfo_path=nfo_path))

    logger.info("Scanner found %d file(s) to process", len(results))
    return results
