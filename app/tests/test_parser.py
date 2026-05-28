"""Tests for the universal filename parser."""

import pytest

from app.parser import parse
from app.plugins.base import ParsedFilename

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parsed(stem: str) -> ParsedFilename:
    result = parse(stem)
    assert result is not None, f"Expected parse result for {stem!r}, got None"
    return result


# ---------------------------------------------------------------------------
# Manual Add form
# ---------------------------------------------------------------------------

class TestAddForm:
    def test_minimal_actor_and_studio(self):
        p = parsed("Add Jane Doe At MyStudio")
        assert p.form == "add"
        assert p.actors == ["Jane Doe"]
        assert p.studio == "MyStudio"
        assert p.date is None
        assert p.title is None
        assert p.genres == []

    def test_date_actor_studio(self):
        p = parsed("Add 2019-01-01 Jane Doe At MyStudio")
        assert p.date == "2019-01-01"
        assert p.actors == ["Jane Doe"]
        assert p.studio == "MyStudio"

    def test_multiple_actors(self):
        p = parsed("Add Jane Doe And Mary Smith In My Scene At MyStudio")
        assert p.actors == ["Jane Doe", "Mary Smith"]
        assert p.title == "My Scene"

    def test_full_add_form(self):
        p = parsed("Add 2019-01-01 Jane Doe And Mary Smith In My Scene At MyStudio With Drama, Comedy")
        assert p.date == "2019-01-01"
        assert p.actors == ["Jane Doe", "Mary Smith"]
        assert p.title == "My Scene"
        assert p.studio == "MyStudio"
        assert p.genres == ["Drama", "Comedy"]

    def test_add_case_insensitive(self):
        p = parsed("add jane doe at mystudio")
        assert p.form == "add"
        assert p.actors == ["jane doe"]

    def test_yy_date_normalised(self):
        p = parsed("Add 19-06-15 Jane Doe At Studio")
        assert p.date == "2019-06-15"

    def test_dot_date_normalised(self):
        p = parsed("Add 2019.06.15 Jane Doe At Studio")
        assert p.date == "2019-06-15"

    def test_match_subtype_is_add(self):
        p = parsed("Add Jane Doe At Studio")
        assert p.match_subtype == "add"


# ---------------------------------------------------------------------------
# General form — top-level parsing
# ---------------------------------------------------------------------------

class TestGeneralFormTopLevel:
    def test_actors_genres_site(self):
        p = parsed("Jane Doe, Mary Smith with Drama, Comedy % examplesite - 12345")
        assert p.form == "general"
        assert p.actors == ["Jane Doe", "Mary Smith"]
        assert p.genres == ["Drama", "Comedy"]
        assert p.site == "examplesite"

    def test_no_genres(self):
        p = parsed("Jane Doe % examplesite - 12345")
        assert p.actors == ["Jane Doe"]
        assert p.genres == []

    def test_site_lowercased(self):
        p = parsed("Jane Doe % ExampleSite - 12345")
        assert p.site == "examplesite"

    def test_shorthand_site(self):
        p = parsed("Jane Doe % SN - 12345")
        assert p.site == "sn"

    def test_unparseable_returns_none(self):
        assert parse("completely random filename with no tokens") is None

    def test_empty_returns_none(self):
        assert parse("") is None

    def test_percent_with_no_payload_returns_none(self):
        # "%" with nothing after it — no site token
        result = parse("Jane Doe %")
        assert result is None or result.site is None


# ---------------------------------------------------------------------------
# Exact match subtype
# ---------------------------------------------------------------------------

class TestExactMatch:
    def test_scene_id_only(self):
        p = parsed("Jane Doe % examplesite - 12345")
        assert p.match_subtype == "exact"
        assert p.scene_id == "12345"

    def test_scene_id_with_date(self):
        p = parsed("Jane Doe % examplesite - 2019-01-01 - 12345")
        assert p.match_subtype == "exact"
        assert p.date == "2019-01-01"
        assert p.scene_id == "12345"

    def test_direct_url_slug(self):
        p = parsed("Jane Doe % warnerbros - eager-hands")
        assert p.match_subtype == "exact"
        assert p.direct_url is not None
        assert "eager-hands" in p.direct_url

    def test_direct_url_with_date(self):
        p = parsed("Jane Doe % warnerbros - 2019.01.01 - eager-hands")
        assert p.date == "2019-01-01"
        assert p.direct_url is not None


# ---------------------------------------------------------------------------
# Enhanced search subtype
# ---------------------------------------------------------------------------

class TestEnhancedSearch:
    def test_full_enhanced(self):
        p = parsed("Jane Doe % examplesite - 19-06-15 - 98765 - An Interesting Plot")
        assert p.match_subtype == "enhanced"
        assert p.date == "2019-06-15"
        assert p.scene_id == "98765"
        assert p.title is not None
        assert "An Interesting Plot" in p.title

    def test_scene_id_with_actor(self):
        p = parsed("Jane Doe % examplesite - 98765 - Jane Doe")
        assert p.match_subtype in ("enhanced", "exact")
        assert p.scene_id == "98765"

    def test_date_and_title(self):
        p = parsed("Jane Doe % examplesite - 19-06-15 - An Interesting Plot")
        assert p.date == "2019-06-15"
        assert p.title is not None

    def test_shorthand_with_title(self):
        p = parsed("Jane Doe % SN - An Interesting Plot")
        assert p.site == "sn"
        assert p.title is not None


# ---------------------------------------------------------------------------
# Limited search subtype
# ---------------------------------------------------------------------------

class TestLimitedSearch:
    def test_title_only(self):
        p = parsed("Jane Doe % examplesite - An Interesting Plot")
        assert p.match_subtype in ("limited", "enhanced")
        assert p.title is not None

    def test_site_and_actor(self):
        p = parsed("% examplesite - Jane Doe")
        assert p.site == "examplesite"
        assert p.title is not None


# ---------------------------------------------------------------------------
# Date normalisation
# ---------------------------------------------------------------------------

class TestDateNormalisation:
    @pytest.mark.parametrize("raw,expected", [
        ("2019-01-15", "2019-01-15"),
        ("19-01-15",   "2019-01-15"),
        ("2019.01.15", "2019-01-15"),
        ("19.01.15",   "2019-01-15"),
    ])
    def test_formats(self, raw, expected):
        p = parsed(f"Jane Doe % examplesite - {raw} - 12345")
        assert p.date == expected


# ---------------------------------------------------------------------------
# Worked examples from FILENAME_PATTERNS.md
# ---------------------------------------------------------------------------

class TestDocumentedExamples:
    def test_actors_genres_exact(self):
        p = parsed("Actress A, Actress B with scary, comedy % warnerbros - 5132234")
        assert p.actors == ["Actress A", "Actress B"]
        assert p.genres == ["scary", "comedy"]
        assert p.site == "warnerbros"
        assert p.scene_id == "5132234"

    def test_warnerbros_direct_url_with_embedded_id(self):
        p = parsed("Jane Doe % warnerbros - 2019.10.10 - Stranger-Than-Fiction 77675")
        assert p.date == "2019-10-10"
        assert p.direct_url is not None

    def test_full_enhanced_example(self):
        p = parsed("Jane Doe % SiteName - 19-01-15 - 98765 - Jane Doe - An Interesting Plot")
        assert p.date == "2019-01-15"
        assert p.scene_id == "98765"


# ---------------------------------------------------------------------------
# T5 — calendrically invalid dates
# ---------------------------------------------------------------------------

class TestInvalidDates:
    def test_feb_30_date_is_none(self):
        """Feb 30 does not exist; the date field must be None but the rest of
        the record should still parse (scene_id still extracted)."""
        result = parse("Jane Doe % site - 2024-02-30 - 12345")
        # The filename may parse to a result (site/scene_id present) but date must be None
        assert result is not None
        assert result.date is None
        assert result.scene_id == "12345"

    def test_month_13_date_is_none(self):
        """Month 13 is invalid; the date field must be None."""
        result = parse("Jane Doe % site - 2019-13-01 - 12345")
        assert result is not None
        assert result.date is None


# ---------------------------------------------------------------------------
# T6 — actor name containing %
# ---------------------------------------------------------------------------

class TestActorNameWithPercent:
    def test_actor_name_with_percent(self):
        """'100% Natural % site - 12345' — the first % is inside the actor name,
        the second % is the site delimiter. The parser splits on the first %, so
        the actor token becomes '100' and '% Natural' is swallowed or discarded.

        This is a known edge case: the grammar treats the first % as the site
        delimiter, so any % in an actor name breaks the split. We document the
        actual behaviour here rather than an idealised result.
        """
        result = parse("100% Natural % site - 12345")
        # The parser either returns None or a partial result where actors=['100']
        # because '100' is everything before the first '%'.
        if result is None:
            # None is acceptable — the parser failed to produce a result for this
            # edge case (nothing after the first split looks like a valid payload).
            return
        # If a result was produced, it must reflect the actual (imperfect) parse:
        # actors will be ['100'] because that's what precedes the first '%'.
        assert result.actors == ["100"]
