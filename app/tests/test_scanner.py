"""Tests for the library scanner."""

from __future__ import annotations

import os

import pytest

from app.scanner import scan_library, MediaFile, _dedup_paths

pytestmark = pytest.mark.unit


class TestScanLibrary:
    def test_finds_video_files(self, tmp_path):
        (tmp_path / "movie.mp4").touch()
        (tmp_path / "movie.txt").touch()
        results, skipped = scan_library([str(tmp_path)])
        assert len(results) == 1
        assert results[0].stem == "movie"
        assert skipped == 0

    def test_skips_files_with_existing_nfo(self, tmp_path):
        (tmp_path / "movie.mp4").touch()
        (tmp_path / "movie.nfo").touch()
        results, skipped = scan_library([str(tmp_path)], force=False)
        assert len(results) == 0
        assert skipped == 1

    def test_force_includes_files_with_nfo(self, tmp_path):
        (tmp_path / "movie.mp4").touch()
        (tmp_path / "movie.nfo").touch()
        results, skipped = scan_library([str(tmp_path)], force=True)
        assert len(results) == 1
        assert skipped == 0

    def test_skips_missing_library_path(self):
        results, skipped = scan_library(["/nonexistent/path"])
        assert results == []
        assert skipped == 0

    def test_nfo_path_derived_correctly(self, tmp_path):
        (tmp_path / "my scene.mkv").touch()
        results, _ = scan_library([str(tmp_path)])
        assert results[0].nfo_path == str(tmp_path / "my scene.nfo")

    def test_scans_subdirectories(self, tmp_path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "deep.mp4").touch()
        results, _ = scan_library([str(tmp_path)])
        assert len(results) == 1

    def test_multiple_library_paths(self, tmp_path):
        d1 = tmp_path / "lib1"
        d2 = tmp_path / "lib2"
        d1.mkdir()
        d2.mkdir()
        (d1 / "a.mp4").touch()
        (d2 / "b.mkv").touch()
        results, _ = scan_library([str(d1), str(d2)])
        assert len(results) == 2

    def test_dedup_removes_subdirectory_path(self, tmp_path):
        child = tmp_path / "sub"
        child.mkdir()
        kept = _dedup_paths([str(tmp_path), str(child)])
        assert kept == [str(tmp_path)]

    def test_dedup_keeps_sibling_paths(self, tmp_path):
        d1 = tmp_path / "a"
        d2 = tmp_path / "b"
        d1.mkdir()
        d2.mkdir()
        kept = _dedup_paths([str(d1), str(d2)])
        assert set(kept) == {str(d1), str(d2)}

    def test_scan_does_not_double_process_subdirectory(self, tmp_path):
        child = tmp_path / "sub"
        child.mkdir()
        (child / "movie.mp4").touch()
        results, _ = scan_library([str(tmp_path), str(child)])
        assert len(results) == 1

    def test_exclude_patterns_skip_matching_files(self, tmp_path):
        (tmp_path / "movie.mp4").touch()
        (tmp_path / "movie.part").touch()
        results, _ = scan_library([str(tmp_path)], exclude_patterns=["*.part"])
        assert len(results) == 1
        assert results[0].stem == "movie"

    def test_exclude_patterns_skip_by_directory_glob(self, tmp_path):
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        (incoming / "wip.mp4").touch()
        (tmp_path / "done.mkv").touch()
        results, _ = scan_library(
            [str(tmp_path)],
            exclude_patterns=[str(incoming / "*")],
        )
        assert len(results) == 1
        assert results[0].stem == "done"

    def test_exclude_patterns_empty_list_scans_all(self, tmp_path):
        (tmp_path / "movie.mp4").touch()
        results, _ = scan_library([str(tmp_path)], exclude_patterns=[])
        assert len(results) == 1


class TestRenameDetection:
    def test_rename_detected_when_one_orphan_nfo_and_one_new_video(self, tmp_path):
        (tmp_path / "new name.mp4").touch()
        (tmp_path / "old name.nfo").touch()
        results, skipped = scan_library([str(tmp_path)])
        assert len(results) == 1
        assert results[0].stem == "new name"
        assert results[0].renamed_from == "old name"
        assert skipped == 0

    def test_normal_files_unaffected_alongside_rename(self, tmp_path):
        (tmp_path / "existing.mp4").touch()
        (tmp_path / "existing.nfo").touch()
        (tmp_path / "new name.mp4").touch()
        (tmp_path / "old name.nfo").touch()
        results, skipped = scan_library([str(tmp_path)])
        assert len(results) == 1
        assert results[0].renamed_from == "old name"
        assert skipped == 1

    def test_ambiguous_rename_falls_through_as_new(self, tmp_path):
        (tmp_path / "new video.mp4").touch()
        (tmp_path / "old name 1.nfo").touch()
        (tmp_path / "old name 2.nfo").touch()
        results, skipped = scan_library([str(tmp_path)])
        assert len(results) == 1
        assert results[0].renamed_from is None

    def test_force_skips_rename_detection(self, tmp_path):
        (tmp_path / "new name.mp4").touch()
        (tmp_path / "old name.nfo").touch()
        results, skipped = scan_library([str(tmp_path)], force=True)
        assert len(results) == 1
        assert results[0].renamed_from is None

    def test_matched_pair_not_treated_as_rename(self, tmp_path):
        (tmp_path / "movie.mp4").touch()
        (tmp_path / "movie.nfo").touch()
        results, skipped = scan_library([str(tmp_path)])
        assert len(results) == 0
        assert skipped == 1


# ---------------------------------------------------------------------------
# T7 — _dedup_paths with identical duplicate paths
# ---------------------------------------------------------------------------

class TestDedupIdentical:
    def test_identical_paths_deduplicated(self):
        """Two identical paths must be reduced to a single entry."""
        result = _dedup_paths(["/same/path", "/same/path"])
        assert len(result) == 1
        assert result[0] == "/same/path"
