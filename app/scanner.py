# -----------------------------------------------------------------------------
# app/scanner.py
#
# Walks configured library directories and returns a list of media files
# that need to be processed.
#
# Per-file classification (decided per directory in a single pass):
#   - NFO exists and stem matches video  → skip (file already processed)
#   - NFO does not exist                 → new file (full workflow)
#   - Exactly one orphan NFO + one new video in the same directory
#                                        → rename (assets renamed, Plex re-pushed
#                                           from existing NFO; no fresh API fetch)
#   - Ambiguous (multiple orphans or multiple new videos) → treat all as new
#
# Only files whose extension is in VIDEO_EXTENSIONS are collected; everything
# else (images, subtitles, text files, etc.) is ignored.
# -----------------------------------------------------------------------------

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field

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
    # Set to the old stem when this file is a rename of an existing processed file.
    # When set, main.py skips the plugin fetch and instead renames the existing
    # sidecar assets and re-pushes metadata from the NFO to Plex.
    renamed_from: str | None = field(default=None)


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
            # Build per-directory stem maps in one pass so rename detection is O(n).
            # video_stems: stem → full_path for every video file in this directory
            # nfo_stems: set of stems that already have a .nfo sidecar here
            video_stems: dict[str, str] = {}
            nfo_stems: set[str] = set()

            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    full_path = os.path.join(dirpath, fname)
                    if not any(fnmatch.fnmatch(full_path, pat) for pat in exclude_patterns):
                        video_stems[os.path.splitext(fname)[0]] = full_path
                    else:
                        logger.debug("Excluding (matches pattern): %s", full_path)
                elif ext == ".nfo":
                    nfo_stems.add(os.path.splitext(fname)[0])

            # Rename detection: one orphan NFO + one new video → rename workflow.
            # An "orphan" NFO has no matching video stem in this directory.
            # A "new" video has no matching NFO stem.
            # Only attempt when both counts are exactly 1 to avoid ambiguous cases
            # (e.g. two files renamed simultaneously, or stale NFOs from deletions).
            if not force:
                orphan_nfos = nfo_stems - video_stems.keys()
                new_videos = {s: p for s, p in video_stems.items() if s not in nfo_stems}
                if len(orphan_nfos) == 1 and len(new_videos) == 1:
                    old_stem = next(iter(orphan_nfos))
                    new_stem, new_path = next(iter(new_videos.items()))
                    nfo_path = os.path.join(dirpath, f"{new_stem}.nfo")
                    logger.info(
                        "Rename detected: %r → %r in %s", old_stem, new_stem, dirpath
                    )
                    results.append(MediaFile(
                        path=new_path,
                        stem=new_stem,
                        nfo_path=nfo_path,
                        renamed_from=old_stem,
                    ))
                    # Account for the remaining matched videos as normal skip/new
                    for stem, full_path in video_stems.items():
                        if stem == new_stem:
                            continue
                        nfo_path = os.path.join(dirpath, f"{stem}.nfo")
                        if stem in nfo_stems:
                            logger.debug("Skipping (sidecar exists): %s", full_path)
                            skipped += 1
                        else:
                            results.append(MediaFile(path=full_path, stem=stem, nfo_path=nfo_path))
                    continue  # this directory's videos were all handled in the rename branch above

            # Normal per-stem classification (no rename candidate, or --force)
            for stem, full_path in video_stems.items():
                nfo_path = os.path.join(dirpath, f"{stem}.nfo")
                if not force and stem in nfo_stems:
                    logger.debug("Skipping (sidecar exists): %s", full_path)
                    skipped += 1
                    continue
                results.append(MediaFile(path=full_path, stem=stem, nfo_path=nfo_path))

    logger.info("Scanner found %d file(s) to process, %d skipped", len(results), skipped)
    return results, skipped
