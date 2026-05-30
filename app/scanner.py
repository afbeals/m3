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

import fnmatch as _fnmatch
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

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

    Uses pathlib.Path for resolution so that on Windows, paths are compared
    case-insensitively (C:\\Media and c:\\media are treated as the same path).
    """
    # Path.resolve() normalises separators, symlinks, and case on Windows
    resolved = [str(Path(p).resolve()) for p in paths]
    _ci = sys.platform == "win32"  # case-insensitive comparison on Windows

    def _eq(a: str, b: str) -> bool:
        return (a.lower() == b.lower()) if _ci else (a == b)

    def _startswith_sep(child: str, parent: str) -> bool:
        prefix = parent + os.sep
        return (child.lower().startswith(prefix.lower()) if _ci
                else child.startswith(prefix))

    # Deduplicate exact-same resolved paths first, preserving order
    seen_resolved: list[str] = []
    seen_original: list[str] = []
    seen_set: set[str] = set()
    for orig, res in zip(paths, resolved):
        norm = res.lower() if _ci else res
        if norm not in seen_set:
            seen_set.add(norm)
            seen_resolved.append(res)
            seen_original.append(orig)

    kept = []
    for i, p in enumerate(seen_resolved):
        # Check whether any other path is a strict prefix of this one
        dominated = any(
            j != i and _startswith_sep(p, seen_resolved[j])
            for j in range(len(seen_resolved))
        )
        if dominated:
            logger.warning(
                "Library path %r is a subdirectory of another configured path and will be "
                "skipped to avoid processing files twice. Remove it from LIBRARY_PATHS — "
                "the parent path already covers it.", seen_original[i]
            )
        else:
            kept.append(seen_original[i])
    return kept


def scan_library(
    library_paths: list[str], force: bool = False, exclude_patterns: list[str] | None = None
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
    if exclude_patterns is None:
        exclude_patterns = []
    results: list[MediaFile] = []
    skipped = 0

    # Compile exclude patterns once for the entire scan, not per library path.
    # os.walk yields backslash paths on Windows, so we pre-normalize patterns
    # to forward slashes before compiling for consistent behaviour.
    _compiled_pats = [
        re.compile(_fnmatch.translate(p.replace("\\", "/")))
        for p in exclude_patterns
    ]

    for lib_path in _dedup_paths(library_paths):
        # Warn and skip paths that don't exist (e.g. misconfigured volume mount)
        if not os.path.isdir(lib_path):
            logger.warning("Library path not found, skipping: %s", lib_path)
            continue

        def _walk_onerror(err):
            logger.warning("Scanner: cannot access %s: %s", err.filename, err)

        # os.walk recursively yields (directory, subdirs, files) for the whole tree
        for dirpath, _, filenames in os.walk(lib_path, onerror=_walk_onerror):
            # Build per-directory stem maps in one pass so rename detection is O(n).
            # video_stems: stem → full_path for every video file in this directory
            # nfo_stems: set of stems that already have a .nfo sidecar here
            video_stems: dict[str, str] = {}
            nfo_stems: set[str] = set()

            excluded_video_stems: set[str] = set()
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext in VIDEO_EXTENSIONS:
                    full_path = os.path.join(dirpath, fname)
                    _norm_path = full_path.replace("\\", "/")
                    if not any(pat.match(_norm_path) for pat in _compiled_pats):
                        video_stems[os.path.splitext(fname)[0]] = full_path
                    else:
                        logger.debug("Excluding (matches pattern): %s", full_path)
                        excluded_video_stems.add(os.path.splitext(fname)[0])
                elif ext == ".nfo":
                    nfo_stems.add(os.path.splitext(fname)[0])

            # On Windows, filesystems are case-insensitive — normalize stems for
            # comparison only. The original-case stems are preserved in video_stems
            # and nfo_stems so MediaFile.stem uses the video file's actual casing
            # (required for correct NFO filename construction).
            if sys.platform == "win32":
                nfo_stems_lower: set[str] = {s.lower() for s in nfo_stems}
                excluded_video_stems_lower: set[str] = {s.lower() for s in excluded_video_stems}
                video_stems_lower: dict[str, str] = {s.lower(): p for s, p in video_stems.items()}
            else:
                nfo_stems_lower = nfo_stems  # type: ignore[assignment]
                excluded_video_stems_lower = excluded_video_stems
                video_stems_lower = video_stems  # type: ignore[assignment]

            # Rename detection: one orphan NFO + one new video → rename workflow.
            # An "orphan" NFO has no matching video stem in this directory.
            # A "new" video has no matching NFO stem.
            # Only attempt when both counts are exactly 1 to avoid ambiguous cases
            # (e.g. two files renamed simultaneously, or stale NFOs from deletions).
            # Excluded video stems are subtracted so their NFOs are never treated as orphans.
            if not force:
                orphan_nfos = (nfo_stems_lower - set(video_stems_lower.keys())) - excluded_video_stems_lower
                new_videos = {s: p for s, p in video_stems.items() if s.lower() not in nfo_stems_lower}
                if len(orphan_nfos) == 1 and len(new_videos) == 1:
                    old_stem = next(iter(orphan_nfos))
                    # Resolve lowercase back to original case on Windows
                    if sys.platform == "win32":
                        old_stem = next(
                            (s for s in nfo_stems if s.lower() == old_stem),
                            old_stem,  # fallback to lowercase if not found
                        )
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
                    # Account for the remaining matched videos as normal skip/new.
                    # Exclude old_stem from the nfo check: it's an orphan NFO whose
                    # video was renamed, so no other video should be classified as
                    # "skipped" because its stem happens to equal old_stem.
                    _remaining_nfo_stems_lower = nfo_stems_lower - {old_stem.lower()}
                    for stem, full_path in video_stems.items():
                        if stem == new_stem:
                            continue
                        _loop_nfo_path = os.path.join(dirpath, f"{stem}.nfo")
                        if stem.lower() in _remaining_nfo_stems_lower:
                            logger.debug("Skipping (sidecar exists): %s", full_path)
                            skipped += 1
                        else:
                            results.append(MediaFile(path=full_path, stem=stem, nfo_path=_loop_nfo_path))
                    continue  # this directory's videos were all handled in the rename branch above

            # Normal per-stem classification (no rename candidate, or --force)
            for stem, full_path in video_stems.items():
                nfo_path = os.path.join(dirpath, f"{stem}.nfo")
                if not force and stem.lower() in nfo_stems_lower:
                    logger.debug("Skipping (sidecar exists): %s", full_path)
                    skipped += 1
                    continue
                results.append(MediaFile(path=full_path, stem=stem, nfo_path=nfo_path))

    logger.info("Scanner found %d file(s) to process, %d skipped", len(results), skipped)
    return results, skipped
