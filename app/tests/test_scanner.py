"""Tests for the library scanner."""

import os
import tempfile

from app.scanner import scan_library, MediaFile, _dedup_paths


class TestScanLibrary:
    def test_finds_video_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "movie.mp4"), "w").close()
            open(os.path.join(tmpdir, "movie.txt"), "w").close()
            results, skipped = scan_library([tmpdir])
        assert len(results) == 1
        assert results[0].stem == "movie"
        assert skipped == 0

    def test_skips_files_with_existing_nfo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "movie.mp4"), "w").close()
            open(os.path.join(tmpdir, "movie.nfo"), "w").close()
            results, skipped = scan_library([tmpdir], force=False)
        assert len(results) == 0
        assert skipped == 1

    def test_force_includes_files_with_nfo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "movie.mp4"), "w").close()
            open(os.path.join(tmpdir, "movie.nfo"), "w").close()
            results, skipped = scan_library([tmpdir], force=True)
        assert len(results) == 1
        assert skipped == 0

    def test_skips_missing_library_path(self):
        results, skipped = scan_library(["/nonexistent/path"])
        assert results == []
        assert skipped == 0

    def test_nfo_path_derived_correctly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "my scene.mkv"), "w").close()
            results, _ = scan_library([tmpdir])
        assert results[0].nfo_path == os.path.join(tmpdir, "my scene.nfo")

    def test_scans_subdirectories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            subdir = os.path.join(tmpdir, "subdir")
            os.makedirs(subdir)
            open(os.path.join(subdir, "deep.mp4"), "w").close()
            results, _ = scan_library([tmpdir])
        assert len(results) == 1

    def test_multiple_library_paths(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            open(os.path.join(d1, "a.mp4"), "w").close()
            open(os.path.join(d2, "b.mkv"), "w").close()
            results, _ = scan_library([d1, d2])
        assert len(results) == 2

    def test_dedup_removes_subdirectory_path(self):
        # /media and /media/Movies: /media/Movies is dominated by /media
        with tempfile.TemporaryDirectory() as parent:
            child = os.path.join(parent, "sub")
            os.makedirs(child)
            kept = _dedup_paths([parent, child])
            assert kept == [parent]

    def test_dedup_keeps_sibling_paths(self):
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            kept = _dedup_paths([d1, d2])
            assert set(kept) == {d1, d2}

    def test_scan_does_not_double_process_subdirectory(self):
        # If /media and /media/sub are both in LIBRARY_PATHS, each file in sub
        # should appear exactly once in the results.
        with tempfile.TemporaryDirectory() as parent:
            child = os.path.join(parent, "sub")
            os.makedirs(child)
            open(os.path.join(child, "movie.mp4"), "w").close()
            results, _ = scan_library([parent, child])
        assert len(results) == 1

    def test_exclude_patterns_skip_matching_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "movie.mp4"), "w").close()
            open(os.path.join(tmpdir, "movie.part"), "w").close()
            results, _ = scan_library([tmpdir], exclude_patterns=["*.part"])
        assert len(results) == 1
        assert results[0].stem == "movie"

    def test_exclude_patterns_skip_by_directory_glob(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            incoming = os.path.join(tmpdir, "incoming")
            os.makedirs(incoming)
            open(os.path.join(incoming, "wip.mp4"), "w").close()
            open(os.path.join(tmpdir, "done.mkv"), "w").close()
            results, _ = scan_library([tmpdir], exclude_patterns=[os.path.join(tmpdir, "incoming", "*")])
        assert len(results) == 1
        assert results[0].stem == "done"

    def test_exclude_patterns_empty_list_scans_all(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "movie.mp4"), "w").close()
            results, _ = scan_library([tmpdir], exclude_patterns=[])
        assert len(results) == 1


class TestRenameDetection:
    def test_rename_detected_when_one_orphan_nfo_and_one_new_video(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "new name.mp4"), "w").close()
            open(os.path.join(tmpdir, "old name.nfo"), "w").close()
            results, skipped = scan_library([tmpdir])
        assert len(results) == 1
        assert results[0].stem == "new name"
        assert results[0].renamed_from == "old name"
        assert skipped == 0

    def test_normal_files_unaffected_alongside_rename(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Matched pair (should skip) + rename pair
            open(os.path.join(tmpdir, "existing.mp4"), "w").close()
            open(os.path.join(tmpdir, "existing.nfo"), "w").close()
            open(os.path.join(tmpdir, "new name.mp4"), "w").close()
            open(os.path.join(tmpdir, "old name.nfo"), "w").close()
            results, skipped = scan_library([tmpdir])
        assert len(results) == 1
        assert results[0].renamed_from == "old name"
        assert skipped == 1

    def test_ambiguous_rename_falls_through_as_new(self):
        # Two orphan NFOs + one new video → ambiguous, treat new video as new file
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "new video.mp4"), "w").close()
            open(os.path.join(tmpdir, "old name 1.nfo"), "w").close()
            open(os.path.join(tmpdir, "old name 2.nfo"), "w").close()
            results, skipped = scan_library([tmpdir])
        assert len(results) == 1
        assert results[0].renamed_from is None

    def test_force_skips_rename_detection(self):
        # --force bypasses rename detection; new video is treated as a new file
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "new name.mp4"), "w").close()
            open(os.path.join(tmpdir, "old name.nfo"), "w").close()
            results, skipped = scan_library([tmpdir], force=True)
        assert len(results) == 1
        assert results[0].renamed_from is None

    def test_matched_pair_not_treated_as_rename(self):
        # Video + matching NFO → skip, not a rename
        with tempfile.TemporaryDirectory() as tmpdir:
            open(os.path.join(tmpdir, "movie.mp4"), "w").close()
            open(os.path.join(tmpdir, "movie.nfo"), "w").close()
            results, skipped = scan_library([tmpdir])
        assert len(results) == 0
        assert skipped == 1
