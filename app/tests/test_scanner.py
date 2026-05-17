"""Tests for the library scanner."""

import os
import tempfile

from app.scanner import scan_library, MediaFile


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
