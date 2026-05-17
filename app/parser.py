"""
Universal filename parser.

Decodes a media filename stem into a ParsedFilename. Two top-level forms:

  Form 1 — Manual Add (starts with keyword "add"):
    Add [<Date>] <Actor> [And <Actor>...] [In <Title>] [At <Studio>] [With <Genre>...]

  Form 2 — General:
    <Actors> [with <Genres>] % <Site> [- <Date>] [- tokens...]

See FILENAME_PATTERNS.md for the full grammar and examples.
"""

from __future__ import annotations

import re

from app.plugins.base import FilenameForm, MatchSubtype, ParsedFilename

# Date patterns accepted anywhere a date token is expected
_DATE_PATTERNS = [
    re.compile(r"^(\d{4})[.\-](\d{2})[.\-](\d{2})$"),  # YYYY-MM-DD / YYYY.MM.DD
    re.compile(r"^(\d{2})[.\-](\d{2})[.\-](\d{2})$"),   # YY-MM-DD / YY.MM.DD
]

_SCENE_ID_RE = re.compile(r"^\d+$")


def _normalise_date(token: str) -> str | None:
    for pat in _DATE_PATTERNS:
        m = pat.match(token.strip())
        if m:
            y, mo, d = m.group(1), m.group(2), m.group(3)
            if len(y) == 2:
                y = f"20{y}"
            return f"{y}-{mo}-{d}"
    return None


def _split_csv(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


def _parse_add_form(stem: str) -> ParsedFilename:
    """Parse the Manual Add form: Add [Date] Actor [And Actor...] [In Title] [At Studio] [With Genre,...]"""
    # Strip leading "Add" keyword (case-insensitive)
    rest = stem.strip()[3:].strip()

    actors: list[str] = []
    genres: list[str] = []
    title: str | None = None
    studio: str | None = None
    date: str | None = None

    # Extract "With <genres>" suffix first (rightmost)
    with_match = re.search(r"\bWith\s+(.+)$", rest, re.IGNORECASE)
    if with_match:
        genres = _split_csv(with_match.group(1))
        rest = rest[: with_match.start()].strip()

    # Extract "At <studio>"
    at_match = re.search(r"\bAt\s+(.+)$", rest, re.IGNORECASE)
    if at_match:
        studio = at_match.group(1).strip()
        rest = rest[: at_match.start()].strip()

    # Extract "In <title>"
    in_match = re.search(r"\bIn\s+(.+)$", rest, re.IGNORECASE)
    if in_match:
        title = in_match.group(1).strip()
        rest = rest[: in_match.start()].strip()

    # What remains is: [Date] Actor [And Actor ...]
    # Check if first token is a date
    first_token_match = re.match(r"^(\S+)\s*(.*)", rest)
    if first_token_match:
        candidate = first_token_match.group(1)
        normalised = _normalise_date(candidate)
        if normalised:
            date = normalised
            rest = first_token_match.group(2).strip()

    # Split remaining by " And " (case-insensitive)
    actor_parts = re.split(r"\s+And\s+", rest, flags=re.IGNORECASE)
    actors = [a.strip() for a in actor_parts if a.strip()]

    return ParsedFilename(
        form="add",
        actors=actors,
        genres=genres,
        title=title,
        studio=studio,
        date=date,
        match_subtype="add",
    )


def _parse_general_form(stem: str) -> ParsedFilename:
    """Parse the general form: <Actors> [with <Genres>] % <Match Payload>"""
    percent_idx = stem.index("%")
    left = stem[:percent_idx].strip()
    raw_payload = stem[percent_idx + 1 :].strip()

    # Parse left side: actors [with genres]
    actors: list[str] = []
    genres: list[str] = []

    with_match = re.search(r"\bwith\s+(.+)$", left, re.IGNORECASE)
    if with_match:
        genres = _split_csv(with_match.group(1))
        left = left[: with_match.start()].strip()

    actors = _split_csv(left)

    # Parse match payload
    # Split on " - " (with optional surrounding spaces)
    # An empty payload (e.g. stem ending with " %") means there is no site token —
    # return None rather than a partially-filled object with site="".
    tokens = [t.strip() for t in re.split(r"\s+-\s+", raw_payload)]
    if not tokens or not tokens[0]:
        return None

    site = tokens[0]
    # Reject a site token that contains % — it means the payload started with an
    # encoded character (e.g. "Actor % %20 - real-site - 12345") and the split
    # landed on the wrong % boundary.
    if "%" in site:
        return None
    rest_tokens = tokens[1:]

    date: str | None = None
    scene_id: str | None = None
    title: str | None = None
    extra_actors: list[str] = []
    direct_url: str | None = None

    idx = 0

    # Optional date immediately after site
    if idx < len(rest_tokens):
        candidate_date = _normalise_date(rest_tokens[idx])
        if candidate_date:
            date = candidate_date
            idx += 1

    # Remaining tokens: classify each
    remaining = rest_tokens[idx:]
    text_tokens: list[str] = []

    for tok in remaining:
        if _SCENE_ID_RE.match(tok):
            if scene_id is None:
                scene_id = tok
            else:
                text_tokens.append(tok)
        else:
            text_tokens.append(tok)

    # Determine subtype and assign text tokens.
    #
    # Direct URL slugs look like one of:
    #   - A single token with no spaces (e.g. "eager-hands")
    #   - A single token with trailing digits (e.g. "Stranger-Than-Fiction 77675")
    # Titles/actors have spaces but no trailing numeric suffix.
    # Plugins always have raw_match_payload available for their own parsing.
    match_subtype: MatchSubtype

    def _looks_like_url_slug(tok: str) -> bool:
        # URL slugs are hyphen-separated, space-free tokens (e.g. "eager-hands",
        # "Stranger-Than-Fiction"). A token with spaces is a title or actor name,
        # not a slug — even if it ends in a digit (e.g. "Chapter 5").
        # The only exception is a slug with an embedded numeric ID at the end
        # separated by a space (e.g. "Stranger-Than-Fiction 77675"), which is
        # still slug-like because it contains hyphens and no internal title words.
        if " " not in tok:
            return True
        # Space-containing token: treat as slug only if it has hyphens (slug part)
        # and the trailing word is purely numeric (an appended ID).
        parts = tok.rsplit(" ", 1)
        return "-" in parts[0] and bool(re.match(r"^\d+$", parts[1]))

    if not text_tokens and scene_id:
        match_subtype = "exact"
    elif not text_tokens and not scene_id:
        match_subtype = "exact"
    elif len(text_tokens) == 1 and _looks_like_url_slug(text_tokens[0]):
        direct_url = text_tokens[0]
        match_subtype = "exact"
    else:
        title = " - ".join(text_tokens)
        match_subtype = "enhanced" if (scene_id or date) else "limited"

    return ParsedFilename(
        form="general",
        actors=actors,
        genres=genres,
        site=site.lower(),
        match_subtype=match_subtype,
        date=date,
        scene_id=scene_id,
        direct_url=direct_url,
        title=title,
        extra_actors=extra_actors,
        raw_match_payload=raw_payload,
    )


def parse(filename_stem: str) -> ParsedFilename | None:
    """
    Parse a filename stem (no extension) into a ParsedFilename.
    Returns None if the filename cannot be parsed.
    """
    stem = filename_stem.strip()
    if not stem:
        return None

    if re.match(r"^add\b", stem, re.IGNORECASE):
        # A stem that is exactly "Add" (nothing after the keyword) has no actors
        # and is not a valid Add form — treat it as unmatched.
        if re.fullmatch(r"add", stem, re.IGNORECASE):
            return None
        return _parse_add_form(stem)

    if "%" in stem:
        return _parse_general_form(stem)

    return None
