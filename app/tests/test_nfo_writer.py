# Tests for app/writers/nfo.py
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest

from app.plugins.base import MetadataResult
from app.scanner import MediaFile
from app.writers.nfo import write_nfo, write_images


def _make_media(tmp_path, name="My Movie"):
    path = str(tmp_path / f"{name}.mp4")
    return MediaFile(
        path=path,
        stem=name,
        nfo_path=str(tmp_path / f"{name}.nfo"),
    )


def _make_result(**kwargs) -> MetadataResult:
    defaults = dict(
        title="Test Scene",
        summary="A test summary.",
        rating=7.5,
        year=2023,
        content_rating="NR",
        genres=["Drama", "Thriller"],
        tags=["tag-1"],
        labels=["label-1"],
        actors=["Jane Doe", "John Smith"],
        source_url="https://example.com/scene/42",
        source_id="42",
    )
    defaults.update(kwargs)
    return MetadataResult(**defaults)


# ---------------------------------------------------------------------------
# write_nfo — basic XML output
# ---------------------------------------------------------------------------

def test_write_nfo_creates_file(tmp_path):
    media = _make_media(tmp_path)
    result = _make_result()
    write_nfo(media, result)
    assert os.path.exists(media.nfo_path)


def test_write_nfo_contains_title(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result(title="My Great Scene"))
    nfo = open(media.nfo_path).read()
    assert "<title>My Great Scene</title>" in nfo


def test_write_nfo_contains_year(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result(year=2022))
    nfo = open(media.nfo_path).read()
    assert "<year>2022</year>" in nfo


def test_write_nfo_contains_rating(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result(rating=8.0))
    nfo = open(media.nfo_path).read()
    assert "<rating>8.0</rating>" in nfo


def test_write_nfo_contains_genres(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result(genres=["Action", "Comedy"]))
    nfo = open(media.nfo_path).read()
    assert "<genre>Action</genre>" in nfo
    assert "<genre>Comedy</genre>" in nfo


def test_write_nfo_contains_actors(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result(actors=["Alice", "Bob"]))
    nfo = open(media.nfo_path).read()
    assert "<name>Alice</name>" in nfo
    assert "<name>Bob</name>" in nfo


def test_write_nfo_labels_written_as_tag_prefix(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result(labels=["award-winner"]))
    nfo = open(media.nfo_path).read()
    assert "<tag>label:award-winner</tag>" in nfo


def test_write_nfo_contains_source_url(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result(source_url="https://example.com/scene/1"))
    nfo = open(media.nfo_path).read()
    assert "<source>https://example.com/scene/1</source>" in nfo


def test_write_nfo_contains_uniqueid(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result(source_id="99"))
    nfo = open(media.nfo_path).read()
    assert 'type="pm"' in nfo
    assert ">99<" in nfo


def test_write_nfo_art_block_references_sidecar_names(tmp_path):
    media = _make_media(tmp_path, name="My Movie")
    write_nfo(media, _make_result(poster_url="http://x.com/p.jpg", fanart_url="http://x.com/f.jpg"))
    nfo = open(media.nfo_path).read()
    assert "My Movie-poster.jpg" in nfo
    assert "My Movie-fanart.jpg" in nfo


def test_write_nfo_no_art_block_when_no_urls(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result(poster_url=None, fanart_url=None))
    nfo = open(media.nfo_path).read()
    assert "<art>" not in nfo


def test_write_nfo_skips_none_fields(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result(summary=None, rating=None, year=None, content_rating=None))
    nfo = open(media.nfo_path).read()
    assert "<plot>" not in nfo
    assert "<rating>" not in nfo
    assert "<year>" not in nfo
    assert "<mpaa>" not in nfo


def test_write_nfo_xml_declaration(tmp_path):
    media = _make_media(tmp_path)
    write_nfo(media, _make_result())
    nfo = open(media.nfo_path, "rb").read()
    assert nfo.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')


def test_write_nfo_is_atomic_cleans_tmp_on_error(tmp_path):
    """If writing fails mid-way, no .tmp file should be left behind."""
    media = _make_media(tmp_path)
    result = _make_result()
    tmp_file = media.nfo_path + ".tmp"

    with patch("builtins.open", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            write_nfo(media, result)

    assert not os.path.exists(tmp_file)


# ---------------------------------------------------------------------------
# write_images — image download delegation
# ---------------------------------------------------------------------------

def test_write_images_downloads_poster_and_fanart(tmp_path):
    media = _make_media(tmp_path)
    result = _make_result(poster_url="http://x.com/p.jpg", fanart_url="http://x.com/f.jpg")

    with patch("app.writers.nfo._download_image") as mock_dl:
        mock_dl.return_value = True
        write_images(media, result)

    assert mock_dl.call_count == 2
    calls = [c.args[0] for c in mock_dl.call_args_list]
    assert "http://x.com/p.jpg" in calls
    assert "http://x.com/f.jpg" in calls


def test_write_images_skips_when_no_urls(tmp_path):
    media = _make_media(tmp_path)
    result = _make_result(poster_url=None, fanart_url=None)

    with patch("app.writers.nfo._download_image") as mock_dl:
        write_images(media, result)

    mock_dl.assert_not_called()


def test_write_images_poster_path_uses_stem(tmp_path):
    media = _make_media(tmp_path, name="My Scene")
    result = _make_result(poster_url="http://x.com/p.jpg", fanart_url=None)

    captured = {}

    def fake_dl(url, dest):
        captured["poster_dest"] = dest
        return True

    with patch("app.writers.nfo._download_image", side_effect=fake_dl):
        write_images(media, result)

    assert captured["poster_dest"].endswith("My Scene-poster.jpg")


# ---------------------------------------------------------------------------
# _download_image — streaming size cap
# ---------------------------------------------------------------------------

def _make_streaming_mock(content_length_header: int | None, body_bytes: int):
    """Build a mock httpx streaming response that yields `body_bytes` of data.

    iter_bytes uses a side_effect factory so retries each get a fresh iterator
    rather than the exhausted one from the first attempt.
    """
    def _make_chunks():
        remaining = body_bytes
        while remaining > 0:
            sz = min(remaining, 65536)
            yield b"x" * sz
            remaining -= sz

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.headers = {
        "content-type": "image/jpeg",
        **({"content-length": str(content_length_header)} if content_length_header is not None else {}),
    }
    # Return a fresh generator each call so retries don't see an empty iterator
    mock_response.iter_bytes = MagicMock(side_effect=lambda chunk_size=65536: _make_chunks())
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    return mock_response


def _wrap_client_with_fresh_response(content_length_header, body_bytes):
    """Return a mock httpx.Client whose .stream() yields a fresh response mock
    on every call — needed because _download_image retries up to 3 times."""
    def make_response():
        return _make_streaming_mock(content_length_header, body_bytes)

    mock_client = MagicMock()
    mock_client.stream.side_effect = lambda *a, **kw: make_response()
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = MagicMock(return_value=False)
    return mock_client


def test_download_image_aborts_on_content_length_header_exceeding_cap(tmp_path):
    """When Content-Length header exceeds the cap the download must be rejected
    before any bytes are streamed to disk."""
    from app.writers.nfo import _download_image, _DOWNLOAD_MAX_BYTES

    dest = str(tmp_path / "img.jpg")
    oversized = _DOWNLOAD_MAX_BYTES + 1

    mock_client = _wrap_client_with_fresh_response(
        content_length_header=oversized, body_bytes=100
    )

    with patch("app.writers.nfo.httpx") as mock_httpx:
        mock_httpx.Client.return_value = mock_client
        result = _download_image("http://example.com/img.jpg", dest)

    assert result is False
    assert not os.path.exists(dest), "Destination file must not be written when Content-Length exceeds cap"


def test_download_image_aborts_mid_stream_when_body_exceeds_cap(tmp_path):
    """When the streamed body grows past the cap the download must abort and
    leave no partial file at the destination path."""
    from app.writers.nfo import _download_image, _DOWNLOAD_MAX_BYTES

    dest = str(tmp_path / "img.jpg")
    # No Content-Length header; body exceeds cap mid-stream
    oversized_body = _DOWNLOAD_MAX_BYTES + 65536

    mock_client = _wrap_client_with_fresh_response(
        content_length_header=None, body_bytes=oversized_body
    )

    with patch("app.writers.nfo.httpx") as mock_httpx:
        mock_httpx.Client.return_value = mock_client
        result = _download_image("http://example.com/img.jpg", dest)

    assert result is False
    assert not os.path.exists(dest), "Partial file must be cleaned up after mid-stream abort"


# ---------------------------------------------------------------------------
# rename_nfo_assets
# ---------------------------------------------------------------------------

class TestRenameNfoAssets:
    def test_renames_nfo_and_images(self, tmp_path):
        from app.writers.nfo import rename_nfo_assets
        from lxml import etree

        nfo = tmp_path / "old name.nfo"
        nfo.write_bytes(
            b'<?xml version=\'1.0\' encoding=\'utf-8\'?>\n'
            b'<movie><title>Test</title>'
            b'<art><poster>old name-poster.jpg</poster>'
            b'<fanart>old name-fanart.jpg</fanart></art></movie>'
        )
        (tmp_path / "old name-poster.jpg").write_bytes(b"poster")
        (tmp_path / "old name-fanart.jpg").write_bytes(b"fanart")

        rename_nfo_assets(str(tmp_path), "old name", "new name")

        assert not (tmp_path / "old name.nfo").exists()
        assert not (tmp_path / "old name-poster.jpg").exists()
        assert not (tmp_path / "old name-fanart.jpg").exists()
        assert (tmp_path / "new name.nfo").exists()
        assert (tmp_path / "new name-poster.jpg").exists()
        assert (tmp_path / "new name-fanart.jpg").exists()

        tree = etree.parse(str(tmp_path / "new name.nfo"))
        root = tree.getroot()
        assert root.findtext("art/poster") == "new name-poster.jpg"
        assert root.findtext("art/fanart") == "new name-fanart.jpg"

    def test_rename_works_when_images_missing(self, tmp_path):
        from app.writers.nfo import rename_nfo_assets

        nfo = tmp_path / "old name.nfo"
        nfo.write_bytes(b'<?xml version=\'1.0\' encoding=\'utf-8\'?>\n<movie><title>T</title></movie>')

        # Should not raise even if images don't exist
        rename_nfo_assets(str(tmp_path), "old name", "new name")
        assert (tmp_path / "new name.nfo").exists()
        assert not (tmp_path / "old name.nfo").exists()

    def test_rename_raises_on_missing_nfo(self, tmp_path):
        from app.writers.nfo import rename_nfo_assets
        import pytest
        with pytest.raises(Exception):
            rename_nfo_assets(str(tmp_path), "nonexistent", "new name")
