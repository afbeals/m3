# Tests for app/writers/plex.py
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest

from app.plugins.base import MetadataResult
from app.writers.plex import connect_plex, find_plex_item, push_to_plex

pytestmark = pytest.mark.unit


def _make_result(**kwargs) -> MetadataResult:
    defaults = dict(
        title="Test Scene",
        summary="A summary.",
        rating=7.5,
        year=2023,
        content_rating="NR",
        genres=["Drama"],
        labels=["award"],
        tags=["tag-1"],
        actors=["Jane Doe"],
        poster_url="http://x.com/p.jpg",
        fanart_url="http://x.com/f.jpg",
        source_url="http://x.com/scene/1",
        source_id="1",
    )
    defaults.update(kwargs)
    return MetadataResult(**defaults)


# ---------------------------------------------------------------------------
# connect_plex
# ---------------------------------------------------------------------------

def test_connect_plex_returns_server_on_success():
    mock_server = MagicMock()
    mock_server.friendlyName = "My Plex"
    with patch("app.writers.plex.PlexServer", return_value=mock_server):
        result = connect_plex("http://localhost:32400", "token")
    assert result is mock_server


def test_connect_plex_returns_none_on_error():
    with patch("app.writers.plex.PlexServer", side_effect=Exception("refused")):
        result = connect_plex("http://localhost:32400", "token")
    assert result is None


# ---------------------------------------------------------------------------
# find_plex_item — fast path
# ---------------------------------------------------------------------------

def test_find_plex_item_uses_fast_path():
    mock_item = MagicMock()
    mock_server = MagicMock()
    mock_server.library.search.return_value = [mock_item]

    result = find_plex_item(mock_server, "/media/movie.mp4")

    assert result is mock_item
    mock_server.library.search.assert_called_once_with(
        filters={"media.filepath": "/media/movie.mp4"}
    )


def test_find_plex_item_falls_back_to_slow_scan():
    mock_part = MagicMock()
    mock_part.file = "/media/movie.mp4"
    mock_media = MagicMock()
    mock_media.parts = [mock_part]
    mock_item = MagicMock()
    mock_item.media = [mock_media]
    mock_section = MagicMock()
    mock_section.search.return_value = [mock_item]

    mock_server = MagicMock()
    # Fast path returns nothing — triggers fallback
    mock_server.library.search.return_value = []
    mock_server.library.sections.return_value = [mock_section]

    result = find_plex_item(mock_server, "/media/movie.mp4")
    assert result is mock_item


def test_find_plex_item_returns_none_when_not_found():
    mock_section = MagicMock()
    mock_section.search.return_value = []
    mock_server = MagicMock()
    mock_server.library.search.return_value = []
    mock_server.library.sections.return_value = [mock_section]

    result = find_plex_item(mock_server, "/media/missing.mp4")
    assert result is None


def test_find_plex_item_returns_none_on_exception():
    mock_server = MagicMock()
    mock_server.library.search.side_effect = Exception("network error")

    result = find_plex_item(mock_server, "/media/movie.mp4")
    assert result is None


# ---------------------------------------------------------------------------
# push_to_plex — field writes
# ---------------------------------------------------------------------------


def test_push_to_plex_returns_false_when_item_not_found():
    mock_server = MagicMock()
    with patch("app.writers.plex.find_plex_item", return_value=None):
        result = push_to_plex(mock_server, "/media/movie.mp4", _make_result())
    assert result is False


def test_push_to_plex_calls_edit_with_scalar_fields():
    mock_item = MagicMock()
    mock_server = MagicMock()
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        push_to_plex(mock_server, "/media/movie.mp4", _make_result())

    call_kwargs = mock_item.edit.call_args.kwargs
    assert call_kwargs["title.value"] == "Test Scene"
    assert call_kwargs["title.locked"] == 1
    assert call_kwargs["rating.value"] == 7.5
    assert call_kwargs["rating.locked"] == 1
    assert call_kwargs["year.value"] == 2023


def test_push_to_plex_removes_stale_list_fields_after_adding_new():
    """Genres/labels/tags/actors must be added first, then cleared so re-runs don't accumulate stale values."""
    mock_item = MagicMock()
    mock_server = MagicMock()
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        push_to_plex(mock_server, "/media/movie.mp4", _make_result())

    mock_item.removeGenres.assert_called_once()
    mock_item.removeLabels.assert_called_once()
    mock_item.removeTags.assert_called_once()
    mock_item.removeActors.assert_called_once()

    # Verify add-before-remove ordering: addGenre must come before removeGenres
    call_names = [c[0] for c in mock_item.method_calls]
    add_idx = call_names.index("addGenre")
    remove_idx = call_names.index("removeGenres")
    assert add_idx < remove_idx, "addGenre must be called before removeGenres"


def test_push_to_plex_does_not_clear_genres_when_empty():
    mock_item = MagicMock()
    mock_server = MagicMock()
    result = _make_result(genres=[])
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        push_to_plex(mock_server, "/media/movie.mp4", result)

    mock_item.removeGenres.assert_not_called()


def test_push_to_plex_adds_genres_and_actors():
    mock_item = MagicMock()
    mock_server = MagicMock()
    result = _make_result(genres=["Action", "Drama"], actors=["Alice", "Bob"])
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        push_to_plex(mock_server, "/media/movie.mp4", result)

    genre_calls = [c.args[0] for c in mock_item.addGenre.call_args_list]
    assert "Action" in genre_calls
    assert "Drama" in genre_calls

    actor_calls = [c.args[0] for c in mock_item.addActor.call_args_list]
    assert "Alice" in actor_calls
    assert "Bob" in actor_calls


def test_push_to_plex_uploads_poster_and_fanart():
    mock_item = MagicMock()
    mock_server = MagicMock()
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        push_to_plex(mock_server, "/media/movie.mp4",
                     _make_result(poster_url="http://p.jpg", fanart_url="http://f.jpg"))

    mock_item.uploadPoster.assert_called_once_with(url="http://p.jpg")
    mock_item.uploadArt.assert_called_once_with(url="http://f.jpg")


def test_push_to_plex_skips_poster_when_none():
    mock_item = MagicMock()
    mock_server = MagicMock()
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        push_to_plex(mock_server, "/media/movie.mp4",
                     _make_result(poster_url=None, fanart_url=None))

    mock_item.uploadPoster.assert_not_called()
    mock_item.uploadArt.assert_not_called()


def test_push_to_plex_returns_true_on_success():
    mock_item = MagicMock()
    mock_server = MagicMock()
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        result = push_to_plex(mock_server, "/media/movie.mp4", _make_result())
    assert result is True


def test_push_to_plex_sends_zero_rating():
    mock_item = MagicMock()
    mock_server = MagicMock()
    result = _make_result(rating=0.0)
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        push_to_plex(mock_server, "/media/movie.mp4", result)

    call_kwargs = mock_item.edit.call_args.kwargs
    assert call_kwargs["rating.value"] == 0.0
    assert call_kwargs["rating.locked"] == 1


def test_push_to_plex_returns_false_on_exception():
    mock_item = MagicMock()
    mock_item.edit.side_effect = Exception("Plex API error")
    mock_server = MagicMock()
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        result = push_to_plex(mock_server, "/media/movie.mp4", _make_result())
    assert result is False


# ---------------------------------------------------------------------------
# push_nfo_to_plex
# ---------------------------------------------------------------------------

def _write_nfo(path, *, title="Test Title", plot="A plot.", rating="7.5",
               mpaa="NR", year="2023", genres=(), actors=(), labels=()):
    from lxml import etree
    root = etree.Element("movie")
    for tag, val in (("title", title), ("plot", plot), ("rating", rating),
                     ("mpaa", mpaa), ("year", year)):
        el = etree.SubElement(root, tag)
        el.text = val
    for g in genres:
        etree.SubElement(root, "genre").text = g
    for a in actors:
        actor_el = etree.SubElement(root, "actor")
        etree.SubElement(actor_el, "name").text = a
    for lbl in labels:
        etree.SubElement(root, "tag").text = f"label:{lbl}"
    tree = etree.ElementTree(root)
    with open(path, "wb") as fh:
        fh.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(fh, encoding="utf-8", xml_declaration=False)


def test_push_nfo_to_plex_calls_edit_with_fields(tmp_path):
    from app.writers.plex import push_nfo_to_plex
    nfo = str(tmp_path / "movie.nfo")
    _write_nfo(nfo, title="Test", plot="summary", rating="8.0", year="2024")
    mock_item = MagicMock()
    mock_server = MagicMock()
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        result = push_nfo_to_plex(mock_server, "/media/movie.mp4", nfo)
    assert result is True
    call_kwargs = mock_item.edit.call_args.kwargs
    assert call_kwargs["title.value"] == "Test"
    assert call_kwargs["summary.value"] == "summary"
    assert call_kwargs["year.value"] == 2024


def test_push_nfo_to_plex_pushes_labels(tmp_path):
    from app.writers.plex import push_nfo_to_plex
    nfo = str(tmp_path / "movie.nfo")
    _write_nfo(nfo, labels=["my-label"])
    mock_item = MagicMock()
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        push_nfo_to_plex(MagicMock(), "/media/movie.mp4", nfo)
    # Labels are written as <tag>label:xxx</tag> by write_nfo; push_nfo_to_plex
    # now correctly extracts them and calls addLabel (not addTag with the prefix).
    mock_item.addLabel.assert_called_once_with("my-label", locked=True)
    mock_item.removeLabels.assert_called_once()
    mock_item.addTag.assert_not_called()


def test_push_nfo_to_plex_returns_false_when_item_not_found(tmp_path):
    from app.writers.plex import push_nfo_to_plex
    nfo = str(tmp_path / "movie.nfo")
    _write_nfo(nfo)
    with patch("app.writers.plex.find_plex_item", return_value=None):
        result = push_nfo_to_plex(MagicMock(), "/media/movie.mp4", nfo)
    assert result is False


def test_push_nfo_to_plex_returns_false_on_bad_nfo(tmp_path):
    from app.writers.plex import push_nfo_to_plex
    nfo = str(tmp_path / "bad.nfo")
    open(nfo, "w").write("not xml <<<")
    result = push_nfo_to_plex(MagicMock(), "/media/movie.mp4", nfo)
    assert result is False


def test_push_nfo_to_plex_uploads_local_images(tmp_path):
    from app.writers.plex import push_nfo_to_plex
    nfo = str(tmp_path / "movie.nfo")
    _write_nfo(nfo)
    poster = tmp_path / "movie-poster.jpg"
    fanart = tmp_path / "movie-fanart.jpg"
    poster.write_bytes(b"poster")
    fanart.write_bytes(b"fanart")
    mock_item = MagicMock()
    with patch("app.writers.plex.find_plex_item", return_value=mock_item):
        push_nfo_to_plex(MagicMock(), "/media/movie.mp4", nfo)
    mock_item.uploadPoster.assert_called_once_with(filepath=str(poster))
    mock_item.uploadArt.assert_called_once_with(filepath=str(fanart))
