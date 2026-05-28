# Tests for app/writers/nfo.py
from __future__ import annotations

import os
from unittest.mock import patch, MagicMock

import pytest
from lxml import etree

from app.plugins.base import MetadataResult
from app.scanner import MediaFile
from app.writers.nfo import write_nfo, write_images

pytestmark = pytest.mark.unit


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
    assert 'type="m3"' in nfo
    assert ">99<" in nfo


def test_write_nfo_art_block_references_sidecar_names(tmp_path):
    media = _make_media(tmp_path, name="My Movie")
    write_nfo(media, _make_result(poster_url="http://x.com/p.jpg", fanart_url="http://x.com/f.jpg"))
    nfo = open(media.nfo_path).read()
    assert "My Movie-poster.jpg" in nfo
    assert "My Movie-fanart.jpg" in nfo


def test_write_nfo_art_block_uses_provided_webp_filename(tmp_path):
    media = _make_media(tmp_path, name="My Scene")
    result = _make_result(poster_url="http://x.com/p.webp", fanart_url=None)

    write_nfo(media, result, poster_filename="My Scene-poster.webp")

    content = open(media.nfo_path, encoding="utf-8").read()
    assert "My Scene-poster.webp" in content
    assert "My Scene-poster.jpg" not in content


def test_write_nfo_art_block_probes_disk_for_webp_when_no_filename(tmp_path):
    """When poster_filename is None and a .webp sidecar exists, the NFO must reference it."""
    media = _make_media(tmp_path, name="My Scene")
    # Create a .webp image on disk next to the (future) NFO
    (tmp_path / "My Scene-poster.webp").write_bytes(b"webp")

    result = _make_result(poster_url="http://x.com/p.webp", fanart_url=None)
    write_nfo(media, result)

    content = open(media.nfo_path, encoding="utf-8").read()
    assert "My Scene-poster.webp" in content
    assert "My Scene-poster.jpg" not in content


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

    # Create the .tmp file first so the test proves cleanup ran, not that it
    # was never created at all.
    open(tmp_file, "w").close()
    assert os.path.exists(tmp_file), "Pre-condition: .tmp file must exist before the failing write"

    with patch("builtins.open", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            write_nfo(media, result)

    assert not os.path.exists(tmp_file)


def test_write_nfo_cleans_tmp_when_atomic_replace_raises(tmp_path):
    """If atomic_replace raises, the .tmp file must be removed."""
    media = MediaFile(
        path=str(tmp_path / "Scene.mp4"),
        stem="Scene",
        nfo_path=str(tmp_path / "Scene.nfo"),
    )
    result = MetadataResult(title="Test Scene", actors=["Actor One"])

    with patch("app.writers.nfo.atomic_replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            write_nfo(media, result)

    # The .nfo.tmp file must not exist after the exception
    assert not os.path.exists(str(tmp_path / "Scene.nfo.tmp"))
    # The .nfo itself should not exist either (write failed)
    assert not os.path.exists(str(tmp_path / "Scene.nfo"))


# ---------------------------------------------------------------------------
# write_images — image download delegation
# ---------------------------------------------------------------------------

def test_write_images_downloads_poster_and_fanart(tmp_path):
    media = _make_media(tmp_path)
    result = _make_result(poster_url="http://x.com/p.jpg", fanart_url="http://x.com/f.jpg")

    def fake_download(url, dest_base):
        # Return the path with .jpg extension as if jpeg was downloaded
        return dest_base + ".jpg"

    with patch("app.writers.nfo._download_image") as mock_dl:
        mock_dl.side_effect = fake_download
        outcome = write_images(media, result)

    assert mock_dl.call_count == 2
    calls = [c.args[0] for c in mock_dl.call_args_list]
    assert "http://x.com/p.jpg" in calls
    assert "http://x.com/f.jpg" in calls
    # write_images returns (success, poster_path, fanart_path)
    success, poster_path, fanart_path = outcome
    assert success is True


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

    def fake_dl(url, dest_base):
        captured["poster_dest"] = dest_base
        return dest_base + ".jpg"

    with patch("app.writers.nfo._download_image", side_effect=fake_dl):
        write_images(media, result)

    # _download_image now receives the base path (without extension); the
    # caller derives the extension from the Content-Type header at runtime.
    assert captured["poster_dest"].endswith("My Scene-poster")


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


def test_download_image_rejects_non_image_content_type(tmp_path):
    """When the response has a non-image Content-Type, _download_image must return
    None and must not write any file to the destination path."""
    from app.writers.nfo import _download_image

    dest_base = str(tmp_path / "img")

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.headers = {"content-type": "text/html"}
    mock_response.iter_bytes = MagicMock(return_value=iter([]))
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    mock_client = MagicMock()
    mock_client.stream.return_value = mock_response
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = MagicMock(return_value=False)

    with patch("app.writers.nfo.httpx") as mock_httpx:
        mock_httpx.Client.return_value = mock_client
        result = _download_image("http://example.com/img.jpg", dest_base)

    assert result is None
    # No file should be written for any extension
    for ext in (".jpg", ".png", ".webp"):
        assert not os.path.exists(dest_base + ext), (
            f"No image file should be written for non-image content-type (checked {ext})"
        )


def test_download_image_aborts_on_content_length_header_exceeding_cap(tmp_path):
    """When Content-Length header exceeds the cap the download must be rejected
    before any bytes are streamed to disk."""
    from app.writers.nfo import _download_image, _DOWNLOAD_MAX_BYTES

    dest_base = str(tmp_path / "img")
    oversized = _DOWNLOAD_MAX_BYTES + 1

    mock_client = _wrap_client_with_fresh_response(
        content_length_header=oversized, body_bytes=100
    )

    with patch("app.writers.nfo.httpx") as mock_httpx:
        mock_httpx.Client.return_value = mock_client
        result = _download_image("http://example.com/img.jpg", dest_base)

    assert result is None
    for ext in (".jpg", ".png", ".webp"):
        assert not os.path.exists(dest_base + ext), (
            f"Destination file must not be written when Content-Length exceeds cap (checked {ext})"
        )


def test_download_image_aborts_mid_stream_when_body_exceeds_cap(tmp_path):
    """When the streamed body grows past the cap the download must abort and
    leave no partial file at the destination path."""
    from app.writers.nfo import _download_image, _DOWNLOAD_MAX_BYTES

    dest_base = str(tmp_path / "img")
    # No Content-Length header; body exceeds cap mid-stream
    oversized_body = _DOWNLOAD_MAX_BYTES + 65536

    mock_client = _wrap_client_with_fresh_response(
        content_length_header=None, body_bytes=oversized_body
    )

    with patch("app.writers.nfo.httpx") as mock_httpx:
        mock_httpx.Client.return_value = mock_client
        result = _download_image("http://example.com/img.jpg", dest_base)

    assert result is None
    for ext in (".jpg", ".png", ".webp"):
        assert not os.path.exists(dest_base + ext), (
            f"Partial file must be cleaned up after mid-stream abort (checked {ext})"
        )


def test_download_image_retries_on_connect_error_and_succeeds(tmp_path):
    """_download_image retries on httpx.ConnectError and returns the written path
    when a later attempt succeeds."""
    import httpx as real_httpx
    from app.writers.nfo import _download_image

    dest_base = str(tmp_path / "img")

    # Build a small successful response for the 3rd attempt (content-type: image/jpeg → .jpg)
    success_response = _make_streaming_mock(content_length_header=None, body_bytes=100)

    call_count = 0

    def stream_side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise real_httpx.ConnectError("connection refused")
        return success_response

    mock_client = MagicMock()
    mock_client.stream.side_effect = stream_side_effect
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = MagicMock(return_value=False)

    with patch("app.writers.nfo.httpx") as mock_httpx:
        mock_httpx.Client.return_value = mock_client
        # ConnectError must be recognised as a retryable exception — not a reraise_on type.
        # Mirror the real httpx.ConnectError so isinstance checks inside retry_with_backoff work.
        mock_httpx.ConnectError = real_httpx.ConnectError
        with patch("app.utils.time.sleep"):  # skip real backoff sleeps
            result = _download_image("http://example.com/img.jpg", dest_base)

    # _download_image now returns the full path written (dest_base + extension) on success
    assert result is not None, "Must return a path string on success, not None"
    assert os.path.exists(result), "Image file must be written after a successful retry"
    assert call_count == 3


def test_download_image_not_retried_on_bad_content_type(tmp_path):
    """ValueError from bad content-type must not trigger retries."""
    from app.writers.nfo import _download_image

    dest_base = str(tmp_path / "stem-poster")

    call_count = 0

    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.headers = {"content-type": "text/html"}
    mock_response.iter_bytes = MagicMock(return_value=iter([]))
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)

    def mock_stream(method, url, **kwargs):
        nonlocal call_count
        call_count += 1
        return mock_response

    mock_client = MagicMock()
    mock_client.stream.side_effect = mock_stream
    mock_client.__enter__ = lambda s: s
    mock_client.__exit__ = MagicMock(return_value=False)

    with patch("app.writers.nfo.httpx") as mock_httpx:
        mock_httpx.Client.return_value = mock_client
        result = _download_image("http://example.com/poster.jpg", dest_base)

    assert result is None
    assert call_count == 1  # must not retry on deterministic ValueError


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

    def test_rename_nfo_assets_unicode_stem(self, tmp_path):
        from app.writers.nfo import rename_nfo_assets
        from lxml import etree

        old_stem = "Résumé Scene"
        new_stem = "Resume Scene"

        nfo = tmp_path / f"{old_stem}.nfo"
        nfo.write_bytes(
            '<?xml version=\'1.0\' encoding=\'utf-8\'?>\n'
            f'<movie><title>{old_stem}</title></movie>'.encode("utf-8")
        )

        rename_nfo_assets(str(tmp_path), old_stem, new_stem)

        new_nfo = tmp_path / f"{new_stem}.nfo"
        assert new_nfo.exists(), "Renamed NFO must exist with new stem"

        tree = etree.parse(str(new_nfo))
        root = tree.getroot()
        assert root.findtext("title") == old_stem, "Title field must be preserved after rename"

    def test_rename_nfo_assets_overwrites_existing_destination(self, tmp_path):
        from app.writers.nfo import rename_nfo_assets

        old_stem = "Old Title"
        new_stem = "New Title"

        src_nfo = tmp_path / f"{old_stem}.nfo"
        src_nfo.write_bytes(
            b'<?xml version=\'1.0\' encoding=\'utf-8\'?>\n'
            b'<movie><title>Old Title</title></movie>'
        )

        dst_nfo = tmp_path / f"{new_stem}.nfo"
        dst_nfo.write_bytes(
            b'<?xml version=\'1.0\' encoding=\'utf-8\'?>\n'
            b'<movie><title>Pre-existing Title</title></movie>'
        )

        rename_nfo_assets(str(tmp_path), old_stem, new_stem)

        assert not src_nfo.exists(), "Source NFO must be removed after rename"
        assert dst_nfo.exists(), "Destination NFO must exist after rename"

        content = dst_nfo.read_text(encoding="utf-8")
        assert "Old Title" in content, "Destination NFO must contain the source title after overwrite"
        assert "Pre-existing Title" not in content, "Pre-existing destination title must be replaced"

    def test_rename_nfo_assets_preserves_webp_extension(self, tmp_path):
        old_stem = "Old Scene"
        new_stem = "New Scene"
        nfo_path = tmp_path / f"{old_stem}.nfo"

        # Write NFO with .webp poster reference
        root = etree.Element("movie")
        etree.SubElement(root, "title").text = old_stem
        art = etree.SubElement(root, "art")
        etree.SubElement(art, "poster").text = f"{old_stem}-poster.webp"
        etree.ElementTree(root).write(str(nfo_path), encoding="utf-8", xml_declaration=True)

        # Create the .webp image
        (tmp_path / f"{old_stem}-poster.webp").write_bytes(b"webp")

        from app.writers.nfo import rename_nfo_assets
        rename_nfo_assets(str(tmp_path), old_stem, new_stem)

        new_nfo = tmp_path / f"{new_stem}.nfo"
        assert new_nfo.exists()
        assert (tmp_path / f"{new_stem}-poster.webp").exists()
        assert not (tmp_path / f"{new_stem}-poster.jpg").exists()

        tree = etree.parse(str(new_nfo))
        poster_el = tree.find("art/poster")
        assert poster_el is not None
        assert poster_el.text == f"{new_stem}-poster.webp"

    def test_rename_nfo_assets_probes_disk_when_nfo_art_ext_unknown(self, tmp_path):
        """When NFO art block has no recognized extension, probe disk for actual image."""
        old_stem = "Old Scene"
        new_stem = "New Scene"
        nfo_path = tmp_path / f"{old_stem}.nfo"

        # Write NFO with an unrecognized extension in the art block
        root = etree.Element("movie")
        etree.SubElement(root, "title").text = old_stem
        art = etree.SubElement(root, "art")
        etree.SubElement(art, "poster").text = f"{old_stem}-poster.unknown"
        etree.ElementTree(root).write(str(nfo_path), encoding="utf-8", xml_declaration=True)

        # Actual image on disk is .png
        (tmp_path / f"{old_stem}-poster.png").write_bytes(b"png")

        from app.writers.nfo import rename_nfo_assets
        rename_nfo_assets(str(tmp_path), old_stem, new_stem)

        new_nfo = tmp_path / f"{new_stem}.nfo"
        assert new_nfo.exists()
        # The renamed NFO should reference .png (probed from disk), not .jpg
        tree = etree.parse(str(new_nfo))
        poster_el = tree.find("art/poster")
        assert poster_el is not None
        assert poster_el.text == f"{new_stem}-poster.png"

    def test_rename_nfo_assets_partial_art_poster_only(self, tmp_path):
        from app.writers.nfo import rename_nfo_assets

        old_stem = "Old Scene"
        new_stem = "New Scene"

        # Create NFO with art block that has poster but no fanart
        nfo_path = tmp_path / f"{old_stem}.nfo"
        root = etree.Element("movie")
        etree.SubElement(root, "title").text = "Old Scene"
        art = etree.SubElement(root, "art")
        etree.SubElement(art, "poster").text = f"{old_stem}-poster.jpg"
        # No fanart element
        etree.ElementTree(root).write(str(nfo_path), encoding="utf-8", xml_declaration=True)

        # Create only the poster image
        poster_path = tmp_path / f"{old_stem}-poster.jpg"
        poster_path.write_bytes(b"fake jpeg")

        rename_nfo_assets(str(tmp_path), old_stem, new_stem)

        # New NFO should exist
        new_nfo = tmp_path / f"{new_stem}.nfo"
        assert new_nfo.exists()

        # Poster was renamed
        assert (tmp_path / f"{new_stem}-poster.jpg").exists()
        assert not (tmp_path / f"{old_stem}-poster.jpg").exists()

        # New NFO's art/poster should reference the new stem
        tree = etree.parse(str(new_nfo))
        poster_el = tree.find("art/poster")
        assert poster_el is not None
        assert new_stem in poster_el.text
