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
