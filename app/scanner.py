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

import fnmatch
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


def _dedup_paths(paths: list[str]) -> list[str]:
    """Return paths with any entry that is a subdirectory of another entry removed.

    If LIBRARY_PATHS contains both /media and /media/Movies, os.walk on /media
    already visits /media/Movies — keeping both would process every file in
    /media/Movies twice (once for each parent path in the list).
    """
    resolved = [os.path.realpath(p) for p in paths]
    kept = []
    for i, p in enumerate(resolved):
        # Check whether any other path is a strict prefix of this one
        dominated = any(
            j != i and (p == resolved[j] or p.startswith(resolved[j] + os.sep))
            for j in range(len(resolved))
        )
        if dominated:
            logger.warning(
                "Library path %r is a subdirectory of another configured path and will be "
                "skipped to avoid processing files twice. Remove it from LIBRARY_PATHS — "
                "the parent path already covers it.", paths[i]
            )
        else:
            kept.append(paths[i])
    return kept


def scan_library(
    library_paths: list[str], force: bool = False, exclude_patterns: list[str] = ()
) -> tuple[list[MediaFile], int]:
    """
    Walk each path in library_paths recursively and collect video files to process.

    Skips files that already have a .nfo sidecar, unless force=True.
    Automatically deduplicates paths: if one configured path is a subdirectory of
    another, the child is dropped (the parent's walk already covers it).
    Files whose full absolute path matches any pattern in exclude_patterns (fnmatch
    glob syntax) are silently skipped regardless of force.
    Returns a tuple of:
      - list of MediaFile objects ready for routing and metadata fetching
      - count of files skipped because a sidecar already exists
    """
    results: list[MediaFile] = []
    skipped = 0

    for lib_path in _dedup_paths(library_paths):
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

                # Skip files matching any configured exclude pattern
                if any(fnmatch.fnmatch(full_path, pat) for pat in exclude_patterns):
                    logger.debug("Excluding (matches pattern): %s", full_path)
                    continue

                stem = os.path.splitext(fname)[0]  # filename without extension
                nfo_path = os.path.join(dirpath, f"{stem}.nfo")

                # If the sidecar already exists and we're not forcing, skip this file
                if not force and os.path.exists(nfo_path):
                    logger.debug("Skipping (sidecar exists): %s", full_path)
                    skipped += 1
                    continue

                results.append(MediaFile(path=full_path, stem=stem, nfo_path=nfo_path))

    logger.info("Scanner found %d file(s) to process, %d skipped", len(results), skipped)
    return results, skipped
